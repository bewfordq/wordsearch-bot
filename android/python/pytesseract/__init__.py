"""pytesseract's interface inside the Android app, backed by Tesseract4Android (the same Tesseract engine and
eng.traineddata as the PC) through the app's Bridge. Only what the bot uses: image_to_data, image_to_string,
Output, get_tesseract_version and pytesseract.tesseract_cmd. This folder is only on the app's Python path.
"""
from __future__ import annotations

import re

import numpy as np

ANDROID_SHIM = True

class Output:
    BYTES = "bytes"
    DATAFRAME = "data.frame"
    DICT = "dict"
    STRING = "string"


class _Settings:
    tesseract_cmd = "tesseract"


pytesseract = _Settings()  # `pytesseract.pytesseract.tesseract_cmd = ...` must keep working


class TesseractNotFoundError(EnvironmentError):
    pass


def get_tesseract_version() -> str:
    return "5.5 (Tesseract4Android)"


def _options(config: str) -> tuple[int, str]:
    psm = re.search(r"--psm\s+(\d+)", config or "")
    wl = re.search(r"tessedit_char_whitelist=(\S+)", config or "")
    return (int(psm.group(1)) if psm else 3), (wl.group(1) if wl else "")


def _gray(image) -> tuple[bytes, int, int]:
    import cv2

    img = np.asarray(image)
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY if img.shape[2] == 3 else cv2.COLOR_BGRA2GRAY)
    img = np.ascontiguousarray(img, dtype=np.uint8)
    return img.tobytes(), int(img.shape[1]), int(img.shape[0])


def parse_tsv(tsv: str) -> dict[str, list]:
    """Tesseract's TSV -> the dict pytesseract returns (its file_to_dict: every column but the text is
    int(float(value)), so a confidence of 16.92 is 16)."""
    rows = [row.split("\t") for row in (tsv or "").strip().split("\n")]
    if len(rows) < 2:
        return {}
    header = rows.pop(0)
    if len(rows[-1]) < len(header):
        rows[-1].append("")
    text_col = len(header) - 1
    out: dict[str, list] = {}
    for i, head in enumerate(header):
        out[head] = []
        for row in rows:
            if len(row) <= i:
                continue
            if i != text_col:
                try:
                    out[head].append(int(float(row[i])))
                except ValueError:
                    out[head].append(row[i])
            else:
                out[head].append(row[i])
    return out


def _bridge():
    from wordbot import android_device

    return android_device.bridge()


def image_to_data(image, lang=None, config="", nice=0, output_type=Output.STRING, timeout=0, pandas_config=None):
    psm, whitelist = _options(config)
    data, w, h = _gray(image)
    tsv = str(_bridge().ocrTsv(data, w, h, psm, whitelist))
    return parse_tsv(tsv) if output_type == Output.DICT else tsv


def image_to_string(image, lang=None, config="", nice=0, output_type=Output.STRING, timeout=0):
    psm, whitelist = _options(config)
    data, w, h = _gray(image)
    return str(_bridge().ocrText(data, w, h, psm, whitelist))
