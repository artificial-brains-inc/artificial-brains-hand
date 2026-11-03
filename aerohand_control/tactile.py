"""Read the five-channel CSV from the sensor acquisition firmware; no motor policy here.

Wire format: time_ms,thumb,index,middle,ring,little, 115200 baud.
Each channel is the firmware's eight-sample average of a 12-bit ADC.
The signal is not a calibrated force measurement. An unloaded baseline corrects sensor offsets; remaining ADC headroom defines
the observation scale. There is no per-object enough-contact capture.
Python never turns a pressure threshold into a motor permission or veto.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import threading
import time
from typing import Callable

from .constants import FINGERS


@dataclass(frozen=True)
class TactileSample:
    device_ms: int
    adc: tuple[int, ...]
    received_at: float
    sequence: int


class CSVParser:
    """Incremental bounded parser; partial USB reads cannot become a sample."""
    def __init__(self):
        self.buffer = bytearray()
        self.discard_line = False
        self.invalid_lines = 0
        self.sequence = 0

    def feed(self, chunk: bytes, now: float) -> list[TactileSample]:
        frames = []
        for value in chunk:
            if value != 10:
                if not self.discard_line:
                    self.buffer.append(value)
                    if len(self.buffer) > 128:
                        self.buffer.clear()
                        self.discard_line = True
                continue
            line = bytes(self.buffer).strip()
            self.buffer.clear()
            if self.discard_line:
                self.discard_line = False
                self.invalid_lines += 1
                continue
            if not line or line == b"time_ms,thumb,index,middle,ring,little":
                continue
            parts = line.split(b",")
            try:
                if len(parts) != 6 or any(not p.isdigit() for p in parts):
                    raise ValueError
                numbers = tuple(int(p) for p in parts)
                if not 0 <= numbers[0] <= 0xFFFFFFFF or any(not 0 <= v <= 4095 for v in numbers[1:]):
                    raise ValueError
            except ValueError:
                self.invalid_lines += 1
                continue
            self.sequence += 1
            frames.append(TactileSample(numbers[0], numbers[1:], now, self.sequence))
        return frames


def resolve_tactile_port(port: str, *, exclude: str | None = None) -> str:
    """Metadata-only discovery; never probe unrelated serial devices."""
    if port != "auto":
        if exclude and os.path.realpath(port) == os.path.realpath(exclude):
            raise ValueError("The tactile board and AeroHand must use different serial ports.")
        return port
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    # STMicroelectronics and common Maple/STM32duino USB CDC identifiers.
    candidates = [p.device for p in ports if p.vid in (0x0483, 0x1EAF)
                  and (not exclude or os.path.realpath(p.device) != os.path.realpath(exclude))]
    if len(candidates) == 1:
        return candidates[0]
    listing = "; ".join(f"{p.device}: {p.description} [{p.serial_number or 'no serial'}]" for p in ports if p.vid)
    raise ValueError("Cannot uniquely select the STM32 tactile board. Pass --tactile-port with its actual port. " + listing)


class TactileReader:
    """Continuously drain CSV; retain only the newest complete reading.

    A read gap never sets a permanent fault. Missing data are never replaced by
    zero pressure. Re-opening after an actual device disconnect is attempted on
    the same selected path; use /dev/serial/by-id for re-enumeration stability.
    """
    def __init__(self, port: str, *, clock: Callable[[], float] = time.monotonic, serial_factory=None, on_sample=None):
        self.port, self.clock = port, clock
        self.serial_factory = serial_factory
        self.on_sample = on_sample or (lambda sample: None)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: TactileSample | None = None
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self.invalid_lines = 0

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Tactile reader already started")
        self._thread = threading.Thread(target=self._run, name="tactile-csv", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        factory = self.serial_factory
        if factory is None:
            import serial
            factory = serial.Serial
        while not self._stop.is_set():
            port = None
            parser = CSVParser()
            try:
                # STM32's SerialUSB is a separate device from the AeroHand ESP32.
                port = factory(self.port, 115200, timeout=0.05, write_timeout=0.2)
                port.dtr = True  # matches monitor_dtr=1 in the supplied platformio.ini
                while not self._stop.is_set():
                    chunk = port.read(max(1, min(getattr(port, "in_waiting", 0), 4096)))
                    if not chunk:
                        continue
                    frames = parser.feed(chunk, self.clock())
                    for sample in frames:
                        self.on_sample(sample)
                    with self._lock:
                        self.invalid_lines = parser.invalid_lines
                        if frames:
                            self._latest = frames[-1]
                            self.error = None
            except Exception as exc:
                with self._lock:
                    self.error = f"{type(exc).__name__}: {exc}"
                    # On disconnect do not re-use the last sample as live data.
                    self._latest = None
            finally:
                if port is not None:
                    try:
                        port.close()
                    except Exception:
                        pass
            self._stop.wait(1.0)

    def latest(self, max_age_s: float | None = None) -> TactileSample | None:
        with self._lock:
            sample = self._latest
        if sample is not None and max_age_s is not None and self.clock() - sample.received_at > max_age_s:
            return None
        return sample

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


class TactileEncoding:
    """Offset/headroom sensor encoding, not a grip decision.

    Without an unloaded baseline, the full ADC endpoint range defines the scale.
    Live startup captures a baseline; observation mode labels this scale uncalibrated.
    """
    def __init__(self, directions=(1, 1, 1, 1, 1), *, baseline=None,
                 noise_deadband=None, source="ADC endpoints (no saved zero)"):
        if (not isinstance(directions, (list, tuple)) or len(directions) != 5
                or any(type(v) is not int or v not in (-1, 1) for v in directions)):
            raise ValueError("tactile_directions needs five integers, each -1 or 1")
        self.directions = tuple(directions)
        baseline = [0 if s == 1 else 4095 for s in directions] if baseline is None else baseline
        self.update_baseline(baseline, [0]*5 if noise_deadband is None else noise_deadband, source=source)

    def update_baseline(self, baseline, noise_deadband, *, source="Unloaded capture"):
        import math
        for values in (baseline, noise_deadband):
            if not isinstance(values, (list, tuple)) or len(values) != 5 or any(
                type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 4095 for v in values
            ):
                raise ValueError("Baseline/deadband needs five finite ADC values")
        headroom = [4095-b if s == 1 else b for b,s in zip(baseline,self.directions)]
        if any(h <= d for h,d in zip(headroom,noise_deadband)):
            raise ValueError("Sensor has no usable ADC headroom after zeroing")
        self.baseline=tuple(float(v) for v in baseline)
        self.noise_deadband=tuple(float(v) for v in noise_deadband)
        self.headroom=tuple(headroom); self.source=str(source)

    def encode(self, sample: TactileSample) -> tuple[float, ...]:
        if (len(sample.adc) != 5 or any(type(v) is not int or not 0 <= v <= 4095
                                      for v in sample.adc)):
            raise ValueError("Tactile sample needs five 12-bit ADC readings")
        return tuple(min(1.0,max(0.0,(sign*(raw-zero)-noise)/(span-noise)))
                     for raw,sign,zero,noise,span in zip(sample.adc,self.directions,
                         self.baseline,self.noise_deadband,self.headroom))

    def status(self, sample: TactileSample | None) -> dict:
        return {
            "adc": dict(zip(FINGERS, sample.adc)) if sample else None,
            "normalized": dict(zip(FINGERS, self.encode(sample))) if sample else None,
            "baseline": dict(zip(FINGERS, self.baseline)),
            "noise_deadband": dict(zip(FINGERS, self.noise_deadband)),
            "headroom": dict(zip(FINGERS, self.headroom)),
            "directions": dict(zip(FINGERS, self.directions)),
            "baseline_source": self.source,
            "encoding": "unloaded offset and remaining ADC headroom; no per-object grip target",
        }
