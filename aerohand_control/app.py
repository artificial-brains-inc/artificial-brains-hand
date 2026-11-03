"""Local operator interfaces and live body/brain coordination."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
from pathlib import Path
import queue
import shlex
import threading
import time
from typing import Callable

from .constants import INPUT_NEURONS, MOTOR_KEYS, OUTPUT_NEURONS, VERSION, FINGERS, FINGER_MOTORS, input_contract, output_contract
from .input_flow import AcquisitionCadence, STARTUP_DRAIN_EVENTS
from .startup import StartupCancelled, open_before_brain, zero_before_brain

ROOT = Path(__file__).resolve().parents[1]


class ControlError(RuntimeError):
    pass


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    # Defaults for optional local configuration fields.
    cfg.setdefault("startup_timeout_s", 5.0)
    cfg.setdefault("motor_directions", [-1, 1, 1, 1, 1, 1, 1])
    cfg.setdefault("feedback_reuse_s", 0.04)
    cfg.setdefault("logging_max_mb", 256)
    cfg.setdefault("startup_open_timeout_s", 30.0)
    cfg.setdefault("startup_open_tolerance_degrees", 2.0)
    if type(cfg["feedback_reuse_s"]) not in (int,float) or not 0 <= cfg["feedback_reuse_s"] <= 0.1:
        raise ValueError("feedback_reuse_s must be 0..0.1 seconds")
    if type(cfg["logging_max_mb"]) is not int or cfg["logging_max_mb"] < 1:
        raise ValueError("logging_max_mb must be a positive integer")
    scalar_fields = (
        "input_hz", "telemetry_timeout_s", "startup_timeout_s", "output_timeout_s",
        "max_output_age_s", "status_interval_s",
        "max_target_lead_degrees", "tactile_max_age_s",
        "startup_open_timeout_s", "startup_open_tolerance_degrees",
    )
    for key in scalar_fields:
        value = cfg.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"config.json: {key} must be finite and greater than zero")
    if cfg["input_hz"] > 100:
        raise ValueError("config.json: input_hz must be at most 100")
    for key in ("per_spike_degrees",):
        values = cfg.get(key)
        if not isinstance(values, list) or len(values) != 7 or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in values
        ):
            raise ValueError(f"config.json: {key} needs seven finite positive values")
    # An optional, operator-configured duration is a body Hold, not a neural
    # completion criterion. By default the brain regulates movement continuously.
    duration = cfg.setdefault("command_timeout_s", None)
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, (int, float))
        or not math.isfinite(duration) or duration <= 0
    ):
        raise ValueError("config.json: command_timeout_s must be null or finite and positive")
    directions = cfg["motor_directions"]
    if (not isinstance(directions, list) or len(directions) != 7
            or any(type(value) is not int or value not in (-1, 1) for value in directions)):
        raise ValueError("config.json: motor_directions needs seven integers, each -1 or 1")
    from .tactile import TactileEncoding
    cfg.setdefault("tactile_directions", [1, 1, 1, 1, 1])
    TactileEncoding(cfg["tactile_directions"])  # validate the wiring convention
    return cfg


class CommandInbox:
    """One pending operator intent; Hold and Stop cannot trail a pose backlog."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: str | None = None
        self._stopped = False

    def put(self, line: str) -> None:
        words = shlex.split(line)
        if not words:
            return
        command = words[0].lower()
        with self._lock:
            if self._stopped:
                raise ValueError("The application is stopping")
            if command in ("quit", "exit"):
                self._stopped = True
                self._pending = "quit"
            elif command in ("hold", "stop"):
                self._pending = "hold"
            elif self._pending == "hold":
                raise ValueError("Hold is being applied; select the next pose afterward")
            elif (self._pending and shlex.split(self._pending)[0] in ("thumb_angle", "thumb_current")
                  and command not in ("thumb_angle", "thumb_current")):
                raise ValueError("Thumb setup is being applied; select the next pose afterward")
            else:
                self._pending = line

    def get_nowait(self) -> str:
        with self._lock:
            if self._pending is None:
                raise queue.Empty
            line, self._pending = self._pending, None
            return line

    @property
    def stopping(self) -> bool:
        with self._lock:
            return self._stopped

    @property
    def urgent(self) -> bool:
        with self._lock:
            return self._stopped or self._pending in ("hold", "quit")

    def discard_pending_pose(self) -> None:
        with self._lock:
            if self._pending not in ("hold", "quit"):
                self._pending = None

    def take_hold(self) -> bool:
        """Consume only a Hold while startup has no command loop yet."""
        with self._lock:
            if self._pending != "hold":
                return False
            self._pending = None
            return True


