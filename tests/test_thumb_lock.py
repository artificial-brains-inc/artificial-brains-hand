"""Manual-thumb behavior suite. Hardware and neural activity are fixtures.

Tests exercise real adapter/controller/UI methods with vendor reference geometry,
not physical motion, tendon mechanics or the installed vendor SDK.
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_open_reference import pose_module, Clock, DIRECTIONS
from aerohand_control.thumb_setup import load_thumb_setup, save_thumb_setup, valid_angle
from aerohand_control.ui import LocalUI
from aerohand_control.app import HandController, handle_command, load_config, pose_menu
from aerohand_control.tactile import TactileEncoding, TactileSample
from aerohand_control.startup import open_before_brain
from aerohand_control.neural import DecodedFrame
ROOT = Path(__file__).resolve().parents[1]

class Hardware:
    def __init__(self, position): self.position=list(position);self.writes=[];self.fail=False
    def get_actuations(self): return self.position[:]
    def set_actuations(self, values):
        if self.fail: raise RuntimeError("fixture write failure")
        self.writes.append(list(values));self.position=list(values)
    def close(self): pass

def real_adapter(pose_module, angle=35):
    poses, hm=pose_module
    logical=poses.compact_to_actuations([angle,20,30,20,20,20,20],motor_directions=DIRECTIONS)
    hardware=Hardware([100-logical[0],*logical[1:]])
    hand=hm.AeroHandAdapter(None,hand=hardware,motor_directions=DIRECTIONS,feedback_reuse_s=0)
    hand.read_positions()
    hand.set_thumb_abduction_lock(angle)
    return hand,hardware

@pytest.mark.parametrize("name",['open','grab','pinch','pinch_middle','pinch_ring','pinch_pinky','point','peace','middle_finger'])
@pytest.mark.parametrize("amount",[0,.5,1])
@pytest.mark.parametrize("angle",[17.,45.,80.])
def test_all_pose_amounts_keep_the_manual_angle(pose_module,name,amount,angle):
    poses, hm=pose_module
    a=poses.pose_to_actuations(name,amount,motor_directions=DIRECTIONS,thumb_abduction=angle)
    assert a[0] == pytest.approx(angle)
    # Other fingers have exactly their old target. Two thumb tendon goals are
    # intentionally computed using this fixed orientation, not the old pose's.
    old=poses.pose_to_actuations(name,amount,motor_directions=DIRECTIONS)
    assert a[3:] == pytest.approx(old[3:])
    hand,_=real_adapter(pose_module)
    q=hand.compact_joint_positions(a)
    assert q[0] == pytest.approx(angle)
    old_q=hand.compact_joint_positions(old)
    assert q[1:] == pytest.approx(old_q[1:])

@pytest.mark.parametrize("delta",[-10.,-.1,.1,10.])
def test_neural_abduction_never_reaches_serial(pose_module,delta):
    h,hw=real_adapter(pose_module)
    before=h.target_positions
    result=h.apply_deltas([delta,0,0,0,0,0,0])
    assert result.positions[0] == before[0] == 35
    assert result.applied_deltas[0] == 0
    assert result.limit_reasons[0] == ('manual_thumb_lock',)
    assert not hw.writes

def test_other_fingers_keep_moving_with_abduction_output(pose_module):
    h,hw=real_adapter(pose_module)
    r=h.apply_deltas([10.,0,0,1.,1.,1.,1.])
    assert r.applied_deltas==pytest.approx([0,0,0,1,1,1,1])
    assert hw.writes[-1][0]==65

def test_thumb_bending_still_accepts_neural_activity(pose_module):
    h,hw=real_adapter(pose_module)
    r=h.apply_deltas([-10.,.4,.4,0,0,0,0])
    assert r.applied_deltas==pytest.approx([0,.4,.4,0,0,0,0])
    assert hw.writes[-1][0]==65

def test_neural_grip_hold_and_feedback_drift_cannot_reanchor_lock(pose_module):
    h,hw=real_adapter(pose_module)
    hw.position[0]=70.0 # actual logical 30; fixed command still 35
    r=h.apply_deltas([2.,0,0,1,0,0,0],grip_holds=[True,True,True,False,False,False,False])
    assert r.positions[0]==35 and hw.writes[-1][0]==65

def test_operational_hold_preserves_fixed_angle_not_drift(pose_module):
    h,hw=real_adapter(pose_module)
    hw.position[0]=70.0
    measured=h.hold()
    assert measured[0]==30.0 # actual feedback is not altered
    assert h.target_positions[0]==35 and hw.writes[-1][0]==65

def test_any_raw_boundary_write_is_pinned(pose_module):
    h,hw=real_adapter(pose_module)
    target=hw.position[:];target[0]=0
    h._write_hardware_positions(target)
    assert hw.writes[-1][0]==65

def test_manual_angle_difference_does_not_fault_other_motors(pose_module):
    h,hw=real_adapter(pose_module)
    hw.position[0]=95 # difference >8-degree neural target lead, intentionally excluded
    r=h.apply_deltas([0,0,0,1,0,0,0])
    assert r.applied_deltas[3]==1 and hw.writes[-1][0]==65

def test_manual_setup_preserves_estimated_thumb_flexion_and_other_fingers(pose_module):
    h,hw=real_adapter(pose_module)
    before=h.read_positions();q=h.compact_joint_positions(before)
    target=h.manual_thumb_adjust(40.)
    assert target[0]==40 and h.thumb_abduction_lock==40
    assert h.compact_joint_positions(target)[1:]==pytest.approx(q[1:])
    assert target[3:]==before[3:]
    assert hw.writes[-1][0]==60

def test_manual_setup_write_failure_keeps_previous_lock_and_target(pose_module):
    h,hw=real_adapter(pose_module);before=h.target_positions;hw.fail=True
    with pytest.raises(pose_module[1].HandCommunicationError):h.manual_thumb_adjust(40.)
    assert h.thumb_abduction_lock==35 and h.target_positions==before

@pytest.mark.parametrize('angle',[float('nan'),float('inf'),True,-.1,100.1,'17'])
def test_invalid_manual_target_never_writes(pose_module,angle):
    h,hw=real_adapter(pose_module)
    with pytest.raises(ValueError):h.manual_thumb_adjust(angle)
    assert not hw.writes and h.thumb_abduction_lock==35

def test_open_completion_does_not_wait_for_manual_axis(pose_module):
    h,hw=real_adapter(pose_module)
    poses,_=pose_module;goal=poses.pose_to_actuations('open',thumb_abduction=35,motor_directions=DIRECTIONS)
    class Lagged:
        def command_startup_pose(self,v):self.sent=v
        def read_positions(self):return [49.2,*goal[1:]]
    clock=Clock();fake=Lagged();events=[]
    values=open_before_brain(fake,goal,confirm_indices=(1,2,3,4,5,6),clock=clock,sleep=clock.sleep,progress=events.append)
    assert values[0]==49.2 and events[-1]['confirmed']
    assert clock.now<1 and events[-1]['error_degrees'][0]==pytest.approx(14.2)

def test_other_axes_still_checked_during_open(pose_module):
    class Stalled:
        def command_startup_pose(self,v):pass
        def read_positions(self):return [99,0,0,0,8,0,0]
    clock=Clock()
    with pytest.raises(TimeoutError,match='middle_tendon'):
        open_before_brain(Stalled(),[0]*7,confirm_indices=(1,2,3,4,5,6),timeout_s=.3,clock=clock,sleep=clock.sleep)

def test_saved_angle_persists_and_motor_orientation_is_checked(tmp_path):
    path=tmp_path/'thumb.json'
    assert load_thumb_setup(path,DIRECTIONS) is None
    data=save_thumb_setup(path,31.2,DIRECTIONS)
    assert load_thumb_setup(path,DIRECTIONS)==31.2
    assert data['logical_degrees']==31.2 and not list(tmp_path.glob('.thumb-*'))
    with pytest.raises(ValueError,match='orientation'):load_thumb_setup(path,[1]*7)

class Sensors:
    def latest(self,*args):return TactileSample(100,(0,)*5,100.,1)
class Neural:
    latest_step=-1
    def poll(self,timeout):return []
    def publish(self,*args):pass

def control(pose_module,tmp_path):
    h,hw=real_adapter(pose_module)
    c=HandController(h,Neural(),load_config(ROOT/'config.json'),emit=lambda msg:None,
        initial_positions=h.read_positions(),tactile=Sensors(),tactile_encoding=TactileEncoding(),
        thumb_setup_path=tmp_path/'thumb.json')
    c.phase='HOLD';c.input_drain_remaining=0;c.hold_pending=False
    return c,h,hw

def test_manual_coordinates_cannot_override_thumb(pose_module,tmp_path):
    c,h,hw=control(pose_module,tmp_path)
    target=list(h.target_positions);target[0]=99
    c.set_goal('coordinates',target)
    assert c.goal[0]==35 and h.thumb_abduction_lock==35

def test_manual_controller_adjust_and_capture_are_saved(pose_module,tmp_path):
    c,h,hw=control(pose_module,tmp_path)
    handle_command('thumb_angle 42',c)
    assert h.thumb_abduction_lock==42 and c.thumb_saved_value==42
    assert load_thumb_setup(tmp_path/'thumb.json',DIRECTIONS)==42
    hw.position[0]=59.8
    handle_command('thumb_current',c)
    assert h.thumb_abduction_lock==pytest.approx(40.2)
    assert load_thumb_setup(tmp_path/'thumb.json',DIRECTIONS)==pytest.approx(40.2)

@pytest.mark.parametrize('phase,observe',[('ACTIVE',False),('HOLD',True)])
def test_manual_setup_not_inserted_into_a_grasp_or_observe_mode(pose_module,tmp_path,phase,observe):
    c,h,hw=control(pose_module,tmp_path);hw.writes.clear();c.phase=phase;c.observe_only=observe
    with pytest.raises(ValueError):c.adjust_thumb(42)
    assert not hw.writes and h.thumb_abduction_lock==35

def test_regular_motor_command_cannot_change_thumb_lock(pose_module,tmp_path):
    c,h,hw=control(pose_module,tmp_path)
    with pytest.raises(ValueError,match='locked'):handle_command('motor thumb_cmc_abd 99',c)
    assert h.thumb_abduction_lock==35

def ui_state():
    return dict(status='hold',mode='live',hold_pending=False,thumb_setup={
        'locked':True,'target':17.,'measured':17.,'can_adjust':True,'limits':[0,100]})

def test_ui_dispatches_manual_command_to_queue_only():
    calls=[];u=LocalUI(pose_menu(),calls.append);u.update(ui_state())
    u._submit({'command':'thumb_angle','angle':32,'generation':0})
    u._submit({'command':'thumb_current','generation':0})
    assert calls==['thumb_angle 32','thumb_current']

@pytest.mark.parametrize('change',[{'status':'active'},{'mode':'observe'},{'hold_pending':True},
    {'baseline_capture':{'active':True}},{'thumb_setup':{'can_adjust':False}}])
def test_ui_rejects_manual_setup_when_not_available(change):
    calls=[];u=LocalUI(pose_menu(),calls.append);s=ui_state();s.update(change);u.update(s)
    with pytest.raises(ValueError):u._submit({'command':'thumb_angle','angle':32,'generation':0})
    assert not calls

def test_stale_ui_manual_request_is_rejected_after_hold():
    calls=[];u=LocalUI(pose_menu(),calls.append);u.update(ui_state());u.invalidate_commands()
    with pytest.raises(ValueError):u._submit({'command':'thumb_angle','angle':32,'generation':0})
    assert not calls

def test_manual_axis_still_has_declared_neural_io():
    data=json.loads((ROOT/'brain.abdesign.json').read_text())
    names={n['name'] for n in data['nodes']}
    assert {'thumb_cmc_abd_flex','thumb_cmc_abd_extend','goal_thumb_cmc_abd','position_thumb_cmc_abd'} <= names


def test_pending_thumb_setup_cannot_be_discarded_by_a_pose():
    from aerohand_control.app import CommandInbox
    inbox=CommandInbox();inbox.put('thumb_angle 32')
    with pytest.raises(ValueError,match='Thumb setup'):inbox.put('grab')
    assert inbox.get_nowait()=='thumb_angle 32'
    inbox.put('thumb_angle 33');inbox.put('hold');assert inbox.get_nowait()=='hold'
    inbox.put('thumb_angle 34');inbox.put('quit');assert inbox.get_nowait()=='quit'


def test_capture_current_uses_one_fresh_read_and_no_second_read(pose_module):
    h,hw=real_adapter(pose_module);hw.position[0]=63.7
    reads=h.position_reads
    values=h.lock_measured_thumb()
    assert h.position_reads==reads+1
    assert h.thumb_abduction_lock==pytest.approx(36.3)
    assert h.target_positions==tuple(values)
    assert hw.writes[-1][0]==pytest.approx(63.7)


def test_capture_current_failure_is_transactional(pose_module):
    h,hw=real_adapter(pose_module);hw.position[0]=63.7;hw.fail=True
    previous=h.target_positions
    with pytest.raises(pose_module[1].HandCommunicationError):h.lock_measured_thumb()
    assert h.thumb_abduction_lock==35 and h.target_positions==previous
