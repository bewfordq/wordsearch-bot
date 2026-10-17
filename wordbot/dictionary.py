"""English words hidden in a grid, most likely first. Used when the word list can't be read: Picture Puzzle
levels show pictures instead of words, so the bot tries real words from the grid and watches which ones the
game accepts (a wrong swipe costs nothing).

Word frequencies: data/words_en.txt.gz, from wordfreq (https://github.com/rspeer/wordfreq), CC-BY-SA 4.0."""
from __future__ import annotations

import gzip
import threading
from dataclasses import dataclass
from pathlib import Path

from .paths import data_file
from .solver import DIRECTIONS

WORDS_FILE = data_file("words_en.txt.gz")


@dataclass
class Candidate:
    word: str
    zipf: float
    cells: list[tuple[int, int]]

    @property
    def start(self) -> tuple[int, int]:
        return self.cells[0]

    @property
    def end(self) -> tuple[int, int]:
        return self.cells[-1]

    @property
    def score(self) -> float:
        # Answers are everyday nouns; longer words are much less likely to be in the grid by chance.
        return self.zipf + 0.5 * len(self.word)


class Dictionary:
    def __init__(self, path: Path = WORDS_FILE):
        self.path = path
        self._freq: dict[str, float] | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.path.exists()

    def freq(self) -> dict[str, float]:
        with self._lock:
            if self._freq is None:
                freq: dict[str, float] = {}
                with gzip.open(self.path, "rt") as f:
                    for line in f:
                        if line.startswith("#"):
                            continue
                        parts = line.split()
                        if len(parts) != 2:
                            continue
                        w, z = parts
                        if w.isalpha() and 3 <= len(w) <= 15 and w not in freq:
                            freq[w] = float(z)
                self._freq = freq
            return self._freq

    def candidates(self, grid: list[list[str]], min_zipf: float = 2.5, min_len: int = 3) -> list[Candidate]:
        """Every dictionary word along a straight line of the grid, best first. A word that appears twice
        is listed once per line."""
        freq = self.freq()
        R, C = len(grid), len(grid[0])
        seen: set[tuple] = set()
        out: list[Candidate] = []
        for r in range(R):
            for c in range(C):
                for dr, dc in DIRECTIONS:
                    s, cells = "", []
                    rr, cc = r, c
                    while 0 <= rr < R and 0 <= cc < C:
                        s += grid[rr][cc]
                        cells.append((rr, cc))
                        if len(s) >= min_len:
                            z = freq.get(s)
                            if z is not None and z >= min_zipf:
                                key = (s, frozenset(cells))
                                if key not in seen:
                                    seen.add(key)
                                    out.append(Candidate(s, z, list(cells)))
                        rr += dr
                        cc += dc
        out.sort(key=lambda k: -k.score)
        return out
