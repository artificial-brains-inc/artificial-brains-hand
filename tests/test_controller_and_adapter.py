"""Exercise real controller and adapter methods with explicit fake hardware.

These tests do not install, simulate, or validate the vendor SDK/neural engine.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch
import pytest
from aerohand_control.app import HandController, handle_command, load_config
from aerohand_control.tactile import TactileEncoding, TactileSample
from aerohand_control.constants import FINGERS, MOTOR_KEYS, input_contract, output_contract
from aerohand_control.neural import DecodedFrame, OutputDecoder
from aerohand_control.body_commands import permitted_motor_counts
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def hand_module(monkeypatch):
    """Stub only imported vendor model names; do not alter runtime source."""
    package=types.ModuleType('aero_open_sdk'); package.__path__=[]
    model=types.ModuleType('aero_open_sdk.actuations_to_joints')
    model.ActuationsToJointsModelCompact=type('UnusedVendorModel',(),{})
    constants=types.ModuleType('aero_open_sdk.aero_hand_constants')
    constants.AeroHandConstants=type('UnusedVendorConstants',(),{})
    monkeypatch.setitem(sys.modules,'aero_open_sdk',package)
    monkeypatch.setitem(sys.modules,model.__name__,model)
    monkeypatch.setitem(sys.modules,constants.__name__,constants)
    spec=importlib.util.spec_from_file_location('aerohand_control.hand',ROOT/'aerohand_control/hand.py')
    m=importlib.util.module_from_spec(spec); monkeypatch.setitem(sys.modules,spec.name,m); spec.loader.exec_module(m)
    return m


def adapter(m):
    h=m.AeroHandAdapter.__new__(m.AeroHandAdapter)
    h._target=[25.]*7; h.limits=[(0.,100.)]*7; h.max_target_lead_degrees=8.
    h.read_positions=lambda:[20.]*7
    h._joint_step_valid=lambda a,b:True  # test wiring, not vendor geometry
    h.writes=[]; h._send_positions=lambda values:h.writes.append(values[:])
    return h


def test_real_adapter_sends_hold_rebase_even_without_increment(hand_module):
    h=adapter(hand_module)
    r=h.apply_deltas([0.]*7,grip_holds=[False,False,False,True,False,False,False])
    assert r.wrote and h.writes[0]==[25.,25.,25.,20.,25.,25.,25.]
    assert h._target==h.writes[0]


def test_real_adapter_preserves_opening_and_other_motor_requests(hand_module):
    h=adapter(hand_module)
    r=h.apply_deltas([0.,0.,0.,-1.,1.,0.,0.],grip_holds=[False,False,False,True,False,False,False])
    assert r.positions==tuple([25.,25.,25.,19.,26.,25.,25.])


def test_real_adapter_does_not_commit_failed_hold_write(hand_module):
    h=adapter(hand_module)
    def fail(values): raise hand_module.HandCommunicationError('synthetic disconnect')
    h._send_positions=fail
    with pytest.raises(hand_module.HandCommunicationError): h.apply_deltas([0.]*7,grip_holds=[True]*7)
    assert h._target==[25.]*7


def test_real_adapter_no_hold_no_activity_keeps_target(hand_module):
    h=adapter(hand_module); r=h.apply_deltas([0.]*7)
    assert not r.wrote and not h.writes and h._target==[25.]*7

class Clock:
    def __init__(self): self.now=100.
    def __call__(self): return self.now
class Sensors:
    def __init__(self,clock): self.clock=clock; self.available=True; self.raw=1800
    def latest(self,*args):
        return TactileSample(10,(self.raw,)*5,self.clock(),1) if self.available else None
class Hand:
    limits=[(0.,100.)]*7
    def __init__(self): self.position=[20.]*7; self.calls=[]
    def hold(self): return self.position[:]
    def read_positions(self): return self.position[:]
    def apply_deltas(self,deltas,*,grip_holds):
        self.calls.append((tuple(deltas),list(grip_holds)))
        return types.SimpleNamespace(limited=(),wrote=True)
class Neural:
    def __init__(self): self.frames=[]; self.published=[]; self.latest_step=-1
    def poll(self,timeout): frames=self.frames; self.frames=[]; return frames
    def publish(self,*args): self.published.append(args)


def controller(tmp_path,hand_module):
    clock=Clock(); h=Hand(); n=Neural(); s=Sensors(clock)
    c=TactileEncoding()
    ctrl=HandController(h,n,load_config(ROOT/'config.json'),clock=clock,emit=lambda x:None,
                        initial_positions=h.position,tactile=s,tactile_encoding=c)
    ctrl.input_drain_remaining=0; ctrl.phase='HOLD'
    return ctrl,h,n,s,c,clock


def enqueue(n,clock,step=1,raw=None,deltas=(1.,)*7):
    n.latest_step=step
    n.frames=[DecodedFrame(step=step,captured_at=clock(),deltas=deltas,raw=raw or {},transport='fixture')]


def active(ctrl):
    ctrl.set_goal('coordinates',[80.]*7); ctrl.resume_pending=False; ctrl.published_at=0


def test_high_adc_alone_does_not_choose_motor_veto(tmp_path,hand_module):
    c,h,n,s,cal,clock=controller(tmp_path,hand_module); active(c)
    enqueue(n,clock,raw={}); c.tick()
    assert h.calls and h.calls[0][1]==[False]*7
    assert n.published[-1][0]==[80.]*7 and n.published[-1][1]==[20.]*7
    assert n.published[-1][3]==pytest.approx((1800/4095,)*5)


def test_neural_hold_translated_even_if_adc_is_low(tmp_path,hand_module):
    c,h,n,s,cal,clock=controller(tmp_path,hand_module); s.raw=100; active(c)
    enqueue(n,clock,raw={'grip_index_hold':1}); c.tick()
    assert h.calls[0][1]==[False,False,False,True,False,False,False]


def test_read_timeout_recoverable_not_permanent(tmp_path,hand_module):
    c,h,n,s,cal,clock=controller(tmp_path,hand_module); active(c)
    original=h.apply_deltas
    def timeout(*a,**kw): raise hand_module.HandReadTimeout('synthetic missed reply')
    h.apply_deltas=timeout
    enqueue(n,clock); c.tick()
    assert c.phase=='ACTIVE' and c.feedback_timeouts==1
    h.apply_deltas=original; clock.now+=.1
    enqueue(n,clock,step=2); c.tick()
    assert h.calls and c.feedback_available


def test_missing_tactile_no_zero_frame_no_motion_and_can_resume(tmp_path,hand_module):
    c,h,n,s,cal,clock=controller(tmp_path,hand_module); active(c)
    enqueue(n,clock); c.tick(); h.calls.clear(); n.published.clear()
    s.available=False; clock.now+=.1
    enqueue(n,clock,2); c.tick()
    assert not h.calls and not n.published and c.phase=='ACTIVE' and c.resume_pending
    s.available=True; clock.now+=2.
    enqueue(n,clock,3); c.tick()
    assert n.published and not h.calls and not c.resume_pending
    clock.now+=.1; enqueue(n,clock,4); c.tick()
    assert h.calls


def test_observe_only_never_applies_motor_or_hold_targets(tmp_path,hand_module):
    c,h,n,s,cal,clock=controller(tmp_path,hand_module)
    c.observe_only=True; active(c)
    enqueue(n,clock,raw={'grip_thumb_hold':1}); c.tick()
    assert not h.calls
    h.hold=lambda:pytest.fail('physical Hold in observation mode')
    c.hold(); assert c.phase=='HOLD'


def test_new_session_can_select_coordinates_without_any_capture(tmp_path,hand_module):
    c,h,n,s,enc,clock=controller(tmp_path,hand_module)
    handle_command('targets 80 80 80 80 80 80 80',c)
    assert c.phase=='ACTIVE' and c.goal==[80.]*7
    assert not list(tmp_path.glob('*.json'))


@pytest.mark.parametrize('command',['calibrate_enough thumb'])
def test_removed_capture_commands_are_rejected(tmp_path,hand_module,command):
    c,*_=controller(tmp_path,hand_module)
    with pytest.raises(ValueError,match='Unknown command'): handle_command(command,c)


def test_zero_and_full_adc_do_not_independently_pick_a_motor(tmp_path,hand_module):
    c,h,n,s,enc,clock=controller(tmp_path,hand_module); active(c)
    for step,raw in enumerate((0,4095),1):
        s.raw=raw; clock.now+=.1
        enqueue(n,clock,step,raw={'index_tendon_flex':1},deltas=(0.,0.,0.,.8,0.,0.,0.))
        c.tick()
    assert all(mask==[False]*7 for _,mask in h.calls)
    assert h.calls[0][0]==h.calls[1][0]


def test_decoder_hold_retains_extension_when_both_directions_active():
    # Verify the real decoder's filtering before its SDK delegate. A transparent
    # test decoder below exposes only the delegated counts, not SDK behaviour.
    d=OutputDecoder.__new__(OutputDecoder)
    d.widths=output_contract(); d.motor_widths={f'{k}_{s}':1 for k in MOTOR_KEYS for s in ('flex','extend')}
    class Delegate:
        def decode(self,event,ctx):
            counts=event['actuation']['channels']
            return {'deltas':{k:counts[k+'_flex']-counts[k+'_extend'] for k in MOTOR_KEYS}}
    d._channels=Delegate()
    event={'step':1,'actuation':{'channels':{'index_tendon_flex':1,'index_tendon_extend':1,'grip_index_hold':1}}}
    f=d.decode(event,received_at=100.)
    assert f.deltas[3]==-1 and f.raw['index_tendon_flex']==1
    assert event['actuation']['channels']['index_tendon_flex']==1
    assert permitted_motor_counts(f.raw)['index_tendon_flex']==0


def test_unloaded_capture_can_start_in_hold_but_not_while_active(tmp_path,hand_module):
    c,h,n,s,enc,clock=controller(tmp_path,hand_module)
    handle_command('calibrate_zero',c)
    assert c.baseline_capture is not None
    with pytest.raises(ValueError,match='zeroing'):
        c.set_goal('grab',[80.]*7)
    c.hold();active(c)
    with pytest.raises(ValueError,match='Hold'):
        c.capture_baseline()


def test_empty_frame_does_not_read_serial(hand_module):
    h=adapter(hand_module)
    h.read_positions=lambda:pytest.fail('empty output must not read serial')
    result=h.apply_deltas([0.]*7)
    assert not result.wrote and result.applied_deltas==(0.,)*7


def test_recent_feedback_reused_without_new_query(hand_module,monkeypatch):
    h=adapter(hand_module);h._last_feedback=[20.]*7;h.last_feedback_at=99.98
    h.feedback_reuse_s=.04;h.position_reuses=0
    monkeypatch.setattr(hand_module.time,'monotonic',lambda:100.)
    h.read_positions=lambda:pytest.fail('recent packet should be reused')
    a=h.apply_deltas([.8]*7);b=h.apply_deltas([.8]*7)
    assert a.wrote and b.wrote and h.position_reuses==2
    assert b.positions==pytest.approx([26.6]*7)


def test_old_feedback_is_not_reused(hand_module,monkeypatch):
    h=adapter(hand_module);h._last_feedback=[2.]*7;h.last_feedback_at=99.
    h.feedback_reuse_s=.04;h.position_reuses=0
    monkeypatch.setattr(hand_module.time,'monotonic',lambda:100.)
    result=h.apply_deltas([.8]*7)
    assert result.measured==(20.,)*7 and h.position_reuses==0


def test_target_lead_reason_is_logged(hand_module):
    h=adapter(hand_module)
    result=h.apply_deltas([9.]*7)
    assert all('target_lead' in reasons for reasons in result.limit_reasons)
    assert result.applied_deltas==(3.,)*7


def test_grip_hold_reason_does_not_block_extension(hand_module):
    h=adapter(hand_module)
    result=h.apply_deltas([-1.]*7,grip_holds=[True]*7)
    assert result.positions==(19.,)*7
    assert all('neural_grip_hold' in reasons for reasons in result.limit_reasons)


def test_controller_zero_capture_finishes_and_persists_without_motor_decisions(tmp_path,hand_module):
    c,h,n,s,enc,clock=controller(tmp_path,hand_module)
    c.baseline_path=tmp_path/'tactile_baseline.json'
    c.capture_baseline()
    unloaded=(80,90,100,2000,110)
    for i in range(50):
        clock.now=100+i*.02
        reading=TactileSample(i,unloaded,clock.now,i)
        c._advance_baseline(reading)
    assert c.baseline_capture is None
    assert c.baseline_path.exists()
    assert enc.encode(reading)==(0.,)*5
    assert c.phase=='HOLD' and h.calls==[]
    from aerohand_control.baseline import load_baseline
    saved=load_baseline(c.baseline_path,[1]*5)
    assert saved['baseline']==list(unloaded)
    assert saved['noise_deadband']==[2]*5
