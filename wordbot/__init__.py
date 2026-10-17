"""Word Search bot."""
import os

# Tesseract starts a thread per core and they spin while waiting: next to the game (or OBS, or a browser) that
# made each call ~4x slower. One thread is faster whenever the CPU is busy.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
