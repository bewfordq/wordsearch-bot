"""The dashboard: a small web server (standard library only) with the live phone picture, the bot's state,
and every control. Open http://127.0.0.1:8765 once `python wordbot.py` is running."""
from __future__ import annotations

import json
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2

from .bot import Bot
from .config import TEMPLATES

STATIC = Path(__file__).resolve().parent / "static"
BOUNDARY = "wordbotframe"


class _Frames:
    """JPEG of the newest phone picture, encoded once however many viewers there are."""

    def __init__(self, bot: Bot, width: int = 540):
        self.bot = bot
        self.width = width
        self._lock = threading.Lock()
        self._key = None
        self._jpeg: bytes | None = None

    def get(self) -> tuple[bytes | None, float]:
        img, t = self.bot.device.live_frame()
        if img is None:
            return None, 0.0
        with self._lock:
            if self._key != (id(img), t):
                h, w = img.shape[:2]
                small = cv2.resize(img, (self.width, round(h * self.width / w)), interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 72])
                if ok:
                    self._jpeg, self._key = buf.tobytes(), (id(img), t)
            return self._jpeg, t


def make_handler(bot: Bot, frames: _Frames, max_fps: float = 30.0):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep the terminal for the bot's own log
            pass

        # --- helpers ------------------------------------------------------------------

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return {}

        def _file(self, path: Path) -> None:
            if not path.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self._send(200, path.read_bytes(), ctype)

        # --- GET --------------------------------------------------------------------

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                return self._file(STATIC / "index.html")
            if url.path.startswith("/static/"):
                p = (STATIC / url.path[len("/static/"):]).resolve()
                if STATIC not in p.parents:
                    return self._send(403, b"no", "text/plain")
                return self._file(p)
            if url.path == "/api/state":
                since = int(q.get("since", ["0"])[0] or 0)
                return self._json(bot.snapshot(since))
            if url.path == "/frame.jpg":
                jpeg, _ = frames.get()
                if jpeg is None:
                    return self._send(204, b"", "image/jpeg")
                return self._send(200, jpeg, "image/jpeg")
            if url.path == "/stream.mjpg":
                return self._mjpeg(min(max_fps, float(q.get("fps", ["12"])[0])))
            if url.path == "/api/templates":
                return self._json(sorted(p.name for p in TEMPLATES.glob("*.png")))
            if url.path.startswith("/templates/"):
                p = (TEMPLATES / Path(url.path).name).resolve()
                return self._file(p)
            return self._send(404, b"not found", "text/plain")

        def _mjpeg(self, fps: float) -> None:
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            last = None
            period = 1.0 / max(1.0, min(30.0, fps))
            try:
                while True:
                    t0 = time.time()
                    jpeg, t = frames.get()
                    if jpeg is not None and t != last:
                        last = t
                        self.wfile.write(f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                                         f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    time.sleep(max(0.01, period - (time.time() - t0)))
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        # --- POST -------------------------------------------------------------------

        def do_POST(self):
            url = urlparse(self.path)
            body = self._body()
            if url.path == "/api/control":
                action = body.get("action")
                fn = {"start": bot.start, "pause": bot.pause, "resume": bot.resume, "step": bot.step,
                      "stop": bot.stop}.get(action)
                if fn is None:
                    return self._json({"error": f"unknown action {action}"}, 400)
                fn()
                return self._json({"ok": True, "status": bot.status})
            if url.path == "/api/cmd":
                name = body.get("name")
                if not name:
                    return self._json({"error": "missing name"}, 400)
                bot.command(name, body.get("args") or {})
                return self._json({"ok": True})
            if url.path == "/api/config":
                data = bot.cfg.update(body.get("changes") or {})
                _apply_device_settings(bot, body.get("changes") or {})
                return self._json({"ok": True, "config": data})
            if url.path == "/api/config/reset":
                return self._json({"ok": True, "config": bot.cfg.reset()})
            return self._send(404, b"not found", "text/plain")

    return Handler


def _apply_device_settings(bot: Bot, changes: dict) -> None:
    """Device settings take effect on the next connect; reconnect now if the stream choice changed."""
    dev = changes.get("device") or {}
    bot.device.cfg = bot.cfg["device"]
    if dev and bot.device.serial is not None:
        bot.command("connect")


def serve(bot: Bot, host: str = "127.0.0.1", port: int = 8765, max_fps: float = 30.0) -> ThreadingHTTPServer:
    """max_fps caps the live picture (the Android app keeps it low: each frame costs the game CPU)."""
    frames = _Frames(bot)
    server = ThreadingHTTPServer((host, port), make_handler(bot, frames, max_fps))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    return server
