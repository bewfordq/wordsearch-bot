"""The Android app's Python side on the PC, against a fake phone (FakeBridge): the screen capture serves a real
screenshot, gestures are recorded, OCR is real Tesseract. No phone or emulator needed.

    .venv/bin/python -m pytest tests/test_android.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SCREENS = Path(__file__).resolve().parent / "screens"

from wordbot import android_device  # noqa: E402
from wordbot.android_device import AndroidDevice  # noqa: E402
from wordbot.glyphs import GlyphReader  # noqa: E402

FAKE_BRIDGE = '''
import struct, cv2, numpy as np, pytesseract

class FakeBridge:
    """Stands in for the app's Kotlin Bridge (BotService.kt / Bridge.kt)."""
    HEADER = struct.Struct("<iiiiiiq")

    def __init__(self, img):
        self.seq = 0
        self.set(img)
        self.taps, self.drags, self.launched, self.actions = [], [], [], []
        self.fg = "in.playsimple.wordsearch"

    def set(self, img):
        self.rgba = cv2.cvtColor(img, cv2.COLOR_BGR2RGBA)
        self.seq += 1

    def newBuffer(self, n): return bytearray(n)

    def copyInto(self, buf, x, y, w, h):
        H, W = self.rgba.shape[:2]
        n = self.HEADER.size + W * H * 4
        if len(buf) < n:
            return -n
        self.HEADER.pack_into(buf, 0, W, H, 0, 0, W, H, self.seq)
        buf[self.HEADER.size:n] = self.rgba.tobytes()
        return n

    def frameSeq(self): return self.seq
    def frameAgeMs(self): return 5
    def waitFrame(self, after, ms): return self.seq
    def captureRunning(self): return True
    def accessibilityRunning(self): return True
    def screenWidth(self): return self.rgba.shape[1]
    def screenHeight(self): return self.rgba.shape[0]
    def tap(self, x, y, hold): self.taps.append((x, y)); return True
    def drag(self, x1, y1, x2, y2, move, hold): self.drags.append((x1, y1, x2, y2)); return True
    def back(self): self.actions.append("back"); return True
    def home(self): self.actions.append("home"); return True
    def launchApp(self, pkg): self.launched.append(pkg); return True
    def foregroundPackage(self): return self.fg
    def foregroundActivity(self): return None
    def batteryPercent(self): return 80
    def charging(self): return True

    @staticmethod
    def _config(psm, whitelist):
        return f"--psm {psm}" + (f" -c tessedit_char_whitelist={whitelist}" if whitelist else "")

    def ocrText(self, gray, w, h, psm, whitelist):
        img = np.frombuffer(bytes(gray), np.uint8).reshape(h, w)
        return pytesseract.image_to_string(img, config=self._config(psm, whitelist))

    def ocrTsv(self, gray, w, h, psm, whitelist):
        img = np.frombuffer(bytes(gray), np.uint8).reshape(h, w)
        return pytesseract.image_to_data(img, config=self._config(psm, whitelist))
