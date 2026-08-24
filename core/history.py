"""
Job history persistence for Smart Video Compiler.

Entries live in ``%APPDATA%/SmartVideoCompiler/history.json``, newest first,
capped at 50 records.
"""

import json
import os
from datetime import datetime
from pathlib import Path

MAX_ENTRIES = 50


def _history_path() -> Path:
    base = Path(os.environ.get("APPDATA", os.path.expanduser("~")))
    directory = base / "SmartVideoCompiler"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "history.json"


def load_entries() -> list[dict]:
    """Return saved history entries (newest first) or an empty list."""
    path = _history_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
    except (ValueError, OSError):
        pass
    return []


def append_entry(mode: str, summary: str, output_path: str, ok: bool) -> None:
    """Add one record and persist immediately."""
    entries = load_entries()
    entries.insert(
        0,
        {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "mode": mode,
            "summary": summary,
            "output": output_path,
            "ok": ok,
        },
    )
    save_entries(entries[:MAX_ENTRIES])


def save_entries(entries: list[dict]) -> None:
    try:
        _history_path().write_text(
            json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        pass


def clear() -> None:
    save_entries([])
