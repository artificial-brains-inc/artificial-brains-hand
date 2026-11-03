"""Tests use artificial byte streams, not physical sensor/force validation."""
import json
import threading
import time
import pytest
from aerohand_control.tactile import CSVParser, TactileEncoding, TactileSample, TactileReader, resolve_tactile_port
from aerohand_control.constants import FINGERS


def sample(values=(100, 100, 100, 100, 100), now=1.):
    return TactileSample(10, tuple(values), now, 1)

@pytest.mark.parametrize('split', range(1, 24))
def test_csv_fragmented_at_every_byte(split):
    wire=b'10,100,200,300,400,500\r\n'
    p=CSVParser(); a=p.feed(wire[:split], 1.); b=p.feed(wire[split:], 2.)
    assert len(a+b)==1
    assert (a+b)[0].adc==(100,200,300,400,500)
    assert (a+b)[0].device_ms==10

@pytest.mark.parametrize('bad', [b'hello',b'0,1',b'0,1,2,3,4,5,6',b'0,1,2,3,4,-1',b'0,1,2,3,4,4096',
                               b'0,1,2,3,4,nan',b'0,1,2,3,4,1.0',b'4294967296,1,2,3,4,5',b'x'*140])
def test_bad_frames_skipped_not_zeroed(bad):
    p=CSVParser()
    frames=p.feed(bad+b'\n10,1,2,3,4,5\n',3.)
    assert p.invalid_lines==1
    assert len(frames)==1 and frames[0].adc==(1,2,3,4,5)


def test_header_timestamp_wrap_and_order():
    p=CSVParser()
    frames=p.feed(b'time_ms,thumb,index,middle,ring,little\n4294967295,1,2,3,4,5\n0,6,7,8,9,10\n', 8.)
    assert p.invalid_lines==0
    assert [f.sequence for f in frames]==[1,2]
    assert frames[-1].device_ms==0
    assert frames[-1].adc==(6,7,8,9,10)

@pytest.mark.parametrize('direction',[1,-1])
def test_raw_encoding_is_continuous_fixed_and_requires_no_capture(direction):
    enc=TactileEncoding([direction]*5)
    values=[enc.encode(sample([raw]*5))[0] for raw in (0,1024,2048,3072,4095)]
    expected=[raw/4095.0 if direction==1 else (4095-raw)/4095.0 for raw in (0,1024,2048,3072,4095)]
    assert values==pytest.approx(expected)
    assert all(0.0<v<1.0 for v in values[1:-1])
    assert not hasattr(enc,'capture_enough') and not hasattr(enc,'capture_zero')


def test_mixed_sensor_polarities_and_status():
    enc=TactileEncoding([1,-1,1,-1,1])
    s=sample([0,0,4095,4095,2048])
    assert enc.encode(s)==pytest.approx((0.,1.,1.,0.,2048/4095))
    status=enc.status(s)
    assert status['adc']['thumb']==0 and status['normalized']['index']==1.
    assert 'reference' not in status and 'calibration' not in status and 'ready' not in status
    assert enc.status(None)['normalized'] is None


@pytest.mark.parametrize('directions',[[1]*4,[1]*6,[True]*5,[0]*5,[1.0]*5,'11111',None])
def test_invalid_wiring_directions_rejected(directions):
    with pytest.raises(ValueError): TactileEncoding(directions)


@pytest.mark.parametrize('bad',[True,float('nan'),-1,4096,'100'])
def test_invalid_adc_rejected(bad):
    with pytest.raises(ValueError): TactileEncoding().encode(sample([bad]*5))


def test_unrelated_calibration_file_is_not_loaded(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path/'tactile_calibration.json').write_text('not even valid JSON')
    assert TactileEncoding().encode(sample([100]*5))==pytest.approx((100/4095,)*5)
    assert sorted(p.name for p in tmp_path.iterdir())==['tactile_calibration.json']


def test_port_separation():
    with pytest.raises(ValueError): resolve_tactile_port('/dev/ttyACM0',exclude='/dev/ttyACM0')
    assert resolve_tactile_port('/dev/ttyACM1',exclude='/dev/ttyACM0')=='/dev/ttyACM1'


def test_reader_keeps_latest_and_marks_stale_without_synthetic_zero():
    class Port:
        in_waiting=256
        def __init__(self,*a,**kw): self.once=True; self.dtr=False; self.closed=False
        def read(self,n):
            if self.once:
                self.once=False
                return b'1,10,20,30,40,50\n2,11,21,31,41,51\n'
            time.sleep(.002); return b''
        def close(self): self.closed=True
    ports=[]; now=[100.]
    def factory(*a,**kw):
        p=Port(); ports.append(p); return p
    r=TactileReader('fake',clock=lambda:now[0],serial_factory=factory); r.start()
    try:
        deadline=time.monotonic()+1
        while r.latest() is None and time.monotonic()<deadline: time.sleep(.005)
        assert r.latest(.25).adc==(11,21,31,41,51)
        assert ports[0].dtr is True
        now[0]+=1
        assert r.latest(.25) is None
        assert r.latest().adc==(11,21,31,41,51)
    finally: r.close()
    assert ports[0].closed
