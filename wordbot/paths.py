"""Where things are. On the PC everything is in the project folder. Inside the Android app the code (and the
data bundled with it) is read-only, so WORDBOT_HOME points at the app's files folder for everything the bot
writes: settings, learned letter shapes and pictures, logs."""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
CODE = PACKAGE.parent
HOME = Path(os.environ.get("WORDBOT_HOME") or CODE)

CONFIG_FILE = HOME / "config.json"
TEMPLATES = HOME / "templates"
LOGS = HOME / "logs"
PICTURES = HOME / "data" / "pictures"
SEED = PACKAGE / "seed"          # the Android app's starting letter shapes, pictures and word list


def data_file(name: str) -> Path:
    """A file from data/: the writable copy if there is one, else the project's, else the app's bundled one."""
    for folder in (HOME / "data", CODE / "data", SEED):
        p = folder / name
        if p.exists():
            return p
    return CODE / "data" / name
