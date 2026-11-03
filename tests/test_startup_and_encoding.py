"""Pre-brain opening and low-pressure fidelity. All hardware here is synthetic."""
import importlib.util
import json
import math
from pathlib import Path
import sys
import types
import pytest
from aerohand_control.startup import open_before_brain, zero_before_brain, StartupCancelled
from aerohand_control.neural import tactile_vector, make_readings
from aerohand_control.tactile import TactileEncoding, TactileSample
from aerohand_control.constants import FINGERS, input_contract
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def hand_module(monkeypatch):
    package=types.ModuleType('aero_open_sdk'); package.__path__=[]
    model=types.ModuleType('aero_open_sdk.actuations_to_joints')
    model.ActuationsToJointsModelCompact=type('UnusedVendorModel',(),{})
    constants=types.ModuleType('aero_open_sdk.aero_hand_constants')
    constants.AeroHandConstants=type('UnusedVendorConstants',(),{})
    monkeypatch.setitem(sys.modules,'aero_open_sdk',package)
    monkeypatch.setitem(sys.modules,model.__name__,model)
    monkeypatch.setitem(sys.modules,constants.__name__,constants)
    spec=importlib.util.spec_from_file_location('aerohand_control.hand',ROOT/'aerohand_control/hand.py')
    mod=importlib.util.module_from_spec(spec);monkeypatch.setitem(sys.modules,spec.name,mod);spec.loader.exec_module(mod)
    return mod

class Clock:
    def __init__(self):self.now=0.
    def __call__(self):return self.now
    def sleep(self,duration):self.now+=duration

class Hand:
    def __init__(self,positions,read_sequence=()):
        self.position=list(positions);self.sequence=list(read_sequence);self.sent=[]
    def command_startup_pose(self,values):self.sent.append(list(values))
    def read_positions(self):
        if self.sequence:
            item=self.sequence.pop(0)
            if isinstance(item,Exception):raise item
            self.position=list(item)
        return self.position[:]


def test_open_is_measured_not_just_sent(hand_module):
    clock=Clock();h=Hand([30]*7,[[20]*7,[3]*7,[1]*7,[1]*7,[.3]*7,[.3]*7,[.3]*7]);events=[]
    pos=open_before_brain(h,[0]*7,clock=clock,sleep=clock.sleep,progress=events.append)
    assert h.sent==[[0.]*7] and max(pos)<2 and clock.now>=.25
    assert events[-1]['confirmed'] and events[-1]['state']=='open'
    assert not events[0]['confirmed']


def test_one_axis_not_open_blocks_confirmation(hand_module):
    clock=Clock();h=Hand([0,0,0,0,8,0,0])
    with pytest.raises(TimeoutError,match='middle_tendon'):
        open_before_brain(h,[0]*7,timeout_s=.4,clock=clock,sleep=clock.sleep)
    assert h.sent==[[0.]*7]


def test_read_timeout_recovers_during_open(hand_module):
    clock=Clock();h=Hand([5]*7,[hand_module.HandReadTimeout('missed'),[0]*7]);events=[]
    open_before_brain(h,[0]*7,clock=clock,sleep=clock.sleep,progress=events.append)
    assert events[-1]['confirmed'] and events[-1]['feedback_timeouts']==1


def test_hold_cancels_before_startup_write(hand_module):
    h=Hand([5]*7)
    with pytest.raises(StartupCancelled):open_before_brain(h,[0]*7,cancelled=lambda:True)
    assert not h.sent


def test_stop_cancels_while_opening(hand_module):
    c=Clock();h=Hand([5]*7)
    with pytest.raises(StartupCancelled):
        open_before_brain(h,[0]*7,cancelled=lambda:c.now>=.1,clock=c,sleep=c.sleep)
    assert len(h.sent)==1


def test_already_open_still_requires_confirmed_settle(hand_module):
    c=Clock();h=Hand([0]*7)
    open_before_brain(h,[0]*7,clock=c,sleep=c.sleep)
    assert len(h.sent)==1 and c.now>=.15


@pytest.mark.parametrize('kwargs',[{'timeout_s':0},{'timeout_s':float('nan')},{'poll_s':0}, {'settle_s':-1},{'tolerance_degrees':0}])
def test_invalid_startup_options_do_not_write(hand_module,kwargs):
    h=Hand([0]*7)
    with pytest.raises(ValueError):open_before_brain(h,[0]*7,**kwargs)
    assert not h.sent


