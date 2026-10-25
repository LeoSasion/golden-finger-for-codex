"""Window attention preferences, deliberately separate from approval settings."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile

DEFAULTS = {"raise_on_request": True, "topmost_pending": True,
            "select_new_request": True, "compact_mode": False}


def load_preferences(path: Path) -> dict[str, bool]:
    result = dict(DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return result
    if isinstance(data, dict):
        for key in result:
            if isinstance(data.get(key), bool):
                result[key] = data[key]
    return result


def save_preferences(path: Path, values: dict[str, bool]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="window-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({key: bool(values.get(key, default)) for key, default in DEFAULTS.items()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
