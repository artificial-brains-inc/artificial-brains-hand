"""Transport timing behaviors; these tests do not model neural dynamics."""

import math
import unittest

from aerohand_control.input_flow import AcquisitionCadence


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class AcquisitionCadenceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cadence = AcquisitionCadence(20.0, clock=self.clock)

    def acquire_if_ready(self):
        if not self.cadence.ready():
            return False
        self.cadence.acquired()
        return True

    def test_no_output_receipts_means_no_acquisition(self):
        self.assertEqual(self.cadence.rate_hz(), 0)
        self.assertFalse(self.cadence.ready())
        self.clock.now += 60
        self.assertFalse(self.cadence.ready())

    def test_two_hz_receipts_limit_long_running_acquisition_to_one_hz(self):
        start = self.clock.now
        acquisitions = []
        # A transport clock advances; each receipt is just a completion notice.
        # No neuron, motor, or device behavior is simulated.
        for hundredth in range(6001):
            self.clock.now = start + hundredth / 100
            if hundredth % 50 == 0:
                self.cadence.observe(self.clock())
            if self.acquire_if_ready():
                acquisitions.append(self.clock())
        self.assertLessEqual(len(acquisitions), 61)
        self.assertGreater(len(acquisitions), 55)
        self.assertTrue(all(right - left >= 1.0 for left, right in zip(acquisitions, acquisitions[1:])))
        self.assertEqual(self.cadence.rate_hz(), 1.0)

    def test_receipt_burst_cannot_become_an_instant_input_burst(self):
        for _ in range(100):
            self.cadence.observe(self.clock())
        self.assertEqual(self.cadence.rate_hz(), 20.0)
        self.assertTrue(self.acquire_if_ready())
        self.assertFalse(any(self.acquire_if_ready() for _ in range(100)))
        self.clock.now += 0.049
        self.assertFalse(self.cadence.ready())
        self.clock.now += 0.002
        self.assertTrue(self.acquire_if_ready())
        self.assertFalse(self.cadence.ready())

    def test_receipts_expire_and_stall_stops_acquisition(self):
        for _ in range(40):
            self.cadence.observe(self.clock())
        self.assertTrue(self.acquire_if_ready())
        self.clock.now += 1.0
        self.assertEqual(self.cadence.rate_hz(), 0.0)
        self.assertFalse(self.cadence.ready())
        self.clock.now += 60
        self.assertFalse(self.cadence.ready())

    def test_long_gap_does_not_accumulate_catch_up_acquisitions(self):
        for _ in range(40):
            self.cadence.observe(self.clock())
        self.assertTrue(self.acquire_if_ready())
        self.clock.now += 100
        self.assertFalse(self.cadence.ready())
        self.cadence.observe(self.clock())
        self.assertTrue(self.acquire_if_ready())
        self.assertFalse(any(self.acquire_if_ready() for _ in range(100)))
        self.clock.now += 0.99
        self.assertFalse(self.cadence.ready())
        self.clock.now += 0.02
        self.assertFalse(self.cadence.ready())

    def test_receipt_slowdown_changes_cadence_without_resetting_budget(self):
        start = self.clock.now
        for _ in range(40):
            self.cadence.observe(start)
        self.assertTrue(self.acquire_if_ready())
        self.clock.now = start + 0.75
        self.cadence.observe(self.clock())
        self.assertTrue(self.acquire_if_ready())
        self.clock.now = start + 1.0
        self.assertEqual(self.cadence.rate_hz(), 0.5)
        self.assertFalse(self.cadence.ready())
        self.clock.now = start + 1.5
        self.cadence.observe(self.clock())
        self.assertEqual(self.cadence.rate_hz(), 1.0)
        self.assertFalse(self.cadence.ready())
        self.clock.now = start + 1.75
        self.cadence.observe(self.clock())
        self.assertTrue(self.acquire_if_ready())

    def test_configured_limit_and_twenty_hz_ceiling_are_both_respected(self):
        low = AcquisitionCadence(3, clock=self.clock)
        high = AcquisitionCadence(100, clock=self.clock)
        for _ in range(100):
            low.observe(self.clock())
            high.observe(self.clock())
        self.assertEqual(low.rate_hz(), 3.0)
        self.assertEqual(high.rate_hz(), 20.0)

    def test_acquisition_finishes_after_window_expires_without_replaying_it(self):
        self.cadence.observe(self.clock())
        self.assertTrue(self.cadence.ready())
        # A slow sensor read must still be recorded, even though admission was
        # granted before its receipt-throughput window became empty.
        self.clock.now += 1.1
        self.cadence.acquired()
        self.assertFalse(self.cadence.ready())

    def test_obsolete_receipts_do_not_create_current_throughput(self):
        self.clock.now += 5
        self.cadence.observe(self.clock() - 4)
        self.assertEqual(self.cadence.rate_hz(), 0)
        self.assertFalse(self.cadence.ready())

    def test_invalid_rates_or_nonfinite_times_fail_explicitly(self):
        for rate in (0, -1, True, math.inf, math.nan, "20"):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                AcquisitionCadence(rate)
        for timestamp in (True, math.inf, math.nan, "now"):
            for method in (self.cadence.observe, self.cadence.ready, self.cadence.rate_hz, self.cadence.acquired):
                with self.subTest(timestamp=timestamp, method=method.__name__), self.assertRaises(ValueError):
                    method(timestamp)

    def test_receipt_order_is_independent_of_later_polling_time(self):
        start = self.clock.now
        self.clock.now += 0.5
        self.cadence.observe(start)
        self.cadence.observe(start + 0.1)
        self.assertEqual(self.cadence.rate_hz(), 1)
        with self.assertRaisesRegex(ValueError, "monotonic order"):
            self.cadence.observe(start - 0.1)


if __name__ == "__main__":
    unittest.main()
