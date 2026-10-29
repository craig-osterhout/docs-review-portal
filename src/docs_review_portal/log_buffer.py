from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from docs_review_portal.config import LOG_RETENTION_SECONDS

_MAX = 1000
_RETENTION_SECONDS = 7 * 24 * 60 * 60
_buffer: deque[dict[str, Any]] = deque(maxlen=_MAX)
_lock = threading.Lock()

# Entries are also saved to the database so they survive restarts. They are queued
# here and a background thread writes them in batches, which keeps a slow or
# unavailable database from holding up requests.
_QUEUE_MAX = 10000
_PENDING_MAX = 5000
_FLUSH_INTERVAL_SECONDS = 1.0
_PURGE_INTERVAL_SECONDS = 60 * 60
_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_MAX)
_wake = threading.Event()
_writer_lock = threading.Lock()
_writer_started = False
_failing = False

# Names this process in the logs. Cloud Run provides the revision, and the random part
# tells instances (and restarts) of the same revision apart.
INSTANCE_ID = f"{os.environ.get('K_REVISION') or 'local'}/{uuid.uuid4().hex[:6]}"

_LEVEL_FILTERS = {
    "all": None,
    "warning": ("WARNING", "ERROR", "CRITICAL"),
    "error": ("ERROR", "CRITICAL"),
}


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
            "instance": INSTANCE_ID,
        }
        with _lock:
            _buffer.append(entry)
        try:
            _queue.put_nowait(entry)
        except queue.Full:
            # Too far behind the database. The entry is still in stdout and the buffer.
            return
        if record.levelno >= logging.WARNING:
            # Problems are written straight away. Cloud Run can slow the CPU down once
            # a request is over, so waiting for the next batch risks losing them.
            _wake.set()


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


def _report(message: str) -> None:
    # Not logging: the logging system is what is failing here.
    try:
        print(f"log_buffer: {message}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _write(entries: list[dict[str, Any]]) -> None:
    from docs_review_portal.data import db_connect

    conn = db_connect()
    try:
        for entry in entries:
            conn.execute(
                "INSERT INTO logs (ts, level, logger, message, instance) VALUES (?, ?, ?, ?, ?)",
                (entry["ts"], entry["level"], entry["logger"], entry["msg"], entry["instance"]),
            )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def purge_old_entries() -> int:
    from docs_review_portal.data import db_connect

    cutoff = int(time.time()) - LOG_RETENTION_SECONDS
    conn = db_connect()
    try:
        cur = conn.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _drain() -> list[dict[str, Any]]:
    drained: list[dict[str, Any]] = []
    while True:
        try:
            drained.append(_queue.get_nowait())
        except queue.Empty:
            return drained


def _flush_pending(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Write what is waiting. Returns whatever could not be written, to try again."""
    global _failing
    pending.extend(_drain())
    if not pending:
        return pending
    try:
        _write(pending)
    except Exception as exc:
        if not _failing:
            _failing = True
            _report(f"could not save logs to the database, keeping them in memory and stdout ({exc})")
        return pending[-_PENDING_MAX:]
    if _failing:
        _failing = False
        _report("saving logs to the database again")
    return []


def _writer_loop() -> None:
    pending: list[dict[str, Any]] = []
    last_purge = 0.0
    while True:
        _wake.wait(timeout=_FLUSH_INTERVAL_SECONDS)
        _wake.clear()
        try:
            pending = _flush_pending(pending)
            if time.time() - last_purge > _PURGE_INTERVAL_SECONDS:
                try:
                    purge_old_entries()
                    last_purge = time.time()
                except Exception:
                    # Try again on the next pass, the database may just be busy.
                    pass
        except Exception as exc:  # never let the writer thread die
            _report(f"log writer error ({exc})")


def start_db_writer() -> None:
    """Start saving logs to the database. Call once the logs table exists. Entries
    logged before this are already queued and get written first."""
    global _writer_started
    with _writer_lock:
        if _writer_started:
            return
        _writer_started = True
    threading.Thread(target=_writer_loop, name="log-writer", daemon=True).start()


def flush() -> None:
    """Write whatever is queued now. Used when the process is shutting down."""
    if not _writer_started:
        return
    try:
        _flush_pending([])
    except Exception:
        pass


def get_entries(limit: int = 200) -> list[dict[str, Any]]:
    """Recent entries from this process's memory. Used when the database can't be read."""
    cutoff = datetime.now(timezone.utc).timestamp() - _RETENTION_SECONDS
    with _lock:
        while _buffer and _buffer[0]["ts"] < cutoff:
            _buffer.popleft()
        entries = list(_buffer)
    return entries[-limit:]


def query_entries(
    level: str = "all",
    search: str = "",
    instance: str = "",
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """Saved entries, newest first. Returns (entries, there are older ones)."""
    from docs_review_portal.data import db_connect

    clauses: list[str] = []
    params: list[Any] = []
    levels = _LEVEL_FILTERS.get(level)
    if levels:
        clauses.append(f"level IN ({', '.join(['?'] * len(levels))})")
        params.extend(levels)
    if instance:
        clauses.append("instance = ?")
        params.append(instance)
    if search:
        clauses.append("(LOWER(message) LIKE LOWER(?) OR LOWER(logger) LIKE LOWER(?))")
        params.extend([f"%{search}%", f"%{search}%"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = db_connect()
    try:
        rows = conn.execute(
            f"""
            SELECT ts, level, logger, message, instance
            FROM logs
            {where}
            ORDER BY ts DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params) + (limit + 1, offset),
        ).fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    entries = [
        {
            "ts": int(row["ts"]),
            "ts_human": datetime.fromtimestamp(int(row["ts"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "level": row["level"],
            "logger": row["logger"],
            "msg": row["message"],
            "instance": row["instance"] or "",
        }
        for row in rows[:limit]
    ]
    return entries, len(rows) > limit


def list_instances(limit: int = 25) -> list[str]:
    """Instances that logged recently, most recent first."""
    from docs_review_portal.data import db_connect

    conn = db_connect()
    try:
        rows = conn.execute(
            """
            SELECT instance, MAX(ts) AS last_ts
            FROM logs
            WHERE instance IS NOT NULL
            GROUP BY instance
            ORDER BY last_ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [str(row["instance"]) for row in rows]
