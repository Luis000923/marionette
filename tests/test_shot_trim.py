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


def test_ocr_ready_inverts_dark_images(tmp_path):
    path = tmp_path / "dark.png"
    Image.new("RGBA", (20, 20), (10, 10, 10, 255)).save(path)
    out, temp = screenshot._ocr_ready(path)
    try:
        assert temp is True and Image.open(out).getpixel((1, 1)) > 200
    finally:
        out.unlink()


def test_ocr_ready_keeps_light_images(tmp_path):
    path = tmp_path / "light.png"
    Image.new("RGB", (20, 20), (240, 240, 240)).save(path)
    assert screenshot._ocr_ready(path) == (path, False)


def _click_args(**kw):
    base = dict(text="guardar", nth=1, x=None, y=None, button="left", window=None,
                desktop=None, then_shot=None, json=True)
    base.update(kw)
    return argparse.Namespace(**base)


def test_locate_text_returns_centre_in_reading_order():
    lines = [{"text": "Guardar b", "x": 300, "y": 200, "w": 100, "h": 20},
             {"text": "Guardar a", "x": 50, "y": 100, "w": 80, "h": 10}]
    with patch("control.cli._shoot", return_value={"lines": lines}):
        assert cli._locate_text(_click_args()) == {"text": "Guardar a", "x": 90, "y": 105}
        assert cli._locate_text(_click_args(nth=2))["text"] == "Guardar b"


def test_locate_text_not_found_is_structured_error():
    with patch("control.cli._shoot", return_value={"lines": []}):
        try:
            cli._locate_text(_click_args())
        except cli.CliError as e:
            assert e.kind == "text_not_found"
        else:
            raise AssertionError("expected CliError")


def test_click_text_and_coords_conflict():
    try:
        cli._cmd_click(_click_args(x=1, y=2))
    except cli.CliError as e:
        assert e.kind == "usage_error"
    else:
        raise AssertionError("expected CliError")


def test_match_box_targets_the_matched_word_not_the_line():
    line = {"text": "Cancelar Guardar todo", "x": 0, "y": 10, "w": 300, "h": 12,
            "words": [{"text": "Cancelar", "x": 0, "y": 10, "w": 80, "h": 12},
                      {"text": "Guardar", "x": 100, "y": 10, "w": 70, "h": 12},
                      {"text": "todo", "x": 180, "y": 10, "w": 40, "h": 12}]}
    assert cli._match_box(line, "guardar") == (100, 10, 70, 12)
    assert cli._match_box(line, "guardar todo") == (100, 10, 120, 12)
    assert cli._match_box({**line, "words": None}, "guardar") == (0, 10, 300, 12)
