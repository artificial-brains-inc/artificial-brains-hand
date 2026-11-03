"""Public Prima-Lux I/O for the hand's independent motor primitives.

The host supplies desired and measured actuator coordinates, five normalized
tactile measurements on the fixed ADC scale. Neural outputs request
FLEX/EXTEND increments and per-finger grip holds. Python never computes the
position difference or a Boolean pressure-threshold decision.

An input HTTP response confirms queueing, not an exact neural execution step.
Likewise ``captured_at`` below is local receive time, not sensor capture time.
There is no acknowledgement circuit or neural acknowledgement gate. Temporary
observation gaps skip affected motor cycles; the existing stream-error Stop path
is retained.

SDK 0.4.0 pushes every completed neural timestep, including unchanged active
outputs and empty outputs. Each accepted step is decoded separately. The SDK
owns stream sequence/epoch validation and reconnects; an unrecoverable stream
error is a latched application fault, never a reason to replay a cached action.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time
from typing import Any, Mapping, Sequence

from .constants import INPUT_NEURONS, TACTILE_NEURONS, MOTOR_KEYS, OUTPUT_NEURONS
from .constants import FINGERS, DIAGNOSTIC_OUTPUTS, input_contract


class NeuralRuntimeError(RuntimeError):
    """An I/O failure that requires the application to hold the hand."""


@dataclass(frozen=True)
class DecodedFrame:
    step: int
    captured_at: float
    deltas: tuple[float, ...]
    raw: dict[str, int]
    transport: str


def _seven_finite(values: Sequence[float], label: str) -> tuple[float, ...]:
    if len(values) != len(MOTOR_KEYS):
        raise ValueError(f"{label} must contain exactly {len(MOTOR_KEYS)} values.")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError(f"{label} must contain finite numbers.")
    return tuple(float(value) for value in values)


def make_readings(
    goal: Sequence[float],
    measured: Sequence[float],
    limits: Sequence[Sequence[float]],
    tactile: Sequence[float],
) -> dict[str, Any]:
    """Scale two independent absolute positions for the SDK encoder.

    Limits are seven ``(low_degrees, high_degrees)`` pairs in MOTOR_KEYS order.
    The SDK's cumulative encoder includes its lowest bin at normalized zero;
    both desired and measured positions use that same unchanged convention.
    Goals must stay inside actuator travel. Measured positions may differ by
    0.02 degrees at a boundary, matching the hand adapter's telemetry tolerance;
    only that quantization margin maps to the nearest encoded endpoint.
    """
    desired = _seven_finite(goal, "Desired positions")
    actual = _seven_finite(measured, "Measured positions")
    if len(limits) != len(MOTOR_KEYS):
        raise ValueError("Provide one actuator range for each motor.")
    readings: dict[str, Any] = {}
    for index, key in enumerate(MOTOR_KEYS):
        bounds = limits[index]
        if len(bounds) != 2 or any(type(x) not in (int, float) or not math.isfinite(x) for x in bounds):
            raise ValueError(f"Invalid actuator range for {key}.")
        low, high = map(float, bounds)
        if low >= high:
            raise ValueError(f"Actuator range for {key} must increase.")
        for prefix, value in (("goal", desired[index]), ("position", actual[index])):
            tolerance = 0.02 if prefix == "position" else 0.0
            if not low - tolerance <= value <= high + tolerance:
                raise ValueError(f"{prefix}_{key} is outside its actuator range.")
            readings[f"{prefix}_{key}"] = {
                "signal": min(1.0, max(0.0, (value - low) / (high - low))),
                "mode": "positive_scalar_cumulative_population",
                "vmax": 1.0,
            }
    if len(tactile) != 5 or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in tactile):
        raise ValueError("Tactile observation needs five finite normalized values")
    for finger, value in zip(FINGERS, tactile):
        # A cumulative scalar emits a FULL first input for every positive value,
        # even 1 ADC count above unloaded zero. Preserve the amount instead:
        # fill complete cumulative bins and give the next bin its fraction.
        # No motor/force threshold, condition, or output veto is computed here.
        readings[f"touch_{finger}"] = {
            "signal": tactile_vector(float(value)), "mode": "vector_f32",
        }
    return readings


def tactile_vector(value: float, width: int = TACTILE_NEURONS) -> list[float]:
    """Zero-origin cumulative sensor encoding with a fractional last bin.

    Sum(vector) == width * value. Zero is silent. This represents one sensor,
    not a bank of externally calculated fuzzy conditions. The SDK's vector_f32
    mode preserves the supplied amplitudes. Neural circuitry chooses actions.
    """
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Tactile level must be finite and in [0,1]")
    if type(width) is not int or width < 1:
        raise ValueError("Tactile input width must be positive")
    level = width * value
    return [min(1.0, max(0.0, level - index)) for index in range(width)]


class OutputDecoder:
    """Validate public output representations, then use the SDK's decoders.

    Native counts may cover multiple spikes. The public SDK does not guarantee
    a count is bounded by an output's neuron count; preserve it as reported.
    Output widths constrain identified neuron indices only.
    """

    def __init__(self, per_spike_degrees: Sequence[float]):
        from ab_sdk.decoder import GenericSpikeDecoder
        from ab_sdk.primalux import ActuationChannelDecoder

        gains = _seven_finite(per_spike_degrees, "Degrees per spike")
        if any(gain <= 0 for gain in gains):
            raise ValueError("Degrees per spike must be positive for every motor.")
        self.motor_widths = {
            f"{key}_{side}": OUTPUT_NEURONS
            for key in MOTOR_KEYS for side in ("flex", "extend")
        }
        self.widths = dict(self.motor_widths)
        self.widths.update({name: OUTPUT_NEURONS for name in DIAGNOSTIC_OUTPUTS})
        channel_mapping: dict[str, dict[str, Any]] = {}
        raw_mapping: list[dict[str, Any]] = []
        for index, key in enumerate(MOTOR_KEYS):
            for side, sign in (("flex", 1), ("extend", -1)):
                name = f"{key}_{side}"
                gain = sign * gains[index]
                channel_mapping[name] = {"motor": key, "gain": gain}
                raw_mapping.append({
                    "output_id": name, "channel": key, "scheme": "addition",
                    "per_step_max": 1.0, "gain": gain, "n": OUTPUT_NEURONS,
                })
        self._channels = ActuationChannelDecoder(channel_mapping)
        self._spikes = GenericSpikeDecoder(raw_mapping)

    def _validate_counts(self, channels: Mapping[str, Any]) -> dict[str, int]:
        unknown = set(channels) - set(self.widths)
        if unknown:
            raise NeuralRuntimeError("Received output names outside this hand's I/O contract.")
        counts: dict[str, int] = {}
        for name in self.widths:
            value = channels.get(name, 0)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or int(value) != value or value < 0):
                raise NeuralRuntimeError(f"Invalid native output count for {name}.")
            counts[name] = int(value)
        return counts

    def _validate_spikes(self, outputs: list[Any]) -> dict[str, int]:
        active: dict[str, set[int]] = {name: set() for name in self.widths}
        for group in outputs:
            if not isinstance(group, dict) or not isinstance(group.get("neurons"), list):
                raise NeuralRuntimeError("Malformed neuron-identified output event.")
            for identifier in group["neurons"]:
                if not isinstance(identifier, str) or ":" not in identifier:
                    raise NeuralRuntimeError("Malformed output neuron identifier.")
                name, index_text = identifier.rsplit(":", 1)
                if name not in self.widths or not index_text.isascii() or not index_text.isdecimal():
                    raise NeuralRuntimeError("Output neuron does not belong to this hand's I/O contract.")
                index = int(index_text)
                if not 0 <= index < self.widths[name]:
                    raise NeuralRuntimeError(f"Output neuron index exceeds the width of {name}.")
                active[name].add(index)
        return {name: len(indices) for name, indices in active.items()}

    def decode(self, event: Mapping[str, Any], *, received_at: float | None = None) -> DecodedFrame:
        if not isinstance(event, Mapping) or type(event.get("step")) is not int or event["step"] < 0:
            raise NeuralRuntimeError("A neural output event needs a nonnegative integer step.")
        actuation = event.get("actuation")
        channels = actuation.get("channels") if isinstance(actuation, Mapping) else None
        outputs = event.get("outputs")
        if isinstance(channels, Mapping):
            raw = self._validate_counts(channels)
            from .body_commands import permitted_motor_counts
            allowed = permitted_motor_counts(raw)
            # Count outputs are authoritative when the device supplies them.
            # Do not convert their counts into invented neuron identities.
            # Grip-HOLD signals remain in ``raw`` for separate body-command translation. They are
            # absent from both the motor mapping and the SDK decoder's event. A neural HOLD
            # masks FLEX before addition so a simultaneous EXTEND is not cancelled.
            motor_event = dict(event)
            motor_event["actuation"] = {
                "channels": allowed,
            }
            command = self._channels.decode(motor_event, {})
            transport = "actuation.channels"
        elif isinstance(outputs, list):
            raw = self._validate_spikes(outputs)
            from .body_commands import permitted_motor_counts
            allowed = permitted_motor_counts(raw)
            motor_event = dict(event)
            motor_event["outputs"] = [
                {**group, "neurons": [identifier for identifier in group["neurons"]
                                      if allowed.get(identifier.rsplit(":", 1)[0], 0) > 0]}
                for group in outputs
            ]
            command = self._spikes.decode(motor_event, {})
            transport = "outputs.neurons"
        else:
            raise NeuralRuntimeError("Neural event has neither public counts nor neuron-identified outputs.")
        timestamp = time.monotonic() if received_at is None else received_at
        if not math.isfinite(timestamp):
            raise NeuralRuntimeError("Invalid output receive timestamp.")
        deltas = tuple(float(command["deltas"].get(key, 0.0)) for key in MOTOR_KEYS)
        if not all(math.isfinite(value) for value in deltas):
            raise NeuralRuntimeError("Decoded movement is not finite; check output counts and degree gains.")
        return DecodedFrame(
            step=event["step"], captured_at=float(timestamp),
            deltas=deltas,
            raw=raw, transport=transport,
        )


class OutputBuffer:
    """Bounded, fault-reporting queue; events stay separate and are used once."""

    def __init__(self, decoder: OutputDecoder, *, queue_limit: int = 512,
                 max_event_age_s: float = 1.0):
        if type(queue_limit) is not int or queue_limit < 1:
            raise ValueError("Output queue capacity must be positive.")
        if not math.isfinite(max_event_age_s) or max_event_age_s <= 0:
            raise ValueError("Maximum event age must be positive.")
        self.decoder = decoder
        self.queue_limit = queue_limit
        self.max_event_age_s = float(max_event_age_s)
        self._condition = threading.Condition()
        self._events: deque[DecodedFrame] = deque()
        self._error: str | None = None
        self.latest_step = -1
        self.last_received: float | None = None
        self.duplicate_events = 0
        self.gap_steps = 0

    def fault(self, reason: str) -> None:
        """Latch the first failure and wake a waiting consumer without I/O."""
        with self._condition:
            if self._error is None:
                self._error = str(reason) or "Neural output processing failed."
            self._condition.notify_all()

    def raise_if_faulted(self) -> None:
        with self._condition:
            if self._error is not None:
                raise NeuralRuntimeError(self._error)

    def establish_baseline(self, step: int) -> None:
        """Set a startup cursor before callbacks are registered, never mid-run."""
        if type(step) is not int or step < -1:
            raise ValueError("Invalid initial output cursor.")
        with self._condition:
            if self._events or self.last_received is not None:
                raise NeuralRuntimeError("Cannot change the output baseline after receiving live events.")
            self.latest_step = step

    def receive(self, event: Mapping[str, Any]) -> None:
        """Short SDK callback; no serial I/O and no exceptions escape its thread."""
        received_at = time.monotonic()
        try:
            if not isinstance(event, Mapping) or type(event.get("step")) is not int or event["step"] < 0:
                raise NeuralRuntimeError("Invalid neural output step.")
            with self._condition:
                if self._error is not None:
                    return
                if event["step"] <= self.latest_step:
                    self.duplicate_events += 1
                    return
                frame = self.decoder.decode(event, received_at=received_at)
                if len(self._events) >= self.queue_limit:
                    raise NeuralRuntimeError("Neural output queue overflow; hold the hand and restart the application.")
                if self.latest_step >= 0:
                    self.gap_steps += max(0, frame.step - self.latest_step - 1)
                self.latest_step = frame.step
                self.last_received = received_at
                self._events.append(frame)
                self._condition.notify_all()
        except Exception as exc:
            self.fault(str(exc))

    def poll(self, timeout: float = 0.05) -> list[DecodedFrame]:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Output poll timeout must be nonnegative.")
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._events and self._error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(remaining)
            if self._error is not None:
                raise NeuralRuntimeError(self._error)
            age = time.monotonic() - self._events[0].captured_at
            if age > self.max_event_age_s:
                self._error = f"Unconsumed neural output is {age:.3f}s old; hold the hand and restart the application."
                raise NeuralRuntimeError(self._error)
            result = list(self._events)
            self._events.clear()
            return result


def _event_baseline(payload: Mapping[str, Any]) -> int:
    if not isinstance(payload, Mapping):
        raise NeuralRuntimeError("Cannot establish initial neural output cursor.")
    if isinstance(payload.get("items"), list):
        items = payload["items"]
    elif isinstance(payload.get("events"), list):
        items = payload["events"]
    elif type(payload.get("step")) is int:
        items = [payload]
    elif payload.get("ok") is True and not (payload.get("items") or payload.get("events")):
        items = []
    else:
        raise NeuralRuntimeError("Unrecognized initial neural event response.")
    for item in items:
        if not isinstance(item, Mapping) or type(item.get("step")) is not int or item["step"] < 0:
            raise NeuralRuntimeError("Invalid step in initial neural event response.")
    return max((item["step"] for item in items), default=-1)


class NeuralRuntime:
    def __init__(self, design_path: str | Path, env_path: str | Path,
                 per_spike_degrees: Sequence[float], *, queue_limit: int = 512,
                 max_event_age_s: float = 1.0, stop_timeout_s: float = 30.0,
                 recorder=None, record_neurons: bool = True):
        if not math.isfinite(stop_timeout_s) or not 0 < stop_timeout_s <= 60:
            raise ValueError("Stop timeout must be greater than zero and at most 60 seconds.")
        self.design_path = Path(design_path).resolve()
        self.env_path = Path(env_path).expanduser().resolve()
        self._buffer = OutputBuffer(OutputDecoder(per_spike_degrees),
                                    queue_limit=queue_limit, max_event_age_s=max_event_age_s)
        self.recorder = recorder
        self.record_neurons = record_neurons
        self.neuron_recorder = None
        self.stop_timeout_s = float(stop_timeout_s)
        self._device: Any = None
        self._session: Any = None
        self.stop_confirmed = False

    @property
    def latest_step(self) -> int:
        return self._buffer.latest_step

    @property
    def last_received(self) -> float | None:
        return self._buffer.last_received

    @property
    def gap_steps(self) -> int:
        return self._buffer.gap_steps

    @property
    def duplicate_events(self) -> int:
        return self._buffer.duplicate_events

    def _receive(self, event):
        if self.recorder is not None:
            self.recorder.record('outputs','sdk_output',dict(event))
        self._buffer.receive(event)

    def _on_control(self, control: Mapping[str, Any]) -> None:
        """SDK callback: record a stream stop; let the main loop hold hardware."""
        if self.recorder is not None:
            self.recorder.record("events","sdk_control",control)
        if not isinstance(control, Mapping):
            self._buffer.fault("Malformed neural stream control event; hold the hand and restart the application.")
        elif control.get("command") == "stop" or control.get("reason") == "output_stream_error":
            detail = control.get("error") or control.get("reason") or "Stop requested by the output stream"
            self._buffer.fault(f"Neural output stream stopped: {detail}. Hold the hand and restart the application.")

    def _check_stream_health(self) -> None:
        # start_session starts its stream before returning. An error may have
        # occurred before on_control could be registered; the public error
        # field preserves that failure. It also covers a concurrent failure
        # immediately before a queued batch would otherwise be returned.
        if self._session is not None:
            error = self._session.output_stream.error
            if error is not None:
                self._buffer.fault(f"Neural output stream stopped: {error}. Hold the hand and restart the application.")
        self._buffer.raise_if_faulted()

    def start(self) -> None:
        from ab import Brain
        from ab_sdk.design_submit import design_contract, design_sha256
        from ab_sdk.primalux import PrimaLuxClient, PrimaLuxConfig

        if self._device is not None or self._session is not None:
            raise NeuralRuntimeError("Neural runtime is already started.")
        brain = Brain.load(self.design_path)
        contract = design_contract(brain)
        inputs = {entry["id"]: entry["n"] for entry in contract["inputs"]}
        outputs = {entry["id"]: entry["n"] for entry in contract["outputs"]}
        expected_inputs = input_contract()
        if inputs != expected_inputs or outputs != self._buffer.decoder.widths:
            raise NeuralRuntimeError("Brain input/output names or widths do not match this hand package.")
        config = PrimaLuxConfig.from_env(self.env_path)
        device = PrimaLuxClient.from_env(self.env_path)
        try:
            status = device.status()
            installed = ((status.get("package") or {}).get("active") or {}).get("design_sha256")
            if installed != design_sha256(brain):
                raise NeuralRuntimeError("Installed brain differs from this hand design. Build the supplied brain and wait for READY.")
            engine = status.get("engine") or {}
            if not engine.get("has_brain") or engine.get("state") not in ("READY", "STOPPED", "RUNNING"):
                raise NeuralRuntimeError("The matching brain is not ready to run.")
            if (status.get("package") or {}).get("pending"):
                raise NeuralRuntimeError("Wait for the brain installation to finish.")
        except Exception:
            device.close()
            raise
        self._device = device
        self.stop_confirmed = False
        try:
            self._session = device.start_session(
                contract, config=config, start_brain=engine["state"] != "RUNNING",
                telemetry_design=brain.to_dict(),
            )
            self._session.on_control(self._on_control)
            self._check_stream_health()
            # Ignore history already present before this application's first
            # live input, including replay delivered late by the SDK stream.
            # This is one startup snapshot, not a live-output polling loop.
            baseline = _event_baseline(device.events())
            self._check_stream_health()
            self._buffer.establish_baseline(baseline)
            self._session.on_output(self._receive)
            if self.recorder is not None and self.record_neurons:
                from .neuron_log import NeuronRecorder
                self.neuron_recorder = NeuronRecorder(config.url,config.api_key,self.recorder)
                self.neuron_recorder.start()
            self._check_stream_health()
        except Exception as startup_error:
            try:
                self.stop()
            except Exception as stop_error:
                raise NeuralRuntimeError(
                    f"Neural startup failed ({startup_error}); Stop also failed "
                    f"({stop_error}). Preservation of neural dynamics is unconfirmed."
                ) from startup_error
            raise

    def publish(self, goal: Sequence[float], measured: Sequence[float],
                limits: Sequence[Sequence[float]], tactile: Sequence[float]) -> dict[str, Any]:
        from ab_sdk.primalux import PrimaLuxError

        if self._session is None:
            raise NeuralRuntimeError("Start the neural runtime before publishing observations.")
        self._check_stream_health()
        current = _seven_finite(measured, "Measured positions")
        readings = make_readings(goal, current, limits, tactile)
        if self.recorder is not None:
            self.recorder.record('sensors','published_observation',
                                 {'goal':list(goal),'positions':list(current),'tactile':list(tactile),
                                  'readings':readings})
        try:
            result = self._session.publish_sensor_frame(readings)
        except PrimaLuxError as exc:
            # This exact native error is raised before the FIFO append. Other
            # HTTP failures and timeouts do not establish whether delivery
            # occurred, and must retain the SDK's original failure. Neither
            # case retries, substitutes a sample, or changes neural dynamics.
            if str(exc) != (
                "Prima-Lux HTTP 503: Native manager rejected request: "
                "External sensor queue overflow"
            ):
                raise
            message = (
                "Prima-Lux input FIFO is full: the new sensor snapshot was "
                "rejected and was not retried. The application will hold the "
                "hand (unless observing only) and request a confirmed "
                "Stop/save. Neural dynamics are not reset."
            )
            self._buffer.fault(message)
            raise NeuralRuntimeError(message) from exc
        if (not isinstance(result, dict) or result.get("ok") is not True
                or result.get("queued_for_hardware") is not True
                or result.get("atomic_frame") is not True):
            raise NeuralRuntimeError("Prima-Lux did not acknowledge this simultaneous observation; do not retry blindly.")
        if self.recorder is not None:
            self.recorder.record("events","input_submission",result)
        self._check_stream_health()
        return result

    def poll(self, timeout: float = 0.05) -> list[DecodedFrame]:
        self._check_stream_health()
        frames = self._buffer.poll(timeout)
        self._check_stream_health()
        return frames

    def stop(self, timeout_s: float | None = None) -> None:
        """Return successfully only after the device confirms neural Stop."""
        timeout = self.stop_timeout_s if timeout_s is None else timeout_s
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("Stop timeout must be greater than zero and at most 60 seconds.")
        device, session = self._device, self._session
        if device is None:
            return
        try:
            response = device.stop_and_wait(timeout=timeout)
            if (not isinstance(response, dict) or response.get("confirmed") is not True
                    or response.get("state") != "STOPPED"):
                raise NeuralRuntimeError("Neural Stop was not confirmed; preservation of neural dynamics is unconfirmed.")
            self.stop_confirmed = True
        finally:
            if self.neuron_recorder is not None:
                self.neuron_recorder.close()
                if self.recorder is not None:
                    self.recorder.record('events','neuron_recorder_final',self.neuron_recorder.status())
            # The physical controller holds the hand before requesting Stop.
            # Close locally even on a failed Stop, but never report it as saved.
            try:
                if session is not None:
                    try:
                        session.close_from_runtime(notify_node=False)
                    finally:
                        session.python_client.close()
            finally:
                try:
                    device.close()
                finally:
                    self._device = None
                    self._session = None
