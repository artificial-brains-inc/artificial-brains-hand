"""Read AeroHand position packets alongside the firmware's startup text.

The published 0.1.0.dev1 SDK reads the next 16 bytes as a position packet.
Firmware also writes complete text lines to that stream.  This reader separates
those two formats without scanning through arbitrary binary bytes for a header.
It sends only GET_POS; motor commands remain in the vendor SDK.

The wire format has no checksum, request identifier, or servo sample timestamp.
Rejecting queued replies does not prove that the firmware's servo cache is fresh
and cannot distinguish an unexpectedly delayed earlier reply from a new one.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from typing import Any, Callable, Iterable


GET_POS_REQUEST = b"\x22" + bytes(15)
_HEADER = b"\x22\x00"
_FRAME_SIZE = 16
_MAX_LINE_BYTES = 2048
_MAX_TRANSACTION_BYTES = 65536


class ProtocolError(RuntimeError):
    """The serial stream cannot be used as a position observation."""


class ReplyTimeout(ProtocolError):
    """A complete reply did not arrive before the transaction deadline."""


class BootloaderError(ProtocolError):
    """The ESP32 ROM is waiting for a download instead of running the hand."""


class RestartError(ProtocolError):
    """The controller restarted after position feedback had been established."""


def _cause_text(exc: Exception) -> str:
    """Include useful OS diagnostics, never raw serial payloads."""
    cause = exc
    seen: set[int] = set()
    while id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, OSError):
            return f"{type(cause).__name__}: {cause}"
        if not isinstance(cause.__cause__, Exception):
            break
        cause = cause.__cause__
    return type(exc).__name__


class _Decoder:
    def __init__(self) -> None:
        self.buffer = bytearray()
        # pySerial's OS-level open can clear RX.  The first retained line can
        # therefore start halfway through UTF-8 text, even though this package
        # does not perform an additional input-buffer clear after opening.
        self.allow_initial_utf8_suffix = True

    def event(self) -> tuple[str, bytes | str] | None:
        if not self.buffer:
            return None
        if self.buffer[0] == _HEADER[0]:
            if len(self.buffer) < 2:
                return None
            if self.buffer[1] != _HEADER[1]:
                raise ProtocolError(
                    "Malformed GET_POS header: expected 0x22 0x00, "
                    f"received 0x22 0x{self.buffer[1]:02x}."
                )
            if len(self.buffer) < _FRAME_SIZE:
                return None
            frame = bytes(self.buffer[:_FRAME_SIZE])
            del self.buffer[:_FRAME_SIZE]
            self.allow_initial_utf8_suffix = False
            return "frame", frame

        newline = self.buffer.find(b"\n")
        size = len(self.buffer) if newline < 0 else newline + 1
        if size > _MAX_LINE_BYTES:
            raise ProtocolError(
                f"Firmware text exceeded {_MAX_LINE_BYTES} bytes without a valid line boundary."
            )
        fragment = self.buffer[:size]
        if any((byte < 32 and byte not in (9, 10, 13)) or byte == 127 for byte in fragment):
            raise ProtocolError(
                "Unexpected binary bytes outside a GET_POS packet; "
                "the position stream is not aligned."
            )
        if newline < 0:
            return None

        raw_line = bytes(self.buffer[:newline])
        del self.buffer[:newline + 1]
        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        if self.allow_initial_utf8_suffix:
            # Only the at-most-three continuation bytes of one clipped code
            # point are tolerated, only at the very start of the first line.
            offset = 0
            while offset < len(raw_line) and 0x80 <= raw_line[offset] <= 0xBF:
                offset += 1
            if offset > 3:
                raise ProtocolError("Invalid UTF-8 at the start of firmware text.")
            raw_line = raw_line[offset:]
        self.allow_initial_utf8_suffix = False
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("Firmware text is not a complete valid UTF-8 line.") from exc
        if any(not character.isprintable() and character != "\t" for character in line):
            raise ProtocolError("Unexpected control character in firmware text.")
        return "line", line

    def pending_description(self) -> str:
        if self.buffer[:1] == _HEADER[:1]:
            return f"truncated GET_POS packet ({len(self.buffer)} of {_FRAME_SIZE} bytes)"
        if self.buffer:
            return f"unfinished firmware text line ({len(self.buffer)} bytes without newline)"
        return "no complete GET_POS packet"


class PositionProtocol:
    """Bounded position reads, with a longer first-read deadline.

    A missing reply is transient. Preserve its partial frame and consume an
    available late reply on the next call before sending another request. If
    no reply bytes are pending, a later call may send one new GET_POS. There is
    no resend loop within a call and no cached-position fallback. Malformed
    streams and transport errors still fault the connection.

    With no outstanding timed-out request, queued complete packets are
    discarded before requesting a new observation.
    """

    def __init__(
        self,
        serial: Any,
        lower_limits: Iterable[float],
        upper_limits: Iterable[float],
        timeout_s: float = 0.15,
        startup_timeout_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        lock: Any | None = None,
    ):
        self.lower = tuple(float(value) for value in lower_limits)
        self.upper = tuple(float(value) for value in upper_limits)
        if (
            len(self.lower) != 7 or len(self.upper) != 7
            or any(not math.isfinite(value) for value in self.lower + self.upper)
            or any(lo >= hi for lo, hi in zip(self.lower, self.upper))
        ):
            raise ValueError("Position protocol requires seven finite actuator travel intervals.")
        if any(not math.isfinite(value) or value <= 0 for value in (timeout_s, startup_timeout_s)):
            raise ValueError("Position protocol deadlines must be positive and finite.")
        self.serial = serial
        self.timeout_s = float(timeout_s)
        self.startup_timeout_s = float(startup_timeout_s)
        self.clock = clock
        self._lock = lock if lock is not None else threading.RLock()
        self._decoder = _Decoder()
        self._ready = False
        self._reply_pending = False
        self._failure: ProtocolError | None = None
        self._bytes_read = 0
        self._last_line = ""

    def _waiting(self) -> int:
        try:
            count = self.serial.in_waiting
        except Exception as exc:
            raise ProtocolError(f"AeroHand serial status read failed: {_cause_text(exc)}.") from exc
        if not isinstance(count, int) or count < 0:
            raise ProtocolError("Serial transport returned an invalid queued-byte count.")
        return count

    def _receive(self, deadline: float) -> None:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ReplyTimeout(self._decoder.pending_description())
        count = min(self._waiting(), 4096) or 1
        try:
            self.serial.timeout = remaining
            data = self.serial.read(count)
        except Exception as exc:
            raise ProtocolError(f"AeroHand serial read failed: {_cause_text(exc)}.") from exc
        if not isinstance(data, bytes):
            raise ProtocolError("Serial transport returned non-byte position data.")
        self._bytes_read += len(data)
        if self._bytes_read > _MAX_TRANSACTION_BYTES:
            raise ProtocolError("Too much unsolicited serial data during one position request.")
        self._decoder.buffer.extend(data)
        if not data and self.clock() >= deadline:
            raise ReplyTimeout(self._decoder.pending_description())

    def _inspect_line(self, line: str) -> None:
        if not line:
            return
        self._last_line = line[-180:]
        lower = line.casefold()
        if "waiting for download" in lower or "download(usb/uart0)" in lower:
            raise BootloaderError(
                "ESP32-S3 is in ROM download mode; the AeroHand application is not running. "
                "Exit download mode using the documented USB recovery procedure. "
                "No automatic reset or firmware change was performed."
            )
        if self._ready and (
            "esp-rom:" in lower or "[boot]" in lower
            or ("rst:" in lower and "boot:" in lower)
        ):
            raise RestartError(
                "AeroHand controller restarted after feedback was established. "
                "Further commands are blocked on this connection; "
                "close and reconnect before continuing."
            )

    def _drain_queued(self, deadline: float, *, wait_for_partial: bool = True) -> None:
        while True:
            event = self._decoder.event()
            if event is not None:
                kind, value = event
                if kind == "line":
                    self._inspect_line(value)
                # An already-queued frame never satisfies a new request.
                continue
            waiting = self._waiting()
            if waiting or (self._decoder.buffer and wait_for_partial):
                # A completed observation must not become a missing-reply
                # timeout just because an unrelated trailing line is partial.
                # Leave that fragment for the next call to inspect.
                if not wait_for_partial and self.clock() >= deadline:
                    return
                self._receive(deadline)
                continue
            return

    def read_positions(self) -> list[float]:
        with self._lock:
            if self._failure is not None:
                raise ProtocolError(
                    "Position link is faulted; close and reconnect. " + str(self._failure)
                ) from self._failure
            budget = self.timeout_s if self._ready else self.startup_timeout_s
            stage = "Runtime" if self._ready else "Startup"
            deadline = self.clock() + budget
            self._bytes_read = 0
            self._last_line = ""
            bytes_before_request = 0
            request_sent = False
            continuing_reply = False
            retained_bytes = len(self._decoder.buffer)
            queued_at_entry = 0
            old_timeout = self.serial.timeout
            try:
                if self._reply_pending:
                    queued_at_entry = self._waiting()
                    continuing_reply = bool(retained_bytes or queued_at_entry)
                if not continuing_reply:
                    if not self._reply_pending:
                        self._drain_queued(deadline)
                    # A previously lost whole reply leaves nothing to finish.
                    # Allow one new query on this call. Do not drain again: an
                    # earlier reply could arrive between the check and write.
                    bytes_before_request = self._bytes_read
                    if self.clock() >= deadline:
                        raise ReplyTimeout("no time remained after draining queued serial data")
                    try:
                        count = self.serial.write(GET_POS_REQUEST)
                        if count != len(GET_POS_REQUEST):
                            raise ProtocolError(
                                f"GET_POS request write was incomplete ({count} of 16 bytes)."
                            )
                        self.serial.flush()
                        request_sent = True
                        self._reply_pending = True
                    except ProtocolError:
                        raise
                    except Exception as exc:
                        raise ProtocolError(f"AeroHand GET_POS write failed: {_cause_text(exc)}.") from exc

                while True:
                    event = self._decoder.event()
                    if event is None:
                        self._receive(deadline)
                        continue
                    kind, value = event
                    if kind == "line":
                        self._inspect_line(value)
                        continue
                    raw = struct.unpack("<2B7H", value)[2:]
                    self._ready = True
                    # Inspect text already following this reply before handing
                    # a position observation to the motor adapter.
                    self._drain_queued(deadline, wait_for_partial=False)
                    self._reply_pending = False
                    return [
                        lo + (value / 65535.0) * (hi - lo)
                        for value, lo, hi in zip(raw, self.lower, self.upper)
                    ]
            except ReplyTimeout as exc:
                detail = f" Last firmware text: {self._last_line}" if self._last_line else ""
                if continuing_reply:
                    queued_read = min(queued_at_entry, self._bytes_read)
                    transport = (
                        " No new GET_POS sent; continuing the previous request; "
                        f"retained {retained_bytes} bytes from the previous read; "
                        f"inspected {queued_read} bytes queued at call entry; "
                        f"received {self._bytes_read - queued_read} further bytes during this call."
                    )
                elif request_sent:
                    received = self._bytes_read - bytes_before_request
                    transport = (
                        f" GET_POS sent once; received {received} bytes after the request; "
                        f"inspected {bytes_before_request} queued bytes before it."
                    )
                else:
                    transport = (
                        " GET_POS was not sent; "
                        f"inspected {self._bytes_read} queued bytes before the request."
                    )
                if retained_bytes and not continuing_reply:
                    transport += (
                        f" At call entry, {retained_bytes} bytes remained from the previous read."
                    )
                failure = ReplyTimeout(
                    f"{stage} position read timed out after {budget:g}s: {exc}."
                    + transport + detail
                )
                raise failure from exc
            except ProtocolError as exc:
                self._failure = exc
                raise
            except (KeyboardInterrupt, SystemExit):
                self._failure = ProtocolError(
                    "Position transaction was interrupted and a reply may still be pending. "
                    "Close and reconnect before another request."
                )
                raise
            finally:
                try:
                    self.serial.timeout = old_timeout
                except Exception as exc:
                    # USB removal can also fail pySerial's port reconfiguration
                    # here. Preserve the more useful original read/write fault.
                    if self._failure is None:
                        failure = ProtocolError(
                            "AeroHand serial timeout restoration failed: "
                            f"{_cause_text(exc)}."
                        )
                        self._failure = failure
                        raise failure from exc
