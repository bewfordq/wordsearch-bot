"""Entry points for the Android app: its Kotlin side calls these through Chaquopy.

init(bridge, home) once per app start, then start / pause / resume / stop / set_dry_run from the notification,
the widget and the app's screen, state() about once a second for what they show, and note() for log lines. The bot is the same as on
the PC (wordbot.bot.Bot) with wordbot.android_device in place of adb, and the same web dashboard runs inside the
app (http://127.0.0.1:8765, shown in the app's screen).

Nothing from the bot is imported at module level: WORDBOT_HOME has to be set before wordbot.paths is imported.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback

PORT = 8765
_bot = None
_cfg = None
_init_error = ""


def init(bridge, home: str) -> str:
    """Set the bot and the dashboard up (no playing yet). Returns "" or what went wrong, for the app's screen."""
    global _bot, _cfg, _init_error
    if _bot is not None:
        return ""
    try:
        os.environ["WORDBOT_HOME"] = str(home)
        # numpy's and OpenCV's thread pools spin on every core after each call, taking CPU from the game.
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        os.environ.setdefault("OMP_THREAD_LIMIT", "1")
        from wordbot import android_device, paths

        android_device.install(bridge)
        _seed(paths)
        import cv2

        cv2.setNumThreads(1)
        from wordbot.bot import Bot
        from wordbot.config import Config
        from wordbot.dashboard import serve

        _cfg = Config()
        _bot = Bot(_cfg, device=android_device.AndroidDevice(_cfg["device"]))
        a = _cfg["android"]
        serve(_bot, "0.0.0.0" if a.get("dashboard_lan") else "127.0.0.1", PORT, max_fps=a.get("dashboard_fps", 4))
        threading.Thread(target=_housekeeping, args=(bridge,), name="housekeeping", daemon=True).start()
        _bot.log("info", "Wordbot app ready: press Start")
        _init_error = ""
    except Exception as e:
        _init_error = f"{e!r}\n{traceback.format_exc()}"
    return _init_error


def _seed(paths) -> None:
    """First run: copy the letter shapes and pictures the PC bot learned (bundled in the app) into the files
    folder, where the bot keeps learning."""
    for src, dst in ((paths.SEED / "templates", paths.TEMPLATES), (paths.SEED / "pictures", paths.PICTURES)):
        dst.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and not any(dst.glob("*.png")):
            for f in src.glob("*.png"):
                shutil.copy2(f, dst / f.name)


def _housekeeping(bridge) -> None:
    """Rests the bot when the battery runs low, and carries on once it has charged back up: the bot uses more
    power than a PC's USB port gives, so the battery can run down even while plugged in (2026-10-12)."""
    resting = False
    while True:
        time.sleep(30)
        try:
            resting = _battery_check(bridge, resting)
        except Exception:
            pass


def _battery_check(bridge, resting: bool) -> bool:
    pct, charging = int(bridge.batteryPercent()), bool(bridge.charging())
    limit = int(_cfg["android"].get("battery_stop_pct", 15))
    if resting and (not _bot.active or not _bot.paused):
        return False                     # stopped or resumed by hand: not ours any more
    if resting:
        if pct >= limit + 20:
            _bot.log("info", f"battery back up to {pct}%: carrying on")
            _bot.resume()
            return False
        return True
    if _bot.active and not _bot.paused and ((pct < limit and not charging) or pct < max(5, limit / 2)):
        _bot.log("warn", f"battery at {pct}%: resting until it has charged to {limit + 20}% (keep the phone plugged in)")
        _bot.pause()
        return True
    return False


def start() -> str:
    """Start (or resume) the bot, with the game brought to the front first: Start comes from the app or the
    home screen."""
    if _bot is None:
        return _init_error or "not initialised"
    if _bot.active and _bot.paused:
        _bot.resume()
        return ""
    _game_to_front()
    _bot.start()
    return ""


def _game_to_front() -> None:
    from wordbot import android_device
    from wordbot.device import PACKAGE

    b = android_device.bridge()
    try:
        for _ in range(3):
            if str(b.foregroundPackage() or "") == PACKAGE:
                return
            b.launchApp(PACKAGE)
            end = time.time() + 8
            while time.time() < end and str(b.foregroundPackage() or "") != PACKAGE:
                time.sleep(0.4)
            time.sleep(1.5)  # let it finish drawing before the first look
    except Exception as e:
        _bot.log("warn", f"couldn't bring the game to the front: {e}")


def pause() -> None:
    if _bot is not None:
        _bot.pause()


def resume() -> None:
    if _bot is not None:
        _bot.resume()


def stop() -> None:
    if _bot is not None:
        _bot.stop()


def note(level: str, message: str) -> None:
    """A line in the bot's log from the app's Kotlin side: the accessibility service switched back on, why Android
    ended the app last time."""
    if _bot is not None:
        _bot.log(str(level), str(message))


def set_dry_run(on: bool) -> None:
    if _cfg is not None:
        _cfg.update({"dry_run": bool(on)})


def state() -> str:
    """JSON for the notification, the widget and the app's screen."""
    if _bot is None:
        return json.dumps({"status": "error", "phase": (_init_error.splitlines() or ["not ready"])[0],
                           "running": False, "paused": False})
    s = _bot.stats
    problems = [e for e in list(_bot.events) if e["level"] in ("warn", "error")][-6:]
    lvl = _bot.level or {}
    return json.dumps({
        "status": _bot.status, "phase": _bot.phase, "running": bool(_bot.active), "paused": bool(_bot.paused),
        "dry_run": bool(_cfg.get("dry_run")), "level": lvl.get("number"), "title": lvl.get("title"),
        "session_levels": s.get("session_levels", 0), "session_words": s.get("session_words", 0),
        "total_levels": s.get("total_levels", 0), "device": _bot.device.mode, "device_error": _bot.device.error,
        "problems": [{"ts": time.strftime("%H:%M:%S", time.localtime(e["t"])), "message": e["msg"]} for e in problems],
        "url": f"http://127.0.0.1:{PORT}/",
    }, default=str)
