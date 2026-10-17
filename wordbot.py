#!/usr/bin/env python3
"""Word Search bot: plays PlaySimple's Word Search (in.playsimple.wordsearch) on a phone plugged in over USB.

    python3 wordbot.py                 dashboard at http://127.0.0.1:8765, bot waits for Start
    python3 wordbot.py --start         start playing straight away
    python3 wordbot.py --levels 5      stop after 5 levels
    python3 wordbot.py --dry-run       read and solve only, never touch the phone
    python3 wordbot.py --no-browser    don't open the browser
    python3 wordbot.py --host 0.0.0.0  reach the dashboard from another device on your network
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "bin" / "python"

# Run with the project's virtualenv even when started as plain `python3 wordbot.py`.
try:
    import cv2  # noqa: F401
    import numpy  # noqa: F401
except ImportError:
    # (.venv/bin/python is a symlink to the system python, so compare prefixes, not executables)
    if VENV_PY.exists() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.exit("Missing packages. Run: .venv/bin/python -m pip install -r requirements.txt")

sys.path.insert(0, str(ROOT))
from wordbot.bot import Bot  # noqa: E402
from wordbot.config import Config  # noqa: E402
from wordbot.dashboard import serve  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Word Search bot with a live dashboard.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard in a browser")
    ap.add_argument("--start", action="store_true", help="start playing right away")
    ap.add_argument("--levels", type=int, help="stop after this many levels (saved, like the dashboard setting)")
    ap.add_argument("--dry-run", action="store_true", help="read and solve, never touch the phone (saved)")
    args = ap.parse_args()

    cfg = Config()
    if args.levels is not None:
        cfg.update({"flow": {"max_levels": args.levels}})
    if args.dry_run:
        cfg.update({"dry_run": True})

    bot = Bot(cfg)
    try:
        serve(bot, args.host, args.port)
    except OSError as e:
        sys.exit(f"Can't open the dashboard on port {args.port}: {e}. Try --port {args.port + 1}.")
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}"
    print(f"Dashboard: {url}   (Ctrl+C here to quit)", flush=True)
    bot.command("connect")
    if args.start:
        bot.start()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        bot.device.close()


if __name__ == "__main__":
    main()
