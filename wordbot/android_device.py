"""The phone itself, from inside the Android app: what device.py does over USB on the PC.

The app's Kotlin side hands over a `Bridge` (android_app.init) with:
  newBuffer(size) -> byte[]            a buffer for copyInto, kept and reused here
  copyInto(buffer, x, y, w, h) -> int  the rectangle of the newest screen frame (MediaProjection; w or h <= 0: to
                                       the frame's edge): a header (HEADER) and its RGBA pixels. Returns the bytes
                                       written, 0 before the first frame, or minus the bytes needed
  frameSeq() -> int, frameAgeMs() -> int, waitFrame(after, ms) -> int
  captureRunning(), accessibilityRunning() -> bool
  screenWidth(), screenHeight() -> int
  tap(x, y, hold_ms), drag(x1, y1, x2, y2, move_ms, hold_ms) -> bool   (accessibility gestures)
  beginDrag(x1, y1, x2, y2, move_ms), endDrag(hold_ms) -> bool          (a drag in two halves, to look in between)
  back(), home(), launchApp(package) -> bool
  foregroundPackage(), foregroundActivity() -> str | None
  ocrTsv(gray, w, h, psm, whitelist), ocrText(...) -> str              (Tesseract, see android_shims)
  batteryPercent() -> int, charging() -> bool

The game and the bot share the phone's CPU, so frames are copied into one reused buffer and converted once each.
"""
from __future__ import annotations

import struct
import threading
import time

import cv2
import numpy as np

from .device import MAIN_ACTIVITY, NEVER_TOUCH, PACKAGE, DeviceError

HEADER = struct.Struct("<iiiiiiq")  # frame width, height; x, y, w, h of the part copied; frame number
_bridge = None


def install(bridge) -> None:
    global _bridge
    _bridge = bridge


def bridge():
    if _bridge is None:
        raise DeviceError("the app's bridge isn't installed (android_app.init)")
    return _bridge


