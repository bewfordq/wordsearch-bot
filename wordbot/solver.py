"""Find each listed word in the letter grid (any of the 8 straight directions, forwards or backwards)."""
from __future__ import annotations

from dataclasses import dataclass, field

DIRECTIONS = [(0, 1), (1, 0), (1, 1), (-1, 1), (0, -1), (-1, 0), (-1, -1), (1, -1)]


@dataclass
class Placement:
    word: str
    start: tuple[int, int]          # (row, col)
    end: tuple[int, int]
    mismatches: int = 0             # cells whose read letter differs from the word (misreads)
    cells: list[tuple[int, int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"word": self.word, "start": list(self.start), "end": list(self.end),
                "mismatches": self.mismatches, "cells": [list(c) for c in self.cells]}


def _walk(grid: list[list[str]], r: int, c: int, dr: int, dc: int, n: int) -> list[tuple[int, int]] | None:
    rows, cols = len(grid), len(grid[0])
    er, ec = r + dr * (n - 1), c + dc * (n - 1)
    if not (0 <= er < rows and 0 <= ec < cols):
        return None
    return [(r + dr * i, c + dc * i) for i in range(n)]


def placements(grid: list[list[str]], word: str, max_mismatches: int = 0) -> list[Placement]:
    """Every placement of `word` with at most `max_mismatches` differing cells, best first."""
    out: list[Placement] = []
    if not grid or not word:
        return out
    n = len(word)
    dirs = DIRECTIONS[:1] if n == 1 else DIRECTIONS
    for r in range(len(grid)):
        for c in range(len(grid[0])):
            for dr, dc in dirs:
                cells = _walk(grid, r, c, dr, dc, n)
                if cells is None:
                    continue
                bad = 0
                for (rr, cc), ch in zip(cells, word):
                    if grid[rr][cc] != ch:
                        bad += 1
                        if bad > max_mismatches:
                            break
                if bad <= max_mismatches:
                    out.append(Placement(word, cells[0], cells[-1], bad, cells))
    out.sort(key=lambda p: p.mismatches)
    return out


def solve(grid: list[list[str]], words: list[str], fuzzy: int = 1) -> tuple[dict[str, Placement], list[str]]:
    """Place every word. Exact matches first; a word with no exact match may use a placement with up to
    `fuzzy` misread cells (only for words of 4+ letters, so a short word can't match noise).
    Returns ({word: placement}, [words not found])."""
    found: dict[str, Placement] = {}
    missing: list[str] = []
    used: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for word in sorted(set(words), key=len, reverse=True):
        allowed = fuzzy if len(word) >= 4 else 0
        cands = placements(grid, word, allowed)
        # The same word can appear twice (or be a palindrome): skip a line another word already uses.
        cands = [p for p in cands if (p.start, p.end) not in used and (p.end, p.start) not in used] or cands
        if not cands:
            missing.append(word)
            continue
        best = cands[0]
        # A fuzzy match is only trusted when it's the single best one.
        if best.mismatches and len([p for p in cands if p.mismatches == best.mismatches]) > 1:
            missing.append(word)
            continue
        found[word] = best
        used.add((best.start, best.end))
    return found, missing
