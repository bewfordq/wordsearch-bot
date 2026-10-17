"""The phone: screenshots and touches, over scrcpy (live, fast) or plain adb (slow but always there)."""
from __future__ import annotations

import shutil
import struct
import subprocess
import threading
import time

import cv2
import numpy as np

from .stream import Stream, StreamError

PACKAGE = "in.playsimple.wordsearch"
# Apps the bot must never touch: Google Play (purchases), Google services (payments, accounts), Wallet.
NEVER_TOUCH = ("com.android.vending", "com.google.android.gms", "com.google.android.apps.walletnfcrel")
MAIN_ACTIVITY = f"{PACKAGE}.MainActivity"


class DeviceError(RuntimeError):
    pass


def adb_devices(adb: str = "adb") -> list[tuple[str, str]]:
    r = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=10)
    out = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            out.append((parts[0], parts[1]))
    return out


class Device:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.adb = shutil.which("adb") or "adb"
        self.serial: str | None = None
        self.stream: Stream | None = None
        self.size = (1080, 2400)
        self.last_frame: np.ndarray | None = None
        self.last_frame_time = 0.0
        self.error: str | None = None
        self._lock = threading.RLock()

    # --- connection ---------------------------------------------------------------------

    @property
    def mode(self) -> str:
        if self.stream is not None and self.stream.alive:
            return "scrcpy"
        return "adb" if self.serial else "disconnected"

    def _run(self, *args: str, timeout: float = 15, text: bool = False) -> subprocess.CompletedProcess:
        cmd = [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)
        return subprocess.run(cmd, capture_output=True, timeout=timeout, text=text)

    def connect(self) -> str:
        """Find the phone and start the live stream if enabled. Returns the mode."""
        with self._lock:
            self.close()
            devs = adb_devices(self.adb)
            ready = [s for s, st in devs if st == "device"]
            if not devs:
                raise DeviceError("No Android device found. Check the USB cable and that USB debugging is on.")
            if not ready:
                s, st = devs[0]
                raise DeviceError(f"Device {s} is '{st}'. Unlock the phone and accept the USB debugging prompt.")
            want = self.cfg.get("serial") or ""
            self.serial = want if want in ready else ready[0]
            out = self._run("shell", "wm", "size", text=True).stdout
            for line in out.splitlines():
                if "size:" in line:
                    w, h = line.split(":")[1].strip().split("x")
                    self.size = (int(w), int(h))
            self.error = None
            if self.cfg.get("use_scrcpy", True):
                try:
                    self.stream = Stream({"max_fps": self.cfg.get("max_fps", 30),
                                          "bit_rate": self.cfg.get("bit_rate", 8_000_000)}, adb=self.adb)
                    self.stream.start()
                except (StreamError, OSError, subprocess.SubprocessError) as e:
                    self.stream = None
                    self.error = f"live stream unavailable, using adb screenshots: {e}"
            return self.mode

    def close(self) -> None:
        with self._lock:
            if self.stream is not None:
                try:
                    self.stream.stop()
                except Exception:
                    pass
                self.stream = None

    def ensure(self) -> None:
        if self.serial is None:
            self.connect()
        elif self.stream is not None and not self.stream.alive:
            # USB hiccup or the server died: try to get the stream back, else fall back to adb.
            self.connect()

    # --- screen -------------------------------------------------------------------------

    def screenshot(self, lossless: bool = False) -> np.ndarray:
        """Newest frame from the live stream, or with `lossless` a pixel-exact screencap (~0.5 s): the
        stream's H.264 picture is soft for a second after it starts and whenever the screen changes a lot,
        which is fine for checking state but not for reading text."""
        self.ensure()
        img = None
        if not lossless and self.stream is not None and self.stream.alive:
            try:
                img = self.stream.screenshot()
            except StreamError as e:
                self.error = str(e)
        if img is None:
            img = self._screencap()
        self.last_frame, self.last_frame_time = img, time.time()
        return img

    def _screencap(self) -> np.ndarray:
        r = self._run("exec-out", "screencap", timeout=20)
        data = r.stdout
        if r.returncode != 0 or len(data) < 16:
            raise DeviceError(f"screencap failed: {r.stderr.decode(errors='replace').strip() or 'no data'}")
        w, h, _fmt = struct.unpack("<III", data[:12])
        header = len(data) - w * h * 4
        if header not in (12, 16):
            raise DeviceError("unexpected screencap format")
        rgba = np.frombuffer(data, np.uint8, offset=header).reshape(h, w, 4)
        return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)

    def live_frame(self) -> tuple[np.ndarray | None, float]:
        """Newest picture without waiting (for the dashboard)."""
        if self.stream is not None and self.stream.alive:
            try:
                img, _, t = self.stream._image()
                return img, t
            except Exception:
                pass
        return self.last_frame, self.last_frame_time

    # --- touches ------------------------------------------------------------------------

    def tap(self, x: int, y: int) -> None:
        self.ensure()
        if self.foreground() in NEVER_TOUCH:   # last line of defence: no tap ever lands on Google Play
            raise DeviceError(f"refusing to tap: {self.foreground()} is in front")
        if self.stream is not None and self.stream.alive:
            try:
                self.stream.tap(int(x), int(y))
                return
            except StreamError as e:
                self.error = str(e)
        self._run("shell", "input", "tap", str(int(x)), str(int(y)))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, move_ms: float = 150, hold_ms: float = 40,
              look=None) -> np.ndarray | None:
        """`look(frame) -> bool` (live stream only): see Stream.drag. Returns the last frame it was shown."""
        self.ensure()
        if self.stream is not None and self.stream.alive:
            try:
                return self.stream.drag(int(x1), int(y1), int(x2), int(y2), move_ms=move_ms, hold_ms=hold_ms,
                                        look=look)
            except StreamError as e:
                self.error = str(e)
        self._run("shell", "input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)),
                  str(int(max(50, move_ms + hold_ms))))
        return None

    def key(self, name: str) -> None:
        self.ensure()
        self._run("shell", "input", "keyevent", name)

    def back(self) -> None:
        self.key("KEYCODE_BACK")

    # --- app ----------------------------------------------------------------------------

    def focus(self) -> tuple[str, str]:
        """(package, activity) of the focused window; ('', '') if unknown."""
        out = self._run("shell", "dumpsys", "window", text=True, timeout=10).stdout
        for key in ("mCurrentFocus", "mFocusedApp"):
            for line in out.splitlines():
                if key in line:
                    for tok in line.replace("}", " ").split():
                        if "/" in tok and "." in tok:
                            pkg, act = tok.split("/", 1)
                            return pkg, (pkg + act if act.startswith(".") else act)
        return "", ""

    def foreground(self) -> str:
        return self.focus()[0]

    def launch_game(self) -> None:
        self._run("shell", "monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1")

    def battery(self) -> int | None:
        out = self._run("shell", "dumpsys", "battery", text=True, timeout=10).stdout
        for line in out.splitlines():
            if line.strip().startswith("level:"):
                return int(line.split(":")[1])
        return None
