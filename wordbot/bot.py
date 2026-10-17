"""The bot: looks at the phone, decides what screen it is, and acts. Runs on its own thread; the dashboard
controls it through commands that run on that same thread, so the bot and the user never touch the
phone at the same time."""
from __future__ import annotations

import json
import queue
import random
import threading
import time
import traceback
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from .config import LOGS, TEMPLATES, Config
from .device import MAIN_ACTIVITY, NEVER_TOUCH, PACKAGE, Device, DeviceError
from .dictionary import Candidate, Dictionary
from .glyphs import GlyphReader
from .pictures import PictureMemory
from .screens import (PURCHASE_WORDS, Screen, classify, coin_balance, hint_button, hint_is_free, level_number,
                      restart_button, safe_label, screen_text)
from .solver import DIRECTIONS, Placement, placements, solve
from .vision import (Puzzle, VisionError, board_visible, cell_hinted, find_layout, hidden_slots, line_covered,
                     selection_shown,
                     line_inside, line_marked, quote_blanks, read_puzzle,
                     tile_vividness, word_is_found)

TEXT_TAPS = ("TAP TO CONTINUE", "NO THANKS", "CLOSE", "SKIP", "LATER", "NOT NOW", "CONTINUE", "COLLECT", "OK")


class Stop(Exception):
    """The user pressed Stop: abandon the current action."""


