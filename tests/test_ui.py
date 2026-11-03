"""Exercise the real loopback HTTP interface without hardware or a brain."""

from __future__ import annotations

import http.client
import json
import math
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from aerohand_control.ui import LocalUI, MAX_BODY_BYTES


POSES = [
    {"name": "open", "label": "Open", "description": "Open palm"},
    {"name": "middle_finger", "label": "Middle finger", "description": "Middle extended"},
]


class LocalUITests(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.ui = LocalUI(POSES, self.commands.append)
        self.ui.update({"status": "ready", "command": "hold", "mode": "preview"})
        self.url = self.ui.start(open_browser=False)
        self.parts = urlsplit(self.url)
        self.origin = f"{self.parts.scheme}://{self.parts.netloc}"

    def tearDown(self):
        self.ui.close()

    def request(self, route, *, method="GET", payload=None, raw=None, headers=None, absolute=False):
        connection = http.client.HTTPConnection(self.parts.hostname, self.parts.port, timeout=3)
        request_headers = {}
        if method == "POST":
            request_headers.update({"Origin": self.origin, "Content-Type": "application/json"})
        request_headers.update(headers or {})
        body = json.dumps(payload).encode("utf-8") if raw is None and payload is not None else raw
        path = route if absolute else self.parts.path + route
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def command(self, command, amount=None, **kwargs):
        payload = {"command": command}
        if amount is not None:
            payload["amount"] = amount
            payload["generation"] = json.loads(self.request("api/state")[2])["generation"]
        return self.request("api/command", method="POST", payload=payload, **kwargs)

    def test_binds_loopback_and_does_not_open_browser_when_disabled(self):
        self.assertEqual(self.parts.hostname, "127.0.0.1")
        self.assertGreater(self.parts.port, 0)
        self.assertFalse(self.ui.browser_opened)
        self.assertEqual(self.ui.start(open_browser=False), self.url)

    def test_browser_receives_exact_private_url_when_requested(self):
        other = LocalUI(POSES, self.commands.append)
        try:
            with patch("aerohand_control.ui.webbrowser.open", return_value=True) as opener:
                address = other.start()
                opener.assert_called_once_with(address, new=2)
                self.assertTrue(other.browser_opened)
        finally:
            other.close()

    def test_assets_and_json_are_same_origin_and_uncacheable(self):
        for route, mime in (("", "text/html"), ("app.js", "text/javascript"), ("style.css", "text/css"), ("api/state", "application/json")):
            with self.subTest(route=route):
                status, headers, body = self.request(route)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(mime))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertTrue(body)
        self.assertEqual(self.commands, [])

    def test_only_authenticated_state_poll_marks_browser_connected(self):
        self.assertFalse(self.ui.ever_connected)
        self.assertIsNone(self.ui.last_seen)
        self.request("")
        self.assertFalse(self.ui.ever_connected)
        self.request("/wrong/api/state", absolute=True)
        self.assertFalse(self.ui.ever_connected)
        before = time.monotonic()
        self.request("api/state")
        self.assertTrue(self.ui.ever_connected)
        self.assertGreaterEqual(self.ui.last_seen, before)

    def test_snapshot_is_copied_and_public_pose_metadata_is_present(self):
        snapshot = {"status": "ready", "position": [0.0] * 7, "message": "<script>text only</script>"}
        self.ui.update(snapshot)
        snapshot["position"][0] = 99
        _, _, body = self.request("api/state")
        state = json.loads(body)
        self.assertEqual(state["position"], [0.0] * 7)
        self.assertEqual(state["poses"], POSES)
        self.assertEqual(state["message"], "<script>text only</script>")
        self.assertGreater(state["revision"], 0)
        with self.assertRaises(ValueError):
            self.ui.update({"position": [math.nan]})
        self.assertEqual(json.loads(self.request("api/state")[2])["position"], [0.0] * 7)

    def test_pose_amount_and_hold_stop_reach_only_the_queue_callback(self):
        self.assertEqual(self.command("middle_finger", 0.75)[0], 202)
        self.assertEqual(self.command("open", 0.0)[0], 202)
        self.assertEqual(self.command("hold")[0], 202)
        self.assertEqual(self.command("quit")[0], 202)
        self.assertEqual(self.commands, ["middle_finger 0.75", "open 0", "hold", "quit"])

    def test_no_get_can_send_commands_or_escape_the_session(self):
        for route in ("api/command", "api/leave", "../config.json", "%2e%2e/config.json", "api/state?command=grab", "//outside"):
            self.assertEqual(self.request(route)[0], 404)
        self.assertEqual(self.request("/api/state", absolute=True)[0], 404)
        self.assertIn(self.request("http://outside.invalid/", absolute=True)[0], (403, 404))
        self.assertFalse(self.ui.ever_connected)
        self.assertEqual(self.commands, [])

    def test_hostname_rebinding_and_cross_origin_requests_are_rejected(self):
        for host in ("outside.invalid", "localhost", "127.0.0.1:80"):
            self.assertEqual(self.request("api/state", headers={"Host": host})[0], 403)
        for origin in ("http://outside.invalid", "null", ""):
            self.assertEqual(self.command("open", 1, headers={"Origin": origin})[0], 403)
        self.assertEqual(self.request("api/state", headers={"Origin": "http://outside.invalid"})[0], 403)
        connection = http.client.HTTPConnection(self.parts.hostname, self.parts.port, timeout=3)
        try:
            connection.request("POST", self.parts.path + "api/command", body=b'{"command":"hold"}', headers={"Content-Type":"application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            connection.close()
        self.assertEqual(self.commands, [])

    def test_invalid_or_nonfinite_amounts_and_unknown_fields_are_rejected(self):
        payloads = [
            {"command": "unknown", "amount": 1}, {"command": "middle_finger"},
            {"command": "middle_finger", "amount": -0.1}, {"command": "middle_finger", "amount": 1.1},
            {"command": "middle_finger", "amount": True}, {"command": "middle_finger", "amount": "1"},
            {"command": "middle_finger", "amount": 1, "motor": 2}, {"command": "hold", "amount": 1},
            ["open", 1], {"command": "open 1", "amount": 1}, {"command": 0},
        ]
        for payload in payloads:
            if isinstance(payload, dict) and "amount" in payload:
                payload.setdefault("generation", 0)
            with self.subTest(payload=payload):
                self.assertEqual(self.request("api/command", method="POST", payload=payload)[0], 409)
        for raw in (b'{"command":"open","amount":NaN}', b'{"command":"open","amount":Infinity}', b"not JSON", b"\xff"):
            self.assertEqual(self.request("api/command", method="POST", raw=raw)[0], 400)
        self.assertEqual(self.commands, [])

    def test_late_pose_cannot_reactivate_movement_after_hold_is_consumed(self):
        old = json.loads(self.request("api/state")[2])["generation"]
        self.assertEqual(self.command("hold")[0], 202)
        self.commands.clear()  # The controller has already consumed the hold.
        late = {"command": "middle_finger", "amount": 1, "generation": old}
        self.assertEqual(self.request("api/command", method="POST", payload=late)[0], 409)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.command("middle_finger", 1)[0], 202)
        self.assertEqual(self.commands, ["middle_finger 1"])

    def test_disconnect_or_duration_hold_invalidates_inflight_pose_requests(self):
        old = json.loads(self.request("api/state")[2])["generation"]
        current = self.ui.invalidate_commands()
        self.assertGreater(current, old)
        self.assertEqual(self.request("api/command", method="POST", payload={"command":"open","amount":1,"generation":old})[0], 409)
        self.assertEqual(self.commands, [])
        self.assertEqual(json.loads(self.request("api/state")[2])["generation"], current)

    def test_leave_and_stop_also_invalidate_old_requests(self):
        for route, payload in (("api/leave", {}), ("api/command", {"command":"quit"})):
            old = json.loads(self.request("api/state")[2])["generation"]
            status, _, body = self.request(route, method="POST", payload=payload)
            self.assertEqual(status, 202)
            self.assertGreater(json.loads(body)["generation"], old)
            self.assertEqual(self.request("api/command", method="POST", payload={"command":"open","amount":1,"generation":old})[0], 409)

    def test_pose_requires_an_exact_integer_generation(self):
        for generation in (None, True, 0.0, -1, "0", 999):
            self.assertEqual(self.request("api/command", method="POST", payload={"command":"open","amount":1,"generation":generation})[0], 409)
        self.assertEqual(self.commands, [])

    def test_body_and_content_type_are_bounded(self):
        self.assertEqual(self.request("api/command", method="POST", raw=b"x" * (MAX_BODY_BYTES+1))[0], 413)
        self.assertEqual(self.request("api/command", method="POST", raw=b"")[0], 413)
        self.assertEqual(self.command("hold", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.command("hold", headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.commands, [])

    def test_motion_is_unavailable_before_ready_or_after_stop(self):
        for status in ("starting", "stopping", "stopped", "error"):
            self.ui.update({"status": status})
            self.assertEqual(self.command("open", 1)[0], 409)
        self.assertEqual(self.commands, [])

    def test_browser_leave_holds_but_does_not_rearm_a_stopped_session(self):
        self.assertEqual(self.request("api/leave", method="POST", payload={})[0], 202)
        self.assertEqual(self.commands, ["hold"])
        self.ui.update({"status": "stopped"})
        self.assertEqual(self.request("api/leave", method="POST", payload={})[0], 202)
        self.assertEqual(self.commands, ["hold"])
        self.assertEqual(self.request("api/leave", method="POST", payload={"command": "open"})[0], 409)

    def test_pending_priority_command_error_is_reported_without_acceptance(self):
        def pending_hold(_line):
            raise ValueError("Hold is pending")
        self.ui._command_sink = pending_hold
        status, _, body = self.command("open", 1)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"error": "Hold is pending"})

    def test_no_grip_calibration_and_zero_requires_generation(self):
        for payload in ({"command":"calibrate_zero"},{"command":"calibrate_enough","finger":"index"}):
            self.assertEqual(self.request("api/command",method="POST",payload=payload)[0],409)
        html=self.request("")[2].decode()
        self.assertIn('id="capture-zero"',html)
        self.assertNotIn('Capture enough contact',html)
        self.assertEqual(self.commands,[])

    def test_coordinates_can_be_sent_without_calibration_metadata(self):
        generation=json.loads(self.request("api/state")[2])["generation"]
        p={"command":"targets","values":[20.]*7,"generation":generation}
        self.assertEqual(self.request("api/command",method="POST",payload=p)[0],202)
        self.assertEqual(self.commands,["targets " + " ".join(["20.0"]*7)])

    def test_invalid_configuration_is_rejected_before_a_server_opens(self):
        for port in (-1, 65536, True, 2.5):
            with self.assertRaises(ValueError):
                LocalUI(POSES, self.commands.append, port=port)
        for items in ([{"name":"quit"}], [{"name":"open"},{"name":"open"}], [{"name":"grab; stop"}]):
            with self.assertRaises(ValueError):
                LocalUI(items, self.commands.append)


if __name__ == "__main__":
    unittest.main()
