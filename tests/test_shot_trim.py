import argparse
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from control import cli, screenshot


def _shadowed(tmp_path):
    im = Image.new("RGBA", (160, 110), (0, 0, 0, 40))
    im.paste((255, 255, 255, 255), (30, 20, 130, 80))
    path = tmp_path / "w.png"
    im.save(path)
    return path


def test_trim_crops_shadow_to_window_size(tmp_path):
    path = _shadowed(tmp_path)
    assert screenshot.trim_window_shadow(path, 100, 60) is True
    assert Image.open(path).size == (100, 60)


def test_trim_leaves_image_when_size_does_not_match(tmp_path):
    path = _shadowed(tmp_path)
    assert screenshot.trim_window_shadow(path, 400, 300) is False
    assert Image.open(path).size == (160, 110)


def test_region_with_crop_is_not_interactive():
    args = argparse.Namespace(mode="region", crop=(0, 0, 5, 5), scale=1.0, ocr=False, window=None)
    with patch("control.screenshot.fullscreen", return_value=Path("/x.png")) as full, \
         patch("control.screenshot.region") as reg, \
         patch("control.screenshot.crop_scale", return_value=Path("/x-s.png")):
        cli._shoot(args)
    full.assert_called_once()
    reg.assert_not_called()