class HandController:
    """Supply coordinate targets and observations; translate neural outputs.

    Preset poses are application-supplied targets. Position comparison and
    tactile opposition and grip-HOLD activity are neural. No raw pressure condition or
    Python position-error sign is used to select a motor direction.
    """

    def __init__(self, hand, neural, cfg: dict, *, emit: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic,
                 on_hold: Callable[[], None] | None = None,
                 interrupt_requested: Callable[[], bool] | None = None,
                 observe_only: bool = False,
                 initial_positions: list[float] | tuple[float, ...] | None = None,
                 tactile=None, tactile_encoding=None, recorder=None, baseline_path=None,
                 thumb_setup_path=None, thumb_saved_value=None):
        self.hand, self.neural, self.cfg = hand, neural, cfg
        self.recorder = recorder
        self.baseline_path = Path(baseline_path) if baseline_path else None
        self.baseline_capture = None
        self.thumb_setup_path = Path(thumb_setup_path) if thumb_setup_path else None
        self.thumb_saved_value = thumb_saved_value
        from .diagnostics import MotorDiagnostics
        self.diagnostics = MotorDiagnostics(clock)
        self.last_motion = None
        self.tactile, self.tactile_encoding = tactile, tactile_encoding
        self.tactile_available = False
        self.emit, self.clock = emit, clock
        self.on_hold = on_hold or (lambda: None)
        self.interrupt_requested = interrupt_requested or (lambda: False)
        self.observe_only = bool(observe_only)
        # Stop preserves the native input FIFO. Let those observations drain
        # before accepting a new command, with no resulting motor actuation.
        # Observation mode still performs no hand movement/settings writes.
        # Main has already required one valid read before configuring the hand
        # or starting the brain. Retain that measurement for display if this
        # additional startup Hold capture misses a runtime reply; never send it
        # back as a substitute for the missing current measurement.
        self.position = list(initial_positions) if initial_positions is not None else []
        self.name = "hold"
        self.goal = [min(hi, max(lo, x)) for x, (lo, hi) in zip(self.position, hand.limits)]
        self.amount = 1.0
        self.phase = "STARTING"
        self.input_drain_remaining = STARTUP_DRAIN_EVENTS
        self.acquisition = AcquisitionCadence(cfg["input_hz"], clock=clock)
        self.after_step = -1
        self.published_at = float("inf")
        self.resume_pending = False
        self.needs_publish = True
        self.last_publish = float("-inf")
        self.last_event = clock()
        self.last_status = clock()
        self.last_limit_message = float("-inf")
        self.started_at: float | None = None
        self.last_deltas = (0.0,) * 7
        self.last_raw: dict[str, int] = {}
        self.last_step = -1
        self.completed_commands = 0
        self.feedback_available = True
        self.feedback_timeouts = 0
        self.skipped_motor_events = 0
        self.hold_pending = True
        if not self._capture_hold("Startup Hold", allow_timeout=initial_positions is not None,
                                  announce=False, notify_timeout=True):
            self.acquisition.acquired(self.clock())

    @property
    def input_ready(self) -> bool:
        return self.input_drain_remaining == 0

    def set_goal(self, name: str, values, *, amount: float = 1.0) -> None:
        from .neural import _seven_finite
        if not self.input_ready:
            raise ValueError("The input stream is clearing saved observations; select a pose when ready")
        if self.tactile.latest(self.cfg["tactile_max_age_s"]) is None:
            raise ValueError("No current tactile sample; the sensor reader will continue trying")
        goal = list(_seven_finite(values, "Actuator targets"))
        fixed = getattr(self.hand, "thumb_abduction_lock", None)
        if fixed is not None:
            goal[0] = fixed  # Only the dedicated operator setup can change this motor.
        if any(not lo <= x <= hi for x, (lo, hi) in zip(goal, self.hand.limits)):
            raise ValueError("Targets exceed actuator travel limits")
        was_held = self.phase == "HOLD"
        if self.baseline_capture is not None:
            raise ValueError("Unloaded zeroing is in progress; keep the sensor pads unloaded")
        self.goal, self.name, self.amount = goal, name, amount
        self.diagnostics.reset()
        self._record('events','command',{'name':name,'goal':goal,'amount':amount})
        self.started_at = self.clock()
        self.phase = "ACTIVE"
        self.needs_publish = True
        if was_held:
            self.resume_pending = True
            self.published_at = float("inf")
            self.last_event = self.started_at
        self.emit(f"{name}: coordinate target selected; neural tactile inhibition remains active.")

    def hold(self, reason: str = "Operator hold") -> None:
        # Latch the operator's intent before reading. A missed reply must not
        # leave the previous gesture selected or let its queued outputs move.
        self.baseline_capture = None
        self._record("events","operator_hold",{"reason":reason})
        self.phase = "HOLD" if self.input_ready else "STARTING"
        self.name = "hold"
        self.started_at = None
        self.needs_publish = True
        self.resume_pending = False
        self.published_at = float("inf")
        self.hold_pending = True
        self.on_hold()
        if not self._capture_hold(reason, notify_timeout=True):
            # Count a missed capture as an acquisition attempt so the retry
            # cannot bypass the existing input rate limit.
            self.acquisition.acquired(self.clock())

    def _feedback_timeout(self, exc: Exception, context: str, *, announce: bool = False) -> None:
        self._record("events","position_timeout",{"context":context,"error":str(exc)})
        was_available = self.feedback_available
        self.feedback_available = False
        self.feedback_timeouts += 1
        if was_available or announce:
            self.emit(f"{context}: {exc}")

    def _feedback_received(self, positions=None) -> None:
        if positions is not None:
            self.position = list(positions)
        if not self.feedback_available:
            self.emit("Position feedback resumed.")
        self.feedback_available = True

    def _capture_hold(self, reason: str, *, allow_timeout: bool = True,
                      announce: bool = True, notify_timeout: bool = False) -> bool:
        from .hand import HandReadTimeout
        try:
            positions = self.hand.read_positions() if self.observe_only else self.hand.hold()
        except HandReadTimeout as exc:
            if not allow_timeout:
                raise
            self.hold_pending = True
            detail = "Position capture pending" if self.observe_only else "Hold pending; Hold was not sent because position feedback was unavailable"
            self._feedback_timeout(exc, f"{reason}. {detail}", announce=notify_timeout)
            return False
        self._feedback_received(positions)
        self.goal = [min(hi, max(lo, x)) for x, (lo, hi) in zip(self.position, self.hand.limits)]
        if getattr(self.hand, "thumb_abduction_lock", None) is not None:
            self.goal[0] = self.hand.thumb_abduction_lock
        self.hold_pending = False
        if announce:
            detail = "Inactive stimulus selected; no motor writes in observation mode" if self.observe_only else "Hold command sent for current measured motor positions"
            self.emit(f"{reason}. {detail}.")
        return True

    def _record(self, stream, kind, data):
        if self.recorder is not None:self.recorder.record(stream,kind,data)

    def adjust_thumb(self, angle=None) -> None:
        """Explicit body setup. It is not a second neural grasp controller."""
        if self.observe_only:
            raise ValueError("Thumb adjustment is disabled in observation mode")
        if self.phase != "HOLD" or self.hold_pending or not self.input_ready:
            raise ValueError("Select Hold before adjusting the thumb")
        if self.baseline_capture is not None:
            raise ValueError("Finish unloaded zeroing before adjusting the thumb")
        from .hand import HandReadTimeout
        from .thumb_setup import save_thumb_setup
        try:
            if angle is None:
                # Adopt an actual measured angle, never an inferred end stop.
                self.position = self.hand.lock_measured_thumb()
                angle = self.hand.thumb_abduction_lock
                kind = "thumb_current_locked"
            else:
                target = self.hand.manual_thumb_adjust(angle)
                self.position = list(getattr(self.hand, "_last_feedback", None) or self.position)
                angle = target[0]
                kind = "thumb_adjusted"
        except HandReadTimeout as exc:
            self._feedback_timeout(exc, "Thumb setup not sent; try again after feedback resumes")
            return
        self.goal = list(self.hand.target_positions)
        self.needs_publish = True
        self._record("events", kind, {"logical_degrees": angle, "targets": self.goal,
                     "measured": self.position, "brain_abduction_applied": False})
        if self.thumb_setup_path is not None:
            try:
                saved = save_thumb_setup(self.thumb_setup_path, angle, self.cfg["motor_directions"])
            except OSError as exc:
                self.emit(f"Thumb is locked for this run, but its setting could not be saved: {exc}")
                self._record("events", "thumb_save_failed", {"error": str(exc)})
                return
            self.thumb_saved_value = angle
            self._record("events", "thumb_setup_saved", saved)
        self.emit(f"Thumb abduction fixed at {angle:.2f} logical degrees. "
                  "Open, Grab, tactile HOLD and neural output cannot change it. "
                  "Other six motors remain available.")

    def capture_baseline(self):
        if self.phase != 'HOLD' or self.hold_pending or not self.input_ready:
            raise ValueError('Select Hold before zeroing unloaded sensors')
        if self.tactile.latest(self.cfg['tactile_max_age_s']) is None:
            raise ValueError('No live tactile readings to zero')
        from .baseline import BaselineCapture
        self.baseline_capture=BaselineCapture(self.clock())
        self.emit('Zeroing unloaded pads: collecting 50 fresh samples. Keep every pad free of contact.')
        self._record('events','baseline_capture_started',{})

    def _advance_baseline(self,sample):
        if self.baseline_capture is None:return
        try:
            if not self.baseline_capture.observe(sample,self.clock()):return
            data=self.baseline_capture.result(self.tactile_encoding.directions)
            if self.baseline_path is not None:
                from .baseline import save_baseline
                save_baseline(self.baseline_path,data)
            self.tactile_encoding.update_baseline(data['baseline'],data['noise_deadband'],source=data['source'])
            self._record('events','baseline_captured',data)
            self.emit('Unloaded zero saved: '+', '.join(f'{v:g}' for v in data['baseline'])+'. No grip-pressure target was set.')
        except (ValueError,TimeoutError,OSError) as exc:
            self.emit('Zero capture failed: '+str(exc))
            self._record('events','baseline_capture_failed',{'error':str(exc)})
        else:
            self.needs_publish=True
        self.baseline_capture=None

    def _motion_record(self, frame, disposition, result=None):
        self.diagnostics.disposition(disposition,result)
        stamp=getattr(self.hand,'last_feedback_at',None)
        payload={'step':frame.step,'command':self.name,'phase':self.phase,'raw':frame.raw,
                 'decoded_delta':list(frame.deltas),'disposition':disposition,
                 'thumb_abduction_lock':getattr(self.hand,'thumb_abduction_lock',None),
                 'feedback_age_s':max(0,self.clock()-stamp) if stamp is not None else None}
        if result is not None:
            payload.update(targets=list(getattr(result,'positions',())),
                           measured=list(getattr(result,'measured',())),
                           previous_targets=list(getattr(result,'previous_targets',())),
                           applied_delta=list(getattr(result,'applied_deltas',())),
                           limit_reasons=[list(x) for x in getattr(result,'limit_reasons',())],
                           wrote=result.wrote)
            # Preserve the actual values associated with
            # a per-finger reduction, including successful no-write frames.
            # These are actuator-target differences, not pressure thresholds.
            actual = payload['measured']
            previous = payload['previous_targets']
            targets = payload['targets']
            if len(actual) == len(previous) == len(targets) == 7:
                payload['target_lead_degrees_before'] = [
                    target - position for target, position in zip(previous, actual)
                ]
                payload['target_lead_degrees_after'] = [
                    target - position for target, position in zip(targets, actual)
                ]
                payload['target_lead_limit_degrees'] = self.cfg.get('max_target_lead_degrees')
            self.last_motion=payload
        self._record('motion','neural_frame_translation',payload)

    def tick(self) -> None:
        from .hand import HandReadTimeout
        now = self.clock()
        duration = self.cfg.get("command_timeout_s")
        if duration is not None and self.started_at is not None and now - self.started_at >= duration:
            self.completed_commands += 1
            self.hold("Command duration reached")

        sample = self.tactile.latest(self.cfg["tactile_max_age_s"])
        self._advance_baseline(sample)
        fresh_tactile = sample is not None
        if self.tactile_available and not fresh_tactile and self.phase == "ACTIVE":
            self.resume_pending = True
            self.published_at = float("inf")
            self.emit("Tactile feedback unavailable; skipping motor updates, not substituting zero pressure. Recovery is automatic.")
        self.tactile_available = fresh_tactile
        draining = not self.input_ready
        # Consume outputs independently of the feedback acquisition timer.
        # No output identity is used as the execution time of an input.
        frames = self.neural.poll(timeout=0.01)
        serial_missed = False
        for frame in frames:
            if self.interrupt_requested():
                # The main loop applies Hold/Stop next. Discard the remainder
                # of this old command's batch rather than actuating past it.
                return
            # Once a read misses, the rest of this batch is diagnostic only.
            # Time spent waiting for that reply must not turn discarded motor
            # events into a second, fatal event-age error.
            if not serial_missed and self.clock() - frame.captured_at > self.cfg["max_output_age_s"]:
                raise ControlError("Neural events accumulated beyond the age limit; holding instead of replaying old movement")
            # Transport progress remains meaningful while movement is held or
            # waiting for its first current observation. It is not an ACK.
            self.last_event = frame.captured_at
            self.acquisition.observe(frame.captured_at)
            self.last_step = frame.step
            self.last_raw = dict(frame.raw)
            self.last_deltas = tuple(frame.deltas)
            self.diagnostics.observe(frame)
            if draining:
                # Count actual events, never the numerical distance between
                # step identifiers. A whole startup batch remains nonmoving.
                self.input_drain_remaining = max(0, self.input_drain_remaining - 1)
                self._motion_record(frame,"startup_drain")
                continue
            # Diagnostic activity remains visible in Hold and observation mode.
            # It never grants permission for motion or chooses a motor winner.
            if (self.phase == "HOLD" or self.hold_pending or self.resume_pending
                    or frame.step <= self.after_step or frame.captured_at < self.published_at):
                self._motion_record(frame,"operator_hold_or_resume")
                continue
            if self.observe_only or not fresh_tactile:
                self._motion_record(frame,"observe_only" if self.observe_only else "missing_tactile")
                continue
            if serial_missed:
                self.skipped_motor_events += 1
                self._motion_record(frame,"serial_timeout_in_batch")
                continue
            try:
                # This translates a neural HOLD command, not a host threshold.
                from .body_commands import motor_hold_mask
                holds = motor_hold_mask(frame.raw)
                result = self.hand.apply_deltas(frame.deltas, grip_holds=holds)
            except HandReadTimeout as exc:
                serial_missed = True
                self.skipped_motor_events += 1
                self.acquisition.acquired(self.clock())
                self._motion_record(frame,"serial_timeout")
                self._feedback_timeout(exc, "Motor output skipped; position feedback unavailable")
                continue
            self._feedback_received(getattr(result,'measured',None) or None)
            self._motion_record(frame,'applied' if result.wrote else 'no_target_change',result)
            if result.limited and self.clock() - self.last_limit_message >= 0.5:
                self.last_limit_message = self.clock()
                self.emit("Movement limited by body bounds or neural grip-hold: " + ", ".join(result.limited))

        now = self.clock()
        if now - self.last_event > self.cfg["output_timeout_s"]:
            raise ControlError("Fresh neural output stopped arriving")
        if draining and self.input_ready:
            self.phase = "HOLD"
            detail = ("motor writes disabled" if self.observe_only else
                      "Hold waiting for position feedback" if self.hold_pending else "Hold command sent")
            self.emit(f"Input stream ready; {detail}. Select a coordinate target; tactile feedback acts through the brain.")

        if (not serial_missed and (self.input_ready or self.hold_pending)
                and self.acquisition.ready(now)):
            # Rate control happens BEFORE capture. There is no saved sensor
            # snapshot, scheduled neural step, or catch-up input batch.
            # Changing commands or pressing Hold cannot bypass this cadence.
            captured = False
            try:
                if self.hold_pending:
                    # A pending physical Hold can finish while saved inputs
                    # drain. That does not authorize any early neural input.
                    captured = self._capture_hold("Pending Hold")
                else:
                    self._feedback_received(getattr(self.hand,"recent_positions",self.hand.read_positions)())
                    captured = True
            except HandReadTimeout as exc:
                self._feedback_timeout(exc, "Position input skipped; position feedback unavailable")
            finally:
                self.acquisition.acquired(self.clock())
            if captured and self.input_ready:
                if self.interrupt_requested():
                    return
                sample = self.tactile.latest(self.cfg["tactile_max_age_s"])
                if sample is None:
                    return
                if self.phase == "HOLD":
                    self.goal = [min(hi, max(lo, x)) for x, (lo, hi) in zip(self.position, self.hand.limits)]
                    if getattr(self.hand, "thumb_abduction_lock", None) is not None:
                        self.goal[0] = self.hand.thumb_abduction_lock
                self._record('sensors','feedback_snapshot',{
                    'positions':list(self.position),'goal':list(self.goal),
                    'position_received_at':getattr(self.hand,'last_feedback_at',None),
                    'tactile_received_at':sample.received_at,'tactile_device_ms':sample.device_ms,
                    'tactile':self.tactile_encoding.status(sample)})
                self.neural.publish(self.goal, self.position, self.hand.limits,
                                    self.tactile_encoding.encode(sample))
                if self.resume_pending:
                    self.after_step = self.neural.latest_step
                    self.published_at = self.clock()
                    self.resume_pending = False
                self.needs_publish = False
                self.last_publish = self.clock()
        if now - self.last_status >= self.cfg["status_interval_s"]:
            self.last_status = now
            if not self.input_ready:
                completed = STARTUP_DRAIN_EVENTS - self.input_drain_remaining
                self.emit(f"Clearing saved input observations: {completed}/{STARTUP_DRAIN_EVENTS} completed steps; motor writes disabled.")
            elif self.phase != "HOLD":
                self.emit(self.status())

    def status(self) -> str:
        pos = ", ".join(f"{p:.1f}" for p in self.position)
        deltas = ", ".join(f"{v:+.2f}" for v in self.last_deltas)
        return (f"{self.name} | {self.phase} | step={self.last_step} | "
                f"mode={'observe' if self.observe_only else 'live'} | input limit={self.acquisition.rate_hz():.1f} Hz | "
                f"feedback={'available' if self.feedback_available else 'waiting (last known positions)'} | "
                f"hold_pending={self.hold_pending} | skipped motor events={self.skipped_motor_events} | "
                f"motor degrees=[{pos}] | last neural delta=[{deltas}]")


