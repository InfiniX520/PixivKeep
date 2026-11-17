"""Independent synthetic boundary probes. Never uses real user data/network."""
from pathlib import Path
import contextlib,copy,hashlib,json,os,sqlite3,struct,sys,zlib
ROOT=Path(os.environ.get('PIXIVKEEP_AUDIT_REPO',Path(__file__).resolve().parents[4]))
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import pytest
from core import library_maintenance as m
from core.workspace_ownership import TemporaryWorkspace
import test_u1a_library_maintenance as h

@pytest.mark.parametrize('suffix',['-journal','-wal','-shm'])
def test_ra01_sqlite_does_not_write_or_remove_foreign_sidecar(tmp_path,monkeypatch,suffix):
    source=h.make_library(tmp_path/'source'/'library.sqlite3',jobs=1)
    victim=tmp_path/'sentinel';before=b'foreign sentinel do not modify\n'*256;victim.write_bytes(before)
    original=TemporaryWorkspace.claim;sidecars=[]
    def claim(self,name,**kw):
        result=original(self,name,**kw)
        if kw.get('purpose')=='online_snapshot':
            p=Path(str(result.path)+suffix);assert p.resolve().is_relative_to(tmp_path.resolve())
            os.link(victim,p);sidecars.append(p)
        return result
    monkeypatch.setattr(TemporaryWorkspace,'claim',claim)
    error=None
    try:m.backup_library(source,tmp_path/'backups',label='online',mode='online_backup')
    except m.LibraryMaintenanceError as e:error=e.code
    p=sidecars[0];print('RA01',suffix,error,'victim',len(victim.read_bytes()),'sidecar_exists',p.exists())
    assert victim.read_bytes()==before and p.exists() and p.read_bytes()==before

def test_ra02_wal_without_shm_is_not_modified_by_read_only_connection(tmp_path):
    from core.follow_facts_v2 import read_only_connection
    original=tmp_path/'source.db';copy_path=tmp_path/'copy.db'
    with contextlib.closing(sqlite3.connect(original)) as writer:
        writer.execute('PRAGMA journal_mode=WAL');writer.execute('CREATE TABLE sample(v)');writer.execute('INSERT INTO sample VALUES (1)');writer.commit()
        copy_path.write_bytes(original.read_bytes());Path(str(copy_path)+'-wal').write_bytes(Path(str(original)+'-wal').read_bytes())
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir() if p.name.startswith('copy.db')}
    conn=read_only_connection(copy_path)
    try:assert conn.execute('SELECT * FROM sample').fetchall()[0][0]==1
    finally:conn.close()
    after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir() if p.name.startswith('copy.db')}
    print('RA02',sorted(before),sorted(after))
    assert after==before

def test_ra03_v2_artist_total_includes_snapshot_members_without_items(tmp_path):
    from core.library_repository import LibraryRepository
    from core.follow_facts_v2 import coverage_facts,read_only_connection
    from test_follow_repository import start,snapshot
    p=tmp_path/'library.sqlite3'
    with LibraryRepository(p) as repo:
        job=start(repo);snapshot(repo,job,7,8)
        assert len(repo.follow_artists('account'))==2
        conn=read_only_connection(p)
        try:conn.execute('BEGIN');facts=coverage_facts(conn,job['job_id'])
        finally:conn.close()
    print('RA03',facts['run']['totals'],facts['artists'])
    assert facts['run']['totals']['artists_total']==2

def test_ra04_visual_receipt_cannot_be_reused_under_new_rule_version():
    from organizer.rules import Rule,RuleSet,classify,review_evidence
    from test_w5_visual_evidence import make_sample,make_receipt,Store
    sample=make_sample();receipt=make_receipt(sample,rules_version='v1')
    visual=review_evidence(Store({'s1':sample}),receipt,current_rules_version='v1',current_snapshot_id=sample['snapshot_id'])
    assert visual.sufficient
    result=classify({'tags':['cat']},RuleSet('v2',(Rule('r','topic','cat',('cats',),weight=2),)),visual=visual)
    print('RA04',visual.rules_version,result['confidence'],result['needs_more'])
    assert result['needs_more'] is True

def test_ra05_scope_keeps_account_identity_for_report_gate():
    from organizer.batches import plan_batch
    from organizer.group_scope import build_scope
    from organizer.report import build_report,ReportError
    snapshot={'id':'same-snapshot','account_id':'account-B','status':'complete','memberships_complete':True,'artists':{'1':{'artist_id':'1','visibility':'0','groups':['g'],'is_following':True}},'memberships':{'show':{'g':{'ids':['1'],'complete':True}},'hide':{}}}
    scope=build_scope(snapshot,groups=['g'])
    batch=plan_batch(['1'],account_id='account-A',artists=1,requests=1,snapshot_id='same-snapshot')
    error=None
    try:report=build_report(batch,[{'artist_id':'1','kind':'unchanged','action':'keep','needs_human':False}],scope)
    except ReportError as e:error=e.code
    print('RA05',dict(scope.source),error)
    assert error is not None

