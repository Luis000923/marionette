"""crop/scale and OCR parsing in screenshot.py; tesseract is mocked."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from PIL import Image

from control import screenshot

TSV = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
       "5\t1\t1\t1\t1\t1\t10\t20\t30\t10\t90\tHello\n"
       "5\t1\t1\t1\t1\t2\t45\t20\t40\t12\t80\tworld\n"
       "5\t1\t1\t1\t2\t1\t10\t50\t30\t10\t10\tnoise\n")


def test_crop_scale_writes_smaller_copy(tmp_path):
    src = tmp_path / "a.png"
    Image.new("RGB", (200, 100)).save(src)
    out = screenshot.crop_scale(src, (10, 10, 100, 50), 0.5)
    assert Image.open(out).size == (50, 25)
    assert out != src


def test_crop_scale_noop_returns_same_path(tmp_path):
    src = tmp_path / "a.png"
    assert screenshot.crop_scale(src) == src


def test_ocr_merges_words_offsets_origin_and_drops_low_confidence(tmp_path):
    done = subprocess.CompletedProcess([], 0, stdout=TSV, stderr="")
    with patch("control.screenshot.subprocess.run", return_value=done):
        lines = screenshot.ocr(tmp_path / "a.png", origin=(100, 200))
    assert lines == [{"text": "Hello world", "x": 110, "y": 220, "w": 75, "h": 12, "conf": 85}]
