"""Remembers Picture Puzzle pictures and their words (data/pictures/<WORD>_<n>.png), learned from levels the
bot already solved, so a picture it has seen before is solved straight away next time."""
from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np

from .paths import PICTURES as FOLDER

SIZE = 48


def _vec(img: np.ndarray) -> np.ndarray:
    v = cv2.resize(img, (SIZE, SIZE), interpolation=cv2.INTER_AREA).astype(np.float32).reshape(-1)
    v -= v.mean()
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


class PictureMemory:
    def __init__(self, folder: Path = FOLDER, min_score: float = 0.9):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.min_score = min_score
        self._lock = threading.Lock()
        self._words: list[str] = []
        self._vecs = np.zeros((0, SIZE * SIZE * 3), np.float32)
        self.load()

    def load(self) -> None:
        words, vecs = [], []
        for p in sorted(self.folder.glob("*.png")):
            im = cv2.imread(str(p))
            if im is None:
                continue
            words.append(p.stem.rsplit("_", 1)[0])
            vecs.append(_vec(im))
        with self._lock:
            self._words = words
            self._vecs = np.array(vecs, np.float32) if vecs else np.zeros((0, SIZE * SIZE * 3), np.float32)

    def __len__(self) -> int:
        return len(self._words)

    def match(self, tile: np.ndarray) -> tuple[str | None, float]:
        with self._lock:
            if not self._words:
                return None, 0.0
            scores = self._vecs @ _vec(tile)
            i = int(np.argmax(scores))
            s = float(scores[i])
            return (self._words[i] if s >= self.min_score else None), s

    def learn(self, tile: np.ndarray, word: str) -> bool:
        known, score = self.match(tile)
        if known == word and score > 0.97:
            return False
        n = 0
        while (self.folder / f"{word}_{n}.png").exists():
            n += 1
        cv2.imwrite(str(self.folder / f"{word}_{n}.png"), tile)
        with self._lock:
            self._words.append(word)
            self._vecs = np.vstack([self._vecs, _vec(tile)[None, :]])
        return True