def test_control_classification_with_missing_visual_evidence_stays_low():
    from organizer.rules import Rule,RuleSet,classify
    r=classify({'tags':['cat']},RuleSet('v1',(Rule('r','topic','cat',('cats',),weight=2),)))
    assert r['needs_more'] is True

def test_control_applied_progress_cannot_roll_back_to_send_apply_again():
    from organizer.batches import plan_batch,advance,rebuild,BatchError,next_action
    batch=plan_batch(['1'],account_id='synthetic',artists=1,requests=1)
    for event in ('sample','review','plan','apply'):batch=advance(batch,'1',event)
    error=None;action=None
    try:
        restored=rebuild(batch,{'1':'planned'});action=next_action(restored,'1')
    except BatchError as e:error=e.code
    print('CONTROL_ROLLBACK',error,action)
    assert error is not None

def _png(pixel_index):
    def chunk(k,d):return struct.pack('>I',len(d))+k+d+struct.pack('>I',zlib.crc32(k+d)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,3,0,0,0))+chunk(b'PLTE',b'\0\0\0')+chunk(b'IDAT',zlib.compress(bytes([0,pixel_index])))+chunk(b'IEND',b'')

def test_ra06_decode_receipt_is_bound_to_the_bytes_it_certifies(tmp_path,monkeypatch):
    from core import quality_check as q
    p=tmp_path/'same.png';bad=_png(1);good=_png(0);assert len(bad)==len(good);p.write_bytes(bad)
    baseline=q.check_file(p,decode_policy='strict');assert baseline['quality']['decode']['state']=='failed'
    old=p.stat();decode=q._decode
    def during_decode(*args,**kwargs):
        p.write_bytes(good)
        try:return decode(*args,**kwargs)
        finally:p.write_bytes(bad);os.utime(p,ns=(old.st_atime_ns,old.st_mtime_ns))
    monkeypatch.setattr(q,'_decode',during_decode)
    report=q.check_file(p,decode_policy='strict')
    print('RA06',report['quality']['decode'],report['path_stability'],m._decode_outcome([report]))
    assert p.read_bytes()==bad
    assert m._decode_outcome([report])!=0

def test_ra04_visual_adapter_requires_a_stored_review_not_a_retyped_mapping(tmp_path):
    from organizer.rules import review_evidence
    from organizer.store import Store
    from test_w5_visual_evidence import make_sample,make_receipt
    sample=make_sample();receipt=make_receipt(sample)
    store=Store(tmp_path/'organizer.sqlite3')
    try:
        store.put('sample',sample['id'],sample)
        assert store.list('review')==[]
        visual=review_evidence(store,receipt,current_rules_version='v1',current_snapshot_id=sample['snapshot_id'])
        print('RA04_UNSTORED',visual.state,visual.receipt_id)
        assert not visual.sufficient
    finally:store.db.close()


def facts(path,job):
    from core.follow_facts_v2 import coverage_facts,read_only_connection
    conn=read_only_connection(path)
    try:
        conn.execute('BEGIN')
        return coverage_facts(conn,job['job_id'])
    finally:conn.close()

def test_ra03_reused_cross_job_page_is_visible(tmp_path):
    from core.library_repository import LibraryRepository
    import test_u1a_j01_health as h
    path=tmp_path/'library.sqlite3'
    with LibraryRepository(path) as repo:
        h.first_task(repo,tmp_path)
        job,accepted=h.second_task(repo)
        assert accepted==()
        result=h.settle(repo,job)
        assert result['pages']['existing']==1
        value=facts(path,job)
        print('RA03_REUSE',value['pages'])
        assert len(value['pages']['observed'])==1

def test_ra03_one_work_remains_one_work_after_resume(tmp_path):
    from core.library_repository import LibraryRepository
    from delivery_fixtures import make_job
    path=tmp_path/'library.sqlite3'
    with LibraryRepository(path) as repo:
        job,ids,_=make_job(repo)
        before=facts(path,job)
        assert before['run']['totals']['works_seen']==1
        repo.request_stop(job['job_id'],'pause')
        assert repo.settle_stop(job['batch_id'])
        resumed=repo.resume(job['job_id'],'resume')
        assert repo.activate_next()['batch_id']==resumed['batch_id']
        row=repo.claim_delivery(ids[0],resumed['batch_id'])
        assert row
        repo.release_delivery(ids[0],resumed['batch_id'],state='suspended')
        value=facts(path,job)
        print('RA03_RESUME',value['run']['totals'],value['pages'])
        assert value['run']['totals']['works_seen']==1
