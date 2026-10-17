"""The bot must never buy anything (2026-10-11: a paid hint without enough coins opened the game's shop, the
unfamiliar-button fallback tapped a price, and Google Play's purchase sheet came up). Every test here uses a
fake phone that records each tap and key press.

    .venv/bin/python -m pytest tests/test_purchases.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
SCREENS = Path(__file__).resolve().parent / "screens"
sys.path.insert(0, str(ROOT))

from wordbot.screens import classify, coin_balance, looks_like_purchase  # noqa: E402


def img(name: str):
    return cv2.imread(str(SCREENS / name))


def test_shop_is_recognised():
    s = classify(img("shop.png"))
    assert s.kind == "shop"


def test_prices_look_like_purchases():
    # the OCR clean-up turns "6.49" into "6 49"
    for label in ("6 49", "$4.89", "10 99", "SUPER PACK", "REMOVE ADS", "AGREE", "BUY NOW"):
        assert looks_like_purchase(label), label
    for label in ("NEXT LEVEL", "CONTINUE", "COLLECT", "CLAIM", "GOT IT", "SEE ALL BADGES"):
        assert not looks_like_purchase(label), label


def test_normal_buttons_still_work():
    assert classify(img("next_level.png")).detail == "NEXT LEVEL"
    assert classify(img("daily_wisdom_continue.png")).detail == "CONTINUE"
    assert classify(img("bonus_words_popup.png")).detail == "CLAIM"


def test_coin_balance():
    assert coin_balance(img("hidden_words_55_coins.png")) == 55
    assert coin_balance(img("picture_L127_stuck.png")) == 213


FAKE = '''
import json, os, sys, time
sys.path.insert(0, ROOT)
os.environ["WORDBOT_HOME"] = HOME
import cv2
from wordbot.bot import Bot
from wordbot.config import Config
from wordbot.device import DeviceError
from wordbot.screens import classify


class FakePhone:
    mode, serial, size, error = "android", "fake", (1080, 2400), None
    last_frame, last_frame_time = None, 0.0

    def __init__(self, frame, front="in.playsimple.wordsearch", after_tap=None):
        self.frame, self.front, self.after_tap = frame, front, after_tap
        self.taps, self.keys, self.launches = [], [], []

    def screenshot(self, lossless=False): return self.frame
    def live_frame(self): return self.frame, time.time()
    def tap(self, x, y):
        self.taps.append((x, y))
        if self.after_tap is not None:
            self.frame = self.after_tap
    def swipe(self, *a, **k): self.taps.append(("swipe",) + a)
    def back(self): self.keys.append("back")
    def key(self, name): self.keys.append(name)
    def focus(self): return self.front, (self.front + ".MainActivity")
    def foreground(self): return self.front
    def launch_game(self): self.launches.append(1)
    def battery(self): return 80
    def connect(self): return self.mode
    def ensure(self): pass
    def close(self): pass


def make(phone, **cfg):
    import shutil
    from pathlib import Path
    (Path(HOME) / "templates").mkdir(parents=True, exist_ok=True)   # seeded like the app's first run
    for f in (Path(ROOT) / "templates").glob("*.png"):
        shutil.copy(f, Path(HOME) / "templates" / f.name)
    c = Config(Path(HOME) / "config.json")
    c.update({"logging": {"save_levels": False, "save_unknown": False}, "flow": {"unknown_wait_s": 0}, **cfg})
    bot = Bot(c, device=phone)     # its play loop stays idle: driven from here
    return bot


def result(phone, bot):
    return {"taps": phone.taps, "keys": phone.keys, "launches": len(phone.launches),
            "events": [e["msg"] for e in bot.events]}
'''


def run(tmp_path: Path, body: str) -> dict:
    script = tmp_path / "case.py"
    script.write_text(f"ROOT = {str(ROOT)!r}\nHOME = {str(tmp_path / 'home')!r}\n" + FAKE + textwrap.dedent(body))
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_shop_only_gets_back(tmp_path):
    r = run(tmp_path, f'''
        phone = FakePhone(cv2.imread({str(SCREENS / "shop.png")!r}))
        bot = make(phone, flow={{"unknown_wait_s": 0, "tap_unknown_green": True}})
        for _ in range(3):
            bot.tick()
        print(json.dumps(result(phone, bot)))
    ''')
    assert r["taps"] == [] and r["keys"] == ["back"] * 3


def test_google_play_in_front_is_never_tapped(tmp_path):
    """The purchase sheet: back to cancel it, and no tap of any kind, even with every tapping option on."""
    r = run(tmp_path, f'''
        phone = FakePhone(cv2.imread({str(SCREENS / "google_play_agree.png")!r}), front="com.android.vending")
        bot = make(phone, flow={{"unknown_wait_s": 0, "tap_unknown_green": True, "unknown_action": "tap_center"}})
        for _ in range(4):
            bot._last_fg_check = 0
            bot.tick()
        print(json.dumps(result(phone, bot)))
    ''')
    assert r["taps"] == []
    assert "back" in r["keys"]
    assert r["launches"] == 0      # back first, not the game launched over the sheet


def test_safe_tap_refuses_other_apps(tmp_path):
    r = run(tmp_path, f'''
        phone = FakePhone(cv2.imread({str(SCREENS / "next_level.png")!r}), front="com.android.vending")
        bot = make(phone)
        ok = bot._safe_tap(540, 1755, "Next Level")
        print(json.dumps({{"ok": ok, **result(phone, bot)}}))
    ''')
    assert r["ok"] is False and r["taps"] == []


def test_paid_hint_needs_coins(tmp_path):
    """Hints cost 100 and only 55 coins are on screen: the hint button is not tapped, even when allowed."""
    r = run(tmp_path, f'''
        from wordbot.vision import read_puzzle
        phone = FakePhone(cv2.imread({str(SCREENS / "hidden_words_55_coins.png")!r}))
        bot = make(phone, hunt={{"paid_hints": True}})
        bot.level = {{"key": "L145"}}
        p = read_puzzle(phone.frame, bot.reader, with_title=False)
        found = bot.hint_assist(p, 1)
        print(json.dumps({{"found": found, **result(phone, bot)}}))
    ''')
    assert r["taps"] == [] and r["found"] == 0
    assert any("55 coins" in m for m in r["events"])


def test_hint_that_opens_the_shop_is_backed_out_of(tmp_path):
    """If a hint tap ever lands on the shop anyway, the bot goes back and stops using hints."""
    r = run(tmp_path, f'''
        from wordbot.vision import read_puzzle
        phone = FakePhone(cv2.imread({str(SCREENS / "picture_L127_stuck.png")!r}),
                          after_tap=cv2.imread({str(SCREENS / "shop.png")!r}))
        bot = make(phone, hunt={{"paid_hints": True}})
        bot.level = {{"key": "L127"}}
        p = read_puzzle(phone.frame, bot.reader, with_title=False)
        bot.hint_assist(p, 1)
        again = bot.hint_assist(p, 1)
        print(json.dumps({{"hints_off": bot.hints_off, "again": again, **result(phone, bot)}}))
    ''')
    assert len(r["taps"]) == 1 and r["keys"] == ["back"]   # the one hint tap (213 coins), then out of the shop
    assert r["hints_off"] is True and r["again"] == 0


# --- after-level stalls (phone, 2026-10-11 evening) --------------------------------------

def test_next_level_with_a_twister_tag():
    """A red "Twister" ribbon over the Next Level button made its label unreadable: the bot stalled there."""
    s = classify(img("next_level_twister.png"))
    assert s.kind == "button" and s.detail == "NEXT LEVEL"


def test_green_title_bar_is_not_a_button():
    """A green-themed puzzle's title bar ("0 BIRD", with the race counter) must not count as a priced button."""
    from wordbot.screens import green_buttons
    assert green_buttons(img("hidden_words.png")) == []


