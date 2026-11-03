"""Persist one operator-selected thumb-abduction setpoint, not a grasp policy."""
from __future__ import annotations
import json
import math
import os
import tempfile
from pathlib import Path

SCHEMA = "artificial-brains/thumb-setup/v1"

def valid_angle(value, limits=(0.0, 100.0)) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Thumb angle must be a finite number")
    lo, hi = limits
    if not lo <= value <= hi:
        raise ValueError(f"Thumb angle must be between {lo:g} and {hi:g} logical degrees")
    return float(value)

def load_thumb_setup(path, motor_directions):
    path = Path(path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError("Invalid thumb setup file; do not substitute poses.json")
    if data.get("motor_directions") != list(motor_directions):
        raise ValueError("Saved thumb setup uses a different motor orientation")
    return valid_angle(data.get("logical_degrees"))

def save_thumb_setup(path, angle, motor_directions):
    data = {"schema": SCHEMA, "logical_degrees": valid_angle(angle),
            "motor_directions": list(motor_directions),
            "meaning": "Operator-selected fixed abduction actuator target; not a measured mechanical limit"}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".thumb-", suffix=".tmp", delete=False) as f:
            temporary = f.name
            json.dump(data, f, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return data
