"""The screen, not the bot's own count, decides whether a level is done (2026-10-12: 'the bot thinks it found all
the words when it hasn't'). Simulated games: the game refuses a word, or every guess leaves a capsule.

    .venv/bin/python -m pytest tests/test_level_end.py
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
import json, os, shutil, sys, time
from pathlib import Path
import cv2, numpy as np
sys.path.insert(0, ROOT)
os.environ["WORDBOT_HOME"] = HOME
(Path(HOME) / "templates").mkdir(parents=True, exist_ok=True)
for f in (Path(ROOT) / "templates").glob("*.png"):
    shutil.copy(f, Path(HOME) / "templates" / f.name)
from wordbot.bot import Bot
from wordbot.config import Config
from wordbot.glyphs import GlyphReader
from wordbot.solver import solve
from wordbot.vision import read_puzzle


class SimGame:
    """A word-list level. A swipe over a listed word's line greys the word in the list and draws a capsule,
    except for words in `refuse`. With `capsule_everything`, every swipe draws a capsule, taken or not. The first
    swipe of a word in `drop` goes unseen, as a few did on the phone: no selection while the finger is down."""
    mode, serial, size, error = "android", "sim", (1080, 2400), None
    last_frame, last_frame_time = None, 0.0

    def __init__(self, img, refuse=(), capsule_everything=False, drop=()):
        self.frame = img.copy()
        self.refuse, self.capsule_everything = set(refuse), capsule_everything
        self.drop = dict(drop) if isinstance(drop, dict) else {w: 1 for w in drop}   # word: swipes to ignore
        self.p = read_puzzle(img, GlyphReader(Path(HOME) / "templates"), with_title=False)
        found, _ = solve(self.p.grid.letters, [w.text for w in self.p.words if w.text])
        self.lines = {frozenset([pl.start, pl.end]): w for w, pl in found.items()}
        self.swipes, self.taken = [], []

    def _cell(self, x, y):
        g = self.p.grid
        return min(((r, c) for r in range(g.rows) for c in range(g.cols)),
                   key=lambda rc: (g.centers[rc[0]][rc[1]][0] - x) ** 2 + (g.centers[rc[0]][rc[1]][1] - y) ** 2)

    def _capsule(self, a, b):
        g = self.p.grid
        (x1, y1), (x2, y2) = g.centers[a[0]][a[1]], g.centers[b[0]][b[1]]
        mask = np.zeros(self.frame.shape[:2], np.uint8)
        cv2.line(mask, (x1, y1), (x2, y2), 255, int(0.58 * g.pitch))
        gray = cv2.cvtColor(self.frame, cv2.COLOR_BGR2GRAY)
        self.frame[(mask > 0) & (gray > 200)] = (177, 143, 255)

    def swipe(self, x1, y1, x2, y2, move_ms=0, hold_ms=0, look=False):
        a, b = self._cell(x1, y1), self._cell(x2, y2)
        word = self.lines.get(frozenset([a, b]))
        self.swipes.append(word or f"{a}-{b}")
        if self.drop.get(word):
            self.drop[word] -= 1
            return self.frame.copy() if look is not None else None
        seen = None
        if look is not None:   # the selection: a dark capsule along the line, under the finger
            g = self.p.grid
            seen = self.frame.copy()
            cv2.line(seen, tuple(g.centers[a[0]][a[1]]), tuple(g.centers[b[0]][b[1]]), (160, 60, 40), int(0.7 * g.pitch))
        if word and word not in self.refuse and word not in self.taken:
            self.taken.append(word)
            self._capsule(a, b)
            box = next(w.box for w in self.p.words if w.text == word)
            roi = self.frame[box.y:box.y + box.h, box.x:box.x + box.w]
            roi[:] = (roi * (55 / 255) + 200).astype(np.uint8)    # greyed in the list
        elif self.capsule_everything:
            self._capsule(a, b)
        return seen

    def screenshot(self, lossless=False): return self.frame.copy()   # a fresh picture each time, like a phone
    def live_frame(self): return self.frame, time.time()
    def tap(self, x, y): pass
    def back(self): pass
    def key(self, name): pass
    def focus(self): return "in.playsimple.wordsearch", "in.playsimple.wordsearch.MainActivity"
    def foreground(self): return "in.playsimple.wordsearch"
    def battery(self): return 80
    def connect(self): return self.mode
    def ensure(self): pass
    def close(self): pass


def play(game, rounds=1):
    cfg = Config(Path(HOME) / "config.json")
    cfg.update({"swipe": {"gap_ms": 0}, "verify": {"delay_ms": 0},
                "hunt": {"check_delay_ms": 0, "max_tries": 30, "use_hints": False},
                "logging": {"save_levels": False, "save_unknown": False}, "flow": {"restart_when_stuck": False}})
    bot = Bot(cfg, device=game)          # (its play loop stays idle: the level is driven from here)
    bot._wait_still = lambda *a, **k: None
    bot.sleep = lambda *a, **k: None
    bot._level_over = lambda *a, **k: False   # the simulated level never ends by itself
    for _ in range(rounds):
        bot.play_level()
    done = [e["msg"] for e in bot.events if e["level"] == "good"]
    return {"taken": game.taken, "swipes": game.swipes, "done": done, "paused": bot.paused,
            "events": [e["msg"] for e in bot.events]}
'''


def run(tmp_path: Path, body: str) -> dict:
    script = tmp_path / "case.py"
    script.write_text(f"ROOT = {str(ROOT)!r}\nHOME = {str(tmp_path / 'home')!r}\n" + SIM + textwrap.dedent(body))
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_refused_word_is_not_taken_for_found(tmp_path):
    """The game never takes SHOW: the level is not counted as done, the bot doesn't stop, and it keeps
    looking (other places for SHOW, then the grid) instead of believing it's finished."""
    r = run(tmp_path, f'''
        game = SimGame(cv2.imread({str(SCREENS / "puzzle_magic.png")!r}), refuse={{"SHOW"}})
        print(json.dumps(play(game, rounds=2)))
    ''')
    assert sorted(r["taken"]) == ["CURSE", "PRANK", "RABBIT", "SPIRIT", "VANISH"]
    assert r["done"] == []                                    # never counted while SHOW is still on screen
    assert r["paused"] is False
    assert any("1 word(s) still to find" in m for m in r["events"])
    assert any("still to find after round" in m for m in r["events"])
    assert r["swipes"].count("SHOW") >= 3                     # swiped, redone, and tried again next round


