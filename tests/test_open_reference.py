# Upstream geometry equations/constants: Copyright (c) 2025 Chestnut Robotics, Inc.
# Those identified portions remain Apache-2.0; see THIRD_PARTY_NOTICES.md.
# Synthetic scenarios and integration tests are original example contributions.
"""Open-reference behavior checks. No physical hand or installed vendor SDK.

The geometric fixtures implement the linear equations published in the vendor's
joints_to_actuations.py / actuations_to_joints.py. They are reference equations,
not an emulation of servo motion or evidence of physical posture convergence.
"""
import importlib.util
import json
import math
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pose_module(monkeypatch):
    package = types.ModuleType('aero_open_sdk'); package.__path__ = []
    monkeypatch.setitem(sys.modules, 'aero_open_sdk', package)
    constants = types.ModuleType('aero_open_sdk.aero_hand_constants')
    class Limits:
        joint_lower_limits = (0.,)*16
        joint_upper_limits = (100.,55.,90.,90.) + (90.,)*12
        actuation_lower_limits = (0.,0.,-15.2789,0.,0.,0.,0.)
        actuation_upper_limits = (100.,104.125,247.15,288.1603,288.1603,288.1603,288.1603)
    constants.AeroHandConstants = Limits
    forward = types.ModuleType('aero_open_sdk.joints_to_actuations')
    class Forward:
        def hand_actuations(self, q):
            a0 = q[0]
            a1 = (2.5*q[0] + 12.4931*q[1])/9.
            a2 = (2.5*q[0] - 2.5*q[1] + 9.4372*q[2] + 12.5*q[3])/9.
            fingers = [(12.4912*q[i] + 7.3211*q[i+1] + 9*q[i+2])/9.
                       for i in (4,7,10,13)]
            return [a0,a1,a2,*fingers]
    forward.JointsToActuationsModel = Forward
    inverse = types.ModuleType('aero_open_sdk.actuations_to_joints')
    class Inverse:
        def hand_joints(self, a):
            q0=a[0];q1=(9*a[1]-2.5*q0)/12.4931
            q2=(9*a[2]-2.5*q0+2.5*q1)/(9.4372+12.5)
            return [q0,q1,q2,*[9*v/(12.4912+7.3211+9) for v in a[3:]]]
    inverse.ActuationsToJointsModelCompact = Inverse
    for mod in (constants, forward, inverse):
        monkeypatch.setitem(sys.modules,mod.__name__,mod)
    # Import the actual adapter definitions with the above import-only geometry
    # fixtures; no serial connection or driver instance is created.
    for module_name,filename in [('aerohand_control.hand','hand.py'),('aerohand_control.poses','poses.py')]:
        spec=importlib.util.spec_from_file_location(module_name,ROOT/'aerohand_control'/filename)
        module=importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules,module_name,module)
        spec.loader.exec_module(module)
    return sys.modules['aerohand_control.poses'],sys.modules['aerohand_control.hand']


def test_open_reference_uses_extended_compact_coordinates(pose_module):
    poses,_=pose_module
    assert poses.get_pose('open')['joint_degrees']==[0.]*7
    assert poses.get_pose('open')['origin']=='coupling-consistent-open'
    assert poses.pose_to_actuations('open')==pytest.approx([0.]*7)
    assert poses.pose_to_actuations('open',motor_directions=(-1,1,1,1,1,1,1))==pytest.approx([0.,250/9,250/9,0,0,0,0])


@pytest.mark.parametrize('name',['open','grab','pinch','pinch_middle','pinch_ring','pinch_pinky','peace','point','middle_finger'])
def test_amount_zero_is_shared_configured_open(name,pose_module):
    poses,_=pose_module
    assert poses.pose_to_actuations(name,0)==pytest.approx(poses.pose_to_actuations('open'))


@pytest.mark.parametrize('amount',[0,.01,.25,.5,.75,1])
def test_open_keeps_its_reference_at_all_slider_amounts(amount,pose_module):
    poses,_=pose_module
    assert poses.pose_to_actuations('open',amount)==pytest.approx(poses.pose_to_actuations('open'))