class AndroidDevice:
    """Same interface as device.Device, on the phone: screen capture for screenshots, accessibility gestures for
    taps and swipes."""

    FRESH_S = 0.15  # after a tap or swipe, wait up to this long for a frame showing the game's reaction
    LOOK_S = 0.4      # a swipe that looks for the game's selection: how long the finger may rest while it looks
    # Android can drop the accessibility service (MIUI force stopping or restarting things); the app switches it
    # back on within a few seconds when it has WRITE_SECURE_SETTINGS (BotService.watchAccessibility).
    ACCESSIBILITY_WAIT_S = 10.0

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.serial: str | None = None
        self.size = (1080, 2400)
        self.error: str | None = None
        self.last_frame: np.ndarray | None = None
        self.last_frame_time = 0.0
        self._buf = None
        self._cache: tuple[int, np.ndarray] | None = None
        self._lock = threading.Lock()
        self._input_at = 0.0
        self.last_look: dict = {}

    # --- connection ---------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return "android" if self.serial else "disconnected"

    def _await_accessibility(self) -> bool:
        """Whether the accessibility service is (back) on, waiting up to ACCESSIBILITY_WAIT_S for it."""
        b = bridge()
        end = time.monotonic() + self.ACCESSIBILITY_WAIT_S
        while not b.accessibilityRunning():
            if time.monotonic() >= end:
                return False
            time.sleep(0.2)
        return True

    def connect(self) -> str:
        b = bridge()
        if not self._await_accessibility():
            self.serial = None
            raise DeviceError("Wordbot's accessibility service is off: Settings > Accessibility > Wordbot")
        if not b.captureRunning():
            self.serial = None
            raise DeviceError("screen capture isn't on: press Start in the Wordbot app")
        if int(b.frameSeq()) == 0 and int(b.waitFrame(0, 5000)) == 0:
            self.serial = None
            raise DeviceError("no screen picture arrived from the capture")
        self.size = (int(b.screenWidth()), int(b.screenHeight()))
        self.serial = "this phone"
        self.error = None
        return self.mode

    def close(self) -> None:
        pass  # the capture belongs to the app

    def ensure(self) -> None:
        b = bridge()
        if self.serial is None or not b.captureRunning() or not b.accessibilityRunning():
            self.connect()

    # --- screen -------------------------------------------------------------------------

    def _copy(self) -> tuple[np.ndarray, int]:
        b = bridge()
        with self._lock:
            seq = int(b.frameSeq())
            if self._cache is not None and self._cache[0] == seq:
                return self._cache[1], seq
            for _ in range(4):  # the frame can change size between tries (a landscape ad)
                if self._buf is None:
                    self._buf = b.newBuffer(HEADER.size)
                n = int(b.copyInto(self._buf, 0, 0, 0, 0))
                if n > 0:
                    break
                if n == 0:
                    raise DeviceError("no screen picture yet")
                self._buf = b.newBuffer(-n)
            else:
                raise DeviceError("the screen picture kept changing size")
            raw = memoryview(self._buf).cast("B")
            fw, fh, x, y, w, h, seq = HEADER.unpack_from(raw, 0)
            rgba = np.frombuffer(raw, np.uint8, count=w * h * 4, offset=HEADER.size).reshape(h, w, 4)
            img = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
            self._cache = (seq, img)
            self.size = (w, h)
            return img, seq

    def screenshot(self, lossless: bool = False) -> np.ndarray:
        """What the screen shows now (screen capture frames are always lossless). Android only sends a frame when
        the picture changes, so the newest one is current unless a tap or swipe of ours is still on its way."""
        self.ensure()
        b = bridge()
        since = time.monotonic() - self._input_at
        if since < self.FRESH_S:
            b.waitFrame(int(b.frameSeq()), int((self.FRESH_S - since) * 1000) + 1)
        img, _ = self._copy()
        self.last_frame, self.last_frame_time = img, time.time()
        return img

    def live_frame(self) -> tuple[np.ndarray | None, float]:
        """Newest picture without waiting (for the dashboard)."""
        try:
            if self.serial is not None and bridge().captureRunning():
                img, _ = self._copy()
                age = max(0, int(bridge().frameAgeMs()))
                return img, time.time() - age / 1000
        except Exception:
            pass
        return self.last_frame, self.last_frame_time

    # --- touches ------------------------------------------------------------------------

    def _failed(self, what: str) -> DeviceError:
        if bridge().accessibilityRunning():  # Android cancels the bot's gesture when someone touches the screen
            return DeviceError(f"the {what} was interrupted (was the screen touched?)")
        if self._await_accessibility():
            return DeviceError(f"the {what} was lost: Android dropped Wordbot's accessibility service (back on now)")
        return DeviceError(f"{what} failed: Wordbot's accessibility service is off")

    def _guard(self) -> None:
        pkg = str(bridge().foregroundPackage() or "")
        if pkg in NEVER_TOUCH:   # last line of defence: no touch ever lands on Google Play
            raise DeviceError(f"refusing to touch the screen: {pkg} is in front")

    def tap(self, x: int, y: int) -> None:
        self.ensure()
        self._guard()
        try:
            if not bridge().tap(int(x), int(y), 40):
                raise self._failed("tap")
        finally:
            self._input_at = time.monotonic()

    def swipe(self, x1: int, y1: int, x2: int, y2: int, move_ms: float = 150, hold_ms: float = 40,
              look=None) -> np.ndarray | None:
        """`look(frame) -> bool` is shown the screen while the finger rests on the end point, frame after frame,
        until it says yes or LOOK_S runs out (the capture lags the screen by a varying amount). Returns the last
        frame it saw."""
        self.ensure()
        self._guard()
        b = bridge()
        try:
            if look is None or not hasattr(b, "beginDrag"):
                if not b.drag(int(x1), int(y1), int(x2), int(y2), int(max(1, move_ms)), int(max(0, hold_ms))):
                    raise self._failed("swipe")
                return None
            if not b.beginDrag(int(x1), int(y1), int(x2), int(y2), int(max(1, move_ms))):
                b.endDrag(1)
                raise self._failed("swipe")
            start, frames, img = time.monotonic(), 0, None
            try:
                while True:
                    img, seq = self._copy()
                    if look(img):
                        break
                    left = self.LOOK_S - (time.monotonic() - start)
                    if left <= 0:
                        break
                    if int(b.waitFrame(seq, max(1, int(left * 1000)))) > seq:
                        frames += 1
            finally:
                self.last_look = {"waited_ms": int((time.monotonic() - start) * 1000), "frames": frames}
                if not b.endDrag(int(max(1, hold_ms))):
                    raise self._failed("swipe")
            return img
        finally:
            self._input_at = time.monotonic()

    def key(self, name: str) -> None:
        if name == "KEYCODE_HOME":
            bridge().home()
        elif name == "KEYCODE_BACK":
            bridge().back()

    def back(self) -> None:
        bridge().back()

    # --- app ----------------------------------------------------------------------------

    def focus(self) -> tuple[str, str]:
        b = bridge()
        pkg = str(b.foregroundPackage() or "")
        act = str(b.foregroundActivity() or "")
        if pkg == PACKAGE and not act:
            act = MAIN_ACTIVITY  # no activity seen yet: assume the game's own (ads show up as other activities)
        return pkg, act

    def foreground(self) -> str:
        return self.focus()[0]

    def launch_game(self) -> None:
        bridge().launchApp(PACKAGE)

    def battery(self) -> int | None:
        try:
            return int(bridge().batteryPercent())
        except Exception:
            return None
