"""Software fixtures only, not physical/neural performance assertions."""
import json
import time
from pathlib import Path
import pytest
from aerohand_control.baseline import BaselineCapture,load_baseline,save_baseline
from aerohand_control.tactile import TactileEncoding,TactileSample
from aerohand_control.neuron_log import sse_messages,NeuronRecorder
from aerohand_control.runlog import RunRecorder,clean
from aerohand_control.neural import make_readings
from aerohand_control.diagnostics import MotorDiagnostics
from aerohand_control.constants import MOTOR_KEYS
ROOT=Path(__file__).resolve().parents[1]
BASE=[80,90,100,2000,110]

def sample(values=BASE,seq=1,stamp=1):return TactileSample(seq,tuple(values),stamp,seq)

def encoding():
    return TactileEncoding([1]*5,baseline=BASE,noise_deadband=[0.0]*5)

def test_unloaded_values_all_encode_as_zero():
    e=encoding();assert e.encode(sample())==(0.,)*5
    assert e.status(sample())['adc']['ring']==2000
    assert e.status(sample())['normalized']['ring']==0
    assert e.status(sample())['headroom']['ring']==2095


def test_sensor_encoding_monotonic_up_to_adc_endpoint():
    e=encoding()
    assert e.encode(sample([4095]*5))==(1.,)*5
    mid=[int((4095+b)/2) for b in BASE]
    assert e.encode(sample(mid))==pytest.approx([.5]*5,abs=.001)
    assert e.encode(sample([0]*5))==(0.,)*5


def test_zero_is_silent_vector_not_sdk_cumulative_floor():
    r=make_readings([20.]*7,[20.]*7,[(0.,100.)]*7,[0.]*5)
    for f in ('thumb','index','middle','ring','little'):
        assert r['touch_'+f]['mode']=='vector_f32'
        assert sum(r['touch_'+f]['signal'])==0
    assert r['goal_index_tendon']==r['position_index_tendon']


def test_downward_polarity_offset():
    e=TactileEncoding([-1]*5,baseline=[2500]*5,noise_deadband=[2]*5)
    assert e.encode(sample([2500]*5))==(0.,)*5
    assert e.encode(sample([2498]*5))==(0.,)*5
    assert e.encode(sample([0]*5))==(1.,)*5
    assert e.encode(sample([2000]*5))==pytest.approx([498/2498]*5)


def test_baseline_capture_uses_unique_new_samples_and_median():
    c=BaselineCapture(1.)
    assert not c.observe(sample(stamp=.9),1.)
    assert len(c.samples)==0
    one=sample(stamp=1.,seq=0)
    for i in range(5):c.observe(one,1.)
    assert len(c.samples)==1
    for i in range(1,50):ready=c.observe(sample(seq=i,stamp=1+i*.02),1+i*.02)
    assert ready
    b=c.result([1]*5);assert b['baseline']==BASE and b['noise_deadband']==[2]*5
    assert b['samples']==50


def test_missing_data_cannot_complete_zeroing():
    c=BaselineCapture(1.)
    with pytest.raises(TimeoutError):c.observe(None,10.)


def test_atomic_save_load(tmp_path):
    c=BaselineCapture(0,count=2,minimum_seconds=0)
    c.observe(sample(seq=1,stamp=1),1);c.observe(sample(seq=2,stamp=2),2)
    b=c.result([1]*5);path=tmp_path/'zero.json';save_baseline(path,b)
    assert load_baseline(path,[1]*5)==b
    assert not list(tmp_path.glob('*.tmp'))

@pytest.mark.parametrize('bad',[float('nan'),True,-1,4096])
def test_invalid_baseline_rejected(bad):
    with pytest.raises(ValueError):TactileEncoding(baseline=[bad]*5)


def test_endpoint_zero_rejected_instead_of_division_by_zero():
    with pytest.raises(ValueError):TactileEncoding(baseline=[4095]*5)


def test_log_writes_all_streams_and_redacts_secrets(tmp_path):
    r=RunRecorder(tmp_path,metadata={'api_key':'plx_secret'},max_mb=1)
    for name in ('events','sensors','outputs','motion','neurons'):
        r.record(name,'fixture',{'value':1,'message':'failed with plx_private123'})
    r.close()
    for name in ('events','sensors','outputs','motion','neurons'):
        lines=(r.path/(name+'.jsonl')).read_text().splitlines();assert len(lines)==1
        obj=json.loads(lines[0]);assert obj['data']['value']==1 and 'plx_' not in lines[0]
    assert json.loads((r.path/'run.json').read_text())['closed_cleanly'] is True
    assert 'plx_secret' not in (r.path/'run.json').read_text()


def test_disk_cap_reported_not_silent_or_control_error(tmp_path):
    r=RunRecorder(tmp_path,max_mb=1);r.max_bytes=300
    for i in range(10):r.record('outputs','frame',{'body':'x'*200})
    r.close();assert r.bytes_written<=300 and r.full and r.dropped['outputs']>0


