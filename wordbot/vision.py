"""Reads the Word Search screen: where the word list and letter grid are, which letters and words they hold,
and which words are already found (found words turn light grey in the list)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .glyphs import ALPHABET, GlyphReader, normalize

try:
    import pytesseract
except ImportError:
    pytesseract = None

INK = 90            # grid letters are ~0 grey, even on the coloured highlight capsules (~150-200)
LIST_INK = 232      # word list text: black (~15) or, once found, light grey (~204), on white
FOUND_DARK = 120    # an unfound word has black pixels; a found one has none darker than ~200


def _saturated(bgr: np.ndarray) -> np.ndarray:
    """Strongly coloured pixels: hint circles in the grid, hint pills in the word list."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 120)


def _colour_shapes(bgr: np.ndarray) -> list[np.ndarray]:
    """Convex hulls of the strongly coloured shapes. Convex hulls, not just outlines: a letter touching a
    pill's edge (the K of BOOK) breaks the outline open, but circles and pills are convex."""
    sat = _saturated(bgr).astype(np.uint8) * 255
    sat = cv2.morphologyEx(sat, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(sat, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return [cv2.convexHull(c) for c in contours if cv2.contourArea(c) > 150]


def _white_on_colour(bgr: np.ndarray, wide_only: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """(white text inside coloured shapes, all the filled shapes), as uint8 masks. Hints show letters this
    way: the first letter of a word as a white letter on a coloured circle, the word itself in a coloured
    pill. With `wide_only`, only pills count as text: round icons in the word list (coins, race tokens)
    aren't letters."""
    hulls = _colour_shapes(bgr)
    filled = np.zeros(bgr.shape[:2], np.uint8)
    cv2.drawContours(filled, hulls, -1, 255, cv2.FILLED)
    text_area = filled
    if wide_only:
        text_area = np.zeros_like(filled)
        # icons are about square (coins 1.0, race tokens 1.15); pills are wider, even around a short word
        wide = [h for h in hulls if cv2.boundingRect(h)[2] > 1.4 * cv2.boundingRect(h)[3]]
        cv2.drawContours(text_area, wide, -1, 255, cv2.FILLED)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    white = (text_area > 0) & (hsv[:, :, 2] > 200) & (hsv[:, :, 1] < 70)
    return white.astype(np.uint8) * 255, filled


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def to_list(self) -> list[int]:
        return [int(self.x), int(self.y), int(self.w), int(self.h)]


@dataclass
class Word:
    text: str
    box: Box
    found: bool = False
    raw: str = ""

    def to_dict(self) -> dict:
        return {"text": self.text, "box": self.box.to_list(), "found": self.found, "raw": self.raw}


@dataclass
class Grid:
    rows: int
    cols: int
    board: Box
    centers: list[list[tuple[int, int]]]            # [row][col] -> screen (x, y)
    letters: list[list[str]]
    conf: list[list[float]]
    source: list[list[str]]
    glyphs: list[list[np.ndarray]] = field(repr=False, default_factory=list)
    pitch: float = 0.0
    hinted: list[tuple[int, int]] = field(default_factory=list)   # cells shown as a hint (white on a circle)

    def text(self) -> list[str]:
        return ["".join(r) for r in self.letters]

    def to_dict(self) -> dict:
        return {"rows": self.rows, "cols": self.cols, "board": self.board.to_list(), "pitch": round(self.pitch, 1),
                "letters": self.letters, "conf": [[round(c, 2) for c in r] for r in self.conf],
                "source": self.source, "centers": [[list(map(int, c)) for c in r] for r in self.centers],
                "hinted": [list(c) for c in self.hinted]}


@dataclass
class Puzzle:
    title: str
    words: list[Word]
    grid: Grid
    list_box: Box
    kind: str = "words"                      # words | pictures (the list shows pictures) | quote (fill the blanks)
    tiles: list[Box] = field(default_factory=list)
    blanks: list[int] = field(default_factory=list)   # quote: letters in each blank
    hidden: list[int] = field(default_factory=list)   # hidden words (rows of coins): letters in each

    def to_dict(self) -> dict:
        return {"title": self.title, "words": [w.to_dict() for w in self.words], "grid": self.grid.to_dict(),
                "list_box": self.list_box.to_list(), "kind": self.kind, "tiles": [t.to_list() for t in self.tiles],
                "blanks": self.blanks, "hidden": self.hidden}


class VisionError(RuntimeError):
    pass


# --- layout ---------------------------------------------------------------------------

def white_panels(img: np.ndarray) -> list[Box]:
    """The big white rounded panels (word list, letter grid), top to bottom."""
    H, W = img.shape[:2]
    white = np.all(img > 235, axis=2).astype(np.uint8)
    # (Wu's algorithm named outright: OpenCV 4.5's default for 4-connectivity, which the Android app's OpenCV
    # is, can crash on big images)
    n, _, stats, _ = cv2.connectedComponentsWithStatsWithAlgorithm(white, 4, cv2.CV_32S, cv2.CCL_WU)
    out = []
    for x, y, w, h, area in stats[1:]:
        if w > 0.8 * W and h > 0.06 * H and area > 0.3 * w * h:
            out.append(Box(int(x), int(y), int(w), int(h)))
    return sorted(out, key=lambda b: b.y)


def _is_header(px: np.ndarray) -> bool:
    """The title bar over the word list is a strong colour: green, orange, ... depending on the country."""
    hi, lo = int(max(px)), int(min(px))
    return hi > 120 and hi - lo > 60


def _title_bar(img: np.ndarray, lst: Box) -> Box:
    """Follow the bar's colour up and down from just above the list, in columns near the bar's ends (long
    titles reach far across). Some themes draw a ribbon that reaches down over the top of the list."""
    H = img.shape[0]
    tops, bottoms = [], []
    for f in (0.12, 0.2, 0.8, 0.88):
        x = lst.x + int(f * lst.w)
        y = lst.y - 10
        if not _is_header(img[y, x]):
            continue
        top = bottom = y
        ref = img[y, x].astype(int)
        while top > 0 and _is_header(img[top - 1, x]) and np.abs(img[top - 1, x].astype(int) - ref).max() < 70:
            top -= 1
            ref = img[top, x].astype(int)
        ref = img[y, x].astype(int)
        while bottom < H - 1 and _is_header(img[bottom + 1, x]) and \
                np.abs(img[bottom + 1, x].astype(int) - ref).max() < 70:
            bottom += 1
            ref = img[bottom, x].astype(int)
        tops.append(top)
        bottoms.append(bottom)
    if not tops:
        return Box(lst.x, max(0, lst.y - 90), lst.w, 90)
    return Box(lst.x, min(tops), lst.w, max(bottoms) - min(tops) + 1)


def find_layout(img: np.ndarray) -> tuple[Box, Box, Box] | None:
    """(title bar, word list, grid board) if this is a puzzle screen, else None."""
    panels = white_panels(img)
    for i in range(len(panels) - 1):
        lst, board = panels[i], panels[i + 1]
        if board.h < lst.h or board.y - (lst.y + lst.h) > 120:
            continue
        # The board lines up with the list; a popup over the board (white, narrower) doesn't.
        if abs(board.x - lst.x) > 20 or abs(board.w - lst.w) > 40:
            continue
        # The coloured title bar sits right on top of the word list.
        y0 = max(0, lst.y - 100)
        strip = img[y0:lst.y - 4, lst.x + lst.w // 4: lst.x + 3 * lst.w // 4]
        if strip.size == 0:
            continue
        coloured = np.mean([_is_header(p) for p in strip.reshape(-1, 3)[::37]])
        if coloured > 0.5:
            title = _title_bar(img, lst)
            bottom = title.y + title.h
            if bottom > lst.y:  # a ribbon reaching over the list: the list starts below it
                lst = Box(lst.x, bottom, lst.w, lst.y + lst.h - bottom)
            return title, lst, board
    return None


def is_puzzle(img: np.ndarray) -> bool:
    return find_layout(img) is not None


# --- grid -----------------------------------------------------------------------------

def _clusters(values: list[float], gap: float) -> list[list[int]]:
    """Indices of `values` grouped into runs whose neighbours are closer than `gap`."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    groups: list[list[int]] = []
    for i in order:
        if groups and values[i] - values[groups[-1][-1]] < gap:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def locate_cells(img: np.ndarray, board: Box) -> tuple[list[list[Box | None]], list[float], list[float], np.ndarray,
                                                       list[tuple[int, int]]]:
    """Letter boxes per [row][col], column x centres, row y centres, the ink mask of the board, and the cells
    holding a hint (a white letter on a coloured circle)."""
    pad = 10
    x0, y0 = board.x + pad, board.y + pad
    crop = img[y0:board.y + board.h - pad, x0:board.x + board.w - pad]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    ink = (gray < INK).astype(np.uint8) * 255
    n, lab, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    comps = [(int(x), int(y), int(w), int(h)) for x, y, w, h, a in stats[1:] if h >= 18 and a >= 60]
    if len(comps) < 4:
        raise VisionError("no letters found on the board")
    hmed = float(np.median([c[3] for c in comps]))
    comps = [c for c in comps if 0.6 * hmed <= c[3] <= 1.6 * hmed]
    xs = [c[0] + c[2] / 2 for c in comps]
    ys = [c[1] + c[3] / 2 for c in comps]
    col_groups = _clusters(xs, 0.6 * hmed)
    row_groups = _clusters(ys, 0.6 * hmed)
    col_of = {i: ci for ci, g in enumerate(col_groups) for i in g}
    row_of = {i: ri for ri, g in enumerate(row_groups) for i in g}
    cols = [float(np.mean([xs[i] for i in g])) + x0 for g in col_groups]
    rows = [float(np.mean([ys[i] for i in g])) + y0 for g in row_groups]
    cells: list[list[Box | None]] = [[None] * len(cols) for _ in rows]
    for i, (x, y, w, h) in enumerate(comps):
        r, c = row_of[i], col_of[i]
        b = Box(x + x0, y + y0, w, h)
        old = cells[r][c]
        if old is None:
            cells[r][c] = b
        else:  # two pieces in one cell: keep the union
            nx, ny = min(old.x, b.x), min(old.y, b.y)
            cells[r][c] = Box(nx, ny, max(old.x + old.w, b.x + b.w) - nx, max(old.y + old.h, b.y + b.h) - ny)
    full = np.zeros(img.shape[:2], np.uint8)
    full[y0:y0 + ink.shape[0], x0:x0 + ink.shape[1]] = ink
    # Cells with no black letter may hold a hint: a white letter on a coloured circle.
    pitch = float(np.median(np.diff(cols))) if len(cols) > 1 else 2 * hmed
    half = int(0.45 * pitch)
    hinted: list[tuple[int, int]] = []
    for r, cy in enumerate(rows):
        for c, cx in enumerate(cols):
            if cells[r][c] is not None:
                continue
            ax, ay = max(0, int(cx) - half), max(0, int(cy) - half)
            white, _ = _white_on_colour(img[ay:int(cy) + half, ax:int(cx) + half])
            n2, _, st2, _ = cv2.connectedComponentsWithStats(white, connectivity=8)
            parts = [t for t in st2[1:] if t[3] >= 0.6 * hmed and t[4] >= 60]
            if not parts:
                continue
            bx = min(t[0] for t in parts)
            by = min(t[1] for t in parts)
            bx2 = max(t[0] + t[2] for t in parts)
            by2 = max(t[1] + t[3] for t in parts)
            cells[r][c] = Box(ax + bx, ay + by, bx2 - bx, by2 - by)
            hinted.append((r, c))
            region = full[ay + by:ay + by2, ax + bx:ax + bx2]
            region[:] = np.maximum(region, white[by:by2, bx:bx2])
    return cells, cols, rows, full, hinted


def read_grid(img: np.ndarray, board: Box, reader: GlyphReader) -> Grid:
    cells, cols, rows, ink, hinted = locate_cells(img, board)
    R, C = len(rows), len(cols)
    if R < 3 or C < 3:
        raise VisionError(f"grid too small ({R}x{C})")
    missing = [(r, c) for r in range(R) for c in range(C) if cells[r][c] is None]
    if missing:
        raise VisionError(f"{len(missing)} empty cells in a {R}x{C} grid, e.g. {missing[:3]}")
    glyphs = []
    for r in range(R):
        row = []
        for c in range(C):
            b = cells[r][c]
            row.append(normalize(ink[b.y:b.y + b.h, b.x:b.x + b.w]))
        glyphs.append(row)
    flat = reader.read([g for row in glyphs for g in row])
    letters = [[flat[r * C + c][0] for c in range(C)] for r in range(R)]
    conf = [[flat[r * C + c][1] for c in range(C)] for r in range(R)]
    source = [[flat[r * C + c][2] for c in range(C)] for r in range(R)]
    pitch = float(np.mean([np.diff(cols).mean() if C > 1 else 0, np.diff(rows).mean() if R > 1 else 0]))
    centers = [[(int(round(cols[c])), int(round(rows[r]))) for c in range(C)] for r in range(R)]
    return Grid(R, C, board, centers, letters, conf, source, glyphs, pitch, hinted)


# --- word list ------------------------------------------------------------------------

def _list_ink(bgr: np.ndarray) -> np.ndarray:
    """Word list text as a mask: black or grey text on white, plus white text in hint pills."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    white, shapes = _white_on_colour(bgr, wide_only=True)
    shapes = cv2.dilate(shapes, np.ones((9, 9), np.uint8))  # icons have a dark outline outside the colour
    dark = (gray < LIST_INK) & (shapes == 0)
    return (dark | (white > 0)).astype(np.uint8) * 255


def word_boxes(img: np.ndarray, lst: Box) -> list[Box]:
    """One box per listed word, in reading order."""
    pad = 8
    x0, y0 = lst.x + pad, lst.y + pad
    ink = _list_ink(img[y0:lst.y + lst.h - pad, x0:lst.x + lst.w - pad])
    n, lab, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    comps = [s for s in stats[1:] if s[3] >= 10 and s[4] >= 15]
    if not comps:
        return []
    th = float(np.median([s[3] for s in comps]))
    # Sequence levels put arrows between the words (SEED → ____ → ...): thin shaft, tall head. Drop them.
    for i, (x, y, w, h, a) in enumerate(stats[1:], start=1):
        if 0.5 * th <= h <= 1.05 * th and 1.1 * h <= w <= 2.2 * h and a <= 0.6 * w * h:
            cols = (lab[y:y + h, x:x + w] == i).sum(axis=0)
            if cols[: w // 2].mean() < 0.35 * h and cols.max() > 0.8 * h:
                ink[lab == i] = 0
    # Join the letters of a word (gaps ~0.15 of the text height) but not neighbouring words (~1x).
    k = max(3, int(round(th * 0.45)))
    merged = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 3)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)
    boxes = []
    for x, y, w, h, a in stats[1:]:
        if h < 0.6 * th or h > 2.5 * th:
            continue
        # undo the dilation's growth
        boxes.append(Box(int(x + x0 + k // 2), int(y + y0 + 1), int(max(1, w - 2 * (k // 2))), int(max(1, h - 2))))
    boxes.sort(key=lambda b: (b.cy, b.x))
    # group into lines, then left to right
    lines: list[list[Box]] = []
    for b in boxes:
        if lines and abs(b.cy - lines[-1][0].cy) < 0.6 * th:
            lines[-1].append(b)
        else:
            lines.append([b])
    return [b for line in lines for b in sorted(line, key=lambda b: b.x)]


def word_is_found(img: np.ndarray, box: Box) -> bool:
    crop = img[box.y:box.y + box.h, box.x:box.x + box.w]
    if crop.size == 0 or float(_saturated(crop).mean()) > 0.15:
        return False  # a word in a hint pill hasn't been found yet
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    ink = int(np.count_nonzero(gray < LIST_INK))
    dark = int(np.count_nonzero(gray < FOUND_DARK))
    return ink > 0 and dark < 0.08 * ink


def _ocr_word(img: np.ndarray, box: Box, mixed: bool = False) -> str:
    if pytesseract is None:
        return ""
    pad = 10
    crop = img[max(0, box.y - pad):box.y + box.h + pad, max(0, box.x - pad):box.x + box.w + pad]
    bw = np.where(_list_ink(crop) > 0, 0, 255).astype(np.uint8)
    if bw.shape[0] < 40:
        f = 40 / bw.shape[0]
        bw = cv2.resize(bw, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    bw = cv2.copyMakeBorder(bw, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)
    allowed = ALPHABET + ALPHABET.lower() if mixed else ALPHABET   # quotes are in mixed case
    txt = pytesseract.image_to_string(bw, config=f"--psm 7 -c tessedit_char_whitelist={allowed}")
    return txt.strip().upper()


def _letter_count(img: np.ndarray, box: Box) -> int:
    ink = _list_ink(img[box.y:box.y + box.h, box.x:box.x + box.w])
    n, _, st, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    return sum(1 for t in st[1:] if t[3] >= 0.5 * box.h)


def read_words(img: np.ndarray, lst: Box, mixed: bool = False) -> list[Word]:
    out = []
    for b in word_boxes(img, lst):
        raw = _ocr_word(img, b, mixed)
        text = re.sub(r"[^A-Z]", "", raw)
        # More letter shapes in the box than letters read means Tesseract dropped some (BOOK -> BOO), and a
        # shortened word could match the wrong letters in the grid. (Fewer is fine: letters can touch.)
        if text and _letter_count(img, b) > len(text):
            text = ""
        # A word Tesseract couldn't read stays in the list with no text, so the caller knows to look again.
        out.append(Word(text if len(text) >= 2 else "", b, word_is_found(img, b), raw))
    return out


# --- Picture Puzzle -----------------------------------------------------------------------

def picture_tiles(img: np.ndarray, lst: Box) -> list[Box]:
    """The square picture tiles of a Picture Puzzle list, in reading order (empty for a word list)."""
    W = img.shape[1]
    pad = 8
    x0, y0 = lst.x + pad, lst.y + pad
    crop = img[y0:lst.y + lst.h - pad, x0:lst.x + lst.w - pad]
    nonwhite = (crop.min(axis=2) < 235).astype(np.uint8) * 255
    nonwhite = cv2.morphologyEx(nonwhite, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    contours, _ = cv2.findContours(nonwhite, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    tiles = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if 0.08 * W <= w <= 0.22 * W and 0.08 * W <= h <= 0.22 * W and 0.75 <= w / h <= 1.33:
            tiles.append(Box(x + x0, y + y0, w, h))
    tiles.sort(key=lambda b: (b.cy, b.x))
    lines: list[list[Box]] = []
    for b in tiles:
        if lines and abs(b.cy - lines[-1][0].cy) < b.h // 2:
            lines[-1].append(b)
        else:
            lines.append([b])
    return [b for line in lines for b in sorted(line, key=lambda b: b.x)]


def quote_blanks(img: np.ndarray, lst: Box) -> list[int]:
    """Quotes and sequences show missing words as blanks of underscores, one per letter
    ("The ____ way to keep your ______ is ..."). Neighbouring underscores sometimes run together into one
    dash, so each dash counts as many letters as its width holds. Returns each blank's letter count."""
    pad = 8
    crop = img[lst.y + pad:lst.y + lst.h - pad, lst.x + pad:lst.x + lst.w - pad]
    ink = _list_ink(crop)
    n, _, st, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    comps = [t for t in st[1:] if t[3] >= 10]
    if not comps:
        return []
    th = float(np.median([t[3] for t in comps]))
    H = crop.shape[0]
    dashes = sorted(((int(x), int(y), int(w)) for x, y, w, h, a in st[1:]
                     if h <= max(3, 0.3 * th) and 0.4 * th <= w <= 16 * th and 4 <= y <= H - 8),  # not borders
                    key=lambda u: (u[1], u[0]))
    singles = [w for _, _, w in dashes if w <= 1.3 * th]
    if not singles:
        return []
    unit = float(np.median(singles))
    blanks: list[list[tuple[int, int, int]]] = []
    for u in dashes:
        last = blanks[-1][-1] if blanks else None
        if last and abs(u[1] - last[1]) <= 3 and 0 <= u[0] - (last[0] + last[2]) <= 0.4 * th:
            blanks[-1].append(u)
        else:
            blanks.append([u])
    counts = [sum(max(1, round((w + 2) / (unit + 2))) for _, _, w in b) for b in blanks]
    # Words of one or two letters are never hidden in the grid, and tile edges look like lone underscores.
    return [c for c in counts if 3 <= c <= 15]


_COIN = cv2.imread(str(Path(__file__).resolve().parent / "assets" / "coin.png"))


def hidden_slots(img: np.ndarray, lst: Box) -> list[int]:
    """Hidden words: a row of gold coins in the word list, one per letter. Returns each one's length.
    Coins are found by template (a race token touching a coin would merge with it as a blob)."""
    if _COIN is None:
        return []
    pad = 8
    crop = img[lst.y + pad:lst.y + lst.h - pad, lst.x + pad:lst.x + lst.w - pad]
    best = None
    for scale in (0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25):
        t = cv2.resize(_COIN, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if t.shape[0] >= crop.shape[0] or t.shape[1] >= crop.shape[1]:
            continue
        res = cv2.matchTemplate(crop, t, cv2.TM_CCOEFF_NORMED)
        if best is None or res.max() > best[0].max():
            best = (res, t.shape[1])
    if best is None or best[0].max() < 0.75:
        return []
    res, w = best
    coins: list[tuple[int, int]] = []
    ys, xs = np.nonzero(res >= 0.75)
    for i in np.argsort(-res[ys, xs]):           # strongest first; drop overlapping matches
        x, y = int(xs[i]), int(ys[i])
        if all(abs(x - cx) > 0.7 * w or abs(y - cy) > 0.7 * w for cx, cy in coins):
            coins.append((x, y))
    coins.sort(key=lambda c: (c[1], c[0]))
    groups: list[list[tuple[int, int]]] = []
    for c in sorted(coins, key=lambda c: (round(c[1] / (0.5 * w)), c[0])):
        last = groups[-1][-1] if groups else None
        if last and abs(c[1] - last[1]) < 0.4 * w and 0 < c[0] - last[0] <= 1.3 * w:
            groups[-1].append(c)
        else:
            groups.append([c])
    return [len(g) for g in groups if len(g) >= 3]


def tile_vividness(img: np.ndarray, box: Box) -> float:
    """Share of strongly coloured pixels; a found picture fades to almost none."""
    hsv = cv2.cvtColor(img[box.y:box.y + box.h, box.x:box.x + box.w], cv2.COLOR_BGR2HSV)
    return float((hsv[:, :, 1] > 100).mean())


def _patch(img: np.ndarray, x: float, y: float, r: int = 4) -> np.ndarray:
    x, y = int(round(x)), int(round(y))
    return img[max(0, y - r):y + r + 1, max(0, x - r):x + r + 1].reshape(-1, 3).astype(np.float32).mean(axis=0)


def _gaps(centers: list[tuple[int, int]]) -> list[tuple[float, float]]:
    return [((x1 + x2) / 2, (y1 + y2) / 2) for (x1, y1), (x2, y2) in zip(centers, centers[1:])]


def _is_background(px: np.ndarray) -> bool:
    return bool(px.min() > 215)


def line_covered(img: np.ndarray, centers: list[tuple[int, int]]) -> bool:
    """Every gap along these cells already lies in a found word's capsule (SKY inside HUSKY)."""
    gaps = _gaps(centers)
    return bool(gaps) and not any(_is_background(_patch(img, x, y)) for x, y in gaps)


def cell_hinted(img: np.ndarray, grid: Grid, r: int, c: int) -> bool:
    """Does this cell still show a hint (a white letter on a coloured circle)? It stops once its word is found."""
    x, y = grid.centers[r][c]
    half = int(0.45 * grid.pitch)
    white, _ = _white_on_colour(img[max(0, y - half):y + half, max(0, x - half):x + half])
    n, _, st, _ = cv2.connectedComponentsWithStats(white, connectivity=8)
    return any(t[3] >= 0.25 * grid.pitch and t[4] >= 60 for t in st[1:])


def board_visible(img: np.ndarray, grid: Grid) -> bool:
    """Every letter of the grid is on show: no toast ("Claim bonus word reward") or banner over it."""
    r = max(6, int(0.22 * grid.pitch))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hinted = set(map(tuple, grid.hinted))
    seen = total = 0
    for i, row in enumerate(grid.centers):
        for j, (x, y) in enumerate(row):
            if (i, j) in hinted:
                continue
            total += 1
            if gray[max(0, y - r):y + r, max(0, x - r):x + r].min() < INK:
                seen += 1
    return total == 0 or seen >= 0.97 * total


def line_inside(img: np.ndarray, centers: list[tuple[int, int]]) -> bool:
    """The line runs inside a found word's capsule all the way (ALL inside BALLOON): coloured not only at the
    gaps between letters but a quarter of the way either side too. A word that merely crosses other capsules
    at its gaps (BUG on level 127) shows background there."""
    pts = [(x1 + (x2 - x1) * f, y1 + (y2 - y1) * f)
           for (x1, y1), (x2, y2) in zip(centers, centers[1:]) for f in (0.25, 0.5, 0.75)]
    return bool(pts) and not any(_is_background(_patch(img, x, y, 3)) for x, y in pts)


def line_marked(before: np.ndarray, after: np.ndarray, centers: list[tuple[int, int]]) -> bool:
    """Did a found-word capsule appear along these cells? Looks at the gaps between neighbouring letters
    that were plain background before: nearly all of them must take one and the same capsule colour.
    Gaps already inside another capsule say nothing (the game flashes those when a word is re-swiped), and
    one tinted gap is just the edge of a capsule crossing nearby."""
    free = [(x, y) for x, y in _gaps(centers) if _is_background(_patch(before, x, y))]
    if not free:
        return False
    colours = []
    for x, y in free:
        a = _patch(after, x, y)
        if float(np.abs(_patch(before, x, y) - a).max()) > 30 and float(a.max() - a.min()) > 40:
            colours.append(a)
    if len(colours) < max(1, int(np.ceil(0.8 * len(free)))):
        return False
    return all(float(np.abs(c - colours[0]).max()) < 45 for c in colours)


def selection_shown(before: np.ndarray, during: np.ndarray, centers: list[tuple[int, int]]) -> bool:
    """While the finger rests on the last letter, the game draws its selection along the line (a dark capsule,
    the letters turned white), over any capsules already there. Without it the game didn't see the swipe
    (2026-10-12: a few swipes in a hundred were dropped on the phone and real words written off)."""
    pts = [p for c in zip(centers, _gaps(centers) + [None]) for p in c if p is not None]
    changed = [float(np.abs(_patch(before, x, y) - _patch(during, x, y)).max()) > 40 for x, y in pts]
    return changed[-1] and sum(changed) >= 0.7 * len(changed)


def read_title(img: np.ndarray, title: Box) -> str:
    if pytesseract is None or title.h < 20:
        return ""
    # The middle of the bar: some themes draw it as a ribbon with an (i) button at the right end.
    # (the River Race counter sits at the bar's left end, the (i) button at its right end)
    crop = img[title.y:title.y + title.h, title.x + int(0.17 * title.w):title.x + int(0.83 * title.w)]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    bw = np.where((hsv[:, :, 2] > 215) & (hsv[:, :, 1] < 60), 0, 255).astype(np.uint8)  # white text
    bw = cv2.copyMakeBorder(bw, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
    title = re.sub(r"[^A-Z0-9 '&-]", "", pytesseract.image_to_string(bw, config="--psm 7").upper()).strip()
    return re.sub(r"^\d+\s+(?=[A-Z])", "", title)   # the River Race counter at the bar's left end


def read_puzzle(img: np.ndarray, reader: GlyphReader, with_title: bool = True) -> Puzzle:
    layout = find_layout(img)
    if layout is None:
        raise VisionError("not a puzzle screen")
    title_box, lst, board = layout
    grid = read_grid(img, board, reader)
    title = read_title(img, title_box) if with_title else ""
    tiles = picture_tiles(img, lst)
    if len(tiles) >= 2:
        return Puzzle(title, [], grid, lst, "pictures", tiles)
    blanks = quote_blanks(img, lst)
    if blanks:
        # The visible words count too on sequence levels (SEED → ____ → ...); on quotes they're just text,
        # and swiping one that happens to be in the grid costs nothing.
        words = [w for w in read_words(img, lst, mixed=True) if len(w.text) >= 3]
        return Puzzle(title, words, grid, lst, "quote", blanks=blanks)
    words = read_words(img, lst)
    if not words:
        raise VisionError("couldn't read the word list")
    return Puzzle(title, words, grid, lst, hidden=hidden_slots(img, lst))
