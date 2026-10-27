"""Screenshots via spectacle, KDE's native (Wayland-safe) screenshot tool.

With an active session (CONTROL_SESSION_ID) captures land in that session's own
directory, so parallel agents never overwrite or pick up each other's images.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

DEFAULT_DIR = Path(tempfile.gettempdir()) / "control-screenshots"


class ScreenshotError(RuntimeError):
    pass


def _capture(mode_flag: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["spectacle", "-b", "-n", mode_flag, "-o", str(out_path)],
            capture_output=True, text=True, timeout=15 if mode_flag != "-r" else 120,
        )
    except subprocess.TimeoutExpired:
        raise ScreenshotError(
            "spectacle timed out"
            + (" waiting for a region selection; pass --crop X,Y,W,H for a non-interactive capture"
               if mode_flag == "-r" else "")) from None
    if result.returncode != 0 or not out_path.exists():
        raise ScreenshotError(result.stderr.strip() or result.stdout.strip() or "spectacle failed")
    return out_path


def output_dir() -> Path:
    name = os.environ.get("CONTROL_SESSION_ID")
    if not name:
        return DEFAULT_DIR
    from . import session as session_mod  # lazy: keep this module dependency-free

    return session_mod.session_dir(name) / "screenshots"


def _timestamped(prefix: str) -> Path:
    # pid in the name: two agents sharing a directory in the same millisecond
    # must still get distinct files.
    return output_dir() / f"{prefix}-{int(time.time() * 1000)}-{os.getpid()}.png"


def fullscreen(out_path: Path | None = None) -> Path:
    return _capture("-f", out_path or _timestamped("full"))


def active_window(out_path: Path | None = None) -> Path:
    return _capture("-a", out_path or _timestamped("window"))


def region(out_path: Path | None = None) -> Path:
    """Interactive region select (blocks until the user drags a rectangle)."""
    return _capture("-r", out_path or _timestamped("region"))


def trim_window_shadow(path: Path, width: int, height: int, tolerance: int = 4) -> bool:
    """Crop the drop shadow KWin adds around a window capture, in place.

    The shadow shifts the image origin away from the window's screen position,
    which would skew every OCR coordinate. The window body is the bounding box of
    the (nearly) opaque pixels; it is only trusted when it matches the window's
    real size, so translucent windows are left untouched. True when it cropped.
    """
    from PIL import Image  # lazy: only needed for post-processing

    with Image.open(path) as im:
        if im.mode != "RGBA" or not width or not height:
            return False
        alpha = im.getchannel("A")
        for threshold in (200, 128, 64):
            box = alpha.point(lambda v, t=threshold: 255 if v >= t else 0).getbbox()
            if not box:
                continue
            if (abs((box[2] - box[0]) - width) <= tolerance
                    and abs((box[3] - box[1]) - height) <= tolerance):
                if box == (0, 0, im.width, im.height):
                    return False
                cropped = im.crop(box)
                break
        else:
            return False
    cropped.save(path)
    return True


# --- image post-processing: crop, shrink, OCR (all local, no model tokens) ----

def _tessdata() -> str | None:
    """Folder holding eng/spa traineddata: CONTROL_TESSDATA, else the user's copy."""
    for cand in (os.environ.get("CONTROL_TESSDATA"),
                 str(Path.home() / ".local/share/marionette/tessdata")):
        if cand and Path(cand).is_dir():
            return cand
    return None


def crop_scale(path: Path, box: tuple[int, int, int, int] | None = None,
               scale: float = 1.0) -> Path:
    """Write a cropped and/or shrunk copy next to `path` (box = x, y, w, h in pixels)."""
    from PIL import Image  # lazy: only needed when the caller asks for it

    if box is None and scale == 1.0:
        return path
    out = path.with_name(f"{path.stem}-s{path.suffix}")
    with Image.open(path) as im:
        if box:
            x, y, w, h = box
            im = im.crop((x, y, x + w, y + h))
        if scale != 1.0:
            im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                           Image.LANCZOS)
        im.save(out, optimize=True)
    return out


def _ocr_ready(path: Path) -> tuple[Path, bool]:
    """Grayscale copy for Tesseract, inverted when dark: it misses light-on-dark text.

    Same size as the original, so coordinates stay valid. (path, is_temporary).
    """
    from PIL import Image, ImageOps, ImageStat  # lazy

    with Image.open(path) as im:
        if im.mode in ("RGBA", "LA"):
            flat = Image.new("RGB", im.size, (0, 0, 0))
            flat.paste(im.convert("RGBA"), mask=im.getchannel("A"))
            im = flat
        gray = im.convert("L")
    if ImageStat.Stat(gray).mean[0] >= 110:
        return path, False
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="ocr-")
    os.close(fd)
    ImageOps.invert(gray).save(tmp)
    return Path(tmp), True


def ocr(path: Path, origin: tuple[int, int] = (0, 0), lang: str = "spa+eng",
        min_conf: int = 30, words: bool = False) -> list[dict]:
    """Text lines in `path` as {text, x, y, w, h, conf}, offset by `origin` so the
    coordinates are screen coordinates an agent can click directly."""
    env = dict(os.environ)
    data = _tessdata()
    if data:
        env["TESSDATA_PREFIX"] = data
    try:
        src, temp = _ocr_ready(path)
    except OSError:
        src, temp = path, False
    try:
        result = subprocess.run(["tesseract", str(src), "stdout", "-l", lang, "--psm", "11", "-c", "tessedit_create_tsv=1"],
                                capture_output=True, text=True, timeout=60, env=env)
    except FileNotFoundError:
        raise ScreenshotError("tesseract not found; install it for --ocr") from None
    finally:
        if temp:
            src.unlink(missing_ok=True)
    if result.returncode != 0:
        raise ScreenshotError(result.stderr.strip() or "tesseract failed")
    lines: dict[tuple, dict] = {}
    for row in result.stdout.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or not f[11].strip() or float(f[10]) < min_conf:
            continue
        key = tuple(f[1:5])  # page, block, paragraph, line
        x, y, w, h = (int(v) for v in f[6:10])
        cur = lines.get(key)
        if cur is None:
            lines[key] = {"text": f[11].strip(), "x": x, "y": y, "x2": x + w, "y2": y + h,
                          "conf": float(f[10]), "n": 1, "words": [(f[11].strip(), x, y, w, h)]}
        else:
            cur["words"].append((f[11].strip(), x, y, w, h))
            cur["text"] += " " + f[11].strip()
            cur["x2"], cur["y2"] = max(cur["x2"], x + w), max(cur["y2"], y + h)
            cur["y"] = min(cur["y"], y)
            cur["conf"] += float(f[10])
            cur["n"] += 1
    out = []
    for c in lines.values():
        line = {"text": c["text"], "x": c["x"] + origin[0], "y": c["y"] + origin[1],
                "w": c["x2"] - c["x"], "h": c["y2"] - c["y"], "conf": round(c["conf"] / c["n"])}
        if words:
            line["words"] = [{"text": t, "x": x + origin[0], "y": y + origin[1], "w": w, "h": h}
                             for t, x, y, w, h in c["words"]]
        out.append(line)
    return out
