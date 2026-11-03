"""Fresh-checkout configuration and per-motor bounded actuation tests."""
from pathlib import Path
import json
import pytest
from aerohand_control.baseline import load_or_initial_baseline, save_baseline
from aerohand_control.constants import VERSION, MOTOR_KEYS
from test_controller_and_adapter import hand_module, adapter
ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('directions', [[1]*5, [-1]*5, [1,-1,1,-1,1]])
def test_first_run_has_no_machine_specific_sensor_zero(tmp_path, directions):
    path=tmp_path/'zero.json'
    data=load_or_initial_baseline(path,directions)
    assert data['samples']==0 and not path.exists()
    assert data['baseline']==[0.0 if d==1 else 4095.0 for d in directions]
    assert data['noise_deadband']==[0.0]*5

def test_skip_zero_requires_an_explicit_saved_file(tmp_path):
    with pytest.raises(ValueError, match='saved'):
        load_or_initial_baseline(tmp_path/'missing.json',[1]*5,require_saved=True)

def test_saved_zero_is_loaded(tmp_path):
    p=tmp_path/'zero.json'
    data=load_or_initial_baseline(p,[1]*5)
    data.update(baseline=[100.0]*5, noise_deadband=[2.0]*5, samples=60)
    save_baseline(p,data)
    assert load_or_initial_baseline(p,[1]*5)==data

@pytest.mark.parametrize('motor', range(7), ids=MOTOR_KEYS)
@pytest.mark.parametrize('direction', [-1,1])
def test_existing_lead_is_nonfatal_for_each_motor(hand_module, motor, direction):
    h=adapter(hand_module)
    h.read_positions=lambda:[50.]*7
    h._target=[50.]*7
    h._target[motor]=50. + direction*12
    request=[0.]*7;request[motor]=direction
    other=(motor+1)%7;request[other]=1.
    result=h.apply_deltas(request)
    assert result.positions[motor]==50.+direction*12
    assert 'target_lead_existing' in result.limit_reasons[motor]
    assert result.positions[other]==51.

@pytest.mark.parametrize('motor', range(7), ids=MOTOR_KEYS)
@pytest.mark.parametrize('direction', [-1,1])
def test_neural_return_toward_feedback_remains_available(hand_module,motor,direction):
    h=adapter(hand_module);h.read_positions=lambda:[50.]*7;h._target=[50.]*7
    h._target[motor]=50.+direction*12
    request=[0.]*7;request[motor]=-direction
    result=h.apply_deltas(request)
    assert result.positions[motor]==50.+direction*11

@pytest.mark.parametrize('motor', range(7), ids=MOTOR_KEYS)
def test_grip_hold_cancels_ahead_target_without_session_stop(hand_module,motor):
    h=adapter(hand_module);h.read_positions=lambda:[50.]*7;h._target=[50.]*7
    h._target[motor]=62.
    hold=[False]*7;hold[motor]=True
    request=[0.]*7;request[motor]=1.
    result=h.apply_deltas(request,grip_holds=hold)
    assert result.positions[motor]==50.
    assert 'neural_grip_hold' in result.limit_reasons[motor]

def test_all_axes_overdue_together_do_not_raise(hand_module):
    h=adapter(hand_module);h.read_positions=lambda:[50.]*7;h._target=[62.]*7
    r=h.apply_deltas([1.]*7)
    assert r.positions==(62.,)*7 and not r.wrote
    r=h.apply_deltas([-1.]*7)
    assert r.positions==(61.,)*7 and r.wrote

def test_public_version_and_local_settings_are_ignored():
    assert VERSION=='0.0.0'
    ignored=(ROOT/'.gitignore').read_text().splitlines()
    for name in ('tactile_baseline.json','thumb_setup.json','.env','config.local.json'):
        assert name in ignored
    assert '!.env.example' in ignored
