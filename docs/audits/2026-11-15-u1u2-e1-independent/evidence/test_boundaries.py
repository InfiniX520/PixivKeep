"""Independent synthetic checks; no real accounts, libraries or network."""
from pathlib import Path
import copy, json, os, sqlite3, struct, sys, threading, zlib
from types import SimpleNamespace
ROOT=Path(os.environ.get('PIXIVKEEP_AUDIT_REPO',Path(__file__).resolve().parents[4]))
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import pytest
from core import library_maintenance as m, quality_check as q
from core.decode_limits import DecodeLimits
from core.workspace_ownership import TemporaryWorkspace
import test_u1a_library_maintenance as h

def png():
    def chunk(k,d):return struct.pack('>I',len(d))+k+d+struct.pack('>I',zlib.crc32(k+d)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,0,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0\0'))+chunk(b'IEND',b'')

def snap(ids=('1',), *, complete=True):
    return {'id':'s1','status':'complete','memberships_complete':complete,'artists':{i:{'visibility':'0','groups':['g'],'is_following':True} for i in ids},'memberships':{'show':{'g':{'ids':list(ids)}},'hide':{}}}

def test_ea01_online_backup_refusal_must_precede_writing_foreign_file(tmp_path,monkeypatch):
    source=h.make_library(tmp_path/'source'/'library.sqlite3',jobs=1)
    victim=tmp_path/'sentinel.sqlite3'
    with sqlite3.connect(victim) as c:c.execute('CREATE TABLE sentinel(value)');c.execute("INSERT INTO sentinel VALUES ('preserve me')")
    before=victim.read_bytes();original=TemporaryWorkspace.claim;hits=[]
    def claim(self,name,**kw):
        result=original(self,name,**kw)
        if kw.get('purpose')=='online_snapshot':
            assert Path(result).resolve().is_relative_to(tmp_path.resolve())
            result.unlink();os.link(victim,result);hits.append(True)
        return result
    monkeypatch.setattr(TemporaryWorkspace,'claim',claim)
    error=None
    try:m.backup_library(source,tmp_path/'backups',label='online',mode='online_backup')
    except m.LibraryMaintenanceError as e:error=e.code
    print('EA01',hits,error,len(before),len(victim.read_bytes()))
    assert hits and victim.read_bytes()==before

def test_ea01_changed_workspace_parent_is_rejected_before_claim(tmp_path):
    p=tmp_path/'workspace';p.mkdir();ws=TemporaryWorkspace.adopt(p,purpose='synthetic')
    p.rename(tmp_path/'original-workspace');p.mkdir()
    try:ws.claim('unexpected.bin')
    except Exception:pass
    print('EA01_PARENT',list(p.iterdir()))
    assert not list(p.iterdir())

def track_reads(monkeypatch,p,stop=None):
    original=Path.open;reads=[]
    class Reader:
        def __init__(self,s):self.s=s
        def __enter__(self):return self
        def __exit__(self,*a):return self.s.__exit__(*a)
        def read(self,*a):
            b=self.s.read(*a);reads.append(len(b))
            if stop is not None and b:stop.set()
            return b
        def __getattr__(self,k):return getattr(self.s,k)
    def opened(path,*a,**kw):
        s=original(path,*a,**kw);return Reader(s) if path==p else s
    monkeypatch.setattr(Path,'open',opened)
    return reads

def test_ea02_command_bytes_account_for_the_decode_and_actual_reads(tmp_path,monkeypatch):
    p=tmp_path/'valid.png';p.write_bytes(png());reads=track_reads(monkeypatch,p)
    limit=3*p.stat().st_size+1
    result=q.check_file(p,decode_policy='strict',limits=DecodeLimits(max_bytes=limit))
    print('EA02',sum(reads),result['budget'],limit,m._decode_outcome([result]))
    assert result['budget']['bytes']==sum(reads) and sum(reads)<=limit

def test_ea02_stop_during_hash_does_not_finish_the_remaining_file(tmp_path,monkeypatch):
    p=tmp_path/'cancel.bin';p.write_bytes(b'x'*(3*1024*1024));stop=threading.Event()
    reads=track_reads(monkeypatch,p,stop)
    q.check_file(p,decode_policy='strict',stop=stop)
    print('EA02_CANCEL',reads)
    assert sum(reads)<p.stat().st_size

@pytest.mark.parametrize('conflict',[False,True])
def test_ea03_coverage_requires_the_expected_content_version(conflict):
    from core.follow_coverage import build_coverage
    new='b'*64;old='a'*64
    facts={'run':{'status':'completed','totals':{'artists_total':1,'works_seen':1,'excluded_by_filter':0,'unknown_fields':0}},'artists':[{'artist_id':'1','state':'completed','works_seen':1}],'incomplete':[],'budget':None,'pages':{'expected':[{'work_id':'100','page':0,'content_sha256':new}],'observed':[{'work_id':'100','page':0,'state':'already_verified','content_sha256':old}]}}
    if conflict:facts['pages']['observed'].append({'work_id':'100','page':0,'state':'already_verified','content_sha256':new})
    result=build_coverage(facts,scope={'kind':'follow_run'})
    print('EA03',conflict,result.delivery,result.overall)
    assert result.delivery['state']!='complete'

def test_ea04_desktop_coverage_can_read_the_current_follow_repository(tmp_path):
    from core.collection_desktop import DesktopRuntime
    from core.library_repository import LibraryRepository
    from core.report_commands import ReportError
    from test_follow_repository import start,snapshot
    logs=tmp_path/'download_logs';logs.mkdir()
    with LibraryRepository(logs/'library.sqlite3') as repo:
        job=start(repo);snapshot(repo,job,7)
        assert len(repo.follow_artists('account'))==1
        rt=object.__new__(DesktopRuntime);rt.attach_paths(SimpleNamespace(download_logs_dir=logs))
        error=None
        try:rt._coverage_summary(run_id=job['job_id'])
        except ReportError as e:error=e.code
    print('EA04',error,rt._crawl_state_path().name)
    assert error is None

def test_ea05_budgeted_batches_do_not_drop_unselected_artists():
    from organizer.batches import plan_batches
    ids=['1','2','3','4','5']
    result=plan_batches(ids,account_id='synthetic',artists=3,requests=1)
    seen=[a for b in result for a in b.artist_ids]
    print('EA05',seen)
    assert sorted(seen)==ids

def test_ea05_applied_but_unverified_artist_remains_resumable():
    from organizer.batches import plan_batch,advance,resume
    b=plan_batch(['1'],account_id='synthetic',artists=1,requests=1)
    for event in ('sample','review','plan','apply'):b=advance(b,'1',event)
    result=resume(b,{})
    print('EA05_VERIFY',b.state_of('1'),result)
    assert '1' in result

@pytest.mark.parametrize('what',['batch_dump','group_scope'])
def test_ea06_checkpoint_scope_cannot_be_mutated_behind_its_owner(what):
    from organizer.batches import plan_batch
    from organizer.group_scope import build_scope
    if what=='batch_dump':
        value=plan_batch(['1'],account_id='synthetic',artists=1,requests=1)
        before=value.as_dict()['fingerprint'];dump=value.as_dict();dump['scope']['artist_ids'].append('999')
        print('EA06_BATCH',value.artist_ids,before,value.fingerprint())
        assert value.artist_ids==('1',)
    else:
        value=build_scope(snap(),groups=['g']);before=value.fingerprint
        try:value.artist_ids=('999',)
        except (AttributeError,TypeError):pass
        print('EA06_SCOPE',value.artist_ids,before,value.fingerprint)
        assert value.artist_ids==('1',)

def test_ea07_one_tag_without_viewed_works_cannot_be_high_confidence():
    from organizer.rules import Rule,RuleSet,classify
    rules=RuleSet('1',(Rule('r','topic','cat',('cats',),weight=2),))
    result=classify({'tags':['cat'],'viewed_works':[]},rules)
    print('EA07',result)
    assert result['needs_more'] is True and result['confidence']!='high'

def test_ea08_complete_flag_does_not_override_missing_membership_evidence():
    from organizer.group_scope import build_scope
    value=snap();value['memberships']['show']['g']['ids']=['1','2']
    result=build_scope(value,groups=['g'])
    print('EA08',result.artist_ids,result.completeness)
    assert not result.complete

def test_ea09_report_does_not_succeed_when_scope_excludes_the_batch(tmp_path,capsys):
    from organizer.cli import main
    from organizer.batches import plan_batch
    from organizer.group_scope import build_scope
    b=plan_batch(['1'],account_id='synthetic',artists=1,requests=1)
    scope=build_scope(snap(('2',)),groups=['g'])
    decisions=[{'artist_id':'1','kind':'unchanged','action':'keep','needs_human':False}]
    for name,value in [('b',b.as_dict()),('s',scope.as_dict()),('d',decisions)]:
        (tmp_path/(name+'.json')).write_text(json.dumps(value),'utf-8')
    code=main(['batch-report','--batch',str(tmp_path/'b.json'),'--scope',str(tmp_path/'s.json'),'--decisions',str(tmp_path/'d.json'),'--json'])
    out=capsys.readouterr();report=json.loads(out.out)
    print('EA09',code,report.get('report',{}).get('scope'))
    assert code!=0

def test_control_normal_scope_roundtrip():
    from organizer.group_scope import build_scope,GroupScope
    scope=build_scope(snap(),groups=['g'])
    assert GroupScope.from_dict(scope.as_dict())==scope
