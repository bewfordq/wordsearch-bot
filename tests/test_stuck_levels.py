"""Level 127 (Picture Puzzle "THINGS THAT FLY"), where the phone app got stuck on 2026-10-11: the last word,
BUG, runs where two found words' capsules cross, so its own capsule can't be seen, and the bot had ruled it out
and then swiped the same junk guesses for 15 minutes. Replayed here with a simulated game.

    .venv/bin/python -m pytest tests/test_stuck_levels.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCREENS = Path(__file__).resolve().parent / "screens"

SIM = '''
import json, os, sys, time
import cv2, numpy as np
sys.path.insert(0, ROOT)
os.environ["WORDBOT_HOME"] = HOME
from wordbot.bot import Bot
from wordbot.config import Config
from wordbot.vision import read_puzzle, tile_vividness

BUG = {(3, 6), (5, 4)}


class SimGame:
    """The level as it stood: BUG left to find. Swiping BUG fades the ladybird picture, as the game does, but
    draws no capsule anyone could see (its gaps are already under other capsules)."""
    mode, serial, size, error = "android", "sim", (1080, 2400), None
    last_frame, last_frame_time = None, 0.0

    def __init__(self, img, accept=True):
        self.frame = img.copy()
        self.accept = accept
        self.swipes = []
        self.lines = []
        self.taps = []
        p = read_puzzle(img, Bot.reader_for_tests, with_title=False)
        self.grid = p.grid
        self.tile = max(p.tiles, key=lambda t: tile_vividness(img, t))   # the one still in colour

    def _cell(self, x, y):
        g = self.grid
        return min(((r, c) for r in range(g.rows) for c in range(g.cols)),
                   key=lambda rc: (g.centers[rc[0]][rc[1]][0] - x) ** 2 + (g.centers[rc[0]][rc[1]][1] - y) ** 2)

    def screenshot(self, lossless=False):
        return self.frame.copy()   # a fresh picture each time, like a phone

    def live_frame(self):
        return self.frame, time.time()

    def swipe(self, x1, y1, x2, y2, move_ms=0, hold_ms=0, look=False):
        a, b = self._cell(x1, y1), self._cell(x2, y2)
        g = self.grid
        cells = [a]
        dr, dc = (b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1])
        while cells[-1] != b and len(cells) < 20:
            cells.append((cells[-1][0] + dr, cells[-1][1] + dc))
        self.swipes.append("".join(g.letters[r][c] for r, c in cells))
        self.lines.append(tuple(sorted([a, b])))   # the same line either way round
        if self.accept and {a, b} == BUG:
            t = self.tile
            roi = self.frame[t.y:t.y + t.h, t.x:t.x + t.w]
            roi[:] = (roi * 0.1 + 255 * 0.9).astype(np.uint8)   # the picture fades out

    def tap(self, x, y):
        self.taps.append((x, y))

    def back(self): pass
    def key(self, name): pass
    def focus(self): return "in.playsimple.wordsearch", "in.playsimple.wordsearch.MainActivity"
    def foreground(self): return "in.playsimple.wordsearch"
    def battery(self): return 80
    def connect(self): return self.mode
    def ensure(self): pass
    def close(self): pass


def run(img, accept=True, passes=1):
    from wordbot.glyphs import GlyphReader
    from wordbot.paths import TEMPLATES
    Bot.reader_for_tests = GlyphReader(Path(ROOT) / "templates")
    game = SimGame(img, accept)
    cfg = Config(Path(HOME) / "config.json")
    cfg.update({"swipe": {"gap_ms": 0}, "hunt": {"check_delay_ms": 0, "max_tries": 1000, "use_hints": False},
                "logging": {"save_levels": False, "save_unknown": False},
                "flow": {"restart_when_stuck": False}})
    bot = Bot(cfg, device=game)   # (its play loop stays idle: the level is driven from here)
    p = read_puzzle(img, bot.reader, with_title=False)
    bot.level = {"key": "L127", "number": 127, "words": len(p.tiles), "started": time.time()}
    rounds, lines = [], []
    for _ in range(passes):
        n = len(game.swipes)
        bot._level_over = lambda *a, **k: False       # the simulated level never ends
        bot.sleep = lambda *a, **k: None              # (no waiting between rounds in a test)
        bot.play_pictures(read_puzzle(game.frame, bot.reader, with_title=False), game.frame, manual=False)
        rounds.append(game.swipes[n:])
        lines.append([list(map(list, ln)) for ln in game.lines[n:]])
    found = [i["word"] for i in bot.plan if i["status"] == "found"]
    return {"found": found, "rounds": rounds, "lines": lines, "paused": bot.paused,
            "events": [e["msg"] for e in bot.events if e["level"] in ("warn", "error")]}
'''


def run_case(tmp_path: Path, body: str) -> dict:
    script = tmp_path / "case.py"
    script.write_text(f"from pathlib import Path\nROOT = {str(ROOT)!r}\nHOME = {str(tmp_path / 'home')!r}\n"
                      + SIM + textwrap.dedent(body))
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_covered_answer_found_by_the_fading_picture(tmp_path):
    """No hint on screen: BUG crosses two capsules, so it's tried alone and the faded picture confirms it."""
    r = run_case(tmp_path, f'''
        img = cv2.imread({str(SCREENS / "picture_L127_stuck.png")!r})
        g = read_puzzle(img, __import__("wordbot.glyphs", fromlist=["x"]).GlyphReader(Path(ROOT) / "templates"),
                        with_title=False).grid
        # cover the hint circle on (3, 6) with the plain B from (4, 1)
        h = int(g.pitch * 0.46)
        (x, y), (sx, sy) = g.centers[3][6], g.centers[4][1]
        img[y - h:y + h, x - h:x + h] = img[sy - h:sy + h, sx - h:sx + h]
        print(json.dumps(run(img)))
    ''')
    assert r["found"] == ["BUG"], r
    assert "BUG" in r["rounds"][0] or "GUB" in r["rounds"][0]
    # (the simulated level never ends, so the bot goes on to look for a word the screen doesn't show)


def test_hint_circle_on_screen_goes_first(tmp_path):
    r = run_case(tmp_path, f'''
        print(json.dumps(run(cv2.imread({str(SCREENS / "picture_L127_stuck.png")!r}))))
    ''')
    assert r["found"] == ["BUG"], r
    assert r["rounds"][0][0] in ("BUG", "GUB")     # the line from the hint circle, before any other guess


def test_keeps_going_when_the_word_cant_be_found(tmp_path):
    """The game takes nothing: no line is swiped twice in a round, the next round tries everything again, and
    the bot never pauses itself (it used to stop: 'the bot just stops randomly')."""
    r = run_case(tmp_path, f'''
        print(json.dumps(run(cv2.imread({str(SCREENS / "picture_L127_stuck.png")!r}), accept=False, passes=2)))
    ''')
    first, second = (sorted(map(str, ln)) for ln in r["lines"])
    assert first and len(first) == len(set(first))   # no line swiped twice in a round
    assert second == first                    # the next round has another go at everything
    assert any("still to find after round 1" in m for m in r["events"])
    assert r["paused"] is False
