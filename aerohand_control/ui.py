"""Loopback-only operator interface; HTTP threads never touch the hand.

The controller owns command ordering and all physical/neural I/O. This module
only validates operator requests, calls its queue callback, and serves snapshots.
"""

from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import webbrowser


ASSETS = Path(__file__).resolve().parent / "web"
MAX_BODY_BYTES = 1024
_POSE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


class LocalUI:
    """Serve one private UI session on 127.0.0.1.

    ``command_sink`` must enqueue work rather than perform hardware I/O. It is
    called with ``'<pose> <amount>'``, ``'hold'``, or ``'quit'`` and may raise
    ValueError when a higher-priority hold/stop is already pending. A browser
    leave notification queues hold; the controller can also use ``last_seen``
    and ``ever_connected`` for an independent polling-disconnect hold.
    """

    def __init__(self, poses: list[dict], command_sink: Callable[[str], None], *, port: int = 0):
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("UI port must be an integer between 0 and 65535")
        if not callable(command_sink):
            raise TypeError("command_sink must be callable")
        self._poses: list[dict[str, str]] = []
        self._pose_names: set[str] = set()
        for pose in poses:
            name = pose.get("name")
            if not isinstance(name, str) or not _POSE_NAME.fullmatch(name) or name in {"hold", "quit"}:
                raise ValueError("Each UI pose needs a distinct lowercase command name")
            if name in self._pose_names:
                raise ValueError("UI pose names must be distinct")
            label = pose.get("label", name.replace("_", " ").title())
            description = pose.get("description", "")
            if not isinstance(label, str) or not isinstance(description, str):
                raise ValueError("UI pose labels and descriptions must be text")
            self._poses.append({"name": name, "label": label, "description": description})
            self._pose_names.add(name)
        self._command_sink = command_sink
        self._port = port
        self._lock = threading.RLock()
        self._snapshot = {"status": "starting", "command": "hold", "mode": "live"}
        self._revision = 0
        self._generation = 0
        self._last_seen: float | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._token = secrets.token_urlsafe(32)
        self._base_path = f"/{self._token}/"
        self._origin = ""
        self.url = ""
        self.browser_opened = False

    @property
    def ever_connected(self) -> bool:
        with self._lock:
            return self._last_seen is not None

    @property
    def last_seen(self) -> float | None:
        with self._lock:
            return self._last_seen

    def update(self, snapshot: dict) -> None:
        """Replace the view atomically; reject non-JSON or nonfinite values."""
        if not isinstance(snapshot, dict):
            raise TypeError("UI snapshot must be a dictionary")
        # Serialization validates nested values before state becomes visible.
        clean = json.loads(json.dumps(snapshot, allow_nan=False))
        with self._lock:
            self._snapshot = clean
            self._revision += 1

    def invalidate_commands(self) -> int:
        """Invalidate pose requests already in transit after a controller hold.

        This is local operator-request ordering; it does not acknowledge neural
        execution or require any handshake in the brain.
        """
        with self._lock:
            self._generation += 1
            self._revision += 1
            return self._generation

    def _state(self) -> dict:
        with self._lock:
            self._last_seen = time.monotonic()
            return {
                **copy.deepcopy(self._snapshot),
                "poses": copy.deepcopy(self._poses),
                "revision": self._revision,
                "generation": self._generation,
            }

    def _submit(self, payload: object, *, leaving: bool = False) -> tuple[str, int]:
        if not isinstance(payload, dict):
            raise ValueError("A command must be a JSON object")
        if not leaving and payload.get("command") in {"thumb_angle", "thumb_current"}:
            with self._lock:
                command = payload["command"]
                keys = {"command", "generation", "angle"} if command == "thumb_angle" else {"command", "generation"}
                if (set(payload) != keys or type(payload.get("generation")) is not int
                        or payload["generation"] != self._generation):
                    raise ValueError("Thumb setup must use the current command generation")
                state = self._snapshot
                if state.get("mode") == "observe":
                    raise ValueError("Thumb setup cannot move hardware in observation mode")
                if (state.get("status") != "hold" or state.get("hold_pending")
                        or state.get("baseline_capture", {}).get("active")
                        or not state.get("thumb_setup", {}).get("can_adjust")):
                    raise ValueError("Select Hold before adjusting the thumb")
                if command == "thumb_angle":
                    from .thumb_setup import valid_angle
                    angle = valid_angle(payload.get("angle"), state["thumb_setup"].get("limits", (0,100)))
                    line = f"thumb_angle {angle:.6g}"
                else:
                    line = "thumb_current"
                self._command_sink(line)
                return line, self._generation
        if not leaving and payload.get("command") == "calibrate_zero":
            with self._lock:
                if set(payload) != {"command", "generation"} or type(payload.get('generation')) is not int or payload['generation'] != self._generation:
                    raise ValueError("Zeroing must use the current command generation")
                if self._snapshot.get('status') != 'hold' or self._snapshot.get('hold_pending') or self._snapshot.get('baseline_capture',{}).get('active'):
                    raise ValueError("Select Hold and keep all pads unloaded before zeroing")
                self._command_sink('calibrate_zero')
                return 'calibrate_zero',self._generation
        if not leaving and payload.get("command") == "targets":
            with self._lock:
                if self._snapshot.get("status", "starting") not in {"ready", "hold", "active"}:
                    raise ValueError("The controller is not ready")
                if (set(payload) != {"command", "values", "generation"}
                        or type(payload.get("generation")) is not int
                        or payload["generation"] != self._generation):
                    raise ValueError("Target request must use the current command generation")
                if self._snapshot.get("baseline_capture",{}).get("active"):
                    raise ValueError("Unloaded zeroing is in progress")
                values = payload["values"]
                if not isinstance(values, list) or len(values) != 7 or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                    raise ValueError("Supply seven finite actuator targets")
                line = "targets " + " ".join(str(v) for v in values)
                self._command_sink(line)
                return line, self._generation
        if leaving:
            if payload:
                raise ValueError("Leave requests must contain an empty object")
            command = "hold"
        else:
            command = payload.get("command")
            if not isinstance(command, str) or command not in self._pose_names | {"hold", "quit"}:
                raise ValueError("Unknown command")
            if command in {"hold", "quit"}:
                if set(payload) != {"command"}:
                    raise ValueError("Hold and stop do not accept an amount")
            else:
                if set(payload) != {"command", "amount", "generation"}:
                    raise ValueError("A pose requires a command, amount, and current command generation")
                amount = payload["amount"]
                if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or not 0 <= amount <= 1:
                    raise ValueError("Posture amount must be between 0 and 1")
                generation = payload["generation"]
                if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
                    raise ValueError("Invalid command generation")
        with self._lock:
            status = self._snapshot.get("status", "starting")
            if command in self._pose_names and self._snapshot.get("baseline_capture",{}).get("active"):
                raise ValueError("Unloaded zeroing is in progress")
            if command in self._pose_names and status not in {"ready", "active", "hold"}:
                raise ValueError("The controller is not ready to accept a pose")
            if leaving and status in {"stopping", "stopped", "error"}:
                return "hold", self._generation
            if command in self._pose_names and payload["generation"] != self._generation:
                raise ValueError("That pose request preceded a hold or stop; choose the pose again")
            if command in {"hold", "quit"}:
                self.invalidate_commands()
            line = command if command in {"hold", "quit"} else f"{command} {float(payload['amount']):.6g}"
            self._command_sink(line)
            return line, self._generation

    def start(self, *, open_browser: bool = True) -> str:
        """Start the server once and optionally open the user's browser."""
        if self._server is not None:
            return self.url
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ArtificialBrainsLocalUI"
            sys_version = ""

            def log_message(self, _format: str, *args) -> None:
                # Avoid logging the session token or noisy 5 Hz state polling.
                pass

            def _send(self, code: int, body: bytes, content_type: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, code: int, payload: dict) -> None:
                self._send(code, json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8")

            def _route(self, *, require_origin: bool) -> str | None:
                if self.headers.get_all("Host", []) != [owner._origin.removeprefix("http://")]:
                    self._json(403, {"error": "Invalid local host"})
                    return None
                origins = self.headers.get_all("Origin", [])
                if (require_origin and origins != [owner._origin]) or (origins and origins != [owner._origin]):
                    self._json(403, {"error": "Invalid request origin"})
                    return None
                target = urlsplit(self.path)
                if target.scheme or target.netloc or target.query or target.fragment or not target.path.startswith(owner._base_path):
                    self._json(404, {"error": "Unknown session"})
                    return None
                return target.path[len(owner._base_path):]

            def do_GET(self) -> None:
                route = self._route(require_origin=False)
                if route is None:
                    return
                if route == "api/state":
                    self._json(200, owner._state())
                    return
                assets = {
                    "": ("index.html", "text/html; charset=utf-8"),
                    "app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "style.css": ("style.css", "text/css; charset=utf-8"),
                    "brand.css": ("brand.css", "text/css; charset=utf-8"),
                    "assets/artificial-brains-logo.png": ("assets/artificial-brains-logo.png", "image/png"),
                }
                if route not in assets:
                    self._json(404, {"error": "Not found"})
                    return
                filename, content_type = assets[route]
                self._send(200, (ASSETS / filename).read_bytes(), content_type)

            def do_POST(self) -> None:
                route = self._route(require_origin=True)
                if route is None:
                    return
                if route not in {"api/command", "api/leave"}:
                    self._json(404, {"error": "Not found"})
                    return
                if self.headers.get("Transfer-Encoding") is not None:
                    self._json(400, {"error": "Transfer encoding is unsupported"})
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isdigit():
                    self._json(411, {"error": "A content length is required"})
                    return
                length = int(lengths[0])
                if not 0 < length <= MAX_BODY_BYTES:
                    self._json(413, {"error": "Request body is too large or empty"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._json(415, {"error": "Use an application/json request"})
                    return
                self.connection.settimeout(2.0)
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("Incomplete request body")
                    payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_nonfinite_json)
                except (UnicodeError, ValueError, TimeoutError, OSError):
                    self._json(400, {"error": "Invalid JSON request"})
                    return
                try:
                    command, generation = owner._submit(payload, leaving=route == "api/leave")
                except ValueError as exc:
                    self._json(409, {"error": str(exc)})
                    return
                self._json(202, {"accepted": True, "command": command, "generation": generation})

        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        self._server.daemon_threads = True
        actual_port = self._server.server_address[1]
        self._origin = f"http://127.0.0.1:{actual_port}"
        self.url = self._origin + self._base_path
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, name="aerohand-ui", daemon=True)
        self._thread.start()
        if open_browser:
            try:
                self.browser_opened = bool(webbrowser.open(self.url, new=2))
            except (OSError, webbrowser.Error):
                self.browser_opened = False
        return self.url

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)
