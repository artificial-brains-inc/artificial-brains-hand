"""Mechanical interpretation of a NEURAL grip-HOLD, never raw pressure logic."""
from __future__ import annotations
from .constants import FINGER_MOTORS


def motor_hold_mask(neural_outputs: dict[str, int]) -> list[bool]:
    mask = [False] * 7
    for finger, indices in FINGER_MOTORS.items():
        if neural_outputs.get(f"grip_{finger}_hold", 0) > 0:
            for i in indices:
                mask[i] = True
    return mask


def held_targets_and_deltas(targets, measured, deltas, holds):
    """Cancel positive target lead, leave opening requests and other motors intact.

    Coordinates are the adapter's logical actuator degrees: positive is the
    declared FLEX direction. In particular the thumb's hardware inversion has
    already been handled before this function is called.
    """
    if any(len(v) != 7 for v in (targets, measured, deltas, holds)):
        raise ValueError("Seven motor values are required")
    if any(type(v) is not bool for v in holds):
        raise ValueError("Neural hold states must be Boolean")
    anchored = [min(t, p) if h else t for t, p, h in zip(targets, measured, holds)]
    increments = [min(d, 0.0) if h else d for d, h in zip(deltas, holds)]
    return anchored, increments


def permitted_motor_counts(raw: dict[str, int]) -> dict[str, int]:
    """Interpret neural HOLD as suppress-closing, even with simultaneous EXTEND.

    Sensor readings are deliberately absent. The brain emits the HOLD command;
    the driver gives that explicit command precedence over FLEX, not EXTEND.
    Raw neural telemetry is retained separately and is never rewritten.
    """
    from .constants import MOTOR_KEYS
    holds = motor_hold_mask(raw)
    return {f"{key}_{side}": (0 if side == "flex" and holds[i] else raw.get(f"{key}_{side}", 0))
            for i, key in enumerate(MOTOR_KEYS) for side in ("flex", "extend")}
