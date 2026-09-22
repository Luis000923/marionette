"""Minimal .env loader (KEY=VALUE lines, `#` comments, blank lines ignored).

No extra dependency: this is a lightweight CLI tool and the format we need
(simple KEY=VALUE pairs, optionally quoted) doesn't warrant python-dotenv.
"""
from __future__ import annotations

import os
from pathlib import Path

# repo root: src/control/env.py -> src/control -> src -> repo root
_DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env(path: Path | str | None = None, override: bool = False) -> dict[str, str]:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Silently does nothing if the file doesn't exist. Returns the parsed values.
    """
    env_path = Path(path) if path is not None else _DEFAULT_ENV_PATH
    if not env_path.is_file():
        return {}
    values = parse_env(env_path.read_text(encoding="utf-8"))
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values
