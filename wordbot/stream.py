"""Live phone screen and touches over scrcpy, instead of one `screencap` / `input` command per action.

scrcpy's server (a small Java program, run on the phone with app_process) encodes the screen as H.264 with
the phone's hardware encoder and streams it over an adb tunnel; a background thread here decodes it with
PyAV and keeps the newest frame. A second socket carries touch events the other way. So a screenshot is
the newest frame (tens of ms old) instead of a ~350 ms screencap, and a tap is a 32-byte message instead
of starting `input` on the phone (~70 ms). Drags can pause before lifting, so the game sees the finger come to rest.

Protocol (scrcpy 3.3.x, see doc/develop.md in scrcpy): with a forward tunnel the first socket receives a
dummy byte, then the 64-byte device name; the video socket then gets 12 bytes of codec metadata and, per
packet, a 12-byte header (config flag, key frame flag, 62-bit PTS; u32 size). All integers big-endian.
Client and server versions must match exactly, so the server file is pinned in data/.
"""
from __future__ import annotations

import random
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
PINNED = sorted((ROOT / "data").glob("scrcpy-server-*"))
SYSTEM_SERVER = Path("/usr/share/scrcpy/scrcpy-server")
DEVICE_PATH = "/data/local/tmp/wordbot-scrcpy-server.jar"

TYPE_INJECT_TOUCH_EVENT = 2
ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
POINTER_ID_GENERIC_FINGER = -2  # injected as a finger on the touchscreen


class StreamError(RuntimeError):
    pass


