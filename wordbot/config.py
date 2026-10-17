"""Settings, with defaults. The dashboard edits them live; they're saved to config.json."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

from .paths import CONFIG_FILE, LOGS, TEMPLATES  # noqa: F401  (others import them from here)

DEFAULTS: dict = {
    "device": {
        "use_scrcpy": True,       # live video + fast touches; off = adb screenshots and `input` commands
        "max_fps": 30,
        "bit_rate": 8_000_000,
        "serial": "",             # pick a phone when several are plugged in
    },
    "swipe": {
        "move_ms": 120,           # finger travel time from first to last letter
        "hold_ms": 40,            # rest on the last letter before lifting
        "gap_ms": 60,             # pause between two words
        "jitter_px": 0,           # random offset of each touch point (looks more human)
        "jitter_ms": 0,           # random extra time on each timing above
        "check_touch": True,      # watch the game draw its selection during each swipe; swipe again if it didn't
    },
    "solve": {
        "order": "list",          # list | longest | shortest | top | random
        "fuzzy": 1,               # letters a word may disagree with the read grid (misreads) and still be used
        "learn_templates": True,  # learn letter shapes from solved words (fewer Tesseract calls)
        "max_read_attempts": 3,
    },
    "verify": {
        "enabled": True,          # check the list afterwards and redo words the game didn't take
        "each_word": False,       # check after every word instead of once at the end (slower)
        "delay_ms": 450,
        "retries": 2,
    },
    "flow": {
        "auto_next": True,        # tap Next Level / Continue / Collect ...
        "max_levels": 0,          # stop after this many levels (0 = never)
        "level_pause_s": 0.0,     # extra wait before starting each level
        "rest_every": 0,          # take a break every N levels (0 = never)
        "rest_s": 60,
        "unknown_wait_s": 8,      # how long an unrecognised screen may stay before acting on it
        "unknown_action": "back", # back | tap_center | wait | pause
        "close_popups": True,     # tap the round X on popups
        "tap_unknown_green": False, # after the wait, tap a green button with an unfamiliar label (never a price)
        "relaunch_game": True,    # reopen the game if another app comes to the front
        "pause_when_stuck": False,  # pause when a level can't be finished (off: keep retrying, never stop)
        "restart_when_stuck": True, # restart a level that can't be finished, for a fresh board (once per level)
        "stop_on_error": False,   # pause the bot when a level can't be read or solved
    },
    "hunt": {                     # finding words without a readable list (Picture Puzzle levels)
        "enabled": True,
        "hidden_words": True,     # look for hidden words (rows of coins in the list) for their coins
        "min_zipf": 2.5,          # how common a word must be to try it (higher = fewer, more common words)
        "batch": 4,               # words swiped before one check
        "check_delay_ms": 650,    # capsules pop in and pictures fade over a moment
        "max_tries": 150,
        "use_hints": True,        # when the search fails, use the game's hint (free ones first, then coins)
        "max_hints": 2,           # per level
        "paid_hints": False,      # also use hints that cost coins (100 each) once the free ones run out
        "paid_hints_when_stuck": False,  # ... or only when every guess has failed (needs 150+ coins on screen)
    },
    "ads": {
        "back_after_s": 5,        # an ad (another activity in front of the game): press back after this long
        "tap_corners": True,      # if back doesn't close it, tap where ad close buttons usually are
    },
    "dry_run": False,             # read and solve, but don't touch the phone
    "safety_version": 1,          # settings files from before 2026-10-11's purchase fix get the risky options off
    "logging": {
        "save_levels": True,      # keep a screenshot of every level read
        "save_unknown": True,     # keep screenshots of screens the bot didn't recognise
    },
    "dashboard": {
        "adb_refresh_s": 1.5,     # live view refresh when there's no scrcpy stream and the bot is idle
    },
    "android": {                  # only used by the Android app
        "battery_stop_pct": 15,   # rest below this battery level (half of it while charging), carry on 20% higher
        "dashboard_fps": 4,       # live picture in the app's dashboard (each frame costs the game CPU)
        "dashboard_lan": False,   # also serve the dashboard to other devices on the Wi-Fi (port 8765)
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            # keep the default's type (the dashboard sends numbers as numbers, but be safe)
            d = out[k]
            try:
                if isinstance(d, bool):
                    v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
                elif isinstance(d, int):
                    v = int(float(v))
                elif isinstance(d, float):
                    v = float(v)
                elif isinstance(d, str):
                    v = str(v)
            except (TypeError, ValueError):
                continue
            out[k] = v
    return out


class Config:
    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.data = copy.deepcopy(DEFAULTS)
        if path.exists():
            try:
                saved = json.loads(path.read_text())
                self.data = _merge(DEFAULTS, saved)
                if int(saved.get("safety_version", 0)) < 1:
                    # Saved when these defaulted to on: a paid hint without coins opened the shop and a price got
                    # tapped. Off again; turn them back on in Settings if wanted.
                    self.data["flow"]["tap_unknown_green"] = False
                    self.data["hunt"]["paid_hints_when_stuck"] = False
                    self.data["hunt"]["paid_hints"] = False
                    self.save()
            except (OSError, ValueError):
                pass

    def __getitem__(self, key: str):
        return self.data[key]

    def get(self, dotted: str, default=None):
        cur = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def update(self, changes: dict) -> dict:
        with self._lock:
            self.data = _merge(self.data, changes)
            self.save()
            return self.data

    def reset(self) -> dict:
        with self._lock:
            self.data = copy.deepcopy(DEFAULTS)
            self.save()
            return self.data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))
