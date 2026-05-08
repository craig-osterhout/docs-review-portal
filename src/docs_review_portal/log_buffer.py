from __future__ import annotations

import logging
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

_MAX = 1000
_RETENTION_SECONDS = 7 * 24 * 60 * 60
_buffer: deque[dict[str, Any]] = deque(maxlen=_MAX)
_lock = threading.Lock()


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        entry = {
            "ts": int(record.created),
            "ts_human": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }
        with _lock:
            _buffer.append(entry)


def setup() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    buf = _BufferHandler()
    buf.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(stdout)
    root.addHandler(buf)


def get_entries(limit: int = 200) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc).timestamp() - _RETENTION_SECONDS
    with _lock:
        while _buffer and _buffer[0]["ts"] < cutoff:
            _buffer.popleft()
        entries = list(_buffer)
    return entries[-limit:]