def server_file() -> tuple[Path, str]:
    """(server jar, its version). The copy in data/ wins: an apt upgrade of scrcpy mustn't break the bot."""
    for p in reversed(PINNED):
        m = re.search(r"scrcpy-server-(\d+\.\d+(?:\.\d+)?)$", p.name)
        if m:
            return p, m.group(1)
    if SYSTEM_SERVER.exists() and shutil.which("scrcpy"):
        out = subprocess.run(["scrcpy", "--version"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"scrcpy (\d+\.\d+(?:\.\d+)?)", out)
        if m:
            return SYSTEM_SERVER, m.group(1)
    raise StreamError("no scrcpy server found (install scrcpy, or put scrcpy-server-<version> in data/)")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise StreamError("stream closed")
        buf += chunk
    return bytes(buf)


def touch_message(action: int, x: int, y: int, width: int, height: int, pressure: float = 1.0) -> bytes:
    """scrcpy's 32-byte touch event (see app/tests/test_control_msg_serialize.c)."""
    p = 0xFFFF if pressure >= 1.0 else int(max(0.0, pressure) * 0x10000)
    return struct.pack(">BBqiiHHHII", TYPE_INJECT_TOUCH_EVENT, action, POINTER_ID_GENERIC_FINGER,
                       int(x), int(y), width, height, p, 0, 0)


class Stream:
    def __init__(self, cfg: dict | None = None, adb: str = "adb"):
        c = cfg or {}
        self.adb = adb
        self.bit_rate = int(c.get("bit_rate", 16_000_000))
        self.max_fps = int(c.get("max_fps", 60))
        self.fresh_ms = float(c.get("fresh_ms", 60))
        self.stay_awake = bool(c.get("stay_awake", True))
        self.proc: subprocess.Popen | None = None
        self.video: socket.socket | None = None
        self.control: socket.socket | None = None
        self.port: int | None = None
        self.size: tuple[int, int] = (0, 0)  # video width, height
        self.error: str | None = None
        self._cond = threading.Condition()
        self._frame = None          # newest decoded av.VideoFrame
        self._seq = 0               # frames decoded so far
        self._time = 0.0            # arrival time of the newest frame
        self._img_cache: tuple[int, np.ndarray] | None = None
        self._send_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopping = False

    # --- lifecycle -------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self.error is None

    def _adb(self, *args: str, timeout: float = 15) -> subprocess.CompletedProcess:
        return subprocess.run([self.adb, *args], capture_output=True, timeout=timeout)

    def start(self, timeout: float = 10.0) -> "Stream":
        try:
            import av  # noqa: F401  (PyAV decodes the H.264 stream)
        except ImportError as e:
            raise StreamError("PyAV is not installed (pip install av)") from e
        jar, version = server_file()
        r = self._adb("push", str(jar), DEVICE_PATH, timeout=30)
        if r.returncode != 0:
            raise StreamError(f"couldn't push the scrcpy server: {r.stderr.decode(errors='replace').strip()}")
        scid = random.randrange(1, 1 << 31)
        name = f"scrcpy_{scid:08x}"
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        r = self._adb("forward", f"tcp:{self.port}", f"localabstract:{name}")
        if r.returncode != 0:
            raise StreamError(f"adb forward failed: {r.stderr.decode(errors='replace').strip()}")
        args = [f"scid={scid:08x}", "log_level=warn", "tunnel_forward=true", "audio=false", "control=true",
                "video_codec=h264", "max_size=0", f"max_fps={self.max_fps}", f"video_bit_rate={self.bit_rate}",
                f"stay_awake={'true' if self.stay_awake else 'false'}", "power_on=false", "clipboard_autosync=false", "cleanup=true"]
        self.proc = subprocess.Popen(
            [self.adb, "shell", f"CLASSPATH={DEVICE_PATH}", "app_process", "/", "com.genymobile.scrcpy.Server",
             version, *args],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            self._connect(timeout)
        except Exception:
            self.stop()
            raise
        self._thread = threading.Thread(target=self._read_loop, name="scrcpy-video", daemon=True)
        self._thread.start()
        # Wait for the first picture, so the caller can use it straight away.
        deadline = time.time() + timeout
        with self._cond:
            while self._seq == 0 and self.error is None and time.time() < deadline:
                self._cond.wait(0.1)
        if self._seq == 0:
            err = self.error or "no video frame arrived"
            self.stop()
            raise StreamError(err)
        return self

    def _connect(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while True:
            if self.proc is not None and self.proc.poll() is not None:
                err = (self.proc.stderr.read() or b"").decode(errors="replace").strip() if self.proc.stderr else ""
                raise StreamError(f"scrcpy server exited: {err[-300:]}")
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
            try:
                if sock.recv(1) == b"\x00":  # the dummy byte: the server is really there
                    break
            except OSError:
                pass
            sock.close()  # adb accepted the connection but nothing listens on the phone yet
            if time.time() > deadline:
                raise StreamError("scrcpy server didn't start")
            time.sleep(0.1)
        sock.settimeout(5)
        self.video = sock
        # The server sends the device name only once every socket is connected.
        self.control = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.control.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _recv_exact(sock, 64)  # device name
        codec, w, h = struct.unpack(">III", _recv_exact(sock, 12))
        if codec != 0x68323634:  # "h264"
            raise StreamError(f"unexpected video codec {codec:#x}")
        self.size = (w, h)
        sock.settimeout(None)

    def _read_loop(self) -> None:
        import av

        codec = av.CodecContext.create("h264", "r")
        config = b""
        try:
            while True:
                pts_flags, size = struct.unpack(">QI", _recv_exact(self.video, 12))
                data = _recv_exact(self.video, size)
                if pts_flags >> 63:  # config packet (SPS/PPS): goes in front of the next frame
                    config = data
                    continue
                if config:
                    data, config = config + data, b""
                for frame in codec.decode(av.Packet(data)):
                    with self._cond:
                        # The phone turns the picture for landscape ads: touches must carry the current size,
                        # or scrcpy ignores them ("generated for a different device size").
                        self.size = (frame.width, frame.height)
                        self._frame = frame
                        self._seq += 1
                        self._time = time.time()
                        self._cond.notify_all()
        except Exception as e:  # socket closed (stop, USB drop) or a decode error
            with self._cond:
                if not self._stopping:
                    self.error = f"video stream ended: {e}"
                self._cond.notify_all()

    def stop(self) -> None:
        self._stopping = True
        for s in (self.video, self.control):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.port is not None:
            try:
                self._adb("forward", "--remove", f"tcp:{self.port}", timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        with self._cond:
            if self.error is None:
                self.error = "stopped"
            self._cond.notify_all()

    # --- frames -------------------------------------------------------------------

    def _image(self) -> tuple[np.ndarray, int, float]:
        """Newest frame as BGR (converted once per frame)."""
        with self._cond:
            frame, seq, t = self._frame, self._seq, self._time
        cache = self._img_cache
        if cache is not None and cache[0] == seq:
            return cache[1], seq, t
        img = frame.to_ndarray(format="bgr24")
        self._img_cache = (seq, img)
        return img, seq, t

    def seq(self) -> int:
        with self._cond:
            return self._seq

    def next_frame(self, after: int, timeout: float = 1.0) -> tuple[np.ndarray, int]:
        """The first frame newer than `after` (frame numbers from `seq` / earlier calls)."""
        deadline = time.time() + timeout
        with self._cond:
            while self._seq <= after and self.error is None:
                left = deadline - time.time()
                if left <= 0:
                    break
                self._cond.wait(left)
            if self.error is not None:
                raise StreamError(self.error)
        img, seq, _ = self._image()
        return img, seq

    def screenshot(self, timeout: float = 0.35) -> np.ndarray:
        """A frame that arrived at least `fresh_ms` after this call, so it shows what the phone shows now
        (anything done just before, like a tap, has had time to be encoded). The phone re-sends the picture
        every 100 ms even when nothing moves, so this never waits long."""
        want = time.time() + self.fresh_ms / 1000
        deadline = time.time() + timeout
        with self._cond:
            while (self._time < want or self._seq == 0) and self.error is None:
                left = deadline - time.time()
                if left <= 0:
                    break
                self._cond.wait(left)
            if self.error is not None:
                raise StreamError(self.error)
            if self._seq == 0:
                raise StreamError("no video frames")
        return self._image()[0].copy()

    # --- touches -------------------------------------------------------------------

    def _send(self, action: int, x: int, y: int) -> None:
        if self.control is None or not self.alive:
            raise StreamError(self.error or "stream not running")
        w, h = self.size
        msg = touch_message(action, x, y, w, h, 0.0 if action == ACTION_UP else 1.0)
        with self._send_lock:
            try:
                self.control.sendall(msg)
            except OSError as e:
                self.error = f"control socket failed: {e}"
                raise StreamError(self.error) from e

    def tap(self, x: int, y: int, hold_ms: float = 40) -> None:
        self._send(ACTION_DOWN, x, y)
        time.sleep(hold_ms / 1000)
        self._send(ACTION_UP, x, y)

    def drag(self, x1: int, y1: int, x2: int, y2: int, move_ms: float = 300, hold_ms: float = 150,
             look=None) -> np.ndarray | None:
        """Press, move at an even pace, hold still, lift: holding still first means no fling. `look(frame) -> bool`
        is shown frames while the finger rests on the end point until it says yes (the stream lags ~50 ms) or
        0.4 s pass; the last of them is returned."""
        steps = max(2, int(move_ms / 10))
        self._send(ACTION_DOWN, x1, y1)
        t0 = time.time()
        for i in range(1, steps + 1):
            f = i / steps
            self._send(ACTION_MOVE, round(x1 + (x2 - x1) * f), round(y1 + (y2 - y1) * f))
            wait = t0 + f * move_ms / 1000 - time.time()
            if wait > 0:
                time.sleep(wait)
        seen = None
        if look is not None:
            until = time.time() + 0.4
            while True:
                time.sleep(0.02)
                self._send(ACTION_MOVE, x2, y2)
                try:
                    seen = self._image()[0]
                except Exception:
                    seen = None
                    break
                if look(seen) or time.time() >= until:
                    break
        end = time.time() + hold_ms / 1000
        while time.time() < end:  # keep reporting the resting finger, so the velocity drops to zero
            time.sleep(0.02)
            self._send(ACTION_MOVE, x2, y2)
        self._send(ACTION_UP, x2, y2)
        return seen
