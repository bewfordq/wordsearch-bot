"""Reads single grid letters.

Each letter is matched against learned templates (templates/<L>_<n>.png). A letter with no confident
template match is read with Tesseract instead. Templates are only learned from cells that a word from the
word list ran through, so a misread can never become a template.
"""
from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np

try:
    import pytesseract
except ImportError:  # the bot still runs on templates alone
    pytesseract = None

SIZE = 40
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
TESS_LETTERS = f"-c tessedit_char_whitelist={ALPHABET}"


def normalize(mask: np.ndarray) -> np.ndarray:
    """Binary glyph crop (ink > 0) -> SIZE x SIZE float image, padded to a square so the aspect ratio
    (I vs O) still counts."""
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return np.zeros((SIZE, SIZE), np.float32)
    crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = crop.shape
    s = max(h, w)
    sq = np.zeros((s, s), np.uint8)
    sq[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = crop
    return cv2.resize(sq, (SIZE, SIZE), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0


def _unit(v: np.ndarray) -> np.ndarray:
    v = v.reshape(-1).astype(np.float32)
    v = v - v.mean()
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


class GlyphReader:
    def __init__(self, folder: Path, per_letter: int = 6, min_score: float = 0.86, min_margin: float = 0.04):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.per_letter = per_letter
        self.min_score = min_score
        self.min_margin = min_margin
        self._lock = threading.Lock()
        self._labels: list[str] = []
        self._vecs = np.zeros((0, SIZE * SIZE), np.float32)
        self._files: list[Path] = []
        self.tesseract_calls = 0
        self.load()

    # --- template store -------------------------------------------------------------

    def load(self) -> None:
        labels, vecs, files = [], [], []
        for p in sorted(self.folder.glob("*.png")):
            ch = p.stem.split("_")[0]
            if len(ch) != 1 or ch not in ALPHABET:
                continue
            im = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if im is None:
                continue
            labels.append(ch)
            vecs.append(_unit(cv2.resize(im, (SIZE, SIZE)).astype(np.float32) / 255.0))
            files.append(p)
        with self._lock:
            self._labels = labels
            self._vecs = np.array(vecs, np.float32) if vecs else np.zeros((0, SIZE * SIZE), np.float32)
            self._files = files

    def known_letters(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for ch in self._labels:
                out[ch] = out.get(ch, 0) + 1
            return out

    def clear(self) -> None:
        for p in self.folder.glob("*.png"):
            p.unlink()
        self.load()

    def learn(self, glyph: np.ndarray, letter: str) -> bool:
        """Store `glyph` as a template for `letter` unless an almost identical one exists or the letter
        already has enough. Returns True if stored."""
        if letter not in ALPHABET:
            return False
        v = _unit(glyph)
        with self._lock:
            same = [i for i, ch in enumerate(self._labels) if ch == letter]
            if len(same) >= self.per_letter:
                return False
            if same and float(np.max(self._vecs[same] @ v)) > 0.97:
                return False
            n = 0
            while (self.folder / f"{letter}_{n}.png").exists():
                n += 1
            path = self.folder / f"{letter}_{n}.png"
            cv2.imwrite(str(path), np.clip(glyph * 255, 0, 255).astype(np.uint8))
            self._labels.append(letter)
            self._vecs = np.vstack([self._vecs, v[None, :]])
            self._files.append(path)
        return True

    # --- reading --------------------------------------------------------------------

    def match(self, glyph: np.ndarray) -> tuple[str, float, float]:
        """(best letter, its score, margin over the best other letter). '?' with no templates."""
        with self._lock:
            if not self._labels:
                return "?", 0.0, 0.0
            scores = self._vecs @ _unit(glyph)
            labels = self._labels
        best: dict[str, float] = {}
        for ch, s in zip(labels, scores):
            if s > best.get(ch, -1.0):
                best[ch] = float(s)
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        top_ch, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        return top_ch, top, top - second

    def confident(self, score: float, margin: float) -> bool:
        return score >= self.min_score and margin >= self.min_margin

    def read(self, glyphs: list[np.ndarray]) -> list[tuple[str, float, str]]:
        """Letters for a batch of glyphs: [(letter, confidence 0..1, source 'template'|'ocr'|'guess')]."""
        out: list[tuple[str, float, str] | None] = []
        unsure: list[int] = []
        for i, g in enumerate(glyphs):
            ch, score, margin = self.match(g)
            if ch != "?" and self.confident(score, margin):
                out.append((ch, score, "template"))
            else:
                out.append((ch, score, "guess") if ch != "?" else None)
                unsure.append(i)
        if unsure:
            letters = self.ocr([glyphs[i] for i in unsure])
            for i, ch in zip(unsure, letters):
                if ch:
                    out[i] = (ch, 0.75, "ocr")
                elif out[i] is None:
                    out[i] = ("?", 0.0, "guess")
        return [o if o is not None else ("?", 0.0, "guess") for o in out]

    # --- tesseract ------------------------------------------------------------------

    @staticmethod
    def _render(glyph: np.ndarray, height: int = 64) -> np.ndarray:
        """Black glyph on white, `height` px tall, as Tesseract likes it."""
        m = (glyph > 0.5).astype(np.uint8) * 255
        ys, xs = np.nonzero(m)
        if len(xs) == 0:
            return np.full((height, height // 2), 255, np.uint8)
        crop = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        h, w = crop.shape
        crop = cv2.resize(crop, (max(1, round(w * height / h)), height), interpolation=cv2.INTER_NEAREST)
        return 255 - crop

    def ocr(self, glyphs: list[np.ndarray]) -> list[str]:
        """Read glyphs with Tesseract: all on one line first (one call), one by one if that doesn't give
        exactly one letter per glyph. '' where nothing was read."""
        if pytesseract is None or not glyphs:
            return [""] * len(glyphs)
        parts = [self._render(g) for g in glyphs]
        gap = np.full((64, 48), 255, np.uint8)
        line = np.hstack([x for p in parts for x in (p, gap)][:-1])
        line = cv2.copyMakeBorder(line, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)
        self.tesseract_calls += 1
        txt = pytesseract.image_to_string(line, config=f"--psm 7 {TESS_LETTERS}")
        letters = [c for c in txt.upper() if c in ALPHABET]
        if len(letters) == len(glyphs):
            return letters
        out = []
        for p in parts:
            p = cv2.copyMakeBorder(p, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)
            self.tesseract_calls += 1
            t = [c for c in pytesseract.image_to_string(p, config=f"--psm 10 {TESS_LETTERS}").upper() if c in ALPHABET]
            out.append(t[0] if t else "")
        return out
