from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_imports: dict[str, dict[str, Any]] = {}

_FAILED_TTL = 10 * 60  # remove failed entries after 10 minutes


def start(tag: str, display_name: str, stage: str = "Uploading") -> None:
    with _lock:
        _imports[tag] = {
            "tag": tag,
            "display_name": display_name,
            "stage": stage,
            "started_at": time.time(),
            "failed": False,
            "error": "",
        }


def update(tag: str, stage: str) -> None:
    with _lock:
        if tag in _imports:
            _imports[tag]["stage"] = stage


def complete(tag: str) -> None:
    with _lock:
        _imports.pop(tag, None)


def fail(tag: str, error: str) -> None:
    with _lock:
        if tag in _imports:
            _imports[tag]["stage"] = "Failed"
            _imports[tag]["failed"] = True
            _imports[tag]["error"] = error
            _imports[tag]["failed_at"] = time.time()


def get_failed(tag: str) -> dict[str, Any] | None:
    with _lock:
        entry = _imports.get(tag)
        return entry if entry and entry["failed"] else None


def is_active(tag: str) -> bool:
    """Return True if there is a non-failed import in progress for this tag."""
    with _lock:
        entry = _imports.get(tag)
        return entry is not None and not entry["failed"]


def get_all() -> list[dict[str, Any]]:
    now = time.time()
    with _lock:
        stale = [
            tag for tag, entry in _imports.items()
            if entry["failed"] and now - entry.get("failed_at", now) > _FAILED_TTL
        ]
        for tag in stale:
            del _imports[tag]
        return list(_imports.values())
