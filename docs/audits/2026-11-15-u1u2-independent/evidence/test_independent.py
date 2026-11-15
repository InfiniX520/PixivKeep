"""Synthetic independent probes. Every filesystem mutation stays in tmp_path."""
from pathlib import Path
import io, json, os, struct, sys, zlib
ROOT=Path(os.environ.get('PIXIVKEEP_AUDIT_REPO',Path(__file__).resolve().parents[4]))
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import pytest
from core import library_maintenance as m
from core import quality_check as q
import test_u1a_library_maintenance as h

def chunk(kind,data):
    return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)

def png(value=0,*,indexed=False):
    header=struct.pack('>IIBBBBB',1,1,8,3 if indexed else 0,0,0,0)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',header)+(chunk(b'PLTE',b'\x00\x00\x00') if indexed else b'')+chunk(b'IDAT',zlib.compress(bytes([0,value])))+chunk(b'IEND',b'')

def sealed(tmp):
    db=h.make_library(tmp/'source'/'library.sqlite3',jobs=1)
    return Path(m.backup_library(db,tmp/'backups',label='baseline')['manifest_path']).parent

def test_ux01_claimed_destination_replacement_cannot_overwrite_other_file(tmp_path,monkeypatch):
    root=sealed(tmp_path);victim=tmp_path/'protected-synthetic.bin';victim.write_bytes(b'owned sentinel bytes')
    before=victim.read_bytes();original=m._copy_and_hash;hits=[]
    def replace(source,destination):
        assert destination.resolve().is_relative_to(tmp_path.resolve())
        assert victim.resolve().is_relative_to(tmp_path.resolve())
        destination.unlink();os.link(victim,destination);hits.append(True)
        return original(source,destination)
    monkeypatch.setattr(m,'_copy_and_hash',replace)
    try:report=m.verify_backup(root)
    except m.LibraryMaintenanceError:report={'ok':False}
    print('UX01',hits,report['ok'],len(victim.read_bytes()),len(before))
    assert hits and victim.read_bytes()==before

def test_ux01_unowned_sidecar_must_not_be_adopted_and_deleted(tmp_path,monkeypatch):
    root=sealed(tmp_path);original=m.inspect_library;created=[]
    def inspect(path,*args,**kwargs):
        report=original(path,*args,**kwargs)
        if '.verify-' in str(path):
            foreign=Path(str(path)+'-shm');foreign.write_bytes(b'not created by SQLite in this synthetic test');created.append(foreign)
        return report
    monkeypatch.setattr(m,'inspect_library',inspect)
    report=m.verify_backup(root)
    print('UX01_SIDECAR',len(created),report['ok'],[p.exists() for p in created])
    assert created and all(p.exists() for p in created)

def test_ux02_palette_index_beyond_palette_is_not_a_strict_pass(tmp_path,capsys):
    p=tmp_path/'bad-index.png';p.write_bytes(png(1,indexed=True))
    code=m.main(['--json','decode-check',str(p)])
    report=json.loads(capsys.readouterr().out)
    print('UX02',code,report['files'][0]['quality'])
    assert code!=0 and report['files'][0]['quality']['decode']['state']!='ok'

def test_control_valid_palette_index_passes(tmp_path):
    p=tmp_path/'good.png';p.write_bytes(png(0,indexed=True))
    assert q.check_file(p,decode_policy='strict')['quality']['decode']['state']=='ok'

def test_ux03_changed_final_file_cannot_receive_success(tmp_path,monkeypatch,capsys):
    p=tmp_path/'changed.png';p.write_bytes(png(0));original=q.quality_evidence
    def change(*args,**kwargs):
        report=original(*args,**kwargs);p.write_bytes(png(1));return report
    monkeypatch.setattr(q,'quality_evidence',change)
    code=m.main(['--json','decode-check',str(p)])
    report=json.loads(capsys.readouterr().out)
    print('UX03_CHANGED',code,report['files'][0]['unchanged'])
    assert code!=0

def test_ux03_mismatched_expected_length_is_not_length_matched(tmp_path):
    p=tmp_path/'length.png';p.write_bytes(png())
    report=q.quality_evidence(p,expected_size=len(p.read_bytes())+1)
    print('UX03_LENGTH',report['quality'])
    assert report['quality']['transport']['state']!='length_matched'

def test_ux04_cancelled_quality_check_does_not_hash_the_whole_file(tmp_path,monkeypatch):
    import threading
    p=tmp_path/'cancelled.png';p.write_bytes(png());stop=threading.Event();stop.set()
    original=q._sha256;calls=[]
    def hashed(path):calls.append(True);return original(path)
    monkeypatch.setattr(q,'_sha256',hashed)
    q.check_file(p,decode_policy='strict',stop=stop)
    print('UX04_CANCEL_HASHES',len(calls))
    assert not calls