def show_poses() -> None:
    for pose in pose_menu():
        print(f"  {pose['name']:16} {pose['description']}")


def handle_command(line: str, controller: HandController) -> bool:
    words = shlex.split(line)
    if not words:
        return True
    command = words[0].lower()
    if command in ("quit", "exit"):
        return False
    if command in ("hold", "stop"):
        controller.hold()
        return True
    if command in ("help", "?"):
        print("Pose: grab [amount], open, pinch, point, peace, middle_finger, pinch_middle, pinch_ring, pinch_pinky")
        print("Targets: targets A0 A1 A2 A3 A4 A5 A6 (logical actuator degrees)")
        print("One motor: motor MOTOR_NAME ANGLE | hold | calibrate_zero | status | quit")
        print("Manual thumb in Hold: thumb_angle DEGREES | thumb_current (save measured angle)")
        return True
    if command == "thumb_angle":
        if len(words) != 2:
            raise ValueError("Use thumb_angle DEGREES")
        controller.adjust_thumb(float(words[1]))
        return True
    if command == "thumb_current":
        if len(words) != 1:
            raise ValueError("thumb_current takes no values")
        controller.adjust_thumb()
        return True
    if command == "calibrate_zero":
        if len(words) != 1:raise ValueError("calibrate_zero takes no values")
        controller.capture_baseline()
        return True
    if command == "status":
        print(controller.status())
        return True
    if command in {p["name"] for p in pose_menu()}:
        from .poses import pose_to_actuations
        if len(words) > 2:
            raise ValueError("Use a pose name and optional amount 0..1")
        amount = float(words[1]) if len(words) == 2 else 1.0
        controller.set_goal(command, pose_to_actuations(command, amount=amount,
                            motor_directions=controller.cfg["motor_directions"],
                            thumb_abduction=getattr(controller.hand,"thumb_abduction_lock",None)), amount=amount)
    elif command == "targets" and len(words) == 8:
        controller.set_goal("coordinates", [float(v) for v in words[1:]])
    elif command == "motor" and len(words) == 3 and words[1] in MOTOR_KEYS:
        if words[1] == "thumb_cmc_abd" and getattr(controller.hand,"thumb_abduction_lock",None) is not None:
            raise ValueError("Thumb abduction is locked. Select Hold and use thumb_angle or the Thumb setup panel.")
        goal = list(controller.goal)
        goal[MOTOR_KEYS.index(words[1])] = float(words[2])
        controller.set_goal("motor " + words[1], goal)
    else:
        raise ValueError("Unknown command or argument count. Type help.")
    return True