def test_actuator_serial_boundary_keeps_thumb_polarity_with_validation_mocked(hand_module,monkeypatch):
    m=hand_module;h=m.AeroHandAdapter.__new__(m.AeroHandAdapter)
    h.limits=[(0.,100.)]*7;h._motor_directions=(-1,1,1,1,1,1,1);writes=[]
    h._write_hardware_positions=lambda p:writes.append(p)
    poses=types.ModuleType('aerohand_control.poses');poses.validate_actuation_target=lambda p,**kw:list(p)
    monkeypatch.setitem(sys.modules,poses.__name__,poses)
    h.command_startup_pose([0.]*7)
    assert writes==[[100.,0.,0.,0.,0.,0.,0.]] and h.target_positions==(0.,)*7


def test_failed_startup_write_does_not_commit(hand_module,monkeypatch):
    m=hand_module;h=m.AeroHandAdapter.__new__(m.AeroHandAdapter)
    h.limits=[(0.,100.)]*7;h._target=[50.]*7;h._motor_directions=(1,)*7
    poses=types.ModuleType('aerohand_control.poses');poses.validate_actuation_target=lambda p,**kw:list(p)
    monkeypatch.setitem(sys.modules,poses.__name__,poses)
    def fail(p):raise m.HandCommunicationError('fixture')
    h._send_positions=fail
    with pytest.raises(m.HandCommunicationError):h.command_startup_pose([0.]*7)
    assert h.target_positions==(50.,)*7


class Reader:
    def __init__(self,clock):self.clock=clock;self.n=0
    def latest(self,max_age):
        self.n+=1
        value=(self.n%3)-1
        return TactileSample(self.n*10,tuple(x+value for x in [50,21,41,2645,24]),self.clock(),self.n)


def test_startup_zero_captures_actual_open_baseline(tmp_path):
    c=Clock();reader=Reader(c);enc=TactileEncoding();path=tmp_path/'zero.json';progress=[]
    out=zero_before_brain(reader,enc,path,clock=c,sleep=c.sleep,progress=progress.append)
    assert out['samples']>=50 and enc.baseline[3]==pytest.approx(2645,abs=1)
    assert out['noise_deadband']==[3]*5 and c.now>=.75
    assert json.loads(path.read_text())['baseline']==list(enc.baseline)
    assert enc.encode(reader.latest(.25))==(0.,)*5
    assert progress[-1]['state']=='zeroed'


def test_startup_zero_can_cancel_without_overwriting(tmp_path):
    path=tmp_path/'zero.json';path.write_text('previous');c=Clock()
    with pytest.raises(StartupCancelled):
        zero_before_brain(Reader(c),TactileEncoding(),path,cancelled=lambda:True)
    assert path.read_text()=='previous'


@pytest.mark.parametrize('v',[0,1/4064,10/4064,48/4064,.1,.25,.5,.75,1])
def test_fractional_tactile_preserves_amount(v):
    vec=tactile_vector(v)
    assert len(vec)==64 and sum(vec)==pytest.approx(64*v)
    assert all(0<=x<=1 for x in vec)
    assert all(a>=b for a,b in zip(vec,vec[1:]))
    assert sum(0<x<1 for x in vec)<=1


def test_one_adc_above_zero_is_not_full_neuron():
    vec=tactile_vector(1/4064)
    assert 0<vec[0]<.016 and vec[1:]==[0.]*63


def test_logged_middle_noise_no_longer_amplifies_to_full_first_bin():
    for raw in range(6,80):
        normalized=max(0,(raw-31)/4064)
        payload=make_readings([80.]*7,[20.]*7,[(0.,100.)]*7,[0,0,normalized,0,0])
        signal=payload['touch_middle']['signal']
        assert sum(signal)==pytest.approx(normalized*64)
        assert max(signal)<1
        assert set(payload)==set(input_contract())


@pytest.mark.parametrize('value',[True,-.1,1.1,float('nan'),float('inf'),'1'])
def test_tactile_vector_validation(value):
    with pytest.raises(ValueError):tactile_vector(value)


def test_assets_are_company_branding():
    h=(ROOT/'aerohand_control/web/index.html').read_text()
    assert 'Artificial Brains' in h and 'class="brand-mark"' not in h
    assert 'assets/artificial-brains-logo.png' in h
    css=(ROOT/'aerohand_control/web/brand.css').read_text()
    assert 'color-scheme:dark' in css and '#080b10' in css and '#c3b6f4' in css
