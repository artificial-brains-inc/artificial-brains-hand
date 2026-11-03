"""Exercise the real application entry point with fake hardware/session boundaries."""
import json,sys,types
from pathlib import Path
import pytest
import aerohand_control.app as app
ROOT=Path(__file__).resolve().parents[1]


def wire(monkeypatch,tmp_path,*,fail_open=False):
    history=[]
    from aerohand_control.baseline import save_baseline
    fixture_baseline = {"schema": "aerohand-tactile-baseline/v1", "source": "Synthetic unloaded readings",
                        "finger_order": ["thumb","index","middle","ring","little"],
                        "baseline": [100.0]*5, "noise_deadband": [2.0]*5,
                        "directions": [1]*5, "samples": 50}
    baseline_path = tmp_path / "baseline.json"

    hm=types.ModuleType('aerohand_control.hand')
    class HandReadTimeout(Exception):pass
    class Hand:
        limits=[(0.,100.)]*7
        last_feedback_at=0
        def __init__(self,*a,**kw):history.append('hand_connect');self.position=[20.]*7
        def read_positions(self):history.append('read');return self.position[:]
        def hardware_coordinates(self,p):return list(p)
        def set_thumb_abduction_lock(self,v):self.thumb_abduction_lock=v;history.append(('thumb_lock',v));return v
        def configure_motion(self,**kw):history.append('settings')
        def hold(self):history.append('hold');return self.position[:]
        def close(self):history.append('close_hand')
    hm.AeroHandAdapter=Hand;hm.HandReadTimeout=HandReadTimeout
    monkeypatch.setitem(sys.modules,hm.__name__,hm)
    pm=types.ModuleType('aerohand_control.poses');pm.pose_to_actuations=lambda name,**kw:[0.]*7
    monkeypatch.setitem(sys.modules,pm.__name__,pm)
    nm=types.ModuleType('aerohand_control.neural')
    class Neural:
        neuron_recorder=None
        stop_confirmed=False
        def __init__(self,*a,**kw):history.append('construct_neural')
        def start(self):history.append('start_brain')
        def stop(self):self.stop_confirmed=True;history.append('stop_brain')
    nm.NeuralRuntime=Neural;monkeypatch.setitem(sys.modules,nm.__name__,nm)
    import aerohand_control.tactile as tactile
    class Reader:
        port='test_tactile';error=None
        def __init__(self,*a,**kw):pass
        def start(self):history.append('sensor_start')
        def close(self):history.append('sensor_close')
    monkeypatch.setattr(tactile,'TactileReader',Reader)
    monkeypatch.setattr(tactile,'resolve_tactile_port',lambda *a,**kw:'test_tactile')
    def opening(hand,goal,**kw):
        history.append('open_request')
        if fail_open:raise TimeoutError('synthetic never opened')
        history.append(('confirm_indices',kw.get('confirm_indices')));hand.position=[0.]*7;history.append('open_confirmed');return hand.position[:]
    monkeypatch.setattr(app,'open_before_brain',opening)
    def zero(reader,encoding,path,**kw):
        history.append('zero_captured')
        save_baseline(Path(path), fixture_baseline)
        return fixture_baseline.copy()
    monkeypatch.setattr(app,'zero_before_brain',zero)
    class Controller:
        def __init__(self,*a,**kw):
            history.append(('controller_initial_positions',kw['initial_positions']))
            raise KeyboardInterrupt
    monkeypatch.setattr(app,'HandController',Controller)
    args=['--port','test_hand','--cli','--command','grab','--log-dir',str(tmp_path/'logs'),
          '--baseline',str(baseline_path),'--thumb-setup',str(tmp_path/'thumb.json'),'--no-neuron-log']
    return history,args


def test_open_and_zero_complete_before_any_neural_session(monkeypatch,tmp_path):
    history,args=wire(monkeypatch,tmp_path)
    assert app.main(args)==0
    assert history.index('open_confirmed')<history.index('zero_captured')<history.index('construct_neural')<history.index('start_brain')
    assert ('controller_initial_positions',[0.]*7) in history


def test_open_failure_never_constructs_or_starts_brain(monkeypatch,tmp_path):
    history,args=wire(monkeypatch,tmp_path,fail_open=True)
    assert app.main(args)==1
    assert 'construct_neural' not in history and 'start_brain' not in history
    assert 'close_hand' in history


def test_observe_only_does_not_open_zero_or_hold_hand(monkeypatch,tmp_path):
    history,args=wire(monkeypatch,tmp_path)
    assert app.main(args+['--observe-only'])==0
    assert not any(x in history for x in ['open_request','settings','zero_captured','hold'])
    assert 'start_brain' in history


def test_read_only_has_no_movement_or_brain(monkeypatch,tmp_path):
    history,args=wire(monkeypatch,tmp_path)
    args=['--port','test_hand','--read-positions','--log-dir',str(tmp_path/'logs')]
    assert app.main(args)==0
    assert not any(x in history for x in ['open_request','settings','zero_captured','hold','construct_neural'])


def test_no_startup_zero_still_opens_first(monkeypatch,tmp_path):
    history,args=wire(monkeypatch,tmp_path)
    from aerohand_control.baseline import load_or_initial_baseline, save_baseline
    path = tmp_path / 'baseline.json'
    save_baseline(path, load_or_initial_baseline(path, [1]*5))
    assert app.main(args+['--no-startup-zero'])==0
    assert 'zero_captured' not in history
    assert history.index('open_confirmed')<history.index('start_brain')


def test_first_launch_pins_measured_thumb_and_excludes_it_from_arrival(monkeypatch,tmp_path):
    history,args=wire(monkeypatch,tmp_path)
    assert app.main(args)==0
    assert ('thumb_lock',20.0) in history
    assert ('confirm_indices',(1,2,3,4,5,6)) in history
    assert history.index(('thumb_lock',20.0))<history.index('open_request')


def test_saved_thumb_angle_is_used_before_opening(monkeypatch,tmp_path):
    from aerohand_control.thumb_setup import save_thumb_setup
    history,args=wire(monkeypatch,tmp_path)
    save_thumb_setup(tmp_path/'thumb.json',31.2,[-1,1,1,1,1,1,1])
    assert app.main(args)==0
    assert ('thumb_lock',31.2) in history
    assert history.index(('thumb_lock',31.2))<history.index('open_request')<history.index('construct_neural')