def _reader(commands: CommandInbox) -> None:
    try:
        while True:
            try:
                commands.put(input("aerohand> "))
            except ValueError as exc:
                print(f"Command rejected: {exc}")
    except EOFError:
        try:
            commands.put("quit")
        except ValueError:
            pass


def check_package(cfg: dict, brain_path: Path, *, offline: bool = False) -> None:
    from .design import design_summary, audit, design_dict
    data = json.loads(brain_path.read_text())
    audit(data)
    if data != design_dict():
        raise ValueError("Exported design differs from the generator; regenerate and rebuild")
    result = {"package_version": VERSION, "design": design_summary(data),
              "hardware_contacted": False, "sdk_validation": "not run (offline)"}
    if not offline:
        from ab import Brain
        from ab_sdk.design_submit import design_sha256
        for dist, expected in (("artificialbrains_sdk", "0.4.0"), ("aero-open-sdk", "0.1.0.dev1")):
            installed = importlib.metadata.version(dist)
            if installed != expected:
                raise ValueError(f"{dist} is {installed}; install requirements.txt ({expected})")
        brain = Brain.load(brain_path)
        warnings = brain.validate()
        if warnings:
            raise ValueError("SDK validation: " + "; ".join(warnings))
        from .poses import pose_names, get_pose, pose_to_actuations
        from .hand import AeroHandAdapter
        from aero_open_sdk.aero_hand_constants import AeroHandConstants
        from aero_open_sdk.actuations_to_joints import ActuationsToJointsModelCompact
        constants = AeroHandConstants()
        probe = AeroHandAdapter.__new__(AeroHandAdapter)  # NO serial constructor
        probe.limits = list(zip(constants.actuation_lower_limits, constants.actuation_upper_limits))
        probe._motor_directions = tuple(cfg["motor_directions"])
        probe._joint_limits = [(constants.joint_lower_limits[i], constants.joint_upper_limits[i])
                               for i in (0, 1, 2, 4, 7, 10, 13)]
        probe._inverse = ActuationsToJointsModelCompact()
        for name in pose_names():
            goal = pose_to_actuations(name, motor_directions=probe.motor_directions)
            recovered = probe.compact_joint_positions(goal)
            if any(abs(a-b) > .02 for a, b in zip(recovered, get_pose(name)["joint_degrees"])):
                raise ValueError(f"Installed vendor geometry does not round-trip pose {name!r}")
        fixed_demo = 17.0
        for name in pose_names():
            locked_goal = pose_to_actuations(name, motor_directions=probe.motor_directions,
                                            thumb_abduction=fixed_demo)
            if abs(locked_goal[0] - fixed_demo) > 1e-8:
                raise ValueError(f"Pose {name} overrides the manual thumb angle")
        opening = pose_to_actuations("open", motor_directions=probe.motor_directions)
        result.update(sdk_validation="passed", design_sha256=design_sha256(brain),
                      thumb_mapping="coupling-preserving native-frame round trip passed",
                      manual_thumb_lock="all presets preserve the selected abduction angle",
                      open_logical_actuators=opening,
                      open_hardware_actuators=probe.hardware_coordinates(opening))
    print(json.dumps(result, indent=2))