class Bot:
    def __init__(self, cfg: Config, device=None):
        self.cfg = cfg
        self.device = device if device is not None else Device(cfg["device"])  # the Android app passes its own
        self.reader = GlyphReader(TEMPLATES)
        self.dictionary = Dictionary()
        self.pictures = PictureMemory()
        LOGS.mkdir(exist_ok=True)
        (LOGS / "levels").mkdir(exist_ok=True)
        (LOGS / "unknown").mkdir(exist_ok=True)
        (LOGS / "shots").mkdir(exist_ok=True)

        self.events: deque[dict] = deque(maxlen=500)
        self._event_id = 0
        self.status = "idle"          # idle | running | paused | resting
        self.phase = "idle"
        self.active = False           # the play loop is on (running or paused)
        self.paused = False
        self._steps = 0               # actions allowed while paused (Step button)
        self._stop = False
        self.cmds: queue.Queue = queue.Queue()

        self.screen: dict | None = None
        self.puzzle: Puzzle | None = None
        self.plan: list[dict] = []
        self.level: dict = {}
        self.unknown_since: float | None = None
        self.ad: dict | None = None
        self.hints_off = False        # set when a hint opened another screen (the shop): no more this session
        self._shop_backs = 0
        self._last_sig: tuple | None = None
        self._last_sig_t = 0.0
        self._repeats = 0
        self._last_unknown_shot = 0.0
        self._last_fg_check = 0.0
        self._last_battery = 0.0
        self.battery: int | None = None
        self.session_levels = 0
        self.stats = self._load_stats()
        self.stats["session_started"] = time.time()
        self.stats.update(session_levels=0, session_words=0, session_errors=0)

        self._thread = threading.Thread(target=self._main, name="bot", daemon=True)
        self._thread.start()

    # --- events ---------------------------------------------------------------------

    def log(self, level: str, msg: str, **data) -> None:
        self._event_id += 1
        ev = {"id": self._event_id, "t": time.time(), "level": level, "msg": msg}
        if data:
            ev["data"] = data
        self.events.append(ev)
        print(f"[{time.strftime('%H:%M:%S')}] {level.upper():5} {msg}", flush=True)
        try:
            with open(LOGS / "events.jsonl", "a") as f:
                f.write(json.dumps(ev, default=str) + "\n")
        except OSError:
            pass

    def _load_stats(self) -> dict:
        base = {"total_levels": 0, "total_words": 0, "total_time": 0.0, "best_time": None, "errors": 0,
                "last_level": None, "history": []}
        try:
            base.update(json.loads((LOGS / "stats.json").read_text()))
        except (OSError, ValueError):
            pass
        return base

    def _save_stats(self) -> None:
        keep = {k: v for k, v in self.stats.items() if not k.startswith("session")}
        keep["history"] = keep.get("history", [])[-300:]
        try:
            (LOGS / "stats.json").write_text(json.dumps(keep, indent=1))
        except OSError:
            pass

    # --- controls (called from the dashboard thread) ------------------------------------

    def start(self) -> None:
        self._stop = False
        self.paused = False
        self.active = True
        self.status = "running"
        self.log("info", "started")

    def pause(self) -> None:
        if self.active:
            self.paused = True
            self.status = "paused"
            self.log("info", "paused")

    def resume(self) -> None:
        if not self.active:
            return self.start()
        self.paused = False
        self.status = "running"
        self.log("info", "resumed")

    def step(self) -> None:
        """One action (one word, one button tap, ...), then pause again."""
        if not self.active:
            self._stop = False
            self.active = True
        self.paused = True
        self.status = "paused"
        self._steps += 1

    def stop(self) -> None:
        if self.active:
            self._stop = True
            self.log("info", "stopping")

    def command(self, name: str, args: dict | None = None) -> None:
        self.cmds.put((name, args or {}))

    # --- pacing ---------------------------------------------------------------------

    def checkpoint(self, what: str = "") -> None:
        """Called before every action: honours Stop, Pause and Step, and runs dashboard commands."""
        self._run_cmds()
        if self._stop:
            raise Stop()
        if not self.paused:
            return
        if self._steps > 0:
            self._steps -= 1
            return
        before = self.phase
        self.phase = f"paused before: {what}" if what else "paused"
        while self.paused and not self._stop:
            self._run_cmds()
            if self._steps > 0:
                self._steps -= 1
                break
            time.sleep(0.05)
        if self._stop:
            raise Stop()
        self.phase = before

    def sleep(self, seconds: float) -> None:
        """Sleep that still reacts to Stop and to commands."""
        end = time.time() + max(0.0, seconds)
        while True:
            self._run_cmds()
            if self._stop and self.active:
                raise Stop()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.05, left))

    def _jit_ms(self, ms: float) -> float:
        j = self.cfg.get("swipe.jitter_ms", 0)
        return max(0.0, ms + (random.uniform(0, j) if j else 0))

    def _jit_px(self, x: int, y: int) -> tuple[int, int]:
        j = self.cfg.get("swipe.jitter_px", 0)
        if not j:
            return x, y
        return x + random.randint(-j, j), y + random.randint(-j, j)

    def _swipe(self, centers: list, what: str, slow: float = 1.0) -> bool:
        """Swipe through these cell centres. While the finger rests on the last letter, check the game draws its
        selection. It doesn't while it plays a found word's animation (the word flying into its blank or picture)
        or a banner covers the board, and those swipes were lost: real words written off as wrong (2026-10-12).
        So a swipe the game didn't follow is made again once the animation is over. False if the game kept
        ignoring it, or the board went away (the level is over): the caller stops swiping."""
        sw = self.cfg["swipe"]
        (x1, y1), (x2, y2) = centers[0], centers[-1]
        check = self.cfg.get("swipe.check_touch") and self.level.get("touch_check", True)
        for attempt in range(4):
            before = self.device.live_frame()[0] if check else None

            def followed(img, before=before) -> bool:
                return img is not None and img.shape == before.shape and selection_shown(before, img, centers)

            seen = self.device.swipe(*self._jit_px(x1, y1), *self._jit_px(x2, y2),
                                     move_ms=self._jit_ms(sw["move_ms"] * slow * (1.25 ** attempt)),
                                     hold_ms=self._jit_ms(sw["hold_ms"]), look=followed if before is not None else None)
            if before is None or seen is None or followed(seen):
                self.level["ignored_run"] = 0
                return True
            self.stats["session_dropped"] = self.stats.get("session_dropped", 0) + 1
            if find_layout(seen) is None:
                return False    # the board is gone: the level is over (or a popup came up)
            self._save_selection(before, seen, centers)
            if attempt < 3:
                self.log("info", f"the game didn't follow the swipe of {what} (busy with an animation?): "
                                 "swiping it again", look=getattr(self.device, "last_look", None))
                self.sleep((0.4, 0.7, 1.0)[attempt])
        run = self.level["ignored_run"] = self.level.get("ignored_run", 0) + 1
        self.log("warn", f"the game ignored {what} four times")
        if run >= 3:
            # Three swipes in a row never followed: the check doesn't suit this screen. Swipe without it.
            self.log("warn", "the game showed no selection for three swipes in a row; not checking swipes this level")
            self.level["touch_check"] = False
        return False

    # --- main loop ------------------------------------------------------------------

    def _main(self) -> None:
        while True:
            try:
                self._run_cmds()
                if not self.active:
                    self.status = "idle"
                    self._idle_refresh()
                    time.sleep(0.05)
                    continue
                self.checkpoint("look at the screen")
                self.tick()
            except Stop:
                self.active = False
                self.paused = False
                self._stop = False
                self._steps = 0
                self.status = "idle"
                self.phase = "stopped"
                self.log("info", "stopped")
            except DeviceError as e:
                self.log("error", str(e))
                self.phase = "phone problem"
                self._error_pause(3)
            except VisionError as e:
                # Usually the screen changed while it was being read (level ending, popup arriving).
                self.log("info", f"couldn't read the puzzle ({e}); looking again")
                time.sleep(0.5)
            except Exception as e:  # keep the thread alive whatever happens
                self.stats["errors"] = self.stats.get("errors", 0) + 1
                self.stats["session_errors"] += 1
                self.log("error", f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-1500:])
                self._error_pause(1)

    def _error_pause(self, seconds: float) -> None:
        if self.active and self.cfg.get("flow.stop_on_error"):
            self.pause()
        try:
            self.sleep(seconds)
        except Stop:
            self.active = False
            self._stop = False
            self.status = "idle"

    def _idle_refresh(self) -> None:
        """Keep the dashboard's picture fresh without the live stream."""
        every = self.cfg.get("dashboard.adb_refresh_s", 1.5)
        if self.device.serial and self.device.mode == "adb" and every and time.time() - self.device.last_frame_time > every:
            try:
                self.device.screenshot()
            except Exception:
                pass
        if time.time() - self._last_battery > 60 and self.device.serial:
            self._last_battery = time.time()
            try:
                self.battery = self.device.battery()
            except Exception:
                pass

    def _ensure_device(self) -> None:
        if self.device.serial is None:
            mode = self.device.connect()
            self.log("info", f"phone connected ({self.device.serial}, {mode})")
            if self.device.error:
                self.log("warn", self.device.error)

    def tick(self) -> None:
        self._ensure_device()
        self.phase = "looking at the screen"
        img = self.device.screenshot()
        scr = classify(img)
        self.screen = scr.to_dict()
        # The same tap on the same screen again and again means it isn't working: try something else.
        sig = (scr.kind, scr.detail, scr.target)
        now = time.time()
        if scr.kind != "puzzle" and sig == self._last_sig and now - self._last_sig_t < 30:
            self._repeats += 1
        else:
            self._repeats = 0
        self._last_sig, self._last_sig_t = sig, now
        if self._repeats >= 4 and scr.kind in ("button", "tutorial", "popup"):
            self.log("warn", f"tapped the same {scr.kind} {self._repeats} times with no effect; trying something else")
            self._repeats = 0
            self.unknown_since = now - self.cfg.get("flow.unknown_wait_s", 8) - 1
            self.handle_unknown(img, Screen("unknown", scr.buttons))
            return
        if scr.kind != "shop":
            self._shop_backs = 0
        if scr.kind == "puzzle":
            self.unknown_since = None
            self.play_level()
        elif scr.kind == "shop":
            self.leave_shop(img)
        elif scr.kind == "button" and self.cfg.get("flow.auto_next"):
            self.unknown_since = None
            self.press_button(scr)
        elif scr.kind == "popup" and self.cfg.get("flow.close_popups"):
            self.unknown_since = None
            self.checkpoint("close the popup")
            x, y = scr.target
            self._save_unknown(img, "popup")
            self.log("info", f"popup: tapping its close button ({x},{y})")
            self._safe_tap(x, y, "the popup's close button")
            self.sleep(1.0)
        elif scr.kind == "tutorial" and self.cfg.get("flow.auto_next"):
            self.unknown_since = None
            self.checkpoint("tap where the tutorial points")
            x, y = scr.target
            self._save_unknown(img, "tutorial")
            self.log("info", f"tutorial: tapping where its arrow points ({x},{y})")
            self._safe_tap(x, y, "where the tutorial points")
            self.sleep(1.2)
        else:
            self.handle_unknown(img, scr)

    # --- buttons and unknown screens -------------------------------------------------

    def _safe_tap(self, x: int, y: int, what: str) -> bool:
        """Every tap the bot decides on goes through here: only while the game itself is in front, never on
        Google Play or any other app (2026-10-11: a tap reached Google Play's purchase screen)."""
        if self.cfg.get("dry_run"):
            self.log("info", f"dry run: would tap {what}")
            return False
        pkg = self.device.foreground()
        if pkg != PACKAGE:
            self.log("warn", f"not tapping {what}: {pkg or 'another app'} is in front, not the game")
            return False
        self.device.tap(x, y)
        return True

    def leave_shop(self, img: np.ndarray) -> None:
        """A shop or an offer (prices on its buttons): never tap anything on it, just go back."""
        self._shop_backs += 1
        if self._shop_backs == 1:
            self._save_unknown(img, "shop")
            self.log("warn", "a shop / offer screen is open: going back (the bot never taps on these)")
        if self._shop_backs > 6:
            self.log("warn", "the shop won't close with back: reopening the game")
            self._shop_backs = 0
            if not self.cfg.get("dry_run"):
                self.device.launch_game()
            self.sleep(3)
            return
        self.checkpoint("leave the shop")
        if not self.cfg.get("dry_run"):
            self.device.back()   # a key press, not a tap: nothing in the shop is touched
        self.sleep(1.5)

    def press_button(self, scr: Screen) -> None:
        label = scr.detail
        btn = next(b for b in scr.buttons if b.label == label)
        if label == "NEXT LEVEL" and self.level.get("key") and not self.level.get("done") and self.puzzle:
            self.level_done(self.puzzle)   # the end of the level went by unseen (a popup, an animation)
        if label.startswith("NEXT LEVEL") and self.cfg.get("flow.max_levels") and \
                self.session_levels >= self.cfg.get("flow.max_levels"):
            self.log("info", f"played {self.session_levels} level(s), the limit set in Flow; stopping")
            raise Stop()
        if label.startswith("NEXT LEVEL") and self.cfg.get("flow.rest_every") and self.session_levels and \
                self.session_levels % self.cfg.get("flow.rest_every") == 0 and not self.level.get("rested"):
            self.level["rested"] = True
            self.status, self.phase = "resting", f"resting {self.cfg.get('flow.rest_s')} s"
            self.log("info", f"resting {self.cfg.get('flow.rest_s')} s after {self.session_levels} levels")
            self.sleep(self.cfg.get("flow.rest_s"))
            self.status = "paused" if self.paused else "running"
        self.checkpoint(f"tap {label}")
        self.phase = f"tapping {label}"
        if self.cfg.get("dry_run"):
            self.log("info", f"dry run: would tap {label}")
            self.sleep(1.0)
            return
        x, y = self._jit_px(btn.box.cx, btn.box.cy)
        if self._safe_tap(x, y, label):
            self.log("info", f"tapped {label}")
        self.sleep(1.0)

    def handle_unknown(self, img: np.ndarray, scr: Screen) -> None:
        now = time.time()
        what = f"unknown screen{' (green button: ' + scr.detail + ')' if scr.detail else ''}"
        if self.unknown_since is None:
            self.unknown_since = now
            self.phase = f"waiting: {what}"
            self.sleep(0.4)
            return
        waited = now - self.unknown_since
        self.phase = f"waiting {waited:.0f}s: {what}"
        if now - self._last_fg_check > 3:
            self._last_fg_check = now
            pkg, act = self.device.focus()
            if pkg in NEVER_TOUCH:
                # Google Play (a purchase sheet, say): never tap it. Back cancels it.
                self.checkpoint("leave Google Play")
                self._save_unknown(img, "googleplay")
                self.log("warn", f"{pkg} is in front: pressing back to cancel it (the bot never taps there)")
                if not self.cfg.get("dry_run"):
                    self.device.back()
                self.unknown_since = now
                self.sleep(1.5)
                return
            if pkg and pkg != PACKAGE and self.cfg.get("flow.relaunch_game"):
                self.checkpoint("reopen the game")
                self._save_unknown(img)
                self.log("warn", f"{pkg} is in front; reopening Word Search")
                if not self.cfg.get("dry_run"):
                    self.device.launch_game()
                self.unknown_since = now
                self.sleep(3)
                return
            if pkg == PACKAGE and act and act != MAIN_ACTIVITY:
                self.handle_ad(img, act)
                return
            self.ad = None
        if waited < self.cfg.get("flow.unknown_wait_s", 8):
            self.sleep(0.4)
            return
        # Something that has stayed too long: try an unfamiliar green button, then a text button.
        self.unknown_since = now
        self._save_unknown(img)
        texts = screen_text(img)
        words = " ".join(t for t, _ in texts)
        selling = any(c in words for c in "$£€¥₹₩") or any(w in words.split() for w in PURCHASE_WORDS)
        if selling:
            # Shop-like words on screen: no tapping at all, whatever the setting below says.
            self.log("warn", "the screen mentions buying or prices: going back instead of tapping")
        if scr.kind == "button?" and self.cfg.get("flow.tap_unknown_green") and not selling:
            btn = next((b for b in scr.buttons if safe_label(b.label)), None)
            if btn is not None:
                self.checkpoint(f"tap unfamiliar button {btn.label}")
                self.log("info", f"unfamiliar green button '{btn.label}': tapping it")
                self._safe_tap(btn.box.cx, btn.box.cy, f"the button '{btn.label}'")
                self.sleep(1.2)
                return
        for txt, box in ([] if selling else texts):
            for want in TEXT_TAPS:
                if txt == want or (len(want) > 3 and want in txt):
                    self.checkpoint(f"tap '{txt}'")
                    self.log("info", f"unrecognised screen: tapping the text '{txt}'")
                    self._safe_tap(box.cx, box.cy, f"the text '{txt}'")
                    self.sleep(1.0)
                    return
        action = "back" if selling else self.cfg.get("flow.unknown_action", "back")
        if action == "pause":
            self.log("warn", "unrecognised screen: pausing for you (Flow → unknown_action)")
            self.pause()
            return
        if action == "wait":
            return
        self.checkpoint(f"{action} on unknown screen")
        self.log("info", f"unrecognised screen for {waited:.0f}s: {action.replace('_', ' ')}")
        if self.cfg.get("dry_run"):
            return
        if action == "back":
            self.device.back()
        elif action == "tap_center":
            w, h = self.device.size
            self._safe_tap(w // 2, h // 2, "the middle of the screen")
        self.sleep(1.0)

    def _save_selection(self, before: np.ndarray, seen: np.ndarray, centers: list) -> None:
        """Before and during a swipe the game didn't seem to follow, side by side around the line (to check the
        check). A few per session."""
        self._noselect_shots = getattr(self, "_noselect_shots", 0) + 1
        if not self.cfg.get("logging.save_unknown") or self._noselect_shots > 12:
            return
        xs, ys = [x for x, _ in centers], [y for _, y in centers]
        x0, x1 = max(0, min(xs) - 150), min(before.shape[1], max(xs) + 150)
        y0, y1 = max(0, min(ys) - 150), min(before.shape[0], max(ys) + 150)
        pair = cv2.hconcat([before[y0:y1, x0:x1], seen[y0:y1, x0:x1]])
        for x, y in centers:
            cv2.circle(pair, (x - x0, y - y0), 6, (0, 0, 255), -1)
        path = LOGS / "unknown" / f"{time.strftime('%Y%m%d_%H%M%S')}_noselect_{self._noselect_shots}.png"
        cv2.imwrite(str(path), pair)

    def _save_unknown(self, img: np.ndarray, kind: str = "unknown") -> None:
        now = time.time()
        if not self.cfg.get("logging.save_unknown") or (kind == "unknown" and now - self._last_unknown_shot < 10):
            return
        if kind == "unknown":
            self._last_unknown_shot = now
        path = LOGS / "unknown" / f"{time.strftime('%Y%m%d_%H%M%S')}_{kind}.png"
        cv2.imwrite(str(path), img)
        self._prune(LOGS / "unknown", 200)
        if kind == "unknown":
            self.log("info", f"saved the unrecognised screen to {path.relative_to(LOGS.parent)}")

    def handle_ad(self, img: np.ndarray, activity: str) -> None:
        """An ad: another activity inside the game's app. Wait, then back; then try the usual close spots."""
        now = time.time()
        if self.ad is None or self.ad.get("activity") != activity:
            self.ad = {"activity": activity, "since": now, "tries": 0, "last": 0.0}
            self.log("info", f"ad showing ({activity.rsplit('.', 1)[-1]})")
            self._save_unknown(img)
        ad = self.ad
        waited = now - ad["since"]
        self.phase = f"ad showing for {waited:.0f}s"
        if waited < self.cfg.get("ads.back_after_s", 5) or now - ad["last"] < 3:
            self.sleep(0.5)
            return
        self.checkpoint("close the ad")
        ad["tries"] += 1
        ad["last"] = now
        if self.cfg.get("dry_run"):
            return
        w, h = self.device.size
        corners = [(w - 60, 90), (60, 90), (w - 60, 150), (w - 100, 60)]
        if ad["tries"] % 3 != 0 or not self.cfg.get("ads.tap_corners"):
            self.device.back()
            self.log("info", "ad: pressed back")
        else:
            x, y = corners[(ad["tries"] // 3 - 1) % len(corners)]
            if self._safe_tap(x, y, "the ad's corner"):
                self.log("info", f"ad: tapped the corner at {x},{y}")
        self.sleep(1.0)

    # --- playing a level ----------------------------------------------------------------

    def _wait_still(self, timeout: float = 2.0) -> None:
        """New levels slide in: wait until two frames ~0.25 s apart agree."""
        if self.device.mode not in ("scrcpy", "android"):
            return
        end = time.time() + timeout
        prev = cv2.resize(self.device.screenshot(), (135, 300), interpolation=cv2.INTER_AREA).astype(np.int16)
        while time.time() < end:
            self.sleep(0.25)
            cur = cv2.resize(self.device.screenshot(), (135, 300), interpolation=cv2.INTER_AREA).astype(np.int16)
            if float(np.abs(cur - prev).mean()) < 1.5:
                return
            prev = cur

    def read(self) -> tuple[Puzzle, dict[str, Placement], list[str], np.ndarray]:
        """Read and solve the puzzle on screen, retrying blurry or half-drawn frames."""
        tries = max(1, self.cfg.get("solve.max_read_attempts", 3))
        last_err = "no puzzle"
        best = None
        for attempt in range(tries):
            self.phase = f"reading the puzzle{f' (try {attempt + 1})' if attempt else ''}"
            img = self.device.screenshot(lossless=True)
            t0 = time.time()
            try:
                p = read_puzzle(img, self.reader, with_title=(best is None))
            except VisionError as e:
                last_err = str(e)
                self.sleep(0.6)
                continue
            if best is not None and not p.title:
                p.title = best[0].title
            blank = [w for w in p.words if not w.text]
            found, missing = solve(p.grid.letters, [w.text for w in p.words if w.text],
                                   fuzzy=self.cfg.get("solve.fuzzy", 1))
            if missing:
                # An icon next to a word can read as an extra letter (KCHARGER): try without it.
                for w in p.words:
                    if w.text in missing and len(w.text) >= 5:
                        for trimmed in (w.text[1:], w.text[:-1]):
                            if placements(p.grid.letters, trimmed):
                                w.text = trimmed
                                break
                found, missing = solve(p.grid.letters, [w.text for w in p.words if w.text],
                                       fuzzy=self.cfg.get("solve.fuzzy", 1))
            took = time.time() - t0
            best = (p, found, missing, img)
            if not blank and not missing:
                what = {"pictures": f"{len(p.tiles)} pictures", "quote": f"a quote with {len(p.blanks)} blanks"}.get(
                    p.kind, f"{len(p.words)} words")
                self.log("info", f"read {p.grid.rows}x{p.grid.cols} grid and {what} in {took:.2f}s")
                return best
            last_err = f"{len(blank)} unreadable word(s), not in grid: {missing}"
            if not blank and len(missing) > len(p.words) / 2:
                # Most words aren't in the grid at all: not a misread but a puzzle like "find the opposite
                # words". Reading again won't help.
                self.log("info", f"most listed words aren't in the grid ({p.title or 'special level'}): "
                                 "searching for the real ones")
                return best
            self.sleep(0.6)
        if best is None:
            raise VisionError(last_err)
        if not (best[2] and len(best[2]) > len(best[0].words) / 2):
            self.log("warn", f"reading isn't perfect after {tries} tries: {last_err}")
        return best

    def _order(self, items: list[dict]) -> list[dict]:
        order = self.cfg.get("solve.order", "list")
        if order == "longest":
            return sorted(items, key=lambda i: -len(i["word"]))
        if order == "shortest":
            return sorted(items, key=lambda i: len(i["word"]))
        if order == "top":
            return sorted(items, key=lambda i: (min(i["from"][1], i["to"][1]), min(i["from"][0], i["to"][0])))
        if order == "random":
            items = items[:]
            random.shuffle(items)
            return items
        return items

    def play_level(self, manual: bool = False) -> None:
        self.phase = "waiting for the board to settle"
        self._wait_still()
        prev = (self.level, self.puzzle, self.plan)
        p, found, missing, img = self.read()
        lvl = level_number(img)
        key = (lvl, tuple(p.grid.text()))
        if self.level.get("key") and self.level.get("key") != key and not self.level.get("done") and prev[1]:
            # A new level: the last one was finished, even if its end went by unseen (no Next Level tapped).
            self.level_done(prev[1])
        self.puzzle = p
        if self.level.get("key") != key:
            n = {"pictures": len(p.tiles), "quote": len(p.blanks)}.get(p.kind, len(p.words))
            what = {"pictures": "pictures (Picture Puzzle)", "quote": "blanks (quote)"}.get(p.kind, "words")
            self.level = {"key": key, "number": lvl, "title": p.title, "started": time.time(),
                          "words": n, "kind": p.kind, "tries": 0, "rested": False}
            self.log("info", f"level {lvl or '?'}: {p.title or 'untitled'}, {p.grid.rows}x{p.grid.cols}, {n} {what}")
            if self.cfg.get("logging.save_levels"):
                cv2.imwrite(str(LOGS / "levels" / f"{time.strftime('%Y%m%d_%H%M%S')}_L{lvl or 0}.png"), img)
                self._prune(LOGS / "levels", 300)
        if p.kind == "pictures":
            self.play_pictures(p, img, manual)
            return
        if p.kind == "quote":
            self.play_quote(p, manual)
            return
        words = {w.text: w for w in p.words if w.text}
        items = []
        for word, pl in found.items():
            w = words.get(word)
            items.append({"word": word, "start": list(pl.start), "end": list(pl.end), "mismatches": pl.mismatches,
                          "from": list(p.grid.centers[pl.start[0]][pl.start[1]]),
                          "to": list(p.grid.centers[pl.end[0]][pl.end[1]]),
                          "cells": [list(c) for c in pl.cells],
                          "status": "found" if (w and w.found) else "pending", "tries": 0})
        special = len(missing) > len(p.words) / 2   # e.g. "find the opposite words": expected, not a misread
        if special:
            # The listed words are clues, not answers, even the ones that happen to be in the grid (LATE, PURE):
            # swiping them only wastes time, and their own opposites still have to be found.
            for it in items:
                if it["status"] == "pending":
                    it["status"] = "clue"
        for word in missing:
            items.append({"word": word, "start": None, "end": None, "status": "not in grid", "tries": 0})
            if not special:
                self.log("warn", f"'{word}' isn't in the grid as read: check the grid in the dashboard")
        if special:
            self.log("info", f"{len(missing)} listed words aren't in the grid ({', '.join(missing)}); "
                             "looking for the words that are")
        order = [w.text for w in p.words]
        items.sort(key=lambda i: order.index(i["word"]) if i["word"] in order else 99)
        self.plan = self._order(items)
        todo = [i for i in self.plan if i["status"] == "pending"]
        unplaced = len(missing) + len([w for w in p.words if not w.text and not w.found])
        if special:
            unplaced = len([w for w in p.words if not w.found])   # every clue not yet greyed needs its answer
        if self.cfg.get("flow.level_pause_s"):
            self.sleep(self.cfg.get("flow.level_pause_s"))
        if p.hidden and self.cfg.get("hunt.enabled") and (self.cfg.get("hunt.hidden_words") or not todo) \
                and not self.cfg.get("dry_run"):
            # Hidden words (rows of coins) pay extra coins. Look for them before the listed words, since
            # the level may end as soon as the listed ones are found.
            lengths: dict[int, int] = {}
            for n in p.hidden:
                lengths[n] = lengths.get(n, 0) + 1
            self.log("info", f"hidden words of {', '.join(map(str, p.hidden))} letters: looking for them first")

            def coins_left(shot: np.ndarray) -> int | None:   # rows of coins still hiding a word
                lay = find_layout(shot)
                return len(hidden_slots(shot, lay[1])) if lay else None

            skip = {w.text for w in p.words if w.text}
            got = self.hunt(p, len(p.hidden), skip=skip, lengths=lengths, remaining=coins_left)
            if got < len(p.hidden) and not todo:   # the hint is only worth it when the level needs the word
                got += self.hint_assist(p, len(p.hidden) - got, lengths, remaining=coins_left, skip=skip)
            self.log("info" if got >= len(p.hidden) else "warn", f"found {got} of {len(p.hidden)} hidden words")
            if not todo:
                self._finish_pass(p, self._words_left(p), "hidden words")
                return
        if todo:
            self.swipe_items(todo)
        if self.cfg.get("dry_run"):
            self.log("info", "dry run: planned swipes only")
            if not manual:
                self.pause()
            return
        if todo:
            self.verify(p)
        words_left = self._words_left(p)
        shot = self._clear_frame(p)
        left = words_left(shot) if find_layout(shot) is not None else None
        if left and self.cfg.get("hunt.enabled"):
            # The screen still shows words to find (unread, misread, not in the grid as read, or a clue's
            # answer): search the grid for them like a Picture Puzzle, until the screen says none are left.
            self.log("info", f"{left} word(s) still to find: searching the grid for them")
            skip = {w.text for w in p.words if w.text} if special else \
                {i["word"] for i in self.plan if i.get("from") and i["status"] == "found"}
            got = self.hunt(p, left, skip=skip, remaining=words_left)
            if find_layout(self.device.screenshot()) is not None and (words_left(self._clear_frame(p)) or 0) > 0:
                self.hint_assist(p, 1, remaining=words_left, skip=skip)
        self._finish_pass(p, words_left, "words")

    # --- Picture Puzzle and word hunting -----------------------------------------------

    def play_pictures(self, p: Puzzle, img: np.ndarray, manual: bool) -> None:
        """The list shows pictures. Pictures seen before are looked up; the rest are found by trying
        dictionary words from the grid and keeping the ones the game accepts."""
        crops = [img[t.y:t.y + t.h, t.x:t.x + t.w].copy() for t in p.tiles]
        viv = [tile_vividness(img, t) for t in p.tiles]
        # A picture already found is faded; on a fresh level every tile is still in colour.
        open_tiles = [i for i, v in enumerate(viv) if v > 0.04]
        known: list[str] = []
        for i in open_tiles:
            word, score = self.pictures.match(crops[i])
            if word and word not in known:
                known.append(word)
        self.plan = []
        if known:
            self.log("info", f"pictures seen before: {', '.join(known)}")
        if self.cfg.get("dry_run"):
            cands = self._hunt_candidates(p, set(), known)[:len(open_tiles) * 3]
            self.plan = [self._cand_item(p, c, "planned") for c in cands]
            self.log("info", f"dry run: would try {', '.join(c.word for c in cands)} ...")
            if not manual:
                self.pause()
            return
        tiles = {"crops": crops, "viv": viv, "open": list(open_tiles), "pending": [], "boxes": list(p.tiles)}

        def remaining(shot: np.ndarray) -> int:   # pictures still in colour
            return sum(1 for i in open_tiles if tile_vividness(shot, p.tiles[i]) >= 0.4 * viv[i])

        got = self.hunt(p, len(open_tiles), skip=set(), known_first=known, tiles=tiles, remaining=remaining)
        shot = self.device.screenshot()
        if find_layout(shot) is not None and (remaining(self._clear_frame(p)) or 0) > 0:
            self.hint_assist(p, 1, remaining=remaining, tiles=tiles)
        self._finish_pass(p, remaining, "Picture Puzzle")

    def play_quote(self, p: Puzzle, manual: bool) -> None:
        """A quote with blanks: each blank's underscores give the length of a missing word."""
        lengths: dict[int, int] = {}
        for n in p.blanks:
            lengths[n] = lengths.get(n, 0) + 1
        self.plan = []
        self.log("info", f"blanks of {', '.join(str(n) for n in p.blanks)} letters")
        # Words on show that are in the grid (sequence levels: SEED → ____ → ...) are swiped as they are.
        shown = []
        for w in p.words:
            pl = placements(p.grid.letters, w.text)
            if w.found or not pl or any(i["word"] == w.text for i in shown):
                continue
            g = p.grid
            shown.append({"word": w.text, "start": list(pl[0].start), "end": list(pl[0].end), "mismatches": 0,
                          "from": list(g.centers[pl[0].start[0]][pl[0].start[1]]),
                          "to": list(g.centers[pl[0].end[0]][pl[0].end[1]]),
                          "cells": [list(c) for c in pl[0].cells], "status": "pending", "tries": 0})
        if self.cfg.get("dry_run"):
            cands = [c for c in self._hunt_candidates(p, set(), []) if len(c.word) in lengths][:len(p.blanks) * 3]
            self.plan = [self._cand_item(p, c, "planned") for c in cands]
            self.log("info", f"dry run: would try {', '.join(c.word for c in cands)} ...")
            if not manual:
                self.pause()
            return
        if shown:
            self.plan = list(shown)
            self.swipe_items(shown)
            if self._level_over(wait=2.0 if not p.blanks else 0.0):
                return
        def remaining(shot: np.ndarray) -> int | None:   # blanks still empty
            lay = find_layout(shot)
            return len(quote_blanks(shot, lay[1])) if lay else None

        skip = {w.text for w in p.words}
        got = self.hunt(p, len(p.blanks), skip=skip, lengths=lengths, remaining=remaining)
        if find_layout(self.device.screenshot()) is not None and (remaining(self._clear_frame(p)) or 0) > 0:
            self.hint_assist(p, 1, lengths, remaining=remaining, skip=skip)
        self._finish_pass(p, remaining, "quote")

    def hint_assist(self, p: Puzzle, needed: int, lengths: dict[int, int] | None = None, remaining=None,
                    skip: set[str] | None = None, tiles: dict | None = None) -> int:
        """When the hunt has run out of guesses: press the game's hint, which puts a circle on the first letter of
        a word still to find, and hunt again (lines from a hint circle go first). Free hints always; hints that
        cost coins only with hunt.paid_hints, or hunt.paid_hints_when_stuck (this is only called when stuck)."""
        if not self.cfg.get("hunt.use_hints") or self.cfg.get("dry_run") or self.hints_off:
            return 0
        found = 0
        while found < needed and self.level.get("hints", 0) < self.cfg.get("hunt.max_hints", 2):
            img = self.device.screenshot(lossless=True)
            if find_layout(img) is None:
                return found
            pos = hint_button(img)
            if pos is None:
                self.log("warn", "can't find the hint button")
                return found
            free = hint_is_free(img, pos)
            if not free:
                if not (self.cfg.get("hunt.paid_hints") or self.cfg.get("hunt.paid_hints_when_stuck")):
                    self.log("warn", "no free hints left; not spending coins (Settings → paid hints)")
                    return found
                # Without enough coins the hint button opens the shop: only with coins to spare, read off the screen.
                coins = coin_balance(img)
                if coins is None or coins < 150:
                    self.log("warn", f"not using a paid hint: {'coins unreadable' if coins is None else f'{coins} coins'}")
                    return found
            self.checkpoint("use a hint")
            self.level["hints"] = self.level.get("hints", 0) + 1
            self.log("info", f"stuck: using a {'free' if free else 'paid (100 coins)'} hint")
            if not self._safe_tap(*pos, "the hint button"):
                return found
            self.sleep(1.6)
            shot = self.device.screenshot()
            end = time.time() + 4
            while find_layout(shot) is None and time.time() < end:   # the hint's own animation covers the board
                self.sleep(0.4)
                shot = self.device.screenshot()
            if find_layout(shot) is None:
                if classify(shot).kind == "shop" or self.device.foreground() != PACKAGE:
                    # The shop (no coins) or something else: leave it, and no more hints this session.
                    self.hints_off = True
                    self.log("error", "the hint button opened the shop or another app: going back, and no more "
                                      "hints this session")
                    self.device.back()
                    self.sleep(1.5)
                return found
            try:
                q = read_puzzle(self.device.screenshot(lossless=True), self.reader, with_title=False)
            except VisionError:
                continue
            if not q.grid.hinted:
                self.log("warn", "the hint didn't show a letter I could see")
                continue
            # Lines from a hinted cell may have been swiped before, unnoticed: try them again.
            cells = set(map(tuple, q.grid.hinted))
            memory = self.level.setdefault("tried", set())
            for k in [k for k in memory if k[0] in cells or k[1] in cells]:
                memory.discard(k)
            found += self.hunt(q, needed - found, skip or set(), lengths=lengths, remaining=remaining, tiles=tiles)
        return found

    def _words_left(self, p: Puzzle):
        """Counts, on a screenshot, the words still to find on a word-list level: listed words not greyed,
        plus rows of coins still hiding a word. None when the board isn't on screen."""
        def left(shot: np.ndarray) -> int | None:
            lay = find_layout(shot)
            if lay is None:
                return None
            n = sum(1 for w in p.words if not word_is_found(shot, w.box))
            if p.hidden:
                n += len(hidden_slots(shot, lay[1]))
            return n
        return left

    def _finish_pass(self, p: Puzzle, remaining, what: str) -> None:
        """End of a pass over a level. What the screen still shows decides what happens next, not the bot's own
        count (a word can be taken unseen, or a guess wrongly thought taken). The level itself is only counted
        once the game moves on (Next Level, or a new level on screen)."""
        if self.cfg.get("dry_run") or self._level_over(wait=1.5):
            return
        shot = self._clear_frame(p)
        left = remaining(shot) if remaining is not None and find_layout(shot) is not None else None
        if left is None:
            return   # covered by a popup or leaving: the next look at the screen sorts it out
        if left == 0:
            if self._level_over(wait=5.0):   # the end animation can take a moment to start
                return
            # Nothing left that the screen shows, yet the level goes on: a word the bot can't see is missing.
            self.log("warn", f"{what}: everything on screen reads as found but the level hasn't ended; "
                             "searching the grid for a missing word")
            self.hunt(p, 1, skip={w.text for w in p.words if w.text})
            if self._level_over(wait=4.0):
                return
        self._escalate(p, what, left)

    def _escalate(self, p: Puzzle, what: str, left: int | None) -> None:
        """Words still missing and nothing new to try. The bot keeps going rather than stop: forget what was
        tried and look again, restart the level if that's turned on, then keep retrying with a growing wait.
        (flow.pause_when_stuck: pause instead, after a few rounds.)"""
        n = self.level["exhausted"] = self.level.get("exhausted", 0) + 1
        self.log("warn", f"{what}: {left if left is not None else 'some'} still to find after round {n}")
        if n == 1:
            self._save_unknown(self.device.screenshot(), "stuck")
        if self.cfg.get("flow.pause_when_stuck") and n >= 3:
            self.log("error", f"stuck on this level ({what}); pausing. Find the last word by hand, then Resume")
            self.level["exhausted"] = 0
            self.pause()
            return
        if n % 3 == 2 and self.cfg.get("flow.restart_when_stuck") and self._restart_level(p):
            return
        self.level["tried"] = set()   # every guess again: a swipe may have gone unnoticed
        if n >= 3:
            wait = min(120, 15 * (n - 2))
            self.log("info", f"still stuck; trying everything again in {wait}s")
            self.phase = f"stuck on this level: retrying in {wait}s"
            self.sleep(wait)

    def _restart_level(self, p: Puzzle) -> bool:
        """The game's restart button (bottom right): a fresh board, so every word is found again from
        scratch, with no capsules in the way. Only once per level."""
        if self.level.get("restarted"):
            return False
        img = self.device.screenshot()
        pos = restart_button(img)
        if pos is None:
            return False
        self.level["restarted"] = True
        self.checkpoint("restart the level")
        self.log("warn", "restarting the level for a fresh board")
        if not self._safe_tap(*pos, "the restart button"):
            return False
        self.sleep(1.5)
        # A confirmation, if the game asks: a green Restart / Yes / OK (never anything priced: shop rules apply).
        scr = classify(self.device.screenshot())
        if scr.kind in ("button", "button?"):
            btn = next((b for b in scr.buttons if safe_label(b.label) and
                        any(w in b.label.split() for w in ("RESTART", "YES", "OK", "OKAY", "CONFIRM", "CONTINUE"))), None)
            if btn is not None:
                self._safe_tap(btn.box.cx, btn.box.cy, f"'{btn.label}' to restart")
                self.sleep(1.5)
        self.level["tried"] = set()
        self.level["exhausted"] = 0
        return True

    def _try_other_places(self, p: Puzzle, it: dict, box) -> None:
        """A listed word the game keeps refusing where it was read: a grid letter was probably misread. Try the
        word's other close matches in the grid (up to 2 letters different), one at a time."""
        tried = {(tuple(it["start"]), tuple(it["end"]))} if it.get("start") else set()
        g = p.grid
        for pl in placements(g.letters, it["word"], max_mismatches=2)[:8]:
            if (pl.start, pl.end) in tried or (pl.end, pl.start) in tried:
                continue
            tried.add((pl.start, pl.end))
            self.checkpoint(f"try {it['word']} elsewhere")
            (x1, y1), (x2, y2) = g.centers[pl.start[0]][pl.start[1]], g.centers[pl.end[0]][pl.end[1]]
            self._swipe([g.centers[r][c] for r, c in pl.cells], it["word"])
            self.sleep(0.8)
            shot = self.device.screenshot()
            if find_layout(shot) is None:
                return
            if word_is_found(shot, box):
                it.update({"start": list(pl.start), "end": list(pl.end), "cells": [list(c) for c in pl.cells],
                           "from": [x1, y1], "to": [x2, y2], "mismatches": pl.mismatches, "status": "found"})
                self.log("info", f"'{it['word']}' found at another place ({pl.mismatches} letter(s) were misread)")
                return
        self.log("warn", f"couldn't place '{it['word']}': the grid search will look for it")

    def _uncount_level(self) -> None:
        s = self.stats
        last = s.get("history", [])[-1:] or [{}]
        s["total_levels"] = max(0, s["total_levels"] - 1)
        s["session_levels"] = max(0, s["session_levels"] - 1)
        self.session_levels = max(0, self.session_levels - 1)
        s["total_words"] = max(0, s["total_words"] - last[0].get("words", 0))
        s["session_words"] = max(0, s["session_words"] - last[0].get("words", 0))
        s["total_time"] = max(0.0, s["total_time"] - last[0].get("secs", 0))
        if s.get("history"):
            s["history"].pop()
        self.level["done"] = False
        self._save_stats()

    def _level_over(self, img: np.ndarray | None = None, wait: float = 0.0, window: float = 1.2) -> bool:
        """Has the level ended? Waits up to `wait` seconds for the board to leave (after the last word it
        stays up for a second or two), then it must stay gone for `window` seconds: a single frame isn't
        enough, because the flying-word banner or a popup can hide the board in the middle of a level."""
        end = time.time() + wait
        if img is None:
            img = self.device.screenshot()
        while find_layout(img) is not None:
            if time.time() >= end:
                return False
            self.sleep(0.15)
            img = self.device.screenshot()
        gone_until = time.time() + window
        while time.time() < gone_until:
            self.sleep(0.15)
            if find_layout(self.device.screenshot()) is not None:
                return False
        return True

    def _clear_frame(self, p: Puzzle, timeout: float = 3.0) -> np.ndarray:
        """A frame with the whole grid on show (waits out toasts and banners that cover letters)."""
        end = time.time() + timeout
        img = self.device.screenshot()
        while time.time() < end and find_layout(img) is not None and not board_visible(img, p.grid):
            self.sleep(0.25)
            img = self.device.screenshot()
        return img

    def _cand_item(self, p: Puzzle, c: Candidate, status: str) -> dict:
        g = p.grid
        return {"word": c.word, "start": list(c.start), "end": list(c.end), "mismatches": 0,
                "from": list(g.centers[c.start[0]][c.start[1]]), "to": list(g.centers[c.end[0]][c.end[1]]),
                "cells": [list(x) for x in c.cells], "status": status, "tries": 1}

    def _hunt_candidates(self, p: Puzzle, skip: set[str], known_first: list[str]) -> list[tuple[int, Candidate]]:
        """(tier, candidate), best first. Tier 0: lines from a hint circle on screen (any letters); 1: pictures
        seen before; 2: common words; 3: rarer words (TOUCAN). Rarer still are mostly abbreviations: left out."""
        out: list[tuple[int, Candidate]] = []
        g = p.grid
        freq = self.dictionary.freq() if self.dictionary.available else {}
        for r, c in map(tuple, g.hinted):
            lines = []
            for dr, dc in DIRECTIONS:
                cells = [(r, c)]
                while True:
                    nr, nc = cells[-1][0] + dr, cells[-1][1] + dc
                    if not (0 <= nr < g.rows and 0 <= nc < g.cols):
                        break
                    cells.append((nr, nc))
                    if len(cells) >= 3:
                        word = "".join(g.letters[a][b] for a, b in cells)
                        lines.append(Candidate(word, freq.get(word, -1.0), list(cells)))
            # dictionary words first, then by length (short lines are the likelier answers)
            lines.sort(key=lambda k: (k.zipf < 0, -k.zipf, len(k.word)))
            out += [(0, k) for k in lines]
        for w in known_first:
            for pl in placements(g.letters, w):
                out.append((1, Candidate(w, 99.0, pl.cells)))
        if self.dictionary.available:
            floor = self.cfg.get("hunt.min_zipf", 2.5)
            all_words = [c for c in self.dictionary.candidates(g.letters, min_zipf=1.5) if c.word not in skip]
            out += [(2, c) for c in all_words if c.zipf >= floor]
            out += [(3, c) for c in all_words if c.zipf < floor]
        else:
            self.log("warn", "data/words_en.txt.gz is missing: can't search the grid for words")
        return out

    def hunt(self, p: Puzzle, needed: int, skip: set[str], known_first: list[str] | None = None,
             tiles: dict | None = None, lengths: dict[int, int] | None = None, remaining=None) -> int:
        """Find `needed` words that aren't readable from the list: swipe likely words from the grid, a few at
        a time, and keep the ones the game takes. Returns how many it found.

        A word counts as found when its capsule appears, or when `remaining(img)` (how many targets the screen
        still shows: open pictures, blanks, coin rows, unfound words) drops: a capsule can't be seen where the
        word only crosses capsules already there (BUG on level 127). Such lines are swiped one at a time, so
        a drop says which one it was. Nothing is swiped twice on a level, across passes."""
        g = p.grid
        memory: set = self.level.setdefault("tried", set())
        cands = self._hunt_candidates(p, skip, known_first or [])
        accepted: list[set] = []
        accepted_words: set[str] = set()
        done: set[int] = set()       # handled this pass: swiped or ruled out
        attempts = 0
        found = 0
        batch_n = max(1, self.cfg.get("hunt.batch", 4))
        max_tries = self.cfg.get("hunt.max_tries", 150)
        sw = self.cfg["swipe"]
        before = self._clear_frame(p)
        left = remaining(before) if remaining else None   # targets on screen, by our count
        hinted = set(map(tuple, g.hinted))

        def key(c: Candidate) -> tuple:   # a line: the game takes a word swiped either way round (DET is TED)
            return tuple(sorted([tuple(c.start), tuple(c.end)]))

        def centers(c: Candidate) -> list:
            return [g.centers[r][cc] for r, cc in c.cells]

        def mids(c: Candidate) -> set:
            return {(a[0] + b[0], a[1] + b[1]) for a, b in zip(c.cells, c.cells[1:])} | {(2 * r, 2 * cc) for r, cc in c.cells}

        def ruled_out(tier: int, c: Candidate) -> bool:
            cs = set(c.cells)
            return (c.word in accepted_words or c.word[::-1] in skip or c.word[::-1] in accepted_words
                    or any(cs <= a for a in accepted)
                    or (lengths is not None and not lengths.get(len(c.word)))
                    or key(c) in memory
                    or (tier == 0 and c.cells[0] not in hinted)
                    or (tier > 0 and line_inside(before, centers(c))))   # part of a word already found

        # Stop when the screen shows nothing left to find (or, without a way to tell, at `needed`).
        while attempts < max_tries and (left > 0 if left is not None else found < needed):
            # The best tier with something left; within it lines that can show a capsule go first, a few at a
            # time, then lines that only cross capsules already there, one at a time.
            batch: list[int] = []
            solo = False
            tier_now = None
            covered_first: int | None = None
            used: set = set()
            for i, (tier, c) in enumerate(cands):
                if i in done:
                    continue
                if ruled_out(tier, c):
                    done.add(i)
                    continue
                if tier_now is None:
                    tier_now = tier
                if tier != tier_now or len(batch) >= batch_n:
                    break
                if tier == 0 or line_covered(before, centers(c)):
                    if covered_first is None:
                        covered_first = i
                    if tier == 0 and not batch:
                        covered_first = i
                        break
                    continue
                m = mids(c)
                if m & used:
                    continue
                batch.append(i)
                used |= m
            if not batch and covered_first is not None:
                batch, solo = [covered_first], True
            if not batch:
                break
            self.log("debug", f"trying {', '.join(cands[k][1].word for k in batch)}" + (" (alone)" if solo else ""))
            for k in batch:
                c = cands[k][1]
                done.add(k)
                memory.add(key(c))
                attempts += 1
                self.checkpoint(f"try {c.word}")
                self.phase = f"trying {c.word} ({found}/{needed} found, {attempts} tried)"
                if not self._swipe(centers(c), c.word):
                    if find_layout(self.device.screenshot()) is None:
                        break    # the level is over (or a popup): the check below sees which
                    ignored = self.level.setdefault("ignored", {})
                    ignored[key(c)] = ignored.get(key(c), 0) + 1
                    if ignored[key(c)] <= 2:   # the game never saw it: not a wrong guess, so try it again later
                        memory.discard(key(c))
                        done.discard(k)
                self.sleep(self._jit_ms(sw["gap_ms"]) / 1000)
            self.level["tries"] = self.level.get("tries", 0) + len(batch)
            # Capsules pop in and pictures fade out over a moment: look once it has all settled.
            self.sleep(self.cfg.get("hunt.check_delay_ms", 650) / 1000 + (0.4 if solo else 0))
            after = self._clear_frame(p)
            if find_layout(after) is None:
                if self._level_over(after):
                    return needed  # the level ended: everything is found
                after = self.device.screenshot()
                if find_layout(after) is None:
                    continue
            if left is not None:
                # Pictures fade and blanks fill a moment after the capsule appears: count once that has happened,
                # or a late fade gets credited to the next batch's guess (LEAF counted as PAS on level 151).
                self.sleep(0.6)
                settled = self._clear_frame(p)
                if find_layout(settled) is not None:
                    after = settled
            hits = [k for k in batch if line_marked(before, after, centers(cands[k][1]))]
            sure = set(hits)
            for k in batch:   # a word from a hint circle is found once its circle is gone
                c = cands[k][1]
                if k not in hits and cands[k][0] == 0 and board_visible(after, g) and \
                        not cell_hinted(after, g, *c.cells[0]):
                    hits.append(k)
            unattributed = 0
            if left is not None:
                now = remaining(after)
                left = max(0, left - len(hits))
                if now is not None and now > left:
                    # The screen shows more left than the bot counted: something thought taken wasn't. Believe it.
                    self.log("info", f"the screen still shows {now} to find (counted {left}): carrying on")
                    left = now
                if now is not None and now < left:
                    extra = left - now
                    others = [k for k in batch if k not in hits]
                    if len(others) == 1 and extra >= 1:   # only one guess unexplained: it must be that one
                        hits += others
                        unattributed = extra - 1
                    else:
                        unattributed = extra              # counted, but no word named
                    left = now
            for k in hits:
                c = cands[k][1]
                found += 1
                accepted.append(set(c.cells))
                accepted_words.add(c.word)
                hinted.discard(c.cells[0])
                if lengths is not None and lengths.get(len(c.word)):
                    lengths[len(c.word)] -= 1
                self.plan.append(self._cand_item(p, c, "found"))
                self.log("info", f"'{c.word}' accepted ({found}/{needed})")
                if tiles is not None and k in sure:   # remember pictures only from words seen being marked
                    tiles["pending"].append(c.word)
            if unattributed:
                found += unattributed
                self.log("info", f"{unattributed} more word(s) taken in that batch ({found}/{needed})")
            if tiles is not None:
                self._learn_tiles(p, after, tiles)
            before = after
        return found

    def _learn_tiles(self, p: Puzzle, img: np.ndarray, tiles: dict) -> None:
        """Pair each accepted word with the picture that faded after it, and remember the pair."""
        faded = [i for i in tiles["open"] if tile_vividness(img, tiles["boxes"][i]) < 0.4 * tiles["viv"][i]]
        if not faded:
            return
        for i in faded:
            tiles["open"].remove(i)
        if len(faded) == 1 and tiles["pending"]:
            word = tiles["pending"].pop(0)
            if self.pictures.learn(tiles["crops"][faded[0]], word):
                self.log("info", f"remembered the picture for {word}")
        else:  # several faded at once: can't tell which is which
            tiles["pending"] = tiles["pending"][len(faded):]

    def swipe_items(self, items: list[dict], slow: float = 1.0) -> None:
        sw = self.cfg["swipe"]
        for it in items:
            self.checkpoint(f"swipe {it['word']}")
            self.phase = f"swiping {it['word']}"
            it["tries"] += 1
            if self.cfg.get("dry_run"):
                it["status"] = "planned"
                continue
            g = self.puzzle.grid if self.puzzle is not None else None
            cells = [g.centers[r][c] for r, c in it["cells"]] if g is not None and it.get("cells") else [it["from"], it["to"]]
            ok = self._swipe(cells, it["word"], slow)
            it["status"] = "swiped"
            if not ok and find_layout(self.device.screenshot()) is None:
                return   # the level is over (or a popup): the check afterwards sees which
            self.sleep(self._jit_ms(sw["gap_ms"]) / 1000)
            if self.cfg.get("verify.each_word") and self.cfg.get("verify.enabled"):
                self.sleep(self.cfg.get("verify.delay_ms", 450) / 1000)
                img = self.device.screenshot()
                if find_layout(img) is None:
                    return
                w = next((w for w in self.puzzle.words if w.text == it["word"]), None)
                if w is not None:
                    it["status"] = "found" if word_is_found(img, w.box) else "not taken"

    def verify(self, p: Puzzle) -> None:
        v = self.cfg["verify"]
        boxes = {w.text: w.box for w in p.words if w.text}
        for attempt in range(v["retries"] + 1):
            self.phase = "checking the word list"
            self.sleep(v["delay_ms"] / 1000)
            img = self.device.screenshot()
            if find_layout(img) is None:
                if self._level_over(img):
                    return
                img = self.device.screenshot()
                if find_layout(img) is None:
                    continue
            if not v["enabled"]:
                return
            for it in self.plan:
                if it["status"] in ("swiped", "not taken", "found") and it["word"] in boxes:
                    it["status"] = "found" if word_is_found(img, boxes[it["word"]]) else "not taken"
            left = [i for i in self.plan if i["status"] == "not taken"]
            if left:
                # A word that was just taken fades out of the list over ~0.5 s; look again before redoing it.
                self.sleep(0.7)
                img = self.device.screenshot()
                if find_layout(img) is None:
                    if self._level_over(img):
                        return
                    img = self.device.screenshot()
                    if find_layout(img) is None:
                        continue
                for it in left:
                    if word_is_found(img, boxes[it["word"]]):
                        it["status"] = "found"
                left = [i for i in self.plan if i["status"] == "not taken"]
            if not left:
                return
            if attempt == v["retries"]:
                self.log("warn", f"the game didn't take: {', '.join(i['word'] for i in left)}; "
                                 "trying the word's other likely places (a misread letter)")
                for it in left:
                    self._try_other_places(p, it, boxes[it["word"]])
                return
            self.log("info", f"redoing {', '.join(i['word'] for i in left)}")
            self.swipe_items(left, slow=1.6)

    def level_done(self, p: Puzzle) -> None:
        if self.level.get("done"):
            return
        took = time.time() - self.level.get("started", time.time())
        for it in self.plan:  # the level only ends once every word is found
            if it.get("from"):
                it["status"] = "found"
        learned = self.learn(p)
        self.session_levels += 1
        n_words = max(len([i for i in self.plan if i["status"] == "found"]), self.level.get("words") or 0)
        s = self.stats
        s["total_levels"] += 1
        s["session_levels"] += 1
        s["total_words"] += n_words
        s["session_words"] += n_words
        s["total_time"] += took
        s["best_time"] = took if s.get("best_time") is None else min(s["best_time"], took)
        s["last_level"] = self.level.get("number")
        s.setdefault("history", []).append({"t": time.time(), "level": self.level.get("number"),
                                            "title": p.title, "secs": round(took, 1), "words": n_words})
        self._save_stats()
        self.log("good", f"level {self.level.get('number') or '?'} done in {took:.1f}s"
                         + (f", learned {learned} letter shape(s)" if learned else ""))
        self.level["done"] = True
        self.level["board_gone"] = True   # the dashboard stops drawing this board on the picture

    def learn(self, p: Puzzle) -> int:
        if not self.cfg.get("solve.learn_templates"):
            return 0
        n = 0
        for it in self.plan:
            if it["status"] != "found" or it.get("mismatches") or not it.get("cells"):
                continue
            for (r, c), ch in zip(it["cells"], it["word"]):
                if self.reader.learn(p.grid.glyphs[r][c], ch):
                    n += 1
        return n

    @staticmethod
    def _prune(folder: Path, keep: int) -> None:
        files = sorted(folder.glob("*.png"))
        for f in files[:-keep]:
            try:
                f.unlink()
            except OSError:
                pass

    # --- dashboard commands ---------------------------------------------------------

    def _run_cmds(self) -> None:
        while True:
            try:
                name, args = self.cmds.get_nowait()
            except queue.Empty:
                return
            try:
                self._cmd(name, args)
            except Stop:
                raise
            except Exception as e:
                self.log("error", f"{name}: {type(e).__name__}: {e}")

    def _cmd(self, name: str, a: dict) -> None:
        d = self.device
        if name == "connect":
            mode = d.connect()
            self.log("info", f"phone connected ({d.serial}, {mode})")
            if d.error:
                self.log("warn", d.error)
            return
        if name == "disconnect":
            d.close()
            d.serial = None
            self.log("info", "phone disconnected")
            return
        if name == "clear_templates":
            self.reader.clear()
            self.log("info", "letter templates cleared")
            return
        if name == "reset_stats":
            for k in ("total_levels", "total_words", "total_time", "errors"):
                self.stats[k] = 0
            self.stats.update(best_time=None, history=[], session_levels=0, session_words=0, session_errors=0)
            self.session_levels = 0
            self._save_stats()
            self.log("info", "stats reset")
            return
        self._ensure_device()
        if name == "tap":
            d.tap(int(a["x"]), int(a["y"]))
            self.log("info", f"manual tap at {int(a['x'])},{int(a['y'])}")
        elif name == "swipe":
            d.swipe(int(a["x1"]), int(a["y1"]), int(a["x2"]), int(a["y2"]),
                    move_ms=float(a.get("ms", self.cfg.get("swipe.move_ms"))), hold_ms=self.cfg.get("swipe.hold_ms"))
            self.log("info", f"manual swipe {int(a['x1'])},{int(a['y1'])} → {int(a['x2'])},{int(a['y2'])}")
        elif name == "back":
            d.back()
            self.log("info", "pressed back")
        elif name == "home":
            d.key("KEYCODE_HOME")
            self.log("info", "pressed home")
        elif name == "launch":
            d.launch_game()
            self.log("info", "opened Word Search")
        elif name == "screenshot":
            img = d.screenshot(lossless=True)
            path = LOGS / "shots" / f"{time.strftime('%Y%m%d_%H%M%S')}.png"
            cv2.imwrite(str(path), img)
            self.log("info", f"saved {path.relative_to(LOGS.parent)}")
        elif name == "classify":
            img = d.screenshot()
            scr = classify(img)
            self.screen = scr.to_dict()
            self.log("info", f"screen: {scr.kind} {scr.detail}".strip())
        elif name == "read":
            p, found, missing, _ = self.read()
            self.puzzle = p
            self.plan = [{"word": w, "start": list(pl.start), "end": list(pl.end), "mismatches": pl.mismatches,
                          "from": list(p.grid.centers[pl.start[0]][pl.start[1]]),
                          "to": list(p.grid.centers[pl.end[0]][pl.end[1]]), "cells": [list(c) for c in pl.cells],
                          "status": "found" if next((x.found for x in p.words if x.text == w), False) else "pending",
                          "tries": 0} for w, pl in found.items()]
            self.plan += [{"word": w, "start": None, "end": None, "status": "not in grid", "tries": 0} for w in missing]
            self.log("info", f"read: {', '.join(found)}" + (f"; not found: {', '.join(missing)}" if missing else ""))
        elif name == "solve":
            self.play_level(manual=True)
        elif name == "swipe_word":
            it = next((i for i in self.plan if i["word"] == a["word"] and i.get("from")), None)
            if it is None:
                self.log("warn", f"no placement for {a['word']}")
                return
            self.swipe_items([it])
        elif name == "swipe_cells":
            if self.puzzle is None:
                self.log("warn", "read the puzzle first")
                return
            g = self.puzzle.grid
            (x1, y1), (x2, y2) = g.centers[int(a["r1"])][int(a["c1"])], g.centers[int(a["r2"])][int(a["c2"])]
            d.swipe(x1, y1, x2, y2, move_ms=self.cfg.get("swipe.move_ms"), hold_ms=self.cfg.get("swipe.hold_ms"))
            self.log("info", f"swiped cells ({a['r1']},{a['c1']}) → ({a['r2']},{a['c2']})")
        elif name == "set_cell":
            if self.puzzle is None:
                return
            g = self.puzzle.grid
            r, c, ch = int(a["r"]), int(a["c"]), str(a["letter"]).upper()[:1]
            g.letters[r][c], g.source[r][c], g.conf[r][c] = ch, "user", 1.0
            self.log("info", f"cell ({r},{c}) set to {ch}")
            self._replan()
        elif name == "tap_button":
            scr = classify(d.screenshot())
            if scr.kind == "shop":
                self.log("warn", "that's a shop / offer screen: not tapping its buttons")
            elif scr.buttons:
                b = scr.buttons[int(a.get("index", 0))]
                d.tap(b.box.cx, b.box.cy)
                self.log("info", f"tapped button {b.label or '?'}")
        else:
            self.log("warn", f"unknown command {name}")

    def _replan(self) -> None:
        p = self.puzzle
        found, missing = solve(p.grid.letters, [w.text for w in p.words if w.text], fuzzy=self.cfg.get("solve.fuzzy", 1))
        old = {i["word"]: i for i in self.plan}
        plan = []
        for w, pl in found.items():
            st = old.get(w, {}).get("status", "pending")
            plan.append({"word": w, "start": list(pl.start), "end": list(pl.end), "mismatches": pl.mismatches,
                         "from": list(p.grid.centers[pl.start[0]][pl.start[1]]),
                         "to": list(p.grid.centers[pl.end[0]][pl.end[1]]), "cells": [list(c) for c in pl.cells],
                         "status": "pending" if st == "not in grid" else st, "tries": 0})
        plan += [{"word": w, "start": None, "end": None, "status": "not in grid", "tries": 0} for w in missing]
        self.plan = plan

    # --- dashboard view ---------------------------------------------------------------

    def snapshot(self, since: int = 0) -> dict:
        d = self.device
        lvl = dict(self.level)
        lvl.pop("key", None)
        if lvl.get("started") and not lvl.get("done"):
            lvl["elapsed"] = round(time.time() - lvl["started"], 1)
        s = self.stats
        avg = (s["total_time"] / s["total_levels"]) if s.get("total_levels") else None
        return {
            "status": self.status, "phase": self.phase, "dry_run": self.cfg.get("dry_run"),
            "device": {"mode": d.mode, "serial": d.serial, "size": list(d.size), "error": d.error,
                       "battery": self.battery},
            "screen": self.screen,
            "level": lvl,
            "puzzle": self.puzzle.to_dict() if self.puzzle else None,
            "plan": self.plan,
            "stats": {**{k: v for k, v in s.items() if k != "history"}, "avg_time": avg,
                      "history": s.get("history", [])[-30:],
                      "session_secs": round(time.time() - s["session_started"])},
            "templates": self.reader.known_letters(),
            "pictures_known": len(self.pictures),
            "tesseract_calls": self.reader.tesseract_calls,
            "events": [e for e in self.events if e["id"] > since],
            "config": self.cfg.data,
        }