def test_ux05_gathered_artist_denominator_survives_composition():
    from core.follow_facts import gather_facts
    from core.follow_coverage import build_coverage
    class Source:
        run={'run_id':'one','status':'completed','total_artists':1}
        def artists(self,run_id):return [{'artist_id':'a','state':'completed','works_seen':3,'swept_at':'2026-11-14T00:00:00Z'}]
    facts=gather_facts(Source());report=build_coverage(facts,scope={'kind':'follow_run'}).as_dict()
    print('UX05',facts['run']['totals'],report['denominators'],report['counts'])
    assert report['denominators']['artists_total']==1 and report['counts']['artworks_seen']==3

def coverage_facts(saved,seen=1):
    return {'run':{'status':'completed','totals':{'total_artists':1,'works_seen':seen,'pages_saved':saved,'pages_already_verified':0,'excluded_by_filter':0,'unknown_fields':0,'duplicate_pages':0}},'artists':[{'artist_id':'a','state':'completed','pages':seen,'swept_at':'2026-11-14'}],'incomplete':[],'budget':None}

def test_ux06_one_multipage_work_is_not_inconsistent_for_saving_three_pages():
    from core.follow_coverage import build_coverage
    report=build_coverage(coverage_facts(3),scope={'kind':'follow_run'})
    print('UX06_MULTIPAGE',report.overall,report.consistent,report.reasons)
    assert report.consistent is True

def test_ux06_one_saved_page_does_not_prove_one_work_fully_delivered():
    from core.follow_coverage import build_coverage
    report=build_coverage(coverage_facts(1),scope={'kind':'follow_run'})
    print('UX06_UNPROVEN',report.overall,report.delivery)
    assert report.delivery['state']!='complete'

@pytest.mark.parametrize('fault',['missing_include','unparsed_import','pip_check_failed'])
def test_ux07_incomplete_dependency_report_cannot_exit_success(tmp_path,monkeypatch,capsys,fault):
    from tools.maintenance import dependency_report as d
    if fault=='missing_include':
        (tmp_path/'requirements.txt').write_text('-r missing.txt\n','utf-8')
    if fault=='unparsed_import':
        (tmp_path/'core').mkdir()
        (tmp_path/'core'/'broken.py').write_text('def :\n','utf-8')
    env={'installed':[], 'pip_check':{'exit':1 if fault=='pip_check_failed' else 0,'output':'synthetic'}}
    monkeypatch.setattr(d.dependency_env,'environment_facts',lambda:env)
    code=d.main(['--json','--root',str(tmp_path)])
    report=json.loads(capsys.readouterr().out)
    reached=bool(report['declared']['errors']) if fault=='missing_include' else bool(report['imports']['unparsed']) if fault=='unparsed_import' else report['environment']['pip_check']['exit']==1
    print('UX07_PUBLIC',fault,reached,code,report['matrix']['verdict'])
    assert reached and code!=0

def test_ux07_all_runtime_version_constraints_must_hold():
    from tools.maintenance.dependency_matrix import compare
    declared={'runtime':[{'name':'example','specifier':'>=1'},{'name':'example','specifier':'<2'}],'dev':[],'build':[],'errors':[]}
    report=compare(declared,{'installed':[{'name':'example','version':'2.5'}]},{'modules':[]})
    print('UX07_CONSTRAINT',report)
    assert report['verdict']!='ok'

def test_control_dependency_missing_runtime_is_attention():
    from tools.maintenance.dependency_matrix import compare
    assert compare({'runtime':[{'name':'example','specifier':'>=1'}]}, {'installed':[]}, {'modules':[]})['verdict']=='attention'

def test_wr04_sidecar_identity_failure_stays_in_typed_cli_protocol(tmp_path,monkeypatch,capsys):
    root=sealed(tmp_path);original=os.lstat;hits=[]
    def unavailable(path,*args,**kwargs):
        if '.verify-' in str(path) and str(path).endswith('-shm'):
            hits.append(True)
            raise PermissionError('synthetic sidecar identity unavailable')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(os,'lstat',unavailable)
    code=m.main(['--json','verify',str(root)])
    result=json.loads(capsys.readouterr().out)
    print('WR04_SIDECAR',hits,code,result)
    assert hits and code==3 and result.get('status')=='error'

def test_ux04_byte_ceiling_is_checked_before_full_input_work(tmp_path,monkeypatch):
    from core.decode_limits import DecodeLimits
    p=tmp_path/'bounded.png';p.write_bytes(png());reads=[]
    original=Path.open
    class Reader:
        def __init__(self,stream):self.stream=stream
        def __enter__(self):return self
        def __exit__(self,*args):return self.stream.__exit__(*args)
        def read(self,*args):
            data=self.stream.read(*args);reads.append(len(data));return data
        def __getattr__(self,key):return getattr(self.stream,key)
    def track(path,*args,**kwargs):
        stream=original(path,*args,**kwargs)
        return Reader(stream) if path==p else stream
    monkeypatch.setattr(Path,'open',track)
    result=q.check_file(p,decode_policy='strict',limits=DecodeLimits(max_bytes=1))
    print('UX04_BYTE_WORK',sum(reads),p.stat().st_size,result['quality']['decode'])
    assert sum(reads)<p.stat().st_size
