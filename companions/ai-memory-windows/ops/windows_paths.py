"""Resolve installation paths without machine identities or embedded credentials."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys

OPS = Path(__file__).resolve().parent
DATA = OPS.parent
USER = Path.home()


def documents() -> Path:
    """Respect Windows' redirected Documents folder."""
    if os.name == "nt":
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0:
            return Path(buffer.value)
    return USER / "Documents"


def read_settings() -> dict:
    path = OPS / "windows-settings.json"
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("windows-settings.json must contain an object.")
    for key in ("binary", "python", "pythonw", "backups"):
        if key in value and (not isinstance(value[key], str) or not Path(value[key]).is_absolute()):
            raise ValueError(f"windows-settings.json: {key} must be an absolute path.")
    return value


SETTINGS = read_settings()
LOCAL = Path(os.environ.get("LOCALAPPDATA", USER / "AppData/Local"))
EXE = Path(SETTINGS.get("binary", LOCAL / "Programs/ai-memory/ai-memory.exe"))
BACKUPS = Path(SETTINGS.get("backups", documents() / "ai-memory-backups"))
PYTHON = Path(SETTINGS.get("python", sys.executable))


def archive_name(path: Path) -> str:
    """Give custom data/program locations stable, non-overlapping archive roots."""
    for root, label in ((DATA, "data"), (EXE.parent, "program"), (USER, "home")):
        try:
            return f"{label}/{path.resolve().relative_to(root.resolve()).as_posix()}"
        except ValueError:
            continue
    raise ValueError("Backup source is outside the configured data, program and home roots.")
