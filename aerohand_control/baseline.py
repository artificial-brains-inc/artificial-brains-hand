"""Unloaded ADC zeroing only. No object/grip target and no motor decision."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import statistics
from .constants import FINGERS

SCHEMA = 'aerohand-tactile-baseline/v1'

def load_baseline(path: Path, directions) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != SCHEMA or data.get('finger_order') != list(FINGERS):
        raise ValueError('Unsupported tactile baseline format or finger order')
    if data.get('directions') != list(directions):
        data['source']=str(data.get('source',''))+'; reusing raw idle offset with current config polarity'
        data['directions']=list(directions)
    for key in ('baseline', 'noise_deadband'):
        values = data.get(key)
        if not isinstance(values, list) or len(values) != 5 or any(
            type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 4095 for v in values
        ):
            raise ValueError(f'Invalid tactile baseline field: {key}')
    for b, d, sign in zip(data['baseline'], data['noise_deadband'], directions):
        if (4095-b if sign == 1 else b) <= d:
            raise ValueError('A sensor has no usable ADC headroom after zeroing; inspect wiring/polarity')
    return data


def save_baseline(path: Path, data: dict) -> None:
    """Atomic replace, never append or overwrite a per-object calibration file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as file:
        json.dump(data, file, indent=2, allow_nan=False)
        file.write('\n'); file.flush(); os.fsync(file.fileno())
    os.replace(temporary, path)


class BaselineCapture:
    """Collect distinct recent unloaded samples. The operator supplies unloaded context."""
    def __init__(self, now: float, *, count: int = 50, minimum_seconds: float = 0.75,
                 timeout_seconds: float = 8.0):
        self.started = now; self.count = count; self.minimum_seconds = minimum_seconds
        self.timeout_seconds = timeout_seconds; self.samples = []; self.last_id = None

    def observe(self, sample, now: float) -> bool:
        if now - self.started > self.timeout_seconds:
            raise TimeoutError('Not enough fresh tactile readings; previous zero is unchanged')
        if sample is not None and sample.received_at >= self.started:
            identity = (sample.sequence, sample.device_ms, sample.received_at)
            if identity != self.last_id:
                self.samples.append(sample.adc); self.last_id = identity
        return len(self.samples) >= self.count and now - self.started >= self.minimum_seconds

    def result(self, directions) -> dict:
        if len(self.samples) < self.count:
            raise ValueError('Capture is not complete')
        baseline = [float(statistics.median(row[i] for row in self.samples)) for i in range(5)]
        # Reject only electronic idle variation. This is not a grip-pressure threshold.
        noise = [max(2, math.ceil(3 * statistics.median(abs(row[i]-baseline[i])
                                                     for row in self.samples))) for i in range(5)]
        for b, d, sign in zip(baseline, noise, directions):
            if (4095-b if sign == 1 else b) <= d:
                raise ValueError('No remaining ADC headroom. Previous baseline kept; inspect sensor polarity/wiring.')
        return {'schema': SCHEMA, 'source': 'Operator-requested unloaded median; never adapted during a grasp',
                'finger_order': list(FINGERS), 'baseline': baseline, 'noise_deadband': noise,
                'directions': list(directions), 'samples': len(self.samples)}


def load_or_initial_baseline(path: Path, directions, *, require_saved: bool = False) -> dict:
    """Load a local zero or provide an explicitly uncalibrated acquisition scale.

    Normal startup replaces this with fresh unloaded measurements before the
    neural session starts. Observation mode may use the raw scale without writes.
    """
    path = Path(path)
    if path.exists():
        return load_baseline(path, directions)
    if require_saved:
        raise ValueError("A saved unloaded sensor baseline is required when startup zeroing is disabled")
    if len(directions) != 5 or any(type(v) is not int or v not in (-1, 1) for v in directions):
        raise ValueError("Five tactile directions of 1 or -1 are required")
    return {"schema": SCHEMA, "source": "Uncalibrated ADC scale; no unloaded capture yet",
            "finger_order": list(FINGERS),
            "baseline": [0.0 if direction == 1 else 4095.0 for direction in directions],
            "noise_deadband": [0.0] * 5, "directions": list(directions), "samples": 0}
