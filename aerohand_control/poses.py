"""Editable nominal poses converted to actuator goals without contacting a hand."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from aero_open_sdk.aero_hand_constants import AeroHandConstants
from aero_open_sdk.joints_to_actuations import JointsToActuationsModel

from .hand import HandInputError, _seven_finite, _motor_directions
from aero_open_sdk.actuations_to_joints import ActuationsToJointsModelCompact


POSES_PATH = Path(__file__).resolve().parents[1] / "poses.json"
COMPACT_JOINT_NAMES = (
    "thumb_cmc_abd", "thumb_cmc_flex", "thumb_mcp_ip",
    "index", "middle", "ring", "pinky",
)


def _load(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    source = Path(path) if path is not None else POSES_PATH
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandInputError(f"Could not read pose definitions from {source.name}.") from exc
    if not isinstance(data, dict) or data.get("units") != "compact_joint_degrees":
        raise HandInputError("poses.json must declare units=compact_joint_degrees.")
    if data.get("joint_order") != list(COMPACT_JOINT_NAMES):
        raise HandInputError("poses.json has an unexpected compact joint order.")
    poses = data.get("poses")
    if not isinstance(poses, dict) or not poses:
        raise HandInputError("poses.json must contain a nonempty poses object.")
    return poses


def pose_names(path: str | Path | None = None) -> tuple[str, ...]:
    return tuple(_load(path))


def get_pose(name: str, path: str | Path | None = None) -> dict[str, Any]:
    poses = _load(path)
    if name not in poses:
        raise HandInputError(f"Unknown pose {name!r}; choose from: {', '.join(poses)}.")
    pose = poses[name]
    if not isinstance(pose, dict):
        raise HandInputError(f"Pose {name!r} must be an object.")
    values = pose.get("joint_degrees")
    try:
        if not isinstance(values, list) or len(values) != 7 or any(isinstance(v, bool) for v in values):
            raise ValueError
        values = [float(v) for v in values]
    except (ValueError, TypeError, OverflowError) as exc:
        raise HandInputError(f"Pose {name!r} requires seven finite joint angles.") from exc
    indices = (0, 1, 2, 4, 7, 10, 13)
    constants = AeroHandConstants()
    for value, index in zip(values, indices):
        if not math.isfinite(value) or not constants.joint_lower_limits[index] <= value <= constants.joint_upper_limits[index]:
            raise HandInputError(f"Pose {name!r} contains a joint angle outside the vendor limits.")
    return {"name": name, **pose, "joint_degrees": values}


def compact_to_actuations(joint_degrees: list[float], *,
                          motor_directions=(1, 1, 1, 1, 1, 1, 1)) -> list[float]:
    """Return logical actuator targets for an oriented compact joint pose.

    1. Interpret the requested joint orientation in the vendor joint frame.
    2. Compute ALL coupled actuators in that frame using the vendor model.
    3. Return those hardware actuations in the application's logical motor frame.

    The serial adapter converts step 3 back to hardware exactly once. Reflecting
    a motor *after* computing its companions in the opposite joint frame is not
    a coupling-preserving conversion. This function does no hardware I/O.
    """
    q = _seven_finite(joint_degrees, "Compact joint pose")
    constants = AeroHandConstants()
    for value, i in zip(q, (0, 1, 2, 4, 7, 10, 13)):
        if not constants.joint_lower_limits[i] <= value <= constants.joint_upper_limits[i]:
            raise HandInputError("Compact joint pose contains an angle outside the vendor joint limits.")
    directions = _motor_directions(motor_directions)
    q = [value if direction == 1 else constants.joint_lower_limits[i] + constants.joint_upper_limits[i] - value
         for value, direction, i in zip(q, directions, (0, 1, 2, 4, 7, 10, 13))]
    full = [q[0], q[1], q[2], q[2], *([q[3]] * 3), *([q[4]] * 3), *([q[5]] * 3), *([q[6]] * 3)]
    hardware = JointsToActuationsModel().hand_actuations(full)
    logical = [value if direction == 1 else lo + hi - value
               for value, direction, lo, hi in zip(hardware, directions,
                   constants.actuation_lower_limits, constants.actuation_upper_limits)]
    return validate_actuation_target(logical, motor_directions=directions)


def validate_actuation_target(values: list[float], *,
                              motor_directions=(1, 1, 1, 1, 1, 1, 1)) -> list[float]:
    """Validate a proposed neural goal against travel and coupled thumb geometry.

    This performs no hardware I/O.  Only sub-millidegree decimal roundoff at a
    declared actuator limit is clamped.  The mapping is a geometric estimate.
    """
    result = _seven_finite(values, "Actuator goal")
    constants = AeroHandConstants()
    if any(
        not lo - 0.001 <= value <= hi + 0.001
        for value, lo, hi in zip(result, constants.actuation_lower_limits, constants.actuation_upper_limits)
    ):
        raise HandInputError("Actuator goal is outside the vendor travel limits.")
    result = [
        max(lo, min(hi, value))
        for value, lo, hi in zip(result, constants.actuation_lower_limits, constants.actuation_upper_limits)
    ]
    directions = _motor_directions(motor_directions)
    hardware = [value if direction == 1 else lo + hi - value
                for value, direction, lo, hi in zip(result, directions,
                    constants.actuation_lower_limits, constants.actuation_upper_limits)]
    # The vendor inverse must see native hardware coordinates, not a vector
    # with the first actuator reflected independently of the two thumb tendons.
    joints = [math.degrees(q) for q in ActuationsToJointsModelCompact().hand_joints(
        [math.radians(a) for a in hardware])]
    # The published raw finger maxima map to approximately 90.012 joint degrees.
    # Permit that small constants/model disagreement; do not admit meaningful
    # negative thumb positions from an independently specified actuator goal.
    for q, i in zip(joints, (0, 1, 2, 4, 7, 10, 13)):
        if not constants.joint_lower_limits[i] - 0.02 <= q <= constants.joint_upper_limits[i] + 0.02:
            raise HandInputError("Actuator goal is outside the vendor coupled-joint model envelope.")
    return result


def pose_to_actuations(name: str, amount: float = 1.0, path: str | Path | None = None, *,
                       motor_directions=(1, 1, 1, 1, 1, 1, 1),
                       thumb_abduction: float | None = None) -> list[float]:
    """Interpolate from the configured Open pose, then convert to seven goals.

    Amount is pose amplitude, not grip force.  This function does not command
    actuators and does not decide which neural motor direction should fire.
    """
    if isinstance(amount, bool) or not math.isfinite(amount) or not 0 <= amount <= 1:
        raise HandInputError("Pose amount must be a finite value from 0 to 1.")
    pose = get_pose(name, path)
    open_pose = get_pose("open", path)
    # Open need not be an all-zero mathematical endpoint on a physical hand.
    # The UI Open button, startup Open, and zero-amount pose commands must agree.
    # Interpolate compact joints *before* the vendor's coupled thumb conversion.
    if name == "open" or amount == 0:
        joints = open_pose["joint_degrees"][:]
    elif amount == 1:
        joints = pose["joint_degrees"][:]
    else:
        joints = [start + amount * (end - start)
                  for start, end in zip(open_pose["joint_degrees"], pose["joint_degrees"])]
    if thumb_abduction is not None:
        from .thumb_setup import valid_angle
        joints[0] = valid_angle(thumb_abduction)
    return compact_to_actuations(joints, motor_directions=motor_directions)