@pytest.mark.parametrize('name',['grab','pinch','pinch_middle','pinch_ring','pinch_pinky','peace','point','middle_finger'])
def test_full_amount_non_open_pose_matches_declared_target(name,pose_module):
    poses,_=pose_module
    q=poses.get_pose(name)['joint_degrees']
    assert poses.pose_to_actuations(name,1)==pytest.approx(poses.compact_to_actuations(q))


def test_half_amount_interpolates_joints_before_thumb_coupling(pose_module):
    poses,_=pose_module
    start=poses.get_pose('open')['joint_degrees'];end=poses.get_pose('grab')['joint_degrees']
    assert poses.pose_to_actuations('grab',.5)==pytest.approx(poses.compact_to_actuations([(a+b)/2 for a,b in zip(start,end)]))


def test_shared_reference_maps_once_to_hardware(pose_module):
    poses,hand_module=pose_module
    adapter=hand_module.AeroHandAdapter.__new__(hand_module.AeroHandAdapter)
    constants=sys.modules['aero_open_sdk.aero_hand_constants'].AeroHandConstants()
    adapter.limits=list(zip(constants.actuation_lower_limits,constants.actuation_upper_limits))
    adapter._motor_directions=(-1,1,1,1,1,1,1)
    logical=poses.pose_to_actuations('open',motor_directions=adapter.motor_directions)
    hardware=adapter.hardware_coordinates(logical)
    assert hardware==pytest.approx([100.,250/9,250/9,0,0,0,0])
    assert adapter.hardware_coordinates(hardware)==pytest.approx(logical)


def test_arrival_remains_configured_per_motor_tolerance():
    config=json.loads((ROOT/'config.json').read_text())
    assert config['startup_open_tolerance_degrees']==2
    assert config['startup_open_timeout_s']==30


class Clock:
    def __init__(self):self.now=0.
    def __call__(self):return self.now
    def sleep(self,seconds):self.now+=seconds


class FixedFeedback:
    """Fixed synthetic encoder observations, not a simulated actuator."""
    def __init__(self):
        self.sent=[]
        self.values=[12.4,0.,0.1,0.5,0.3,0.3,0.1]
    def command_startup_pose(self,goal):self.sent.append(list(goal))
    def read_positions(self):return self.values[:]


def test_unreached_startup_target_is_not_confirmed(pose_module):
    from aerohand_control.startup import open_before_brain
    clock=Clock();hand=FixedFeedback()
    with pytest.raises(TimeoutError,match=r'thumb_cmc_abd: 12.4 deg'):
        open_before_brain(hand,[0.]*7,timeout_s=.4,clock=clock,sleep=clock.sleep)


def test_companion_targets_are_also_checked(pose_module):
    from aerohand_control.startup import open_before_brain
    poses,_=pose_module;clock=Clock();hand=FixedFeedback()
    # All three coupled coordinates must match the intended starting posture.
    with pytest.raises(TimeoutError,match='thumb_cmc_flex'):
        open_before_brain(hand,poses.pose_to_actuations('open',motor_directions=(-1,1,1,1,1,1,1)),timeout_s=.4,clock=clock,sleep=clock.sleep)


def test_new_target_confirmation_and_hardware_diagnostics(pose_module):
    from aerohand_control.startup import open_before_brain
    poses,_=pose_module;goal=poses.pose_to_actuations('open',motor_directions=(-1,1,1,1,1,1,1));clock=Clock();hand=FixedFeedback()
    hand.values=goal[:]  # explicitly synthetic feedback for controller test
    hand.hardware_coordinates=lambda p:[100-p[0],*p[1:]]
    events=[]
    result=open_before_brain(hand,goal,clock=clock,sleep=clock.sleep,progress=events.append)
    assert result==goal and hand.sent==[goal]
    assert events[-1]['confirmed']
    assert events[-1]['hardware_target']==pytest.approx([100.,250/9,250/9,0,0,0,0])
    assert clock.now>=.15