'''
ns: dict = {}
exec(FAKE_BRIDGE, ns)
FakeBridge = ns["FakeBridge"]


def img(name: str) -> np.ndarray:
    return cv2.imread(str(SCREENS / name))


@pytest.fixture()
def phone():
    b = FakeBridge(img("puzzle_magic.png"))
    android_device.install(b)
    yield b
    android_device.install(None)


def load_shim():
    spec = importlib.util.spec_from_file_location("pytesseract_shim", ROOT / "android/python/pytesseract/__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_device_frames_and_gestures(phone):
    d = AndroidDevice({})
    assert d.connect() == "android" and d.size == (1080, 2400)
    shot = d.screenshot(lossless=True)
    assert np.array_equal(shot, img("puzzle_magic.png"))
    d.tap(100, 200)
    d.swipe(10, 20, 300, 20, move_ms=120, hold_ms=40)
    d.back()
    d.key("KEYCODE_HOME")
    d.launch_game()
    assert phone.taps == [(100, 200)] and phone.drags == [(10, 20, 300, 20)]
    assert phone.actions == ["back", "home"] and phone.launched == ["in.playsimple.wordsearch"]
    assert d.focus() == ("in.playsimple.wordsearch", "in.playsimple.wordsearch.MainActivity")


def test_device_reports_what_to_turn_on(phone, monkeypatch):
    monkeypatch.setattr(AndroidDevice, "ACCESSIBILITY_WAIT_S", 0.3)
    phone.accessibilityRunning = lambda: False
    with pytest.raises(Exception, match="accessibility"):
        AndroidDevice({}).connect()
    phone.accessibilityRunning = lambda: True
    phone.captureRunning = lambda: False
    with pytest.raises(Exception, match="Start"):
        AndroidDevice({}).connect()


def test_device_waits_for_the_accessibility_service_to_come_back(phone, monkeypatch):
    """Android drops the accessibility service (MIUI force stopping or restarting the app); the app switches it back
    on within seconds. The bot waits for that rather than failing every swipe."""
    import threading

    monkeypatch.setattr(AndroidDevice, "ACCESSIBILITY_WAIT_S", 3)
    d = AndroidDevice({})
    d.connect()
    on = threading.Event()
    phone.accessibilityRunning = on.is_set
    threading.Timer(0.5, on.set).start()
    d.tap(100, 200)                              # waits for it, then taps
    assert phone.taps == [(100, 200)]

    def dropped(*a):                             # gone mid-swipe: the swipe is lost, reported as such
        on.clear()
        threading.Timer(0.3, on.set).start()
        return False
    phone.drag = dropped
    with pytest.raises(Exception, match="back on now"):
        d.swipe(10, 20, 300, 20)


def test_reading_through_the_tesseract_stand_in(phone, monkeypatch, tmp_path):
    """The puzzle reads the same with the app's pytesseract (Tesseract4Android via the bridge)."""
    from wordbot import glyphs, screens, vision

    shim = load_shim()
    for mod in (glyphs, vision, screens):
        monkeypatch.setattr(mod, "pytesseract", shim)
    p = vision.read_puzzle(img("puzzle_magic.png"), GlyphReader(tmp_path / "t"))
    assert p.title == "MAGIC"
    assert p.grid.text() == ["RABBIT", "VANISH", "SPIRIT", "HWOHSP", "WCURSE", "TKNARP"]
    assert [w.text for w in p.words] == ["CURSE", "PRANK", "RABBIT", "SHOW", "SPIRIT", "VANISH"]
    s = screens.classify(img("next_level.png"))
    assert s.kind == "button" and s.detail == "NEXT LEVEL"


def test_app_starts_bot_and_dashboard(tmp_path):
    """android_app.init in a fresh interpreter (WORDBOT_HOME must be set before the bot is imported): the bot
    and its dashboard come up, the learned shapes are seeded, and a read of the screen works."""
    seed = tmp_path / "pkg"
    script = tmp_path / "run.py"
    script.write_text(FAKE_BRIDGE + textwrap.dedent(f'''
        import json, sys, time, socket, urllib.request
        sys.path.insert(0, {str(ROOT)!r})
        sys.path.insert(0, {str(ROOT / "android/python")!r})
        import android_app
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); android_app.PORT = s.getsockname()[1]
        bridge = FakeBridge(cv2.imread({str(SCREENS / "puzzle_magic.png")!r}))
        err = android_app.init(bridge, {str(tmp_path / "home")!r})
        assert err == "", err
        from wordbot import paths
        state = json.loads(android_app.state())
        page = urllib.request.urlopen(f"http://127.0.0.1:{{android_app.PORT}}/").read()
        api = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{{android_app.PORT}}/api/state").read())
        android_app._bot.command("connect")
        android_app._bot.command("read")
        end = time.time() + 60
        while android_app._bot.puzzle is None and time.time() < end:
            time.sleep(0.2)
        p = android_app._bot.puzzle
        print(json.dumps({{"state": state, "page": b"Word Search Bot" in page, "device": api["device"]["mode"],
                          "home": str(paths.HOME), "words": [w.text for w in p.words] if p else None,
                          "plan": len(android_app._bot.plan), "connected": android_app._bot.device.mode}}))
    '''))
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["state"]["status"] == "idle" and r["state"]["running"] is False
    assert r["page"] and r["device"] == "disconnected"     # until Start turns the screen capture on
    assert r["connected"] == "android"
    assert r["home"] == str(tmp_path / "home")
    assert r["words"] == ["CURSE", "PRANK", "RABBIT", "SHOW", "SPIRIT", "VANISH"] and r["plan"] == 6
    assert (tmp_path / "home" / "config.json").exists() or (tmp_path / "home" / "logs").is_dir()


def test_app_notes_go_to_the_bot_log(monkeypatch):
    """BotService writes into the bot's log (accessibility service switched back on, why Android ended the app)."""
    sys.path.insert(0, str(ROOT / "android/python"))
    import android_app

    lines = []
    monkeypatch.setattr(android_app, "_bot", type("Bot", (), {"log": lambda self, lvl, msg: lines.append((lvl, msg))})())
    android_app.note("warn", "Android ended Wordbot while the bot played: force stopped")
    assert lines == [("warn", "Android ended Wordbot while the bot played: force stopped")]
    monkeypatch.setattr(android_app, "_bot", None)
    android_app.note("info", "before the bot is up: dropped")


def test_low_battery_rests_then_carries_on(monkeypatch):
    """The bot drains the battery faster than a PC's USB port charges it. At 7% (still "charging") it rests;
    at 35% it carries on by itself. Unplugged, it rests below 15%."""
    sys.path.insert(0, str(ROOT / "android/python"))
    import android_app

    class Bot:
        active, paused = True, False
        def __init__(self): self.events = []
        def log(self, level, msg): self.events.append(msg)
        def pause(self): self.paused = True
        def resume(self): self.paused = False

    class Battery:
        pct, plugged = 7, True
        def batteryPercent(self): return self.pct
        def charging(self): return self.plugged

    bot, bat = Bot(), Battery()
    monkeypatch.setattr(android_app, "_bot", bot)
    monkeypatch.setattr(android_app, "_cfg", {"android": {"battery_stop_pct": 15}})
    resting = android_app._battery_check(bat, False)
    assert resting and bot.paused
    bat.pct = 30
    assert android_app._battery_check(bat, resting) and bot.paused     # not yet
    bat.pct = 35
    assert not android_app._battery_check(bat, True) and not bot.paused
    bat.pct, bat.plugged = 12, True
    assert not android_app._battery_check(bat, False) and not bot.paused   # charging, and above half the limit
    bat.plugged = False
    assert android_app._battery_check(bat, False) and bot.paused
    bot.paused = False                                                    # resumed by hand while low
    assert not android_app._battery_check(bat, True)
