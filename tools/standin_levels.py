"""Builds the emulator stand-in game's levels (android/standin) from level screenshots the bot saved.

For each level: the screenshot, and per word where a swipe has to start and end, a capsule overlay (drawn on the
background only, so the letters stay black, like the game's) and the word's box in the list (greyed when found).
Plus the level complete screen with its Next Level button.

    .venv/bin/python tools/standin_levels.py [screenshot.png ...]     (default: a few from logs/levels)
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from wordbot.glyphs import GlyphReader  # noqa: E402
from wordbot.screens import classify  # noqa: E402
from wordbot.solver import solve  # noqa: E402
from wordbot.vision import VisionError, read_puzzle  # noqa: E402

OUT = ROOT / "android" / "standin" / "app" / "src" / "main" / "assets" / "levels"
PASTELS = [(255, 143, 177), (136, 191, 255), (147, 147, 255), (150, 230, 160), (255, 180, 120), (200, 150, 255)]  # RGB


def level(img: np.ndarray, reader: GlyphReader, n: int) -> dict | None:
    try:
        p = read_puzzle(img, reader, with_title=False)
    except VisionError:
        return None
    if p.kind != "words" or p.hidden or any(w.found or not w.text for w in p.words):
        return None
    found, missing = solve(p.grid.letters, [w.text for w in p.words])
    if missing or any(pl.mismatches for pl in found.values()):
        return None
    g = p.grid
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    words = []
    for i, w in enumerate(p.words):
        pl = found[w.text]
        (x1, y1), (x2, y2) = g.centers[pl.start[0]][pl.start[1]], g.centers[pl.end[0]][pl.end[1]]
        d = np.array([x2 - x1, y2 - y1], float)
        d = d / (np.linalg.norm(d) or 1)
        ext = 0.28 * g.pitch
        a = (int(x1 - d[0] * ext), int(y1 - d[1] * ext))
        b = (int(x2 + d[0] * ext), int(y2 + d[1] * ext))
        mask = np.zeros(gray.shape, np.uint8)
        cv2.line(mask, a, b, 255, int(0.58 * g.pitch), cv2.LINE_AA)
        mask = (mask > 127) & (gray > 200)          # paint the background, keep the letters
        ys, xs = np.nonzero(mask)
        x0, y0, x9, y9 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
        rgba = np.zeros((y9 - y0, x9 - x0, 4), np.uint8)
        r, gg, bb = PASTELS[i % len(PASTELS)]
        sub = mask[y0:y9, x0:x9]
        rgba[sub] = (bb, gg, r, 255)                 # BGRA for cv2.imwrite
        name = f"L{n}_w{i}.png"
        cv2.imwrite(str(OUT / name), rgba)
        words.append({"word": w.text, "from": [x1, y1], "to": [x2, y2], "tol": round(0.42 * g.pitch),
                      "overlay": name, "ox": int(x0), "oy": int(y0), "box": w.box.to_list()})
    shot = f"L{n}.png"
    cv2.imwrite(str(OUT / shot), img)
    return {"image": shot, "words": words}


def main() -> None:
    files = [Path(a) for a in sys.argv[1:]] or sorted((ROOT / "logs" / "levels").glob("*.png"))
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    reader = GlyphReader(ROOT / "templates")
    levels = []
    for f in files:
        if len(levels) >= 4:
            break
        img = cv2.imread(str(f))
        if img is None or img.shape[:2] != (2400, 1080):
            continue
        lv = level(img, reader, len(levels) + 1)
        if lv:
            levels.append(lv)
            print(f"{f.name}: {len(lv['words'])} words")
    done = cv2.imread(str(ROOT / "tests" / "screens" / "next_level.png"))
    btn = classify(done).buttons[0].box
    cv2.imwrite(str(OUT / "complete.png"), done)
    (OUT / "levels.json").write_text(json.dumps({"levels": levels, "complete": "complete.png",
                                                 "next": btn.to_list()}, indent=1))
    print(f"{len(levels)} levels -> {OUT}")


if __name__ == "__main__":
    main()