def test_custom_open_reference_drives_amount_zero_too(tmp_path,pose_module):
    poses,_=pose_module
    data=json.loads((ROOT/'poses.json').read_text());data['poses']['open']['joint_degrees'][0]=15
    path=tmp_path/'poses.json';path.write_text(json.dumps(data))
    assert poses.pose_to_actuations('grab',0,path)[0]==15
    assert poses.pose_to_actuations('open',1,path)[0]==15


def test_no_direct_motion_added_to_regular_pose_selection():
    # Changes remain in coordinate authoring, not a host substitute for motor
    # decisions. Normal pose requests still enter the neural goal interface.
    source=(ROOT/'aerohand_control/app.py').read_text()
    handler=source.split('def handle_command',1)[1].split('def _reader',1)[0]
    assert 'controller.set_goal(command, pose_to_actuations' in handler
    assert 'command_startup_pose' not in handler and '.set_actuations' not in handler


def test_packed_logs_include_pose_reference_not_credentials(tmp_path):
    spec=importlib.util.spec_from_file_location('test_pack_module',ROOT/'tools/pack_logs.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    run=tmp_path/'run-fixture';run.mkdir()
    (run/'run.json').write_text('{}')
    (run/'poses-at-start.json').write_text((ROOT/'poses.json').read_text())
    (run/'.env').write_text('AB_DEVICE_API_KEY=not-a-real-key')
    import zipfile
    with zipfile.ZipFile(module.pack(run)) as z:
        names=z.namelist()
        assert 'run-fixture/poses-at-start.json' in names
        assert all(not name.endswith('.env') for name in names)


def test_export_matches_neural_generator():
    from aerohand_control.design import design_dict
    assert json.loads((ROOT/'brain.abdesign.json').read_text()) == design_dict()


# Checks exercise the actual forward/inverse mapping with independent
# published linear geometry fixtures. These are NOT physical servo simulations.
DIRECTIONS = (-1, 1, 1, 1, 1, 1, 1)


def make_geometry_adapter(hand_module, directions=DIRECTIONS):
    c=sys.modules['aero_open_sdk.aero_hand_constants'].AeroHandConstants()
    adapter=hand_module.AeroHandAdapter.__new__(hand_module.AeroHandAdapter)
    adapter.limits=list(zip(c.actuation_lower_limits,c.actuation_upper_limits))
    adapter._motor_directions=tuple(directions)
    adapter._joint_limits=[(c.joint_lower_limits[i],c.joint_upper_limits[i]) for i in (0,1,2,4,7,10,13)]
    adapter._inverse=sys.modules['aero_open_sdk.actuations_to_joints'].ActuationsToJointsModelCompact()
    return adapter


@pytest.mark.parametrize('name',['open','grab','pinch','pinch_middle','pinch_ring','pinch_pinky','peace','point','middle_finger'])
@pytest.mark.parametrize('directions',[(1,)*7,DIRECTIONS])
@pytest.mark.parametrize('amount',[0.,.25,.5,1.])
def test_entire_pose_path_round_trips_in_actual_hardware_frame(name,directions,amount,pose_module):
    poses,hm=pose_module;adapter=make_geometry_adapter(hm,directions)
    a=poses.pose_to_actuations(name,amount,motor_directions=directions)
    q=adapter.compact_joint_positions(a)
    start=poses.get_pose('open')['joint_degrees'];end=poses.get_pose(name)['joint_degrees']
    expected=[x+amount*(y-x) for x,y in zip(start,end)]
    assert q==pytest.approx(expected,abs=1e-8)
    hardware=adapter.hardware_coordinates(a)
    native_q=[math.degrees(x) for x in adapter._inverse.hand_joints([math.radians(x) for x in hardware])]
    assert native_q[1:]==pytest.approx(expected[1:],abs=1e-8)
    assert native_q[0]==pytest.approx(100-expected[0] if directions[0]<0 else expected[0])


@pytest.mark.parametrize('old_goal',[[0.,0.,0.,0.,0.,0.,0.],[12.5,250/72,250/72,0.,0.,0.,0.]])
def test_old_reflect_after_coupling_open_goals_are_rejected(old_goal,pose_module):
    poses,hm=pose_module
    with pytest.raises(hm.HandInputError,match='coupled-joint'):
        poses.validate_actuation_target(old_goal,motor_directions=DIRECTIONS)
    q=make_geometry_adapter(hm).compact_joint_positions(old_goal)
    assert q[1]<-10  # negative flexion hidden by the old inverse-frame check


def test_corrected_open_model_is_extended_not_falsely_flexed(pose_module):
    poses,hm=pose_module;adapter=make_geometry_adapter(hm)
    a=poses.pose_to_actuations('open',motor_directions=DIRECTIONS)
    assert adapter.compact_joint_positions(a)==pytest.approx([0.]*7,abs=1e-9)
    assert adapter._joint_step_valid(a,a)
    # The two tendon motor coordinates are nonzero but their JOINT flexion is 0.
    assert a[1]==pytest.approx(250/9) and a[2]==pytest.approx(250/9)


def test_startup_sends_the_validated_coupled_hardware_vector_once(pose_module):
    poses,hm=pose_module;adapter=make_geometry_adapter(hm);sent=[]
    adapter._write_hardware_positions=lambda p:sent.append(list(p))
    goal=poses.pose_to_actuations('open',motor_directions=DIRECTIONS)
    adapter.command_startup_pose(goal)
    assert sent==[pytest.approx([100.,250/9,250/9,0,0,0,0])]
    assert adapter.target_positions==tuple(goal)


def test_native_feedback_is_reported_as_logical_joints_and_hold_preserves_raw(pose_module):
    import threading
    poses,hm=pose_module;adapter=make_geometry_adapter(hm)
    raw=[100.,250/9,250/9,0,0,0,0]
    adapter._hand=types.SimpleNamespace(get_actuations=lambda:raw[:])
    adapter._io_lock=threading.RLock();adapter._position_protocol=None
    adapter._target=None;adapter.position_reads=0;adapter.total_read_duration_s=0.
    sent=[];adapter._write_hardware_positions=lambda p:sent.append(list(p))
    a=adapter.read_positions()
    assert a==pytest.approx([0.,250/9,250/9,0,0,0,0])
    assert adapter.compact_joint_positions(a)==pytest.approx([0.]*7,abs=1e-8)
    adapter.hold()
    assert sent==[raw]


def test_neural_motor_outputs_do_not_generate_unrequested_companion_motion(pose_module):
    poses,hm=pose_module;adapter=make_geometry_adapter(hm)
    start=poses.compact_to_actuations([50.,25.,30.,30.,30.,30.,30.],motor_directions=DIRECTIONS)
    adapter._target=start[:];adapter._last_feedback=start[:]
    adapter.recent_positions=lambda:start[:];adapter.max_target_lead_degrees=8.
    sent=[];adapter._write_hardware_positions=lambda p:sent.append(list(p))
    result=adapter.apply_deltas([.5,0.,0.,0.,0.,0.,0.])
    assert result.applied_deltas==pytest.approx([.5,0,0,0,0,0,0])
    before=adapter.hardware_coordinates(start)
    assert [x-y for x,y in zip(sent[0],before)]==pytest.approx([-.5,0,0,0,0,0,0])


def test_normal_pose_selection_propagates_the_configured_orientation(pose_module):
    from aerohand_control.app import handle_command
    poses,_=pose_module;received=[]
    c=types.SimpleNamespace(hand=types.SimpleNamespace(thumb_abduction_lock=None),cfg={'motor_directions':list(DIRECTIONS)},
                            set_goal=lambda *a,**kw:received.append((a,kw)))
    handle_command('open',c)
    assert received[0][0][1]==pytest.approx([0.,250/9,250/9,0,0,0,0])


def test_stationary_feedback_does_not_redefine_open_pose(pose_module):
    poses,hm=pose_module
    from aerohand_control.startup import open_before_brain
    h=FixedFeedback();h.values=[20.,.5,4.,.5,.5,.5,0.]
    goal=poses.pose_to_actuations('open',motor_directions=DIRECTIONS)
    clock=Clock()
    with pytest.raises(TimeoutError):
        open_before_brain(h,goal,timeout_s=.4,clock=clock,sleep=clock.sleep)
    assert goal[0]==0.  # The authored pose is not silently replaced by a stationary measurement.