def test_all_words_taken_ends_the_search(tmp_path):
    r = run(tmp_path, f'''
        game = SimGame(cv2.imread({str(SCREENS / "puzzle_magic.png")!r}))
        print(json.dumps(play(game)))
    ''')
    assert len(r["taken"]) == 6
    assert not any("word(s) still to find" in m for m in r["events"])   # never thought words were left


def test_fake_capsules_dont_fool_the_hunt(tmp_path):
    """Every guess leaves a capsule (as if taken) but the list doesn't change: the bot's own count passes the
    target, yet it believes the screen and carries on."""
    r = run(tmp_path, f'''
        game = SimGame(cv2.imread({str(SCREENS / "puzzle_magic.png")!r}), refuse={{"SHOW"}}, capsule_everything=True)
        print(json.dumps(play(game)))
    ''')
    assert r["done"] == []
    assert any("the screen still shows" in m for m in r["events"])


def test_dropped_swipe_is_swiped_again(tmp_path):
    """The game doesn't see the first swipe of SHOW (no selection under the finger): the bot swipes it again at
    once, and the level is done in one pass instead of SHOW being written off."""
    r = run(tmp_path, f'''
        game = SimGame(cv2.imread({str(SCREENS / "puzzle_magic.png")!r}), drop={{"SHOW"}})
        print(json.dumps(play(game)))
    ''')
    assert len(r["taken"]) == 6
    assert r["swipes"].count("SHOW") == 2
    assert any("didn't follow the swipe of SHOW" in m for m in r["events"])
    assert not any("word(s) still to find" in m for m in r["events"])   # (the simulated level never ends)
    assert not any("redoing" in m for m in r["events"])


def test_long_ignored_swipe_is_redone_later(tmp_path):
    """The game ignores SHOW six times (a long animation): the bot gives up on it for the moment, then redoes it
    after checking the list, and it's taken."""
    r = run(tmp_path, f'''
        game = SimGame(cv2.imread({str(SCREENS / "puzzle_magic.png")!r}), drop={{"SHOW": 6}})
        print(json.dumps(play(game)))
    ''')
    assert len(r["taken"]) == 6
    assert any("ignored SHOW four times" in m for m in r["events"])
    assert r["paused"] is False


def test_selection_check_on_the_phone():
    """Frames from the phone (scrcpy) before and during a swipe of N-S-K on level 261: the game's selection is
    seen along that line, and not along a line it didn't touch."""
    sys.path.insert(0, str(ROOT))
    import cv2
    from wordbot.glyphs import GlyphReader
    from wordbot.vision import read_puzzle, selection_shown
    before, during = cv2.imread(str(SCREENS / "selection_before.png")), cv2.imread(str(SCREENS / "selection_during.png"))
    g = read_puzzle(before, GlyphReader(ROOT / "templates"), with_title=False).grid
    assert selection_shown(before, during, [g.centers[5][c] for c in (1, 2, 3)])
    assert not selection_shown(before, during, [g.centers[3][c] for c in (1, 2, 3)])
    assert not selection_shown(before, before, [g.centers[5][c] for c in (1, 2, 3)])
