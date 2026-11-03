"""Position transport contracts exercised entirely with an in-memory serial port."""

from collections import deque
import errno
import struct
import unittest


from aerohand_control.protocol import (
    BootloaderError,
    PositionProtocol,
    ProtocolError,
    ReplyTimeout,
    RestartError,
)


GET_POS_REQUEST = b"\x22" + bytes(15)
LOWER = [-20.0, 0.0, -30.0, 0.0, 0.0, 0.0, 0.0]  # synthetic test ranges, not vendor calibration
UPPER = [100.0, 80.0, 90.0, 100.0, 100.0, 100.0, 100.0]


def frame(values):
    return struct.pack("<BB7H", 0x22, 0, *values)


def degrees(values):
    return [lo + value / 65535 * (hi - lo)
            for value, lo, hi in zip(values, LOWER, UPPER)]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += max(0.0, seconds)


class MemorySerial:
    """Deliver scheduled input without opening or emulating a real device.

    A response is either bytes or a list of (delay_after_write, bytes) events.
    ``read_limit`` produces short reads even when more bytes are available.
    """

    def __init__(self, clock, responses=(), initial=b"", read_limit=None):
        self.clock = clock
        self.responses = deque(responses)
        self.buffer = bytearray(initial)
        self.events = []
        self.read_limit = read_limit
        self.timeout = 0.01
        self.write_timeout = 0.01
        self.writes = []
        self.write_times = []
        self.flushes = 0
        self.short_write = False
        self.read_error = None
        self.write_error = None
        self.flush_error = None

    def schedule(self, delay, data):
        self.events.append((self.clock() + delay, bytes(data)))
        self.events.sort(key=lambda item: item[0])

    def _release(self):
        while self.events and self.events[0][0] <= self.clock():
            _, data = self.events.pop(0)
            self.buffer.extend(data)

    @property
    def in_waiting(self):
        self._release()
        return len(self.buffer)

    def read(self, size=1):
        if self.read_error is not None:
            raise self.read_error
        self._release()
        if not self.buffer:
            duration = max(float(self.timeout or 0), 0.001)
            if self.events:
                duration = min(duration, max(0, self.events[0][0] - self.clock()))
            self.clock.advance(duration)
            self._release()
        count = min(size, len(self.buffer))
        if self.read_limit is not None:
            count = min(count, self.read_limit)
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def write(self, data):
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(bytes(data))
        self.write_times.append(self.clock())
        if self.short_write:
            return len(data) - 1
        if self.responses:
            response = self.responses.popleft()
            events = [(0, response)] if isinstance(response, bytes) else response
            for delay, incoming in events:
                self.schedule(delay, incoming)
        return len(data)

    def flush(self):
        self.flushes += 1
        if self.flush_error is not None:
            raise self.flush_error

    def reset_input_buffer(self):
        raise AssertionError("A drain must inspect queued input, not erase it.")

    def reset_output_buffer(self):
        raise AssertionError("The position reader must not clear outgoing data.")

    def setDTR(self, value):
        raise AssertionError("The position reader must not send reset signals.")

    def setRTS(self, value):
        raise AssertionError("The position reader must not send reset signals.")


class RestoreFaultSerial(MemorySerial):
    """Model a disconnect when pySerial restores its original timeout."""

    def __init__(self, *args, **kwargs):
        self.fail_restore = False
        self.restore_attempts = 0
        super().__init__(*args, **kwargs)
        self.original_timeout = self.timeout
        self.fail_restore = True

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        if self.fail_restore and value == self.original_timeout:
            self.restore_attempts += 1
            raise OSError(errno.ENODEV, "timeout restore failed")
        self._timeout = value