def pose_menu() -> list[dict]:
    # Menu labels do not need the vendor SDK or a hardware connection.
    poses = json.loads((ROOT / "poses.json").read_text())["poses"]
    order = ("open", "pinch", "grab", "point", "peace", "middle_finger", "pinch_middle", "pinch_ring", "pinch_pinky")
    labels = {"open": "Open hand", "middle_finger": "Middle finger"}
    return [{"name": key, "label": labels.get(key, key.replace("_", " ").title()),
             "description": poses[key].get("description", "")} for key in order]


def preview_ui(*, open_browser: bool, port: int) -> int:
    """Show operator controls without creating a hand or a neural session."""
    from .ui import LocalUI
    commands = CommandInbox()
    ui = LocalUI(pose_menu(), commands.put, port=port)
    state = {"mode": "preview", "version": VERSION, "status": "ready", "command": "hold",
             "step": -1, "device": "No device connected", "movement_gain":1.0,
             "message": "Interface preview - no hand or brain connected.", "error": None,
             "amount": 1.0, "tactile": {"fresh": True,
                 "adc": dict(zip(FINGERS, [120, 150, 180, 130, 160])),
                 "normalized": dict(zip(FINGERS, [v / 4095 for v in (120, 150, 180, 130, 160)])),
                 "baseline": dict.fromkeys(FINGERS, 0.0),
                 "directions": dict.fromkeys(FINGERS, 1),
                 "holds": dict.fromkeys(FINGERS, 0), "port": "Preview only", "age_s": 0.0}}
    state["thumb_setup"] = {"locked": True, "target": 17.0, "measured": 17.0,
                            "saved": None, "limits": [0,100], "can_adjust": True,
                            "source": "Preview only; no hardware or settings file"}
    state["status"] = "hold"
    ui.update(state)
    try:
        print("AeroHand interface preview: " + ui.start(open_browser=open_browser))
        while True:
            try:
                command = commands.get_nowait()
            except queue.Empty:
                time.sleep(0.05)
                continue
            if command == "quit":
                break
            words = shlex.split(command)
            if words[0] in ('thumb_angle', 'thumb_current'):
                t = state['thumb_setup']
                angle = float(words[1]) if words[0] == 'thumb_angle' else t['measured']
                t.update(target=angle, measured=angle, saved=angle)
                state.update(command='hold', status='hold',
                             message='Preview only: simulated fixed thumb angle; no motor command or saved file.')
                ui.update(state)
                continue
            if words[0] == 'calibrate_zero':
                t=state['tactile'];t['baseline']=dict(t['adc']);t['normalized']=dict.fromkeys(FINGERS,0.0)
                state.update(command='hold',status='hold',message='Preview only: simulated unloaded zero. No sensor file was written.')
                ui.update(state)
                continue
            state["command"] = "coordinates" if words[0] == "targets" else words[0]
            if words[0] == "targets":
                state["goal"] = [float(v) for v in words[1:]]
            state["amount"] = float(words[1]) if len(words) == 2 else 1.0
            state["status"] = "hold" if command == "hold" else "active"
            state["thumb_setup"]["can_adjust"] = state["status"] == "hold"
            ui.update(state)
    except KeyboardInterrupt:
        pass
    finally:
        state.update(status="stopped", message="Preview closed. No hardware was contacted.")
        ui.update(state)
        time.sleep(0.6)
        ui.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AeroHand: fourteen neural motor primitives on Prima-Lux")
    device = parser.add_mutually_exclusive_group()
    device.add_argument("--port", help="AeroHand USB port, for example /dev/ttyACM2 or COM3")
    device.add_argument("--serial-number", help="Resolve the hand's ESP32-S3 USB serial number, even if its port number changes")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--tactile-port", default="auto", help="Separate STM32 tactile serial port, or auto for a unique STM32 USB device")
    parser.add_argument("--brain", type=Path, default=ROOT / "brain.abdesign.json")
    parser.add_argument("--config", type=Path, default=(ROOT / "config.local.json" if (ROOT / "config.local.json").exists() else ROOT / "config.json"))
    parser.add_argument("--thumb-setup", type=Path, default=ROOT / "thumb_setup.json",
                        help="Saved manual thumb-abduction angle; missing file keeps current measured angle")
    parser.add_argument("--baseline", type=Path, default=ROOT / "tactile_baseline.json", help="Saved unloaded offsets, not object pressure targets")
    parser.add_argument("--log-dir", type=Path, default=ROOT / "logs", help="Directory for automatic per-run telemetry logs")
    parser.add_argument("--no-neuron-log", action="store_true", help="Keep input/output/motion logs, omit the separate full-neuron observation subscriber")
    parser.add_argument("--baudrate", type=int, default=921600)
    parser.add_argument("--no-startup-zero", action="store_true", help="Reuse saved unloaded zero instead of recapturing after startup OPEN")
    parser.add_argument("--gain", type=float, default=4.0, help="Explicit multiplier for degrees per output spike")
    parser.add_argument("--speed", type=int, help="Optional vendor position-speed setting for all motors (0..32766)")
    parser.add_argument("--torque", type=int, help="Optional vendor position-mode torque limit (0..1000)")
    parser.add_argument("--command", help="Select a coordinate pose, e.g. pinch; use --duration for a timed operator Hold")
    parser.add_argument("--duration", type=float, help="Seconds of active control before operator-style hold")
    parser.add_argument("--cli", action="store_true", help="Use terminal commands instead of the browser interface")
    parser.add_argument("--no-browser", action="store_true", help="Serve the interface and print its URL without opening a browser")
    parser.add_argument("--ui-port", type=int, default=0, help="Local interface port; 0 selects an available port")
    parser.add_argument("--observe-only", action="store_true", help="Use real hand feedback and neural outputs with all hand motion/settings writes disabled")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-offline", action="store_true", help="Check public graph structure without requiring SDK installation")
    modes.add_argument("--read-tactile", action="store_true", help="Print one current five-channel ADC sample; no hand or brain commands")
    modes.add_argument("--check", action="store_true", help="Check files and interfaces without contacting hardware")
    modes.add_argument("--list-commands", "--list-poses", dest="list_commands", action="store_true")
    modes.add_argument("--read-positions", action="store_true", help="Read the hand once without starting the brain or commanding motion")
    modes.add_argument("--preview-ui", action="store_true", help="Open the interface without contacting a hand or brain")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if not 0 <= args.ui_port <= 65535:
        parser.error("--ui-port must be from 0 to 65535")
    if args.preview_ui and (args.cli or args.command):
        parser.error("--preview-ui cannot be combined with --cli or --command")
    if args.observe_only and (args.speed is not None or args.torque is not None):
        parser.error("--observe-only disables motion/settings writes; omit --speed and --torque")
    if args.observe_only and (args.preview_ui or args.read_positions or args.check or args.list_commands):
        parser.error("--observe-only is for a live hand/brain observation session")
    if args.command:
        words = shlex.split(args.command)
        if not words or words[0] not in {p["name"] for p in pose_menu()}:
            parser.error("--command requires a pose name and optional amount")
    if not math.isfinite(args.gain) or args.gain <= 0:
        parser.error("--gain must be finite and positive")
    if args.duration is not None:
        if not math.isfinite(args.duration) or args.duration <= 0:
            parser.error("--duration must be finite and positive")
        cfg["command_timeout_s"] = args.duration
    if args.check or args.check_offline:
        check_package(cfg, args.brain, offline=args.check_offline)
        return 0
    if args.list_commands:
        show_poses()
        return 0
    if args.preview_ui:
        return preview_ui(open_browser=not args.no_browser, port=args.ui_port)
    if args.read_tactile:
        from .tactile import TactileReader, resolve_tactile_port
        reader = TactileReader(resolve_tactile_port(args.tactile_port))
        reader.start()
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                sample = reader.latest(cfg["tactile_max_age_s"])
                if sample:
                    print(json.dumps({"device_ms": sample.device_ms, "adc": dict(zip(FINGERS, sample.adc))}, indent=2))
                    return 0
                time.sleep(0.05)
            print("No complete tactile CSV sample received. " + (reader.error or "Check the separate sensor port."))
            return 1
        finally:
            reader.close()
    if not args.port and args.serial_number is None:
        parser.error("--port or --serial-number is required for hardware operations")

    # Missing sensor zero is valid on a fresh checkout; normal startup captures it.
    if args.no_startup_zero and not args.baseline.is_file():
        parser.error("--no-startup-zero requires a saved tactile_baseline.json; omit the flag for unloaded startup capture")
    from .runlog import RunRecorder, file_sha256
    recorder=RunRecorder(args.log_dir,metadata={'package_version':VERSION,'config':cfg,'gain':args.gain,
                        'speed_request':args.speed,'torque_request':args.torque,
                        'observe_only':args.observe_only,'brain_sha256':file_sha256(args.brain),
                        'adapter_file':str(ROOT/'aerohand_control/hand.py'),
                        'adapter_sha256':file_sha256(ROOT/'aerohand_control/hand.py'),
                        'adapter_target_lead_policy':'nonfatal_all_motors'},
                        max_mb=cfg['logging_max_mb'])
    (recorder.path/'brain.abdesign.json').write_bytes(args.brain.read_bytes())
    # Keep the physical pose reference alongside the graph for reproducible
    # startup diagnostics. Neither file contains credentials.
    (recorder.path/'poses-at-start.json').write_bytes((ROOT/'poses.json').read_bytes())
    print('Telemetry logs: '+str(recorder.path))
    from .hand import AeroHandAdapter, HandReadTimeout
    print("[AB][ADAPTER] target-lead policy: nonfatal for all motors", flush=True)
    print("[AB][ADAPTER] loaded file: " + str(ROOT / "aerohand_control/hand.py"), flush=True)
    hand = None
    neural = None
    controller = None
    ui = None
    tactile = None
    tactile_encoding = None
    commands = CommandInbox()
    hand_ready = False
    exit_code = 0
    device_label = args.serial_number or args.port
    last_message = "Connecting to the hand and brain..."
    error_message = None
    startup_state = {"state":"connecting", "confirmed":False}
    startup_positions = None
    thumb_saved_value = None

    def report(message: str) -> None:
        nonlocal last_message
        print(message)
        recorder.record("events","message",{"text":message})
        if " | step=" not in message:
            last_message = message

    def refresh_ui(status: str | None = None) -> None:
        if ui is None:
            return
        state = {"mode": "observe" if args.observe_only else "live", "version": VERSION, "device": device_label,
                 "status": status or (controller.phase.lower() if controller else
                           {"opening":"opening", "open":"starting", "zeroing":"zeroing", "zeroed":"starting"}.get(startup_state["state"], "starting")),
                 "message": last_message, "error": error_message, "command": "hold"}
        state['startup'] = dict(startup_state)
        if controller is None and startup_positions is not None:
            state.update(position=list(startup_positions), goal=list(startup_state.get('target') or startup_positions),
                         limits=list(hand.limits), command='open' if startup_state['state']=='opening' else 'hold',
                         input_ready=False, motor_writes_enabled=False,
                         baseline_capture={'active':startup_state['state']=='zeroing',
                                           'samples':startup_state.get('samples',0),'required':50})
        state['logging']=recorder.status()
        state['neuron_logging']=(neural.neuron_recorder.status() if neural is not None and neural.neuron_recorder is not None
                                 else {'status':'disabled' if args.no_neuron_log else 'starting'})
        state['movement_gain']=args.gain
        fixed = getattr(hand, "thumb_abduction_lock", None) if hand is not None else None
        feedback = (controller.position if controller is not None else startup_positions)
        state['thumb_setup'] = {
            'locked': fixed is not None, 'target': fixed,
            'measured': feedback[0] if feedback else None,
            'saved': controller.thumb_saved_value if controller is not None else thumb_saved_value,
            'limits': list(hand.limits[0]) if hand is not None else [0,100],
            'can_adjust': bool(controller is not None and controller.phase == 'HOLD'
                               and controller.input_ready and not controller.hold_pending
                               and not controller.baseline_capture and not args.observe_only),
            'source': 'manual axis; neural abduction commands are not applied' if fixed is not None
                      else 'read-only mode' if args.observe_only else 'waiting for hand feedback',
        }
        if controller is not None:
            state['diagnostics']=controller.diagnostics.snapshot()
            state['baseline_capture']={'active':controller.baseline_capture is not None,
                         'samples':len(controller.baseline_capture.samples) if controller.baseline_capture is not None else 0,
                         'required':50}
            state['serial_stats']={'reads':getattr(hand,'position_reads',0),'reused':getattr(hand,'position_reuses',0),
                                   'last_read_ms':getattr(hand,'last_read_duration_s',0)*1000}
            state.update(command=controller.name, amount=controller.amount,
                         goal=list(controller.goal), position=list(controller.position),
                         limits=list(hand.limits), step=controller.last_step,
                         input_ready=controller.input_ready,
                         input_drain_remaining=controller.input_drain_remaining,
                         input_rate_limit_hz=controller.acquisition.rate_hz(),
                         feedback_available=controller.feedback_available,
                         hold_pending=controller.hold_pending,
                         feedback_timeouts=controller.feedback_timeouts,
                         skipped_motor_events=controller.skipped_motor_events,
                         motor_writes_enabled=(not args.observe_only and controller.phase == "ACTIVE"
                                               and not controller.resume_pending and not controller.hold_pending
                                               and controller.feedback_available and controller.tactile_available))
            sample = tactile.latest() if tactile is not None else None
            if tactile_encoding is not None:
                state["tactile"] = tactile_encoding.status(sample)
                state["tactile"].update(port=tactile.port, error=tactile.error,
                    age_s=max(0.0, time.monotonic()-sample.received_at) if sample else None,
                    fresh=tactile.latest(cfg["tactile_max_age_s"]) is not None,
                    holds={finger: controller.last_raw.get(f"grip_{finger}_hold",0) for finger in FINGERS})
            if controller.last_step >= 0:
                state.update(last_deltas=list(controller.last_deltas),
                             motor_activity={f"{key}_{side}": controller.last_raw.get(f"{key}_{side}", 0)
                                             for key in MOTOR_KEYS for side in ("flex", "extend")},
                             grip_activity={finger: controller.last_raw.get(f"grip_{finger}_hold", 0)
                                            for finger in FINGERS})
        ui.update(state)

    def startup_progress(data: dict) -> None:
        nonlocal startup_positions
        startup_state.update(data)
        if data.get('positions') is not None:
            startup_positions = list(data['positions'])
        recorder.record('events', 'startup_' + data['state'], data)
        refresh_ui()

    def invalidate_operator_motion() -> None:
        if ui is not None:
            ui.invalidate_commands()
        commands.discard_pending_pose()

    def apply_startup_hold() -> None:
        if hand_ready and commands.take_hold():
            invalidate_operator_motion()
            if not args.observe_only:
                try:
                    hand.hold()
                except HandReadTimeout as exc:
                    # The initial hardware read has already succeeded. The
                    # controller's pending startup Hold will capture again;
                    # do not discard the operator's intent or abort the brain.
                    report(f"Operator Hold pending during startup; Hold was not sent because position feedback was unavailable: {exc}")
                else:
                    report("Operator hold command sent while the controller starts.")
            else:
                report("Inactive command selected during startup; motor writes disabled.")

    try:
        if not args.read_positions and not args.cli and not args.command:
            from .ui import LocalUI
            ui = LocalUI(pose_menu(), commands.put, port=args.ui_port)
            refresh_ui("starting")
            print("Artificial Brains hand controls: " + ui.start(open_browser=not args.no_browser))
        port = args.port
        if args.serial_number is not None:
            from .ports import resolve_serial_number
            port = resolve_serial_number(args.serial_number)
            print(f"Using AeroHand on {port} (USB serial {args.serial_number})")
        device_label = f"{port}" + (f" · {args.serial_number}" if args.serial_number else "")
        hand = AeroHandAdapter(port, baudrate=args.baudrate,
                               timeout_s=cfg["telemetry_timeout_s"],
                               startup_timeout_s=cfg["startup_timeout_s"],
                               motor_directions=cfg["motor_directions"],
                               max_target_lead_degrees=cfg["max_target_lead_degrees"],
                               feedback_reuse_s=cfg["feedback_reuse_s"])
        positions = hand.read_positions()
        startup_positions = positions[:]
        hand_ready = True
        if args.read_positions:
            print(json.dumps(dict(zip(MOTOR_KEYS, positions)), indent=2))
        elif not commands.stopping:
            if not args.observe_only:
                hand.configure_motion(speed=args.speed, torque=args.torque)
                from .poses import pose_to_actuations
                from .thumb_setup import load_thumb_setup
                thumb_saved_value = load_thumb_setup(args.thumb_setup, cfg['motor_directions'])
                fixed_thumb = positions[0] if thumb_saved_value is None else thumb_saved_value
                hand.set_thumb_abduction_lock(fixed_thumb)
                recorder.record('events','thumb_lock_initialized',{
                    'logical_degrees':fixed_thumb,'measured_degrees':positions[0],
                    'source':'current measured angle' if thumb_saved_value is None else 'saved operator setting'})
                report(f"Thumb abduction is fixed at {fixed_thumb:.2f} logical degrees. "
                       "Adjust it in the UI while in Hold; neural output cannot move this motor.")
                report("Opening the other six motors before starting the brain. Keep the hand empty and fingertip pads clear.")
                open_goal = pose_to_actuations('open', motor_directions=cfg['motor_directions'],
                                               thumb_abduction=fixed_thumb)
                report("Startup OPEN actuator targets (logical degrees): "
                       + ", ".join(f"{v:.2f}" for v in open_goal))
                report("Startup OPEN actuator targets (hardware degrees): "
                       + ", ".join(f"{v:.2f}" for v in hand.hardware_coordinates(open_goal)))
                positions = open_before_brain(
                    hand, open_goal,
                    timeout_s=cfg['startup_open_timeout_s'],
                    tolerance_degrees=cfg['startup_open_tolerance_degrees'],
                    cancelled=lambda: commands.urgent, progress=startup_progress,
                    confirm_indices=(1,2,3,4,5,6))
                report("Open position confirmed on the six controlled motors. "
                       "Thumb abduction remains at the manual setpoint and is not an arrival gate.")
            else:
                report("Observation mode: startup opening and automatic zeroing are skipped; no hand motion is sent.")
            from .tactile import TactileReader, TactileEncoding, resolve_tactile_port
            sensor_port = resolve_tactile_port(args.tactile_port, exclude=port)
            from .baseline import load_or_initial_baseline
            baseline=load_or_initial_baseline(args.baseline,cfg['tactile_directions'], require_saved=args.no_startup_zero)
            tactile_encoding = TactileEncoding(cfg["tactile_directions"],baseline=baseline['baseline'],
                                noise_deadband=baseline['noise_deadband'],source=baseline['source'])
            recorder.record('events','baseline_loaded',baseline)
            (recorder.path/'baseline-at-start.json').write_text(json.dumps(baseline,indent=2)+'\n')
            tactile = TactileReader(sensor_port,on_sample=lambda sample:recorder.record('sensors','tactile_raw',
                {'adc':list(sample.adc),'device_ms':sample.device_ms,'sequence':sample.sequence,
                 'received_at':sample.received_at}))
            tactile.start()
            report(f"Tactile board on {sensor_port}; five raw ADC channels at 115200 baud.")
            if not args.observe_only and not args.no_startup_zero:
                report("Zeroing unloaded fingertip offsets/noise with the hand open. This is not a grip-force target.")
                baseline = zero_before_brain(tactile, tactile_encoding, args.baseline,
                    cancelled=lambda: commands.urgent, progress=startup_progress)
                recorder.record('events', 'baseline_captured_at_startup', baseline)
                (recorder.path/'baseline-at-start.json').write_text(json.dumps(baseline,indent=2)+'\n')
                report("Unloaded zero captured. Starting the brain from the open hand posture.")
            apply_startup_hold()
            from .neural import NeuralRuntime
            if not commands.stopping:
                neural = NeuralRuntime(args.brain, args.env,
                                       per_spike_degrees=[v * args.gain for v in cfg["per_spike_degrees"]],
                                       max_event_age_s=cfg["max_output_age_s"],
                                       recorder=recorder,record_neurons=not args.no_neuron_log)
            apply_startup_hold()
            if neural is not None and not commands.stopping:
                neural.start()
            apply_startup_hold()
            # A Stop pressed during serial/session startup stays latched.
            if commands.stopping:
                running = False
            else:
                controller = HandController(hand, neural, cfg, emit=report,
                                            on_hold=invalidate_operator_motion,
                                            interrupt_requested=lambda: commands.urgent,
                                            observe_only=args.observe_only,
                                            initial_positions=positions,
                                            tactile=tactile, tactile_encoding=tactile_encoding,
                                            recorder=recorder,baseline_path=args.baseline,
                                            thumb_setup_path=args.thumb_setup,
                                            thumb_saved_value=thumb_saved_value)
                running = True
            print(f"AeroHand Prima-Lux v{VERSION}. Motor order: " + ", ".join(MOTOR_KEYS))
            if running:
                if args.observe_only:
                    report("Preparing the input stream from saved neural state. Observation mode keeps motor writes disabled; Stop saves neural dynamics.")
                elif controller.hold_pending:
                    report("Preparing the input stream from saved neural state. Hold is waiting for current position feedback; Stop saves neural dynamics.")
                else:
                    report("Preparing the input stream from saved neural state. Hold command sent while pending observations drain; Stop saves neural dynamics.")
            refresh_ui()
            disconnect_held = False
            initial_command = args.command
            if running and args.cli and not args.command:
                print("Enter pinch, grab, open, point, peace, middle_finger, hold, or help. Ctrl+C stops.")
                threading.Thread(target=_reader, args=(commands,), daemon=True, name="AeroHandCommands").start()
            while running:
                if ui is not None and ui.ever_connected:
                    disconnected = time.monotonic() - ui.last_seen > 3.0
                    if disconnected and not disconnect_held and not commands.stopping:
                        commands.put("hold")
                        disconnect_held = True
                        report("Browser connection lost; Hold requested. Select a command after reconnecting.")
                    elif not disconnected:
                        disconnect_held = False
                try:
                    command = commands.get_nowait()
                except queue.Empty:
                    command = None
                if command is not None:
                    try:
                        running = handle_command(command, controller)
                        if command in ("hold", "stop"):
                            initial_command = None
                    except (ValueError, KeyError) as exc:
                        report(f"Command rejected: {exc}")
                if not running:
                    break
                if (initial_command is not None and controller.input_ready
                        and controller.tactile.latest(cfg["tactile_max_age_s"]) is not None):
                    handle_command(initial_command, controller)
                    initial_command = None
                controller.tick()
                refresh_ui()
                if args.command and controller.completed_commands:
                    break
    except StartupCancelled as exc:
        report(str(exc))
    except KeyboardInterrupt:
        report("Stop requested.")
    except Exception as exc:
        exit_code = 1
        error_message = f"{type(exc).__name__}: {exc}"
        report(f"Stopped: {error_message}")
    finally:
        try:
            commands.put("quit")
        except ValueError:
            pass
        invalidate_operator_motion()
        refresh_ui("stopping")
        if hand is not None and hand_ready and not args.read_positions and not args.observe_only:
            if controller is not None:
                controller.phase = "HOLD"
                controller.name = "hold"
                controller.hold_pending = True
            try:
                held = hand.hold()
                if controller is not None:
                    controller._feedback_received(held)
                    controller.hold_pending = False
                report("Hand hold command sent.")
            except HandReadTimeout as exc:
                exit_code = 1
                if controller is not None:
                    controller.feedback_available = False
                    controller.feedback_timeouts += 1
                error_message = f"Hold was not sent: position feedback was unavailable. {type(exc).__name__}: {exc}"
                report(error_message)
            except Exception as exc:
                exit_code = 1
                error_message = f"Hand Hold failed; serial command delivery is unconfirmed: {type(exc).__name__}: {exc}"
                report(error_message)
        if neural is not None:
            report("Stopping brain and waiting for saved neural dynamics...")
            refresh_ui("stopping")
            try:
                neural.stop()
                if neural.stop_confirmed:
                    report("Brain stopped; neural dynamics preserved.")
            except Exception as exc:
                exit_code = 1
                error_message = f"Brain Stop was not confirmed: {type(exc).__name__}: {exc}"
                report(error_message)
        if tactile is not None:
            tactile.close()
        if hand is not None:
            try:
                hand.close()
            except Exception as exc:
                exit_code = 1
                error_message = f"Serial close failed: {type(exc).__name__}."
                report(error_message)
        if ui is not None:
            refresh_ui("error" if exit_code else "stopped")
            time.sleep(0.6)  # Let the browser receive the final saved/stopped status.
            ui.close()
        recorder.record('events','session_end',{'exit_code':exit_code,'error':error_message,
                        'diagnostics':controller.diagnostics.snapshot() if controller else None,
                        'serial_reads':getattr(hand,'position_reads',0),'serial_reuses':getattr(hand,'position_reuses',0),
                        'read_seconds':getattr(hand,'total_read_duration_s',0)})
        recorder.close()
        print('Telemetry saved: '+str(recorder.path))
    return exit_code

