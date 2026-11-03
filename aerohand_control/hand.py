"""Translate neural degree increments into bounded AeroHand actuator targets.

Normal neural translation does not select a motor direction. The operator can
explicitly position and pin thumb abduction during setup; that axis then ignores
neural requests. All other motors continue using neural increments. Its bounds
only reduce a requested increment. The vendor's linear compact-joint model is
a constraint on commanded changes, not direct finger sensing or an absolute
validity test for an observed resting posture. An existing model-limit violation
may stay unchanged or decrease, but a requested move may not increase it.

Motor orientation is an adapter coordinate transform. Goals, decoded neural
increments, measured positions, and model constraints all use logical motor
coordinates. Only serial feedback and serial targets use hardware coordinates.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

from aero_open_sdk.actuations_to_joints import ActuationsToJointsModelCompact
from aero_open_sdk.aero_hand_constants import AeroHandConstants

from .constants import MOTOR_KEYS
from .protocol import PositionProtocol, ProtocolError, ReplyTimeout


class HandError(RuntimeError):
    """A hand operation could not be completed."""


class HandCommunicationError(HandError):
    """A complete valid hand packet could not be sent or received."""


class HandReadTimeout(HandCommunicationError):
    """No complete position observation arrived; a later read may recover."""


class HandStateError(HandError):
    """The observed state is unsuitable for another motion command."""


class HandInputError(HandError, ValueError):
    """A caller supplied an invalid value."""


@dataclass(frozen=True)
class MotionResult:
    positions: tuple[float, ...]
    limited: tuple[str, ...]
    wrote: bool
    measured: tuple[float, ...] = ()
    applied_deltas: tuple[float, ...] = ()
    limit_reasons: tuple[tuple[str, ...], ...] = ()
    previous_targets: tuple[float, ...] = ()


def _seven_finite(values: Iterable[float], label: str) -> list[float]:
    if isinstance(values, (str, bytes)) or values is None:
        raise HandInputError(f"{label} must contain seven finite numbers.")
    try:
        items = list(values)
        if len(items) != 7 or any(isinstance(v, bool) for v in items):
            raise ValueError
        result = [float(v) for v in items]
    except (TypeError, ValueError, OverflowError) as exc:
        raise HandInputError(f"{label} must contain seven finite numbers.") from exc
    if not all(math.isfinite(v) for v in result):
        raise HandInputError(f"{label} must contain seven finite numbers.")
    return result


def _motor_directions(values: Iterable[int]) -> tuple[int, ...]:
    """Validate polarity before opening a port or taking ownership of a hand."""
    try:
        directions = tuple(values)
    except TypeError as exc:
        raise HandInputError("Motor directions must contain seven values of +1 or -1.") from exc
    if len(directions) != 7 or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value not in (-1, 1)
        for value in directions
    ):
        raise HandInputError("Motor directions must contain seven values of +1 or -1.")
    return tuple(int(value) for value in directions)


class _CheckedSerial:
    """Make transport write failures escape the vendor's print-and-return path.

    The vendor catches SerialTimeoutException in set_actuations.  Converting it
    at the serial boundary to our own exception prevents a failed write being
    treated as success.  Partial writes are also failures.  No payload is logged.
    """

    def __init__(self, serial: Any):
        object.__setattr__(self, "_serial", serial)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._serial, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._serial, name, value)

    def write(self, data: bytes) -> int:
        try:
            count = self._serial.write(data)
        except Exception as exc:
            raise HandCommunicationError("AeroHand serial write failed.") from exc
        if count != len(data):
            raise HandCommunicationError("AeroHand serial write was incomplete.")
        return count

    def flush(self) -> None:
        try:
            self._serial.flush()
        except Exception as exc:
            raise HandCommunicationError("AeroHand serial flush failed.") from exc


class AeroHandAdapter:
    # Encoder transport quantization is approximately 0.005 actuator degrees.
    # This tolerance permits telemetry at a raw travel boundary. Model changes
    # use only floating-point epsilon, avoiding a per-step outward allowance.
    TARGET_LEAD_POLICY = "nonfatal_all_motors"
    _TELEMETRY_EPS_DEG = 0.02
    _NUMERIC_EPS = 1e-9

    def __init__(
        self,
        port: str | None,
        baudrate: int = 921600,
        timeout_s: float = 0.15,
        hand: Any | None = None,
        max_target_lead_degrees: float = 8.0,
        startup_timeout_s: float = 5.0,
        motor_directions: Iterable[int] = (1, 1, 1, 1, 1, 1, 1),
        feedback_reuse_s: float = 0.04,
    ):
        directions = _motor_directions(motor_directions)
        if not math.isfinite(feedback_reuse_s) or not 0 <= feedback_reuse_s <= 0.1:
            raise HandInputError("Feedback reuse must be between 0 and 0.1 seconds")
        self.feedback_reuse_s = float(feedback_reuse_s)
        self.position_reads = 0
        self.position_reuses = 0
        self.last_read_duration_s = 0.0
        self.total_read_duration_s = 0.0
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise HandInputError("Serial timeout must be positive and finite.")
        if not math.isfinite(startup_timeout_s) or startup_timeout_s <= 0:
            raise HandInputError("Startup timeout must be positive and finite.")
        if (
            not math.isfinite(max_target_lead_degrees)
            or max_target_lead_degrees <= 0
        ):
            raise HandInputError("Maximum target lead must be positive and finite.")
        owns_hand = hand is None
        if owns_hand:
            from .serial_open import ControlledAeroHand

            try:
                hand = ControlledAeroHand(
                    port=port, baudrate=baudrate, timeout_s=timeout_s,
                )
            except Exception as exc:
                raise HandCommunicationError(
                    f"Could not open the AeroHand serial port {port!r}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        try:
            self._hand = hand
            # The pinned published SDK has no serial transaction lock.
            self._io_lock = threading.RLock()
            if hasattr(hand, "ser"):
                hand.ser.timeout = timeout_s
                hand.ser.write_timeout = timeout_s
                if not isinstance(hand.ser, _CheckedSerial):
                    hand.ser = _CheckedSerial(hand.ser)
            constants = AeroHandConstants()
            lower = _seven_finite(
                getattr(hand, "actuation_lower_limits", constants.actuation_lower_limits),
                "Actuator lower limits",
            )
            upper = _seven_finite(
                getattr(hand, "actuation_upper_limits", constants.actuation_upper_limits),
                "Actuator upper limits",
            )
            if any(lo >= hi for lo, hi in zip(lower, upper)):
                raise HandInputError("Every actuator lower limit must be below its upper limit.")
            self.limits = list(zip(lower, upper))
            self._motor_directions = directions
            self._position_protocol = None
            if hasattr(hand, "ser") and callable(getattr(hand.ser, "read", None)):
                self._position_protocol = PositionProtocol(
                    hand.ser, lower, upper, timeout_s=timeout_s,
                    startup_timeout_s=startup_timeout_s, lock=self._io_lock,
                )
            compact_indices = (0, 1, 2, 4, 7, 10, 13)
            self._joint_limits = [
                (constants.joint_lower_limits[i], constants.joint_upper_limits[i])
                for i in compact_indices
            ]
            self._inverse = ActuationsToJointsModelCompact()
            self.max_target_lead_degrees = float(max_target_lead_degrees)
            self._thumb_abduction_lock: float | None = None
            self._target: list[float] | None = None
            self._last_feedback: list[float] | None = None
            self._last_raw_feedback: list[float] | None = None
            self.last_feedback_at: float | None = None
        except BaseException:
            # The caller retains ownership of an injected hand. An internally
            # opened hand must be released if any later adapter setup fails.
            if owns_hand:
                try:
                    hand.close()
                except BaseException:
                    pass
            raise

    @property
    def target_positions(self) -> tuple[float, ...] | None:
        return tuple(self._target) if self._target is not None else None

    @property
    def thumb_abduction_lock(self) -> float | None:
        return getattr(self, "_thumb_abduction_lock", None)

    def set_thumb_abduction_lock(self, angle: float) -> float:
        """Configure the fixed logical target; no serial write is made here.

        Used before startup OPEN, or to adopt a fresh measured position. This
        axis is not driven by neural increments, pose changes, or grip-HOLD.
        """
        from .thumb_setup import valid_angle
        angle = valid_angle(angle, self.limits[0])
        self._thumb_abduction_lock = angle
        return angle

    def lock_measured_thumb(self) -> list[float]:
        """Adopt and hold one fresh measured angle with one read/write transaction.

        Do not change the selected lock if transmission fails. No second serial
        read is needed after capturing the operator-requested measured position.
        """
        measured = self.read_positions()
        previous_lock = self.thumb_abduction_lock
        self.set_thumb_abduction_lock(measured[0])
        try:
            self._write_hardware_positions(self._last_raw_feedback)
        except BaseException:
            self._thumb_abduction_lock = previous_lock
            raise
        self._target = measured[:]
        return measured

    def manual_thumb_adjust(self, angle: float) -> list[float]:
        """Operator-only setup motion, with the other fingers held in place.

        Changing abduction changes the thumb's tendon geometry. Recompute just
        those two companion actuator targets while preserving the measured
        compact thumb-flexion angles. This compensation happens ONLY for an
        explicit manual setup command, never for a neural output.
        """
        from .thumb_setup import valid_angle
        from aero_open_sdk.joints_to_actuations import JointsToActuationsModel
        angle = valid_angle(angle, self.limits[0])
        measured = self.read_positions()
        raw = self.hardware_coordinates(measured)
        native = [math.degrees(v) for v in self._inverse.hand_joints(
            [math.radians(v) for v in raw])]
        native[0] = (angle if self._motor_directions[0] == 1
                     else sum(self._joint_limits[0]) - angle)
        full = [native[0], native[1], native[2], native[2],
                *([native[3]] * 3), *([native[4]] * 3),
                *([native[5]] * 3), *([native[6]] * 3)]
        converted = self._orient_positions(JointsToActuationsModel().hand_actuations(full))
        target = [angle, converted[1], converted[2], *measured[3:]]
        if not self._actuator_state_valid(target, self._TELEMETRY_EPS_DEG):
            raise HandInputError("Thumb adjustment exceeds actuator travel; choose a smaller adjustment")
        if not self._joint_step_valid(measured, target):
            raise HandInputError("Thumb adjustment would worsen the coupled model limits")
        previous_lock = self.thumb_abduction_lock
        self._thumb_abduction_lock = angle
        try:
            self._send_positions(target)
        except BaseException:
            self._thumb_abduction_lock = previous_lock
            raise
        self._target = target[:]
        return target

    @property
    def motor_directions(self) -> tuple[int, ...]:
        return self._motor_directions

    def _orient_positions(self, positions: list[float]) -> list[float]:
        """Reflect reversed axes within their bounds, in either direction.

        Reflection is its own inverse and preserves the travel interval. It
        is used for measured feedback and serial targets. The pose/constraint
        geometry must be evaluated in the vendor frame separately.
        """
        return [
            value if direction == 1 else lower + upper - value
            for value, direction, (lower, upper) in zip(
                positions, self._motor_directions, self.limits,
            )
        ]

    def hardware_coordinates(self, positions: Iterable[float]) -> list[float]:
        """Preview the serial target coordinates, without sending any command."""
        return self._orient_positions(_seven_finite(positions, "Actuator positions"))

    def compact_joint_positions(self, actuations: Iterable[float]) -> list[float]:
        """Estimate logical joints via the vendor's NATIVE actuator frame.

        This is a body model, not direct joint sensing. Undo actuator orientation
        before the coupled inverse, then express the result in logical joint
        orientation. Otherwise a reversed thumb is checked against the wrong
        tendon geometry, and even a nominal Open target can be contradictory.
        """
        values = _seven_finite(actuations, "Actuator positions")
        hardware = self._orient_positions(values)
        native_joints = [math.degrees(v) for v in self._inverse.hand_joints(
            [math.radians(v) for v in hardware])]
        return [value if direction == 1 else lo + hi - value
                for value, direction, (lo, hi) in zip(
                    native_joints, self._motor_directions, self._joint_limits)]

    def recent_positions(self) -> list[float]:
        """Reuse an actual recent packet, never a target or an invented measurement.

        Intended for adjacent output frames and feedback publication in the same
        host loop. Every packet retains its real receipt time. There is no reuse
        following a failed read, nor beyond feedback_reuse_s (40 ms by default).
        """
        stamp = getattr(self, 'last_feedback_at', None)
        values = getattr(self, '_last_feedback', None)
        age = time.monotonic()-stamp if stamp is not None else float('inf')
        if values is not None and 0 <= age < getattr(self,'feedback_reuse_s',0):
            self.position_reuses += 1
            return values[:]
        return self.read_positions()

    def read_positions(self) -> list[float]:
        """Return logical actuator feedback and initialize the target once.

        The first serial transaction allows startup_timeout_s for a reply while
        consuming complete firmware text lines. Later transactions use the
        ordinary timeout. A missing reply raises HandReadTimeout without
        blocking later reads. This call does not retry or return cached data.
        Firmware supplies no servo-sample timestamp, so cache freshness cannot
        be established from a newly requested packet.
        """
        started_read = time.monotonic()
        self.position_reads = getattr(self, "position_reads", 0) + 1
        try:
            with self._io_lock:
                values = (
                    self._position_protocol.read_positions()
                    if self._position_protocol is not None
                    else self._hand.get_actuations()
                )
            raw_positions = _seven_finite(values, "Actuator position feedback")
        except ReplyTimeout as exc:
            self.last_feedback_at = None
            self.last_read_duration_s = time.monotonic()-started_read
            self.total_read_duration_s += self.last_read_duration_s
            raise HandReadTimeout(str(exc)) from exc
        except ProtocolError as exc:
            raise HandCommunicationError(str(exc)) from exc
        except HandCommunicationError:
            raise
        except Exception as exc:
            raise HandCommunicationError("AeroHand did not return seven valid actuator positions.") from exc
        for position, (lower, upper) in zip(raw_positions, self.limits):
            if not lower - self._TELEMETRY_EPS_DEG <= position <= upper + self._TELEMETRY_EPS_DEG:
                raise HandStateError("AeroHand reported a position outside its actuator travel limits.")
        positions = self._orient_positions(raw_positions)
        self._last_raw_feedback = raw_positions[:]
        self._last_feedback = positions[:]
        self.last_feedback_at = time.monotonic()
        self.last_read_duration_s = self.last_feedback_at-started_read
        self.total_read_duration_s += self.last_read_duration_s
        if self._target is None:
            self._target = positions[:]
        return positions

    def _actuator_state_valid(self, positions: list[float], tolerance: float = 0.0) -> bool:
        return all(
            lo - tolerance <= value <= hi + tolerance
            for value, (lo, hi) in zip(positions, self.limits)
        )

    def _joint_step_valid(self, initial: list[float], candidate: list[float]) -> bool:
        """Keep each modeled joint inside its nominal or current-target bounds.

        Firmware's extended actuator endpoints need not map into the ideal
        joint box. Preserve their actual coordinates and accept only requested
        changes that do not worsen an existing violation. Recompute from the
        last committed target so earlier inward progress cannot be undone by
        reusing a wider startup allowance. Only floating-point epsilon belongs
        here; adding telemetry tolerance each step could permit outward drift.
        """
        before = self.compact_joint_positions(initial)
        after = self.compact_joint_positions(candidate)
        return all(
            min(lower, q0) - self._NUMERIC_EPS <= q1 <= max(upper, q0) + self._NUMERIC_EPS
            for q0, q1, (lower, upper) in zip(before, after, self._joint_limits)
        )

    def _axis_fraction(self, positions: list[float], index: int, delta: float) -> float:
        if not delta:
            return 0.0
        trial = positions[:]
        trial[index] += delta
        before = self.compact_joint_positions(positions)
        after = self.compact_joint_positions(trial)
        fraction = 1.0
        for q0, q1, (lower, upper) in zip(before, after, self._joint_limits):
            # An existing model-limit violation may be retained or reduced,
            # never enlarged or corrected with movement the brain did not ask
            # for. Each accepted component can only tighten this allowance.
            lower, upper = min(lower, q0), max(upper, q0)
            if q1 < lower and q1 < q0:
                fraction = min(fraction, (q0 - lower) / (q0 - q1))
            elif q1 > upper and q1 > q0:
                fraction = min(fraction, (upper - q0) / (q1 - q0))
        return max(0.0, min(1.0, fraction))

    def _send_positions(self, positions: list[float]) -> None:
        positions = positions[:]
        if self.thumb_abduction_lock is not None:
            positions[0] = self.thumb_abduction_lock
        self._write_hardware_positions(self._orient_positions(positions))

    def _write_hardware_positions(self, positions: list[float]) -> None:
        positions = positions[:]
        if self.thumb_abduction_lock is not None:
            angle = self.thumb_abduction_lock
            positions[0] = (angle if self._motor_directions[0] == 1
                            else sum(self.limits[0]) - angle)
        try:
            with self._io_lock:
                self._hand.set_actuations(positions[:])
        except HandCommunicationError:
            raise
        except Exception as exc:
            raise HandCommunicationError("AeroHand position command failed.") from exc

    def apply_deltas(self, deltas: Iterable[float], *, grip_holds: Iterable[bool] | None = None) -> MotionResult:
        """Apply only signed neural degree increments, with visible reductions.

        The commanded target persists across iterations.  Encoder drift never
        becomes a new goal.  Movements use a valid feedback packet no older than feedback_reuse_s.
        Empty neural frames require no serial transaction. A zero increment makes no position write unless a new neural
        grip-HOLD must cancel an outstanding closing target.
        """
        requested = _seven_finite(deltas, "Neural motor increments")
        incoming = requested[:]
        holds = [False] * 7 if grip_holds is None else list(grip_holds)
        if len(holds) != 7 or any(type(v) is not bool for v in holds):
            raise HandInputError("Neural grip hold needs seven Boolean channel states")
        locked = self.thumb_abduction_lock
        locked_request = locked is not None and (bool(requested[0]) or holds[0])
        if locked is not None:
            requested[0] = 0.0
            holds[0] = False
        # Empty output frames carry no new actuator request. Do not spend a
        # serial round trip on each empty neural frame; the input loop still
        # acquires actual position feedback at its normal cadence.
        if not any(requested) and not any(holds) and self._target is not None:
            return MotionResult(tuple(self._target),
                                (MOTOR_KEYS[0],) if locked_request else (), False,
                                tuple(getattr(self,'_last_feedback',None) or ()),
                                (0.0,)*7,
                                (("manual_thumb_lock",),)+((),)*6 if locked_request else ((),)*7,
                                tuple(self._target))
        measured = self.recent_positions()
        assert self._target is not None
        original = self._target[:]
        if locked is not None:
            original[0] = locked
        reasons = [[] for _ in range(7)]
        if locked_request:
            reasons[0].append("manual_thumb_lock")
        # A neural HOLD cancels remaining positive travel. Preserve an existing
        # opening target and never move the hold anchor farther into closure.
        # Sustained HOLD suppresses positive increments, never negative ones.
        from .body_commands import held_targets_and_deltas
        initial, requested = held_targets_and_deltas(original, measured, requested, holds)
        for i in range(7):
            if (i != 0 or locked is None) and (incoming[i] != requested[i] or initial[i] != original[i]):
                reasons[i].append('neural_grip_hold')
        if not self._actuator_state_valid(initial, self._TELEMETRY_EPS_DEG):
            raise HandStateError(
                "Current actuator target lies outside the vendor actuator travel limits."
            )
        # A committed actuator target can fall outside
        # the lead window when measured feedback shifts. This is not a fatal
        # hand state. Keep the committed target, report the discrepancy, and
        # let the per-axis bounds below prevent further outward advance.
        # Neural increments toward the measurement remain available, as do
        # grip-HOLD above and movement on all other axes. Never substitute a
        # measured position for a target without an explicit HOLD command.
        for i, (target, actual) in enumerate(zip(initial, measured)):
            if (i != 0 or locked is None) and abs(target - actual) > (
                self.max_target_lead_degrees + self._TELEMETRY_EPS_DEG
            ):
                reasons[i].append("target_lead_existing")

        bounded: list[float] = []
        for i, delta in enumerate(requested):
            lower, upper = self.limits[i]
            lower = max(lower, measured[i] - self.max_target_lead_degrees)
            upper = min(upper, measured[i] + self.max_target_lead_degrees)
            # If telemetry has shifted across a bound, permit no further movement
            # outward.  Never turn a positive requested delta into a negative one.
            if delta > 0:
                bounded.append(min(delta, max(0.0, upper - initial[i])))
            elif delta < 0:
                bounded.append(max(delta, min(0.0, lower - initial[i])))
            else:
                bounded.append(0.0)

        for i, delta in enumerate(requested):
            if abs(bounded[i]-delta) > self._NUMERIC_EPS:
                raw_candidate=initial[i]+delta
                lo,hi=self.limits[i]
                if not lo <= raw_candidate <= hi: reasons[i].append('actuator_range')
                if abs(raw_candidate-measured[i]) > self.max_target_lead_degrees:
                    reasons[i].append('target_lead')
        candidate = [value + delta for value, delta in zip(initial, bounded)]
        if not self._joint_step_valid(initial, candidate):
            # The full simultaneous vector was invalid.  Accept only components
            # supported by the non-worsening model bounds, with a second pass
            # after other requested components have made room.
            candidate = initial[:]
            remaining = bounded[:]
            for _ in range(2):
                for i in (2, 1, 0, 3, 4, 5, 6):
                    accepted = remaining[i] * self._axis_fraction(candidate, i, remaining[i])
                    candidate[i] += accepted
                    remaining[i] -= accepted

        if (
            not self._actuator_state_valid(candidate, self._TELEMETRY_EPS_DEG)
            or not self._joint_step_valid(initial, candidate)
        ):
            raise HandStateError(
                "Motor increment could not preserve actuator travel and non-worsening model bounds."
            )
        for i in range(7):
            if abs(candidate[i]-initial[i]-bounded[i]) > self._NUMERIC_EPS:
                reasons[i].append('coupled_joint_model')
        limited = tuple(
            MOTOR_KEYS[i]
            for i in range(7)
            if abs((candidate[i] - initial[i]) - incoming[i]) > self._NUMERIC_EPS
            or abs(initial[i] - original[i]) > self._NUMERIC_EPS
        )
        wrote = any(abs(a - b) > self._NUMERIC_EPS for a, b in zip(candidate, original))
        if wrote:
            self._send_positions(candidate)
            # Commit only after a complete serial write.  The firmware provides
            # no position-command ACK; following encoder reads assess movement.
            self._target = candidate[:]
        return MotionResult(tuple(self._target), limited, wrote, tuple(measured),
                            tuple(candidate[i]-initial[i] for i in range(7)),
                            tuple(tuple(x) for x in reasons),tuple(original))

    def command_startup_pose(self, positions: Iterable[float]) -> None:
        """Explicit pre-brain initialization, not a neural motor decision.

        Only startup.py calls this method. Validate the absolute target, apply
        the same polarity transform as normal motion, and commit after write.
        Completion is separately verified using measured positions.
        """
        from .poses import validate_actuation_target
        target = _seven_finite(positions, "Startup pose")
        if self.thumb_abduction_lock is not None:
            target[0] = self.thumb_abduction_lock
        target = validate_actuation_target(target, motor_directions=self._motor_directions)
        if not self._actuator_state_valid(target):
            raise HandInputError("Startup pose is outside actuator travel")
        self._send_positions(target)
        self._target = target[:]

    def hold(self) -> list[float]:
        """Command freshly measured actuator positions as an operational hold.

        A successful serial write is not a torque disable or hardware stop ACK.
        Communication failure can prevent this hold from reaching the hand.
        """
        positions = self.read_positions()
        # Use that exact raw observation: reflecting it twice can introduce a
        # floating-point rounding difference before the vendor's uint16 cast.
        assert self._last_raw_feedback is not None
        self._write_hardware_positions(self._last_raw_feedback)
        self._target = positions[:]
        if self.thumb_abduction_lock is not None:
            self._target[0] = self.thumb_abduction_lock
        return positions

    def configure_motion(self, speed: int | None = None, torque: int | None = None) -> None:
        """Change vendor execution settings only when explicitly supplied."""
        if speed is not None and (isinstance(speed, bool) or not isinstance(speed, int) or not 0 <= speed <= 32766):
            raise HandInputError("Speed must be an integer from 0 to 32766.")
        if torque is not None and (isinstance(torque, bool) or not isinstance(torque, int) or not 0 <= torque <= 1000):
            raise HandInputError("Torque setting must be an integer from 0 to 1000.")
        with self._io_lock:
            for i in range(7):
                try:
                    if speed is not None:
                        self._hand.set_speed(i, speed)
                    if torque is not None:
                        self._hand.set_torque(i, torque)
                except Exception as exc:
                    raise HandCommunicationError("AeroHand motion-setting acknowledgment failed.") from exc

    def close(self) -> None:
        """Close serial only.  Call hold first when an operational stop is needed."""
        try:
            with self._io_lock:
                self._hand.close()
        except Exception as exc:
            raise HandCommunicationError("Could not close AeroHand serial connection.") from exc