class PositionProtocolTests(unittest.TestCase):
    def make_protocol(self, responses=(), initial=b"", read_limit=None,
                      timeout_s=0.05, startup_timeout_s=0.5,
                      serial_type=MemorySerial):
        clock = FakeClock()
        serial = serial_type(clock, responses, initial, read_limit)
        protocol = PositionProtocol(
            serial, LOWER, UPPER, timeout_s=timeout_s,
            startup_timeout_s=startup_timeout_s, clock=clock,
        )
        return protocol, serial, clock

    def assert_positions(self, actual, raw):
        self.assertEqual(len(actual), 7)
        for value, expected in zip(actual, degrees(raw)):
            self.assertAlmostEqual(value, expected, places=10)

    def test_vendor_uint16_scaling_including_endpoints(self):
        raw = [0, 65535, 1, 32768, 1024, 65000, 12345]
        protocol, serial, _ = self.make_protocol([frame(raw)])
        self.assert_positions(protocol.read_positions(), raw)
        self.assertEqual(serial.writes, [GET_POS_REQUEST])
        self.assertGreaterEqual(serial.flushes, 1)

    def test_frame_split_at_every_boundary(self):
        raw = [3, 45, 670, 8901, 12345, 23456, 54321]
        packet = frame(raw)
        for split in range(1, 16):
            with self.subTest(split=split):
                response = [(0, packet[:split]), (0.002, packet[split:])]
                protocol, serial, _ = self.make_protocol([response])
                self.assert_positions(protocol.read_positions(), raw)
                self.assertEqual(serial.writes, [GET_POS_REQUEST])

    def test_payload_delimiters_are_opaque(self):
        raw = [0x000A, 0x000D, 0x005B, 0x0022, 0x0A22, 0x2222, 0xFFFF]
        protocol, _, _ = self.make_protocol([frame(raw)], read_limit=1)
        self.assert_positions(protocol.read_positions(), raw)

    def test_boot_lines_and_split_utf8_before_reply(self):
        raw = [1200] * 7
        text = ("[BOOT] Hand Type: RIGHT_HAND\r\n"
                "[BOOT] Non-power reset → skipping homing\r\n").encode()
        protocol, serial, _ = self.make_protocol([text + frame(raw)], read_limit=1)
        self.assert_positions(protocol.read_positions(), raw)
        self.assertEqual(serial.writes, [GET_POS_REQUEST])

    def test_clipped_initial_log_prefix_can_finish(self):
        raw = [2100] * 7
        protocol, _, _ = self.make_protocol([
            b"\x86\x92 skipping homing\r\n" + frame(raw)
        ], read_limit=1)
        self.assert_positions(protocol.read_positions(), raw)

    def test_utf8_continuations_are_not_accepted_after_first_text_line(self):
        protocol, _, _ = self.make_protocol([
            b"[BOOT] Hand Type: RIGHT_HAND\r\n\x86\x92 other text\n" + frame([1] * 7)
        ])
        with self.assertRaises(ProtocolError):
            protocol.read_positions()

    def test_invalid_utf8_is_not_hidden_as_a_boot_log(self):
        protocol, _, _ = self.make_protocol([b"broken \xe2(\xa1\n" + frame([1] * 7)])
        with self.assertRaises(ProtocolError):
            protocol.read_positions()

    def test_stale_buffered_frame_cannot_satisfy_new_request(self):
        raw = [32000] * 7
        protocol, serial, _ = self.make_protocol(
            [frame(raw)], initial=frame([123] * 7), read_limit=3,
        )
        self.assert_positions(protocol.read_positions(), raw)
        self.assertEqual(serial.writes, [GET_POS_REQUEST])

    def test_partial_stale_frame_finishes_before_request_is_sent(self):
        stale = frame([1000] * 7)
        raw = [24000] * 7
        protocol, serial, _ = self.make_protocol([frame(raw)], initial=stale[:8])
        serial.schedule(0.02, stale[8:])
        self.assert_positions(protocol.read_positions(), raw)
        self.assertGreaterEqual(serial.write_times[0], 0.02)
        self.assertEqual(serial.writes, [GET_POS_REQUEST])

    def test_unfinished_stale_frame_blocks_request(self):
        protocol, serial, _ = self.make_protocol(
            [frame([25000] * 7)], initial=frame([1000] * 7)[:8],
        )
        with self.assertRaises(ProtocolError):
            protocol.read_positions()
        self.assertEqual(serial.writes, [])

    def test_duplicate_reply_is_discarded_before_next_request(self):
        first, duplicate, fresh = [100] * 7, [200] * 7, [300] * 7
        protocol, serial, _ = self.make_protocol([
            frame(first) + frame(duplicate), frame(fresh),
        ])
        self.assert_positions(protocol.read_positions(), first)
        self.assert_positions(protocol.read_positions(), fresh)
        self.assertEqual(serial.writes, [GET_POS_REQUEST, GET_POS_REQUEST])

    def test_startup_waits_for_delayed_response_without_retransmitting(self):
        raw = [27000] * 7
        protocol, serial, clock = self.make_protocol([[(0.3, frame(raw))]])
        self.assert_positions(protocol.read_positions(), raw)
        self.assertGreaterEqual(clock(), 0.3)
        self.assertEqual(serial.writes, [GET_POS_REQUEST])

    def test_startup_timeout_is_bounded_and_one_request_only(self):
        protocol, serial, clock = self.make_protocol([b""])
        with self.assertRaises(ReplyTimeout) as raised:
            protocol.read_positions()
        self.assertGreaterEqual(clock(), 0.5)
        self.assertLess(clock(), 0.7)
        self.assertEqual(serial.writes, [GET_POS_REQUEST])
        message = str(raised.exception)
        self.assertIn("GET_POS sent once", message)
        self.assertIn("received 0 bytes after the request", message)
        self.assertIn("inspected 0 queued bytes before it", message)

    def test_timeout_distinguishes_queued_bytes_from_reply_bytes(self):
        before = b"[BOOT] Hand Type: RIGHT_HAND\r\n" + frame([1000] * 7)
        after = b"[STATUS] awaiting servos\r\n" + frame([2000] * 7)[:7]
        protocol, serial, _ = self.make_protocol([after], initial=before)
        with self.assertRaises(ReplyTimeout) as raised:
            protocol.read_positions()
        message = str(raised.exception)
        self.assertIn("truncated GET_POS packet (7 of 16 bytes)", message)
        self.assertIn(f"received {len(after)} bytes after the request", message)
        self.assertIn(f"inspected {len(before)} queued bytes before it", message)
        self.assertIn("Last firmware text: [STATUS] awaiting servos", message)
        self.assertEqual(serial.writes, [GET_POS_REQUEST])

    def test_timeout_during_initial_drain_reports_no_request_sent(self):
        partial = frame([1000] * 7)[:8]
        protocol, serial, _ = self.make_protocol(initial=partial)
        with self.assertRaises(ReplyTimeout) as raised:
            protocol.read_positions()
        message = str(raised.exception)
        self.assertIn("GET_POS was not sent", message)
        self.assertIn("inspected 8 queued bytes before the request", message)
        self.assertEqual(serial.writes, [])

    def test_timeout_does_not_relabel_previous_read_text_as_current(self):
        protocol, serial, _ = self.make_protocol([
            b"[BOOT] previous transaction\r\n" + frame([1000] * 7), b"",
        ])
        protocol.read_positions()
        with self.assertRaises(ReplyTimeout) as raised:
            protocol.read_positions()
        message = str(raised.exception)
        self.assertIn("Runtime position read timed out", message)
        self.assertIn("received 0 bytes after the request", message)
        self.assertNotIn("previous transaction", message)
        self.assertNotIn("Last firmware text", message)
        self.assertEqual(serial.writes, [GET_POS_REQUEST, GET_POS_REQUEST])

    def test_runtime_timeout_uses_short_deadline_and_accepts_late_whole_reply(self):
        protocol, serial, clock = self.make_protocol([
            frame([1000] * 7), [(0.3, frame([2000] * 7))],
        ])
        protocol.read_positions()
        before = clock()
        with self.assertRaises(ReplyTimeout):
            protocol.read_positions()
        self.assertLess(clock() - before, 0.15)
        writes_before = len(serial.writes)
        clock.advance(0.5)
        self.assert_positions(protocol.read_positions(), [2000] * 7)
        self.assertEqual(len(serial.writes), writes_before)

    def test_late_fragment_completes_pending_reply_without_another_request(self):
        raw = [2000, 3000, 4000, 5000, 6000, 7000, 8000]
        packet = frame(raw)
        for split in range(1, 16):
            with self.subTest(split=split):
                protocol, serial, clock = self.make_protocol([
                    frame([1000] * 7),
                    [(0, packet[:split]), (0.07, packet[split:])],
                ])
                protocol.read_positions()
                with self.assertRaises(ReplyTimeout):
                    protocol.read_positions()
                self.assertAlmostEqual(clock(), 0.05)
                self.assert_positions(protocol.read_positions(), raw)
                self.assertEqual(serial.writes, [GET_POS_REQUEST, GET_POS_REQUEST])

    def test_late_partial_reply_queued_between_calls_is_finished_before_new_request(self):
        raw = [23000] * 7
        packet = frame(raw)
        protocol, serial, clock = self.make_protocol([
            frame([1000] * 7), [(0.06, packet[:8]), (0.08, packet[8:])],
        ])
        protocol.read_positions()
        with self.assertRaises(ReplyTimeout):
            protocol.read_positions()
        clock.advance(0.02)
        self.assert_positions(protocol.read_positions(), raw)
        self.assertEqual(serial.writes, [GET_POS_REQUEST, GET_POS_REQUEST])

    def test_whole_reply_lost_allows_one_new_request_on_next_call(self):
        raw = [32000] * 7
        protocol, serial, _ = self.make_protocol([
            frame([1000] * 7), b"", frame(raw),
        ])
        protocol.read_positions()
        with self.assertRaises(ReplyTimeout):
            protocol.read_positions()
        self.assert_positions(protocol.read_positions(), raw)
        self.assertEqual(serial.writes, [GET_POS_REQUEST] * 3)

    def test_repeated_missing_whole_replies_remain_bounded_one_request_per_call(self):
        protocol, serial, clock = self.make_protocol([frame([1000] * 7)])
        protocol.read_positions()
        for attempt in range(3):
            before = clock()
            with self.assertRaises(ReplyTimeout):
                protocol.read_positions()
            self.assertAlmostEqual(clock() - before, 0.05)
            self.assertEqual(serial.writes, [GET_POS_REQUEST] * (attempt + 2))

    def test_partial_reply_survives_multiple_timeouts_with_continuation_diagnostics(self):
        raw = [2000] * 7
        packet = frame(raw)
        protocol, serial, _ = self.make_protocol([
            frame([1000] * 7), packet[:7],
        ])
        protocol.read_positions()
        with self.assertRaises(ReplyTimeout):
            protocol.read_positions()
        serial.schedule(0, packet[7:9])
        serial.schedule(0.01, packet[9:10])
        with self.assertRaises(ReplyTimeout) as raised:
            protocol.read_positions()
        message = str(raised.exception)
        self.assertIn("truncated GET_POS packet (10 of 16 bytes)", message)
        self.assertIn("No new GET_POS sent; continuing the previous request", message)
        self.assertIn("retained 7 bytes from the previous read", message)
        self.assertIn("inspected 2 bytes queued at call entry", message)
        self.assertIn("received 1 further bytes during this call", message)
        self.assertEqual(serial.writes, [GET_POS_REQUEST] * 2)
        serial.schedule(0, packet[10:])
        self.assert_positions(protocol.read_positions(), raw)
        self.assertEqual(serial.writes, [GET_POS_REQUEST] * 2)

    def test_initial_queued_fragment_can_finish_after_timeout_before_new_query(self):
        stale, fresh = frame([1000] * 7), [2000] * 7
        protocol, serial, _ = self.make_protocol([frame(fresh)], initial=stale[:8])
        with self.assertRaises(ReplyTimeout):
            protocol.read_positions()
        self.assertEqual(serial.writes, [])
        serial.schedule(0, stale[8:])
        self.assert_positions(protocol.read_positions(), fresh)
        self.assertEqual(serial.writes, [GET_POS_REQUEST])

    def test_complete_reply_is_returned_while_trailing_status_line_is_partial(self):
        first, fresh = [1000] * 7, [2000] * 7
        protocol, serial, clock = self.make_protocol([
            frame(first) + b"[STATUS]", frame(fresh),
        ])
        self.assert_positions(protocol.read_positions(), first)
        self.assertEqual(clock(), 0)
        serial.schedule(0.01, b" telemetry ready\n")
        self.assert_positions(protocol.read_positions(), fresh)
        self.assertEqual(serial.writes, [GET_POS_REQUEST] * 2)

    def test_late_reply_recovery_still_faults_on_malformed_or_restart_data(self):
        for incoming, error in (
            (b"\x01" + frame([2000] * 7), ProtocolError),
            (b"ESP-ROM:esp32s3-20210327\r\n", RestartError),
            (b"waiting for download\r\n", BootloaderError),
        ):
            with self.subTest(incoming=incoming):
                protocol, serial, _ = self.make_protocol([frame([1000] * 7), b""])
                protocol.read_positions()
                with self.assertRaises(ReplyTimeout):
                    protocol.read_positions()
                serial.schedule(0, incoming)
                with self.assertRaises(error) as raised:
                    protocol.read_positions()
                self.assertNotIsInstance(raised.exception, ReplyTimeout)
                with self.assertRaises(ProtocolError):
                    protocol.read_positions()
                self.assertEqual(serial.writes, [GET_POS_REQUEST] * 2)

    def test_queued_bootloader_message_is_diagnosed_before_transmission(self):
        protocol, serial, _ = self.make_protocol(initial=(
            b"ESP-ROM:esp32s3-20210327\r\n"
            b"rst:0x15 (USB_UART_CHIP_RESET),boot:0x0 (DOWNLOAD(USB/UART0))\r\n"
            b"waiting for download\r\n"
        ))
        with self.assertRaises(BootloaderError):
            protocol.read_positions()
        self.assertEqual(serial.writes, [])

    def test_download_message_after_request_is_specific_error(self):
        protocol, _, _ = self.make_protocol([b"waiting for download\r\n"])
        with self.assertRaises(BootloaderError):
            protocol.read_positions()

    def test_runtime_reboot_followed_by_valid_packet_still_faults_and_latches(self):
        for text in (b"[BOOT] Hand Type: RIGHT_HAND\r\n",
                     b"ESP-ROM:esp32s3-20210327\r\n"):
            with self.subTest(text=text):
                protocol, serial, _ = self.make_protocol([
                    frame([1000] * 7), text + frame([2000] * 7),
                ])
                protocol.read_positions()
                with self.assertRaises(RestartError):
                    protocol.read_positions()
                writes_before = len(serial.writes)
                with self.assertRaises(ProtocolError):
                    protocol.read_positions()
                self.assertEqual(len(serial.writes), writes_before)

    def test_runtime_ordinary_status_line_does_not_hide_reply(self):
        raw = [2000] * 7
        protocol, _, _ = self.make_protocol([
            frame([1000] * 7), b"[STATUS] telemetry ready\r\n" + frame(raw),
        ])
        protocol.read_positions()
        self.assert_positions(protocol.read_positions(), raw)

    def test_malformed_input_is_not_blindly_scanned_for_valid_header(self):
        valid = frame([1000] * 7)
        malformed = (
            b"\x01" + valid,
            b"\x22\x01" + bytes(14) + valid,
            b"\x23\x00" + bytes(14) + valid,
            b"[BOOT] unfinished text" + valid,
            b"[STATUS] text\x00bad\n" + valid,
        )
        for response in malformed:
            with self.subTest(response=response):
                protocol, _, _ = self.make_protocol([response])
                with self.assertRaises(ProtocolError):
                    protocol.read_positions()

    def test_truncated_reply_fails_without_returning_partial_positions(self):
        for length in (1, 2, 8, 15):
            with self.subTest(length=length):
                protocol, _, _ = self.make_protocol([frame([1000] * 7)[:length]])
                with self.assertRaises(ProtocolError):
                    protocol.read_positions()

    def test_excessive_text_line_is_bounded(self):
        protocol, _, _ = self.make_protocol([b"A" * 2049 + b"\n" + frame([1] * 7)])
        with self.assertRaises(ProtocolError):
            protocol.read_positions()

    def test_partial_write_and_serial_errors_are_not_timeout_fallbacks(self):
        for fault in ("short_write", "read_error", "write_error", "flush_error"):
            with self.subTest(fault=fault):
                protocol, serial, _ = self.make_protocol([frame([1000] * 7)])
                setattr(serial, fault, True if fault == "short_write" else OSError("transport broke"))
                with self.assertRaises(ProtocolError) as captured:
                    protocol.read_positions()
                self.assertNotIsInstance(captured.exception, ReplyTimeout)

    def test_runtime_protocol_fault_latches_before_any_further_request(self):
        protocol, serial, _ = self.make_protocol([
            frame([1000] * 7), b"\x01" + frame([2000] * 7), frame([3000] * 7),
        ])
        protocol.read_positions()
        with self.assertRaises(ProtocolError):
            protocol.read_positions()
        writes_before = len(serial.writes)
        with self.assertRaises(ProtocolError):
            protocol.read_positions()
        self.assertEqual(len(serial.writes), writes_before)
        self.assertTrue(all(packet == GET_POS_REQUEST for packet in serial.writes))

    def test_timeout_restoration_failure_preserves_original_read_fault(self):
        protocol, serial, _ = self.make_protocol(
            [frame([1000] * 7)], serial_type=RestoreFaultSerial,
        )
        original = OSError(errno.EIO, "original read failure")
        serial.read_error = original
        with self.assertRaises(ProtocolError) as captured:
            protocol.read_positions()
        self.assertIn("original read failure", str(captured.exception))
        self.assertNotIn("timeout restore failed", str(captured.exception))
        self.assertIs(captured.exception.__cause__, original)
        self.assertEqual(serial.restore_attempts, 1)
        self.assertEqual(serial.writes, [GET_POS_REQUEST])
        with self.assertRaises(ProtocolError):
            protocol.read_positions()
        self.assertEqual(serial.writes, [GET_POS_REQUEST])

    def test_timeout_restoration_failure_rejects_valid_frame_and_latches(self):
        protocol, serial, _ = self.make_protocol(
            [frame([1000] * 7)], serial_type=RestoreFaultSerial,
        )
        with self.assertRaises(ProtocolError) as captured:
            protocol.read_positions()
        self.assertIn("timeout restoration failed", str(captured.exception))
        self.assertIn("timeout restore failed", str(captured.exception))
        self.assertEqual(serial.restore_attempts, 1)
        self.assertEqual(serial.writes, [GET_POS_REQUEST])
        with self.assertRaises(ProtocolError):
            protocol.read_positions()
        self.assertEqual(serial.writes, [GET_POS_REQUEST])

    def test_timeout_restoration_usb_fault_is_fatal_even_after_missing_reply(self):
        protocol, serial, _ = self.make_protocol([b""], serial_type=RestoreFaultSerial)
        with self.assertRaises(ProtocolError) as captured:
            protocol.read_positions()
        self.assertNotIsInstance(captured.exception, ReplyTimeout)
        self.assertIn("timeout restoration failed", str(captured.exception))
        self.assertEqual(serial.restore_attempts, 1)
        with self.assertRaises(ProtocolError):
            protocol.read_positions()
        self.assertEqual(serial.writes, [GET_POS_REQUEST])

    def test_wrapped_write_oserror_keeps_useful_cause_and_latches(self):
        protocol, serial, _ = self.make_protocol([frame([1000] * 7)])
        original = OSError(errno.ENODEV, "USB disconnected")
        wrapped = RuntimeError("guarded serial write failed")
        wrapped.__cause__ = original
        serial.write_error = wrapped
        with self.assertRaises(ProtocolError) as captured:
            protocol.read_positions()
        self.assertIn("USB disconnected", str(captured.exception))
        self.assertIn(f"Errno {errno.ENODEV}", str(captured.exception))
        self.assertIs(captured.exception.__cause__, wrapped)
        writes_before = len(serial.writes)
        with self.assertRaises(ProtocolError):
            protocol.read_positions()
        self.assertEqual(len(serial.writes), writes_before)

    def test_interrupted_read_latches_before_another_request(self):
        protocol, serial, _ = self.make_protocol([frame([1000] * 7)])
        serial.read_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            protocol.read_positions()
        self.assertEqual(serial.writes, [GET_POS_REQUEST])
        with self.assertRaises(ProtocolError) as captured:
            protocol.read_positions()
        self.assertIn("interrupted", str(captured.exception))
        self.assertEqual(serial.writes, [GET_POS_REQUEST])


if __name__ == "__main__":
    unittest.main()
