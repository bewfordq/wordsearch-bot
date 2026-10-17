"""Everything that isn't the puzzle: level complete, popups, home screen. The game's action buttons are
bright green pills with a white label, so they're found by colour and their label is read with Tesseract."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .vision import Box, find_layout

try:
    import pytesseract
except ImportError:
    pytesseract = None


@dataclass
class Button:
    label: str
    box: Box

    def to_dict(self) -> dict:
        return {"label": self.label, "box": self.box.to_list()}


def _clean(txt: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 !'?]", " ", txt)).strip().upper()


def _read_white_text(img: np.ndarray, box: Box) -> str:
    if pytesseract is None:
        return ""
    crop = img[box.y:box.y + box.h, box.x:box.x + box.w]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    text = (hsv[:, :, 2] > 200) & (hsv[:, :, 1] < 70)
    bw = np.where(text, 0, 255).astype(np.uint8)
    if bw.shape[0] < 60:
        f = 60 / bw.shape[0]
        bw = cv2.resize(bw, None, fx=f, fy=f, interpolation=cv2.INTER_NEAREST)
    bw = cv2.copyMakeBorder(bw, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
    line = _clean(pytesseract.image_to_string(bw, config="--psm 7"))
    if any(_label_is(line, a) for a in ADVANCE + DISMISS):
        return line
    # A tag over the button (Next Level with a red "Twister" ribbon) breaks the one-line read: read it as a block.
    block = _clean(pytesseract.image_to_string(bw, config="--psm 6"))
    return block if any(_label_is(block, a) for a in ADVANCE + DISMISS) else line


def _pills(img: np.ndarray, ranges, read: bool) -> list[Button]:
    H, W = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    boxes: list[Box] = []
    for lo, hi in ranges:
        m = cv2.inRange(hsv, lo, hi)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)))
        n, _, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        for x, y, w, h, a in stats[1:]:
            # (not the full-width green title bar of a green-themed puzzle: no button is that wide)
            if w < 0.15 * W or w > 0.8 * W or h < 0.032 * H or h > 0.12 * H or w < 1.6 * h or a < 0.6 * w * h:
                continue
            b = Box(int(x), int(y), int(w), int(h))
            if not any(abs(b.cx - o.cx) < o.w / 2 and abs(b.cy - o.cy) < o.h / 2 for o in boxes):
                boxes.append(b)
    out = [Button(_read_white_text(img, b) if read else "", b) for b in boxes]
    return sorted(out, key=lambda b: -b.box.w * b.box.h)


# The game's green; then only its brightest part, for a button on a green background (the duel's level-complete
# screen) that the first pass merges with the background.
_GREEN = [((45, 150, 140), (66, 255, 255)), ((45, 170, 190), (66, 255, 255))]
_BLUE = [((95, 140, 200), (110, 255, 255))]


def green_buttons(img: np.ndarray, read: bool = True) -> list[Button]:
    """Bright green pill buttons (Next Level, Continue, ...), largest first."""
    return _pills(img, _GREEN, read)


def blue_buttons(img: np.ndarray, read: bool = True) -> list[Button]:
    """Blue pill buttons: the "no" choice on some popups (Play Later next to Duel Again)."""
    return _pills(img, _BLUE, read)


def screen_text(img: np.ndarray, scale: float = 0.5) -> list[tuple[str, Box]]:
    """Words on screen (dark text and white text) with their boxes, for unknown screens."""
    if pytesseract is None:
        return []
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    white_text = np.where((hsv[:, :, 2] > 215) & (hsv[:, :, 1] < 50), 0, 255).astype(np.uint8)
    dark_text = np.where(gray < 80, 0, 255).astype(np.uint8)
    out: list[tuple[str, Box]] = []
    for im in (white_text, dark_text):
        d = pytesseract.image_to_data(im, config="--psm 11", output_type=pytesseract.Output.DICT)
        for txt, conf, x, y, w, h in zip(d["text"], d["conf"], d["left"], d["top"], d["width"], d["height"]):
            t = _clean(txt)
            if len(t) >= 2 and float(conf) > 55:
                out.append((t, Box(int(x / scale), int(y / scale), int(w / scale), int(h / scale))))
    return out


def level_number(img: np.ndarray) -> int | None:
    """'Level 7' above the puzzle."""
    if pytesseract is None:
        return None
    H, W = img.shape[:2]
    crop = img[int(0.04 * H):int(0.085 * H), int(0.3 * W):int(0.7 * W)]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    bw = np.where((hsv[:, :, 2] > 215) & (hsv[:, :, 1] < 60), 0, 255).astype(np.uint8)
    m = re.search(r"LEVEL\s*(\d+)", _clean(pytesseract.image_to_string(bw, config="--psm 7")))
    return int(m.group(1)) if m else None


_CLOSE_X = cv2.imread(str(Path(__file__).resolve().parent / "assets" / "close_x.png"))


def close_button(img: np.ndarray, min_score: float = 0.8) -> tuple[int, int] | None:
    """The round grey X that closes the game's popups."""
    if _CLOSE_X is None:
        return None
    best, where = 0.0, None
    for scale in (0.8, 0.9, 1.0, 1.15):
        t = cv2.resize(_CLOSE_X, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if mx > best:
            best, where = mx, (loc[0] + t.shape[1] // 2, loc[1] + t.shape[0] // 2)
    return where if best >= min_score else None


_BULB = cv2.imread(str(Path(__file__).resolve().parent / "assets" / "hint_bulb.png"), cv2.IMREAD_GRAYSCALE)


def hint_button(img: np.ndarray, min_score: float = 0.7) -> tuple[int, int] | None:
    """The light-bulb hint button under the grid. Matched on its white shape, so the theme colour of the
    buttons doesn't matter."""
    if _BULB is None:
        return None
    H = img.shape[0]
    y0 = int(0.7 * H)
    hsv = cv2.cvtColor(img[y0:], cv2.COLOR_BGR2HSV)
    # White while hints are free; once they cost coins the bulb is a smaller, pale tint above "100".
    white = (((hsv[:, :, 2] > 200) & (hsv[:, :, 1] < 110)).astype(np.uint8) * 255)
    best, where = 0.0, None
    for scale in (0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2):
        t = cv2.resize(_BULB, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        res = cv2.matchTemplate(white, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if mx > best:
            best, where = mx, (loc[0] + t.shape[1] // 2, y0 + loc[1] + t.shape[0] // 2)
    return where if best >= min_score else None


_RESTART = cv2.imread(str(Path(__file__).resolve().parent / "assets" / "restart.png"), cv2.IMREAD_GRAYSCALE)


def restart_button(img: np.ndarray, min_score: float = 0.7) -> tuple[int, int] | None:
    """The circular-arrow restart button under the grid, matched on its white shape like the hint bulb."""
    if _RESTART is None:
        return None
    H, W = img.shape[:2]
    y0, x0 = int(0.7 * H), int(0.6 * W)
    hsv = cv2.cvtColor(img[y0:, x0:], cv2.COLOR_BGR2HSV)
    white = (((hsv[:, :, 2] > 200) & (hsv[:, :, 1] < 110)).astype(np.uint8) * 255)
    best, where = 0.0, None
    for scale in (0.8, 0.9, 1.0, 1.1, 1.2):
        t = cv2.resize(_RESTART, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        res = cv2.matchTemplate(white, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if mx > best:
            best, where = mx, (x0 + loc[0] + t.shape[1] // 2, y0 + loc[1] + t.shape[0] // 2)
    return where if best >= min_score else None


def hint_is_free(img: np.ndarray, pos: tuple[int, int]) -> bool:
    """Free hints show a count badge (red or blue) on the bulb's top-right; without one a hint costs coins.
    The badge is a strong colour that isn't the button's own."""
    x, y = pos
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    button = hsv[y - 5:y + 5, max(0, x - 50):x - 40].reshape(-1, 3)
    button_hue = float(np.median(button[:, 0])) if len(button) else -100.0
    badge = hsv[max(0, y - 80):max(1, y - 15), x + 10:x + 80].reshape(-1, 3)
    strong = badge[(badge[:, 1] > 120) & (badge[:, 2] > 150)]
    if len(strong) == 0:
        return False
    dh = np.abs(strong[:, 0].astype(float) - button_hue)
    other = np.minimum(dh, 180 - dh) > 15
    return float(other.sum()) > 0.12 * len(badge)


def tutorial_arrow(img: np.ndarray) -> tuple[int, int] | None:
    """Tutorials dim the screen and point a white arrow (up, down, left or right) at what to tap. Returns the
    point the arrow points at."""
    H, W = img.shape[:2]
    # Tutorials dim everything around the bubble (median brightness < 90; ordinary screens are > 170).
    if float(np.median(cv2.cvtColor(img[::4, ::4], cv2.COLOR_BGR2HSV)[:, :, 2])) > 130:
        return None
    white = np.all(img > 240, axis=2).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(white, connectivity=8)
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i])
        long_, short = max(w, h), min(w, h)
        if not (0.05 * W <= short <= 0.12 * W and 1.2 * short <= long_ <= 2.0 * short and 0.35 <= a / (w * h) <= 0.6):
            continue
        m = lab[y:y + h, x:x + w] == i
        # The arrow stands out from the dimmed screen; a cloud in a pale sky (the duel popup) fades into it.
        x0, y0 = max(0, x - 6), max(0, y - 6)
        near = lab[y0:y + h + 6, x0:x + w + 6] == i
        ring = cv2.dilate(near.astype(np.uint8), np.ones((9, 9), np.uint8)).astype(bool) & ~near
        gray = cv2.cvtColor(img[y0:y + h + 6, x0:x + w + 6], cv2.COLOR_BGR2GRAY)
        if ring.any() and float(gray[ring].mean()) > 190:
            continue
        vertical = h > w
        prof = m.sum(axis=1) / w if vertical else m.sum(axis=0) / h
        q = max(1, len(prof) // 4)
        first, last = prof[:q].mean(), prof[-q:].mean()
        reach = 1.1 * long_
        if first > 0.5 and last < 0.4:        # head at the top / left
            return (x + w // 2, max(0, int(y - reach))) if vertical else (max(0, int(x - reach)), y + h // 2)
        if last > 0.5 and first < 0.4:        # head at the bottom / right
            return (x + w // 2, min(H - 1, int(y + h + reach))) if vertical else (min(W - 1, int(x + w + reach)), y + h // 2)
    return None


# --- classification ---------------------------------------------------------------------

ADVANCE = ("NEXT LEVEL", "CONTINUE", "COLLECT", "CLAIM", "GOT IT", "OKAY", "OK", "TAP TO CONTINUE", "PLAY",
           "START", "AWESOME", "THANKS", "NEXT", "SEE ALL BADGES")
# The "no thanks" choice on a popup that offers something else (a duel rematch): tapped to carry on.
DISMISS = ("PLAY LATER", "MAYBE LATER", "NOT NOW", "NO THANKS", "LATER", "SKIP")
# Anything that could cost money. The OCR clean-up drops currency signs and "." ("6.49" reads "6 49"), so any digit on a
# green button counts as a price too. A screen with such a button is a shop: nothing on it is ever tapped.
PURCHASE_WORDS = ("BUY", "PURCHASE", "PAY", "PRICE", "SHOP", "STORE", "PACK", "OFFER", "DEAL", "SALE", "SUBSCRIBE",
                  "TRIAL", "REMOVE ADS", "NO ADS", "UNLOCK", "VIP", "PREMIUM", "AGREE", "CONFIRM", "CHECKOUT",
                  "COINS", "GEMS", "BUNDLE", "SPECIAL")


def looks_like_purchase(text: str) -> bool:
    t = text.upper()
    return bool(re.search(r"\d", t)) or any(c in t for c in "$£€¥₹₩") or \
        any(re.search(rf"(^|[^A-Z]){re.escape(w)}([^A-Z]|$)", t) for w in PURCHASE_WORDS)


# Long enough to match even when Tesseract runs the words together or adds junk ("R GOTIT!").
_LOOSE = ("NEXT LEVEL", "CONTINUE", "COLLECT", "CLAIM", "GOT IT", "AWESOME", "SEE ALL BADGES", "TAP TO CONTINUE",
          "PLAY LATER", "MAYBE LATER", "NOT NOW", "NO THANKS")


def _label_is(label: str, phrase: str) -> bool:
    # whole words anywhere in the label: a shine on the button can read as junk ("Q CLAIM")
    if re.search(rf"(^|\s){re.escape(phrase)}(\s|!|$)", label):
        return True
    return phrase in _LOOSE and re.sub(r"[^A-Z]", "", phrase) in re.sub(r"[^A-Z]", "", label)


def safe_label(label: str) -> bool:
    return bool(label) and not looks_like_purchase(label)


def coin_balance(img: np.ndarray) -> int | None:
    """The coins shown at the top right of a puzzle ("213"), or None if they can't be read."""
    if pytesseract is None:
        return None
    H, W = img.shape[:2]
    crop = img[int(0.035 * H):int(0.09 * H), int(0.6 * W):int(0.85 * W)]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    # Only inside the dark pill behind the number: the sky around it can be just as white.
    dark = cv2.morphologyEx((hsv[:, :, 2] < 120).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(dark, connectivity=8)
    if n < 2:
        return None
    x, y, w, h, _ = max(st[1:], key=lambda t: t[4])
    pill = hsv[y:y + h, x:x + w]
    bw = np.where((pill[:, :, 2] > 200) & (pill[:, :, 1] < 60), 0, 255).astype(np.uint8)
    bw = cv2.copyMakeBorder(bw, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
    m = re.search(r"\d+", pytesseract.image_to_string(bw, config="--psm 7 -c tessedit_char_whitelist=0123456789"))
    return int(m.group()) if m else None


@dataclass
class Screen:
    kind: str                       # puzzle | shop | button | tutorial | popup | button? | unknown
    buttons: list[Button]
    detail: str = ""
    target: tuple[int, int] | None = None   # where a tutorial wants a tap

    def to_dict(self) -> dict:
        return {"kind": self.kind, "buttons": [b.to_dict() for b in self.buttons], "detail": self.detail,
                "target": list(self.target) if self.target else None}


def classify(img: np.ndarray) -> Screen:
    if find_layout(img) is not None:
        return Screen("puzzle", [])
    buttons = green_buttons(img)
    blue = blue_buttons(img)
    # A price anywhere means a shop or an offer: never tap on it, only go back (2026-10-11: a paid hint without
    # enough coins opened the shop, and the unfamiliar-button fallback tapped a price).
    if any(looks_like_purchase(b.label) for b in buttons + blue):
        return Screen("shop", buttons + blue, "a screen selling something")
    for b in buttons:
        for a in ADVANCE:
            if _label_is(b.label, a):
                b.label = a
                return Screen("button", buttons + blue, a)
    for b in blue + buttons:
        for a in DISMISS:
            if _label_is(b.label, a):
                b.label = a
                return Screen("button", blue + buttons, a)
    arrow = tutorial_arrow(img)
    if arrow is not None:
        return Screen("tutorial", buttons, "tutorial arrow", arrow)
    x = close_button(img)
    if x is not None:   # before unfamiliar green buttons: a popup selling something gets closed, not bought
        return Screen("popup", buttons, "popup with a close button", x)
    if buttons:
        return Screen("button?", buttons, buttons[0].label)
    return Screen("unknown", [])