def test_opposite_level_never_swipes_the_clues(tmp_path):
    """"Find the opposite" (Twister) levels: PURE is a clue that happens to be in the grid. It is not swiped;
    every clue still needs its answer found."""
    r = run(tmp_path, f'''
        phone = FakePhone(cv2.imread({str(SCREENS / "opposite_with_clue_in_grid.png")!r}))
        bot = make(phone, dry_run=True)
        bot.active = False
        bot._wait_still = lambda *a, **k: None
        bot.play_level(manual=True)
        print(json.dumps({{"plan": {{i["word"]: i["status"] for i in bot.plan}}, **result(phone, bot)}}))
    ''')
    assert r["taps"] == []
    assert r["plan"].get("PURE") == "clue"


def test_duel_screens():
    """After a Wordsmith Duel: Next Level on a green background, and the rematch popup, where Play Later is the
    way on (a cloud in its sky was taken for a tutorial arrow, so the bot tapped the sky)."""
    assert classify(img("next_level_duel.png")).detail == "NEXT LEVEL"
    s = classify(img("duel_play_later.png"))
    assert s.kind == "button" and s.detail == "PLAY LATER"
    for name in ("tutorial_up.png", "tutorial_down.png", "tutorial_left.png"):
        assert classify(img(name)).kind == "tutorial", name