def test_sse_parser_preserves_raw_bits_and_layout():
    lines=['event: hello','data: {"layout":{"neurons":["X:0"]}}','',
           'id: '+('a'*32)+':1','event: frame','data: {"sequence":1,"telemetry":{"bits":"AQ==","input_bits":"AA=="}}','']
    messages=list(sse_messages(lines));assert len(messages)==2
    assert messages[1][2]['telemetry']['bits']=='AQ=='


def test_neuron_recording_handles_gap_and_does_not_invent_bits(tmp_path):
    r=RunRecorder(tmp_path,max_mb=1);n=NeuronRecorder('http://local','plx_private',r)
    epoch='a'*32
    assert n._event('hello',None,{'layout':{'neurons':['X:0']}})
    for seq in (1,3):
        assert n._event('frame',epoch+':'+str(seq),{'epoch':epoch,'sequence':seq,'item':{'step':seq},'telemetry':None})
    assert n.frames==2 and n.state=='waiting_for_neurons'
    assert not n._event('gap',None,{'message':'history expired'})
    r.close();items=[json.loads(l) for l in (r.path/'neurons.jsonl').read_text().splitlines()]
    assert any(x['kind']=='sequence_gap' for x in items)
    assert all(x['data']['telemetry'] is None for x in items if x['kind']=='frame')


def test_logging_failure_does_not_choose_motor_command(tmp_path):
    r=RunRecorder(tmp_path,max_mb=1);r.error='synthetic disk error'
    r.record('outputs','frame',{'step':5});r.close()
    assert r.dropped['outputs']==1


def test_recorded_diagnostics_preserve_both_directions():
    from aerohand_control.neural import DecodedFrame
    clock=lambda:1.
    d=MotorDiagnostics(clock)
    f=DecodedFrame(1,1.,(0.,)*7,{'index_tendon_flex':1,'index_tendon_extend':1},'fixture')
    d.observe(f);d.disposition('no_target_change')
    m=d.snapshot()['motors']['index_tendon']
    assert m['flex_count']==m['extend_count']==1 and m['both_active_frames']==1


def test_public_neuron_stream_get_records_layout_bits_and_device_errors(tmp_path):
    import httpx
    requests=[]
    epoch='b'*32
    body=('event: hello\ndata: '+json.dumps({'layout':{'neurons':['T:0','T:1']}})+'\n\n'
          'id: '+epoch+':7\nevent: frame\ndata: '+json.dumps({'epoch':epoch,'sequence':7,
          'item':{'step':7},'telemetry':{'bits':'AQ==','input_bits':'AA=='}})+'\n\n'
          'event: error\ndata: {"message":"synthetic stream ending"}\n\n')
    def handler(request):
        requests.append(request)
        return httpx.Response(200,headers={'Content-Type':'text/event-stream'},text=body)
    def factory(**kwargs):return httpx.Client(transport=httpx.MockTransport(handler),**kwargs)
    r=RunRecorder(tmp_path,max_mb=1)
    n=NeuronRecorder('http://device.local:8080','plx_fixture',r,client_factory=factory)
    n.start();n.thread.join(timeout=3);assert not n.thread.is_alive()
    assert len(requests)==1 and requests[0].method=='GET'
    assert requests[0].url.path=='/api/brain/stream'
    assert dict(requests[0].url.params)=={'telemetry':'true','replay':'false'}
    assert requests[0].headers['authorization']=='Bearer plx_fixture'
    assert n.frames==1 and n.state=='error'
    r.close()
    content=(r.path/'neurons.jsonl').read_text()
    assert 'AQ==' in content and 'T:0' in content and 'plx_fixture' not in content


def test_missing_neuron_endpoint_does_not_retry_or_stop_control_logs(tmp_path):
    import httpx
    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(404)),**kwargs)
    r=RunRecorder(tmp_path,max_mb=1)
    n=NeuronRecorder('http://device.local','key',r,client_factory=factory)
    n.start();n.thread.join(timeout=3)
    assert not n.thread.is_alive() and n.state=='unavailable' and n.frames==0
    r.record('motion','fixture_motor_record',{'wrote':False});r.close()
    assert 'fixture_motor_record' in (r.path/'motion.jsonl').read_text()


def test_log_export_whitelist_excludes_env_and_symlinks(tmp_path):
    import importlib.util,zipfile
    spec=importlib.util.spec_from_file_location('pack_run_test',ROOT/'tools/pack_logs.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    run=tmp_path/'run-fixture';run.mkdir()
    (run/'run.json').write_text('{}');(run/'events.jsonl').write_text('{}\n')
    (run/'.env').write_text('KEY=secret');(run/'unrelated.json').write_text('secret')
    (run/'neurons.jsonl').symlink_to(run/'.env')
    output=module.pack(run)
    with zipfile.ZipFile(output) as z:
        assert sorted(z.namelist())==['run-fixture/events.jsonl','run-fixture/run.json']
