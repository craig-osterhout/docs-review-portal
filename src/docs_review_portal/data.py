from __future__ import annotations

import json
import logging as _logging
import mimetypes
import re
from html.parser import HTMLParser
import shutil
import sqlite3
import tarfile
import tempfile
import threading as _threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from docs_review_portal.config import (
    BUILDS_DIR,
    LOCAL_CACHE_DIR,
    DATABASE_URL,
    DB_BACKEND,
    DB_PATH,
    DATA_DIR,
    DEFAULT_REWRITE_HOST,
    GCS_BUCKET,
    GCS_PREFIX,
    INJECT_END,
    INJECT_START,
    PREVIOUS_SITE_DIRNAME,
    SITE_STORAGE_BACKEND,
    SOFT_DELETE_GRACE_SECONDS,
    normalize_rewrite_host,
)
from docs_review_portal.helpers import (
    canonical_page_path,
    format_ts,
    now_ts,
    review_path,
    rewrite_docs_domain_urls,
    slugify_tag,
)
def _without_nul(value: Any) -> Any:
    """PostgreSQL text can't hold NUL (0x00) characters and rejects the whole statement
    if one turns up, for example in a diff of a file with a stray NUL in it. They are
    replaced with the Unicode replacement character, so a reader can see something was
    there and the text keeps its length and line breaks."""
    if isinstance(value, str) and "\x00" in value:
        return value.replace("\x00", "\ufffd")
    return value


class DBConnectionProxy:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def _prepare_sql(self, statement: str) -> str:
        if DB_BACKEND == "postgres":
            return statement.replace("?", "%s")
        return statement

    def execute(self, statement: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        # Applied on every backend so SQLite behaves like PostgreSQL does.
        clean = tuple(_without_nul(p) for p in params) if params else params
        return self._conn.execute(self._prepare_sql(statement), clean)

    def executescript(self, script: str) -> None:
        if DB_BACKEND == "sqlite":
            self._conn.executescript(script)
            return
        statements = [chunk.strip() for chunk in script.split(";") if chunk.strip()]
        for statement in statements:
            self._conn.execute(statement)

    def __enter__(self) -> DBConnectionProxy:
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        return self._conn.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def db_connect() -> DBConnectionProxy:
    if DB_BACKEND == "sqlite":
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return DBConnectionProxy(conn)

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Postgres backend requires psycopg. Install it or switch to sqlite by unsetting REVIEW_DATABASE_URL."
        ) from exc
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return DBConnectionProxy(conn)


# Storage tag for versions after the first is "<review key>~v<n>". slugify_tag never
# produces "~", so a version tag can't collide with a name someone uploads.
_VERSION_TAG_SEPARATOR = "~"
_CHANGED_PAGES_FILE = ".changed-pages"  # reserved filename in upload archives
_DIFFS_DIR = ".diffs"  # reserved directory prefix for per-page diff files


class SiteStore:
    def init_storage(self) -> None:
        raise NotImplementedError

    def begin_replace(self, tag: str) -> bool:
        raise NotImplementedError

    def rollback_replace(self, tag: str, had_existing_site: bool) -> None:
        raise NotImplementedError

    def replace_site_from_archive(self, archive_path: Path, tag: str, progress_callback: Callable[[int, int], None] | None = None) -> str | None:
        raise NotImplementedError

    def read_file(self, tag: str, rel_path: str) -> bytes | None:
        raise NotImplementedError

    def exists(self, tag: str, rel_path: str) -> bool:
        """Whether the site has this file. Stores override it with a check that doesn't
        download the file."""
        return self.read_file(tag, rel_path) is not None

    def delete_build(self, tag: str) -> None:
        raise NotImplementedError


class FilesystemSiteStore(SiteStore):
    def __init__(self, builds_root: Path) -> None:
        self._builds_root = builds_root

    def _build_dir(self, tag: str) -> Path:
        return self._builds_root / tag

    def _site_dir(self, tag: str, snapshot: str = "site") -> Path:
        return self._build_dir(tag) / snapshot

    def init_storage(self) -> None:
        self._builds_root.mkdir(parents=True, exist_ok=True)

    def begin_replace(self, tag: str) -> bool:
        import threading
        import time as _time
        site_dir = self._site_dir(tag, "site")
        self._build_dir(tag).mkdir(parents=True, exist_ok=True)

        had_existing_site = site_dir.exists()
        if had_existing_site:
            # Rename to a temp name then delete in a background thread so we
            # don't block the import on a slow GCS FUSE directory walk.
            tmp_dir = self._build_dir(tag) / f"_deleting_{int(_time.time())}"
            try:
                site_dir.rename(tmp_dir)
                threading.Thread(
                    target=lambda: shutil.rmtree(tmp_dir, ignore_errors=True),
                    daemon=True,
                ).start()
            except OSError:
                # GCS FUSE rename fails with ENFILE — delete in background too.
                threading.Thread(
                    target=lambda d=site_dir: shutil.rmtree(d, ignore_errors=True),
                    daemon=True,
                ).start()
        return had_existing_site

    def rollback_replace(self, tag: str, had_existing_site: bool) -> None:
        site_dir = self._site_dir(tag, "site")
        previous_site_dir = self._site_dir(tag, PREVIOUS_SITE_DIRNAME)
        if site_dir.exists():
            _rmtree_parallel(site_dir)
        if had_existing_site and previous_site_dir.exists():
            try:
                previous_site_dir.rename(site_dir)
            except OSError:
                _rmtree_parallel(previous_site_dir)

    def replace_site_from_archive(self, archive_path: Path, tag: str, progress_callback: Callable[[int, int], None] | None = None) -> str | None:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import logging
        import threading

        _log = logging.getLogger(__name__)
        dest = self._site_dir(tag, "site")
        dest.mkdir(parents=True, exist_ok=True)

        # Count regular site files (exclude reserved metadata and diff files)
        total = sum(
            1 for m in tarfile.open(archive_path, mode="r:*").getmembers()
            if m.isreg()
            and m.name not in (_CHANGED_PAGES_FILE, f"./{_CHANGED_PAGES_FILE}")
            and not any(p == _DIFFS_DIR for p in Path(m.name).parts)
        )
        _log.info("replace_site_from_archive %r: writing %d files in parallel to %s", tag, total, dest)

        completed = [0]
        counter_lock = threading.Lock()
        # Semaphore limits how many file payloads are in memory simultaneously.
        sem = threading.Semaphore(64)
        changed_pages: str | None = None

        def _write(target: Path, data: bytes) -> None:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                if progress_callback:
                    with counter_lock:
                        completed[0] += 1
                        n = completed[0]
                    if n % 50 == 0 or n == total:
                        progress_callback(n, total)
            finally:
                sem.release()

        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = []
            with tarfile.open(archive_path, mode="r:*") as archive:
                for member in archive.getmembers():
                    rel = Path(member.name)
                    parts = [part for part in rel.parts if part not in ("", ".")]
                    if not parts or ".." in parts:
                        continue
                    rel = Path(*parts)
                    # Intercept reserved metadata file — store, don't write to disk.
                    if str(rel) == _CHANGED_PAGES_FILE and member.isreg():
                        src = archive.extractfile(member)
                        if src is not None:
                            changed_pages = src.read().decode("utf-8", errors="replace").strip() or None
                        continue
                    # Skip reserved diff files — extracted separately.
                    if rel.parts[0] == _DIFFS_DIR:
                        continue
                    target = _safe_target(dest, rel)
                    if target is None:
                        continue
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if member.isreg():
                        src = archive.extractfile(member)
                        if src is None:
                            continue
                        data = src.read()
                        sem.acquire()
                        futures.append(pool.submit(_write, target, data))
            for f in as_completed(futures):
                f.result()
        _log.info("replace_site_from_archive %r: done", tag)
        return changed_pages

    def read_file(self, tag: str, rel_path: str) -> bytes | None:
        site_root = self._site_dir(tag, "site").resolve()
        file_path = (site_root / rel_path).resolve()
        try:
            file_path.relative_to(site_root)
        except ValueError:
            return None
        if not file_path.exists() or not file_path.is_file():
            return None
        return file_path.read_bytes()

    def exists(self, tag: str, rel_path: str) -> bool:
        site_root = self._site_dir(tag, "site").resolve()
        file_path = (site_root / rel_path).resolve()
        try:
            file_path.relative_to(site_root)
        except ValueError:
            return False
        return file_path.is_file()

    def delete_build(self, tag: str) -> None:
        build_dir = self._build_dir(tag)
        if build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)


class GCSSiteStore(SiteStore):
    def __init__(self, bucket_name: str, key_prefix: str) -> None:
        if not bucket_name:
            raise ValueError("REVIEW_GCS_BUCKET is required when REVIEW_SITE_STORAGE=gcs")
        try:
            from google.cloud import storage
            from google.api_core.exceptions import NotFound
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "GCS storage backend requires google-cloud-storage. Install it or use REVIEW_SITE_STORAGE=filesystem."
            ) from exc
        self._not_found = NotFound
        self._bucket_name = bucket_name
        self._prefix = key_prefix.strip("/")
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)

    def init_storage(self) -> None:
        return

    def _prefix_with_root(self, suffix: str) -> str:
        if not self._prefix:
            return suffix
        return f"{self._prefix}/{suffix}"

    def _snapshot_prefix(self, tag: str, snapshot: str) -> str:
        return self._prefix_with_root(f"builds/{tag}/{snapshot}/")

    def _has_prefix(self, prefix: str) -> bool:
        for _ in self._bucket.list_blobs(prefix=prefix, max_results=1):
            return True
        return False

    def _delete_prefix(self, prefix: str) -> None:
        blobs = list(self._bucket.list_blobs(prefix=prefix))
        if blobs:
            self._bucket.delete_blobs(blobs)

    def _copy_prefix(self, source_prefix: str, destination_prefix: str) -> None:
        for blob in self._bucket.list_blobs(prefix=source_prefix):
            suffix = blob.name[len(source_prefix) :]
            self._bucket.copy_blob(blob, self._bucket, new_name=f"{destination_prefix}{suffix}")

    def begin_replace(self, tag: str) -> bool:
        site_prefix = self._snapshot_prefix(tag, "site")
        previous_prefix = self._snapshot_prefix(tag, PREVIOUS_SITE_DIRNAME)
        self._delete_prefix(previous_prefix)
        had_existing_site = self._has_prefix(site_prefix)
        if had_existing_site:
            self._copy_prefix(site_prefix, previous_prefix)
        self._delete_prefix(site_prefix)
        return had_existing_site

    def rollback_replace(self, tag: str, had_existing_site: bool) -> None:
        site_prefix = self._snapshot_prefix(tag, "site")
        previous_prefix = self._snapshot_prefix(tag, PREVIOUS_SITE_DIRNAME)
        self._delete_prefix(site_prefix)
        if had_existing_site and self._has_prefix(previous_prefix):
            self._copy_prefix(previous_prefix, site_prefix)

    def replace_site_from_archive(self, archive_path: Path, tag: str, progress_callback: Callable[[int, int], None] | None = None) -> str | None:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        site_prefix = self._snapshot_prefix(tag, "site")

        uploads: list[tuple[str, bytes, str]] = []
        changed_pages: str | None = None
        with tarfile.open(archive_path, mode="r:*") as archive:
            for member in archive.getmembers():
                if not member.isreg():
                    continue
                rel = Path(member.name)
                parts = [part for part in rel.parts if part not in ("", ".")]
                if not parts or ".." in parts:
                    continue
                rel_str = Path(*parts).as_posix()
                # Intercept reserved metadata file — store, don't upload to GCS.
                if rel_str == _CHANGED_PAGES_FILE:
                    src = archive.extractfile(member)
                    if src is not None:
                        changed_pages = src.read().decode("utf-8", errors="replace").strip() or None
                    continue
                # Skip reserved diff files — extracted separately.
                if rel_str.startswith(f"{_DIFFS_DIR}/"):
                    continue
                src = archive.extractfile(member)
                if src is None:
                    continue
                ctype = mimetypes.guess_type(rel_str)[0] or "application/octet-stream"
                uploads.append((f"{site_prefix}{rel_str}", src.read(), ctype))

        total = len(uploads)
        completed = [0]
        counter_lock = threading.Lock()

        def _upload(blob_name: str, data: bytes, content_type: str) -> None:
            self._bucket.blob(blob_name).upload_from_string(data, content_type=content_type)
            if progress_callback:
                with counter_lock:
                    completed[0] += 1
                    n = completed[0]
                if n % 50 == 0 or n == total:
                    progress_callback(n, total)

        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = {pool.submit(_upload, *u): u[0] for u in uploads}
            for f in as_completed(futures):
                f.result()
        return changed_pages

    def read_file(self, tag: str, rel_path: str) -> bytes | None:
        blob = self._bucket.blob(f"{self._snapshot_prefix(tag, 'site')}{rel_path}")
        try:
            return blob.download_as_bytes()
        except self._not_found:
            return None

    def exists(self, tag: str, rel_path: str) -> bool:
        return self._bucket.blob(f"{self._snapshot_prefix(tag, 'site')}{rel_path}").exists()

    def delete_build(self, tag: str) -> None:
        self._delete_prefix(self._prefix_with_root(f"builds/{tag}/"))


class LocalCacheSiteStore(SiteStore):
    def __init__(self, local_dir: Path, backing_dir: Path) -> None:
        self._local = local_dir      # container-local disk — fast r/w
        self._backing = backing_dir  # GCS FUSE — scanned at startup to recover any legacy tar backups
        self._log = _logging.getLogger(__name__)

    def _site_dir(self, tag: str) -> Path:
        return self._local / tag / "site"

    def _prev_dir(self, tag: str) -> Path:
        return self._local / tag / PREVIOUS_SITE_DIRNAME


    def init_storage(self) -> None:
        self._local.mkdir(parents=True, exist_ok=True)
        # GCS FUSE recovery disabled while testing container-local serving.
        # Uncomment to restore restart recovery from tar backups on GCS FUSE.
        # self._backing.mkdir(parents=True, exist_ok=True)
        # try:
        #     to_recover = [
        #         (tag_dir.name, tag_dir / "site.tar.gz")
        #         for tag_dir in self._backing.iterdir()
        #         if tag_dir.is_dir()
        #         and (tag_dir / "site.tar.gz").exists()
        #         and not self._site_dir(tag_dir.name).exists()
        #     ]
        # except Exception as exc:
        #     self._log.error("init recovery scan failed: %s", exc)
        #     return
        # if to_recover:
        #     self._log.info("scheduling sequential recovery of %d build(s)", len(to_recover))
        #     _threading.Thread(target=self._recover_all, args=(to_recover,), daemon=True).start()

    def _recover_all(self, builds: list[tuple[str, Path]]) -> None:
        for tag, tar_path in builds:
            try:
                self._log.info("recovering %r from archive...", tag)
                extract_site_archive(tar_path, self._site_dir(tag))
                self._log.info("recovered %r", tag)
            except Exception as exc:
                self._log.error("recovery of %r failed: %s", tag, exc)

    def begin_replace(self, tag: str) -> bool:
        site = self._site_dir(tag)
        prev = self._prev_dir(tag)
        (self._local / tag).mkdir(parents=True, exist_ok=True)
        had = site.exists()
        if had:
            if prev.exists():
                shutil.rmtree(prev, ignore_errors=True)
            site.rename(prev)  # local FS rename — fast, no ENFILE
        return had

    def rollback_replace(self, tag: str, had_existing_site: bool) -> None:
        site = self._site_dir(tag)
        prev = self._prev_dir(tag)
        if site.exists():
            shutil.rmtree(site, ignore_errors=True)
        if had_existing_site and prev.exists():
            prev.rename(site)

    def replace_site_from_archive(self, archive_path: Path, tag: str, progress_callback: Callable[[int, int], None] | None = None) -> str | None:
        dest = self._backing / tag / "site"
        fuse = _parse_fuse_mount(self._backing)
        if fuse:
            bucket, mount_point = fuse
            gcs_prefix = dest.relative_to(mount_point).as_posix()
            self._log.info("uploading %r to gs://%s/%s/ via GCS SDK...", tag, bucket, gcs_prefix)
            return _upload_archive_to_gcs(archive_path, bucket, gcs_prefix, self._log)
        else:
            self._log.info("extracting %r to local backing store...", tag)
            changed_pages = extract_site_archive(archive_path, dest)
            self._log.info("extracted %r to local backing store", tag)
            return changed_pages

    def read_file(self, tag: str, rel_path: str) -> bytes | None:
        # Check FUSE backing store first (tar imports and direct GCS uploads land here).
        backing_root = (self._backing / tag / "site").resolve()
        backing_path = (backing_root / rel_path.lstrip("/")).resolve()
        try:
            backing_path.relative_to(backing_root)
        except ValueError:
            return None
        if backing_path.is_file():
            return backing_path.read_bytes()
        # Fall back to local cache for any build extracted there previously.
        site_root = self._site_dir(tag).resolve()
        path = (site_root / rel_path.lstrip("/")).resolve()
        try:
            path.relative_to(site_root)
        except ValueError:
            return None
        if not path.is_file():
            return None
        return path.read_bytes()

    def exists(self, tag: str, rel_path: str) -> bool:
        backing_root = (self._backing / tag / "site").resolve()
        backing_path = (backing_root / rel_path.lstrip("/")).resolve()
        try:
            backing_path.relative_to(backing_root)
        except ValueError:
            return False
        if backing_path.is_file():
            return True
        site_root = self._site_dir(tag).resolve()
        path = (site_root / rel_path.lstrip("/")).resolve()
        try:
            path.relative_to(site_root)
        except ValueError:
            return False
        return path.is_file()

    def delete_build(self, tag: str) -> None:
        local_dir = self._local / tag
        if local_dir.exists():
            shutil.rmtree(local_dir, ignore_errors=True)
        backing_dir = self._backing / tag
        if backing_dir.exists():
            _threading.Thread(
                target=lambda: shutil.rmtree(backing_dir, ignore_errors=True),
                daemon=True,
            ).start()


def create_site_store() -> SiteStore:
    if SITE_STORAGE_BACKEND == "filesystem":
        return LocalCacheSiteStore(LOCAL_CACHE_DIR, BUILDS_DIR)
    if SITE_STORAGE_BACKEND == "gcs":
        return GCSSiteStore(GCS_BUCKET, GCS_PREFIX)
    raise ValueError(
        f"Unsupported REVIEW_SITE_STORAGE={SITE_STORAGE_BACKEND!r}. Use 'filesystem' or 'gcs'."
    )


SITE_STORE = create_site_store()


def init_storage() -> None:
    SITE_STORE.init_storage()
    with db_connect() as conn:
        if DB_BACKEND == "sqlite":
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS builds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_ref TEXT NOT NULL,
                    tag TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    rewrite_host TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER,
                    archived_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    build_id INTEGER NOT NULL,
                    page_path TEXT NOT NULL,
                    line_start INTEGER,
                    line_end INTEGER,
                    selected_text TEXT NOT NULL,
                    selection_json TEXT,
                    body TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    parent_id INTEGER,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    resolved_at INTEGER,
                    FOREIGN KEY(build_id) REFERENCES builds(id),
                    FOREIGN KEY(parent_id) REFERENCES comments(id)
                );

                CREATE INDEX IF NOT EXISTS idx_builds_tag ON builds(tag);
                CREATE INDEX IF NOT EXISTS idx_comments_build_page ON comments(build_id, page_path);
                CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);
                CREATE INDEX IF NOT EXISTS idx_comments_resolved ON comments(resolved);

                CREATE TABLE IF NOT EXISTS page_diffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    build_id INTEGER NOT NULL,
                    page_path TEXT NOT NULL,
                    diff_content TEXT NOT NULL,
                    FOREIGN KEY(build_id) REFERENCES builds(id)
                );

                CREATE INDEX IF NOT EXISTS idx_page_diffs_build_page ON page_diffs(build_id, page_path);

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    logger TEXT NOT NULL,
                    message TEXT NOT NULL,
                    instance TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
                """
            )
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(builds)").fetchall()}
            if "updated_at" not in columns:
                conn.execute("ALTER TABLE builds ADD COLUMN updated_at INTEGER")
            if "rewrite_host" not in columns:
                conn.execute("ALTER TABLE builds ADD COLUMN rewrite_host TEXT")
            if "changed_pages" not in columns:
                conn.execute("ALTER TABLE builds ADD COLUMN changed_pages TEXT")
            if "diff_pages" not in columns:
                conn.execute("ALTER TABLE builds ADD COLUMN diff_pages TEXT")
            comment_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(comments)").fetchall()}
            if "selection_json" not in comment_columns:
                conn.execute("ALTER TABLE comments ADD COLUMN selection_json TEXT")
            if "kind" not in comment_columns:
                conn.execute("ALTER TABLE comments ADD COLUMN kind TEXT NOT NULL DEFAULT 'preview'")
            if "diff_start_key" not in comment_columns:
                conn.execute("ALTER TABLE comments ADD COLUMN diff_start_key TEXT")
            if "diff_end_key" not in comment_columns:
                conn.execute("ALTER TABLE comments ADD COLUMN diff_end_key TEXT")
            if "edited_at" not in comment_columns:
                conn.execute("ALTER TABLE comments ADD COLUMN edited_at INTEGER")
            if "review_key" not in columns:
                conn.execute("ALTER TABLE builds ADD COLUMN review_key TEXT")
            if "version" not in columns:
                conn.execute("ALTER TABLE builds ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            if "carried_from_id" not in comment_columns:
                conn.execute("ALTER TABLE comments ADD COLUMN carried_from_id INTEGER")
            if "context_before" not in comment_columns:
                conn.execute("ALTER TABLE comments ADD COLUMN context_before TEXT")
            if "context_after" not in comment_columns:
                conn.execute("ALTER TABLE comments ADD COLUMN context_after TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_builds_review_key ON builds(review_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_carried_from ON comments(carried_from_id)")
        else:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS builds (
                    id BIGSERIAL PRIMARY KEY,
                    image_ref TEXT NOT NULL,
                    tag TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    rewrite_host TEXT,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT,
                    archived_at BIGINT
                );

                CREATE TABLE IF NOT EXISTS comments (
                    id BIGSERIAL PRIMARY KEY,
                    build_id BIGINT NOT NULL REFERENCES builds(id),
                    page_path TEXT NOT NULL,
                    line_start INTEGER,
                    line_end INTEGER,
                    selected_text TEXT NOT NULL,
                    selection_json TEXT,
                    body TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    parent_id BIGINT REFERENCES comments(id),
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at BIGINT NOT NULL,
                    resolved_at BIGINT
                );

                CREATE INDEX IF NOT EXISTS idx_builds_tag ON builds(tag);
                CREATE INDEX IF NOT EXISTS idx_comments_build_page ON comments(build_id, page_path);
                CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);
                CREATE INDEX IF NOT EXISTS idx_comments_resolved ON comments(resolved);

                ALTER TABLE builds ADD COLUMN IF NOT EXISTS updated_at BIGINT;
                ALTER TABLE builds ADD COLUMN IF NOT EXISTS rewrite_host TEXT;
                ALTER TABLE builds ADD COLUMN IF NOT EXISTS changed_pages TEXT;
                ALTER TABLE builds ADD COLUMN IF NOT EXISTS diff_pages TEXT;
                ALTER TABLE comments ADD COLUMN IF NOT EXISTS selection_json TEXT;
                ALTER TABLE comments ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'preview';
                ALTER TABLE comments ADD COLUMN IF NOT EXISTS diff_start_key TEXT;
                ALTER TABLE comments ADD COLUMN IF NOT EXISTS diff_end_key TEXT;
                ALTER TABLE comments ADD COLUMN IF NOT EXISTS edited_at BIGINT;
                ALTER TABLE builds ADD COLUMN IF NOT EXISTS review_key TEXT;
                ALTER TABLE builds ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
                ALTER TABLE comments ADD COLUMN IF NOT EXISTS carried_from_id BIGINT;
                ALTER TABLE comments ADD COLUMN IF NOT EXISTS context_before TEXT;
                ALTER TABLE comments ADD COLUMN IF NOT EXISTS context_after TEXT;
                CREATE INDEX IF NOT EXISTS idx_builds_review_key ON builds(review_key);
                CREATE INDEX IF NOT EXISTS idx_comments_carried_from ON comments(carried_from_id);

                CREATE TABLE IF NOT EXISTS page_diffs (
                    id BIGSERIAL PRIMARY KEY,
                    build_id BIGINT NOT NULL REFERENCES builds(id),
                    page_path TEXT NOT NULL,
                    diff_content TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_page_diffs_build_page ON page_diffs(build_id, page_path);

                CREATE TABLE IF NOT EXISTS logs (
                    id BIGSERIAL PRIMARY KEY,
                    ts BIGINT NOT NULL,
                    level TEXT NOT NULL,
                    logger TEXT NOT NULL,
                    message TEXT NOT NULL,
                    instance TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
                """
            )

        conn.execute("UPDATE builds SET updated_at = created_at WHERE updated_at IS NULL")
        # Existing previews become version 1 of a review named after their tag.
        conn.execute("UPDATE builds SET review_key = tag WHERE review_key IS NULL")
        conn.execute(
            "UPDATE builds SET rewrite_host = ? WHERE rewrite_host IS NULL",
            (DEFAULT_REWRITE_HOST,),
        )
        conn.commit()


def _safe_target(base_dir: Path, rel_path: Path) -> Path | None:
    candidate = (base_dir / rel_path).resolve()
    try:
        candidate.relative_to(base_dir.resolve())
    except ValueError:
        return None
    return candidate


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _rmtree_parallel(path: Path, max_workers: int = 32) -> None:
    """Delete a directory tree using a thread pool — fast on GCS FUSE mounts."""
    from concurrent.futures import ThreadPoolExecutor
    if not path.exists():
        return
    files = [p for p in path.rglob("*") if p.is_file() or p.is_symlink()]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(lambda p=p: p.unlink(missing_ok=True)) for p in files]
        for f in futures:
            try:
                f.result()
            except Exception:
                pass
    shutil.rmtree(path, ignore_errors=True)



def _parse_fuse_mount(path: Path) -> tuple[str, str] | None:
    """Return (bucket, mount_point) if path is under a GCS FUSE mount, else None."""
    try:
        mounts_text = Path("/proc/mounts").read_text()
        best: tuple[int, str, str] = (0, "", "")
        for line in mounts_text.splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[2] != "fuse.gcsfuse":
                continue
            device, mount_point = parts[0], parts[1]
            bucket = device[len("gcsfuse#"):] if device.startswith("gcsfuse#") else device
            if not bucket:
                continue
            try:
                path.relative_to(mount_point)
                if len(mount_point) > best[0]:
                    best = (len(mount_point), bucket, mount_point)
            except ValueError:
                continue
        return (best[1], best[2]) if best[1] else None
    except Exception:
        return None


def _upload_archive_to_gcs(
    archive_path: Path,
    bucket_name: str,
    gcs_prefix: str,
    log: _logging.Logger,
) -> str | None:
    from concurrent.futures import ThreadPoolExecutor

    try:
        from google.cloud import storage as _gcs
    except ImportError as exc:
        raise RuntimeError("google-cloud-storage is required for GCS uploads") from exc

    client = _gcs.Client()
    bucket = client.bucket(bucket_name)

    # Snapshot existing blob names before uploading. New files are written first
    # (overwriting matching paths in place) so the preview stays live throughout.
    # Only after all uploads succeed do we delete blobs that are no longer present.
    existing_names = {b.name for b in bucket.list_blobs(prefix=f"{gcs_prefix}/")}
    if existing_names:
        log.info("found %d existing objects at gs://%s/%s/, will remove stale after upload",
                 len(existing_names), bucket_name, gcs_prefix)

    sem = _threading.Semaphore(64)
    uploaded = [0]
    counter_lock = _threading.Lock()
    new_names: set[str] = set()
    changed_pages: str | None = None

    def _upload(blob_name: str, data: bytes, content_type: str) -> None:
        try:
            bucket.blob(blob_name).upload_from_string(data, content_type=content_type)
            with counter_lock:
                uploaded[0] += 1
        finally:
            sem.release()

    futures = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        with tarfile.open(archive_path, mode="r|gz") as archive:
            for member in archive:
                if not member.isreg():
                    continue
                rel = Path(member.name)
                parts = [p for p in rel.parts if p not in ("", ".")]
                if not parts or ".." in parts:
                    continue
                rel_str = Path(*parts).as_posix()
                # Intercept reserved metadata file — store, don't upload to GCS.
                if rel_str == _CHANGED_PAGES_FILE:
                    src = archive.extractfile(member)
                    if src is not None:
                        changed_pages = src.read().decode("utf-8", errors="replace").strip() or None
                    continue
                # Skip reserved diff files — extracted separately.
                if rel_str.startswith(f"{_DIFFS_DIR}/"):
                    continue
                src = archive.extractfile(member)
                if src is None:
                    continue
                data = src.read()
                content_type = mimetypes.guess_type(rel_str)[0] or "application/octet-stream"
                blob_name = f"{gcs_prefix}/{rel_str}"
                new_names.add(blob_name)  # tracked from main thread, no lock needed
                sem.acquire()
                futures.append(pool.submit(_upload, blob_name, data, content_type))

    for f in futures:
        f.result()

    log.info("uploaded %d objects to gs://%s/%s/", uploaded[0], bucket_name, gcs_prefix)

    # Delete any blobs from the old set that are not in the new archive.
    stale = existing_names - new_names
    if stale:
        log.info("deleting %d stale objects at gs://%s/%s/", len(stale), bucket_name, gcs_prefix)
        bucket.delete_blobs([bucket.blob(name) for name in stale])

    return changed_pages


def extract_site_archive(archive_path: Path, destination: Path) -> str | None:
    import time as _time
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    changed_pages: str | None = None

    # r|gz streaming mode: single decompression pass (r:gz decompresses twice —
    # once for getmembers(), again for each extractfile() call).
    with tarfile.open(archive_path, mode="r|gz") as archive:
        for i, member in enumerate(archive):
            rel = Path(member.name)
            parts = [part for part in rel.parts if part not in ("", ".")]
            if not parts or ".." in parts:
                continue
            rel = Path(*parts)
            # Intercept reserved metadata file — store, don't write to disk.
            if str(rel) == _CHANGED_PAGES_FILE and member.isreg():
                src = archive.extractfile(member)
                if src is not None:
                    changed_pages = src.read().decode("utf-8", errors="replace").strip() or None
                continue
            # Skip reserved diff files — extracted separately.
            if rel.parts[0] == _DIFFS_DIR:
                continue
            target = _safe_target(destination, rel)
            if target is None:
                continue

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isreg():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = archive.extractfile(member)
                if src is not None:
                    with src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)
            elif member.issym():
                link_target = Path(member.linkname)
                if not (link_target.is_absolute() or ".." in link_target.parts):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _remove_path(target)
                    try:
                        target.symlink_to(member.linkname)
                    except OSError:
                        pass

            # Yield GIL every 50 members so HTTP handler threads stay responsive.
            if i % 50 == 0:
                _time.sleep(0)

    return changed_pages


def _extract_diffs_from_archive(archive_path: Path) -> dict[str, str]:
    """Return {canonical_page_path: diff_content} for all .diffs/ entries in the archive."""
    result: dict[str, str] = {}
    with tarfile.open(archive_path, mode="r:*") as archive:
        for member in archive.getmembers():
            if not member.isreg():
                continue
            rel = Path(member.name)
            parts = [p for p in rel.parts if p not in ("", ".")]
            if len(parts) < 2 or parts[0] != _DIFFS_DIR:
                continue
            src = archive.extractfile(member)
            if src is None:
                continue
            diff_rel = "/".join(parts[1:])
            page_path = canonical_page_path(f"/{diff_rel}")
            result[page_path] = src.read().decode("utf-8", errors="replace")
    return result


def store_page_diffs(conn: DBConnectionProxy, build_id: int, page_diffs_map: dict[str, str]) -> None:
    conn.execute("DELETE FROM page_diffs WHERE build_id = ?", (build_id,))
    for page_path, diff_content in page_diffs_map.items():
        conn.execute(
            "INSERT INTO page_diffs (build_id, page_path, diff_content) VALUES (?, ?, ?)",
            (build_id, page_path, diff_content),
        )


def save_page_diffs_best_effort(build_id: int, page_diffs_map: dict[str, str], name: str) -> None:
    """Save a preview's page diffs without ever failing the upload.

    The diffs only feed the changed pages view, so a problem with one should cost that
    page its diff and nothing more. All of them are tried in one go first, and if that
    fails they are retried one page at a time to find the ones that can't be saved.
    Pages whose diff was lost are taken off the build's list of pages with a diff, so
    the view doesn't offer a diff that isn't there. Problems are logged as warnings.
    """
    log = _logging.getLogger(__name__)
    try:
        # With no diffs this just clears any old ones, as an overwritten preview needs.
        with db_connect() as conn:
            store_page_diffs(conn, build_id, page_diffs_map)
            conn.commit()
        return
    except Exception as exc:
        log.warning("%r: saving page diffs together failed (%s), retrying one page at a time", name, exc)

    saved: list[str] = []
    failed: list[str] = []
    try:
        with db_connect() as conn:
            conn.execute("DELETE FROM page_diffs WHERE build_id = ?", (build_id,))
            conn.commit()
            for page_path, diff_content in page_diffs_map.items():
                try:
                    conn.execute(
                        "INSERT INTO page_diffs (build_id, page_path, diff_content) VALUES (?, ?, ?)",
                        (build_id, page_path, diff_content),
                    )
                    conn.commit()
                    saved.append(page_path)
                except Exception as exc:
                    conn.rollback()
                    failed.append(page_path)
                    log.warning("%r: diff for %s could not be saved (%s)", name, page_path, exc)
    except Exception as exc:
        # Not even a connection. Treat it as no diffs, the preview itself is already saved.
        log.warning("%r: page diffs could not be saved at all (%s)", name, exc)
        saved, failed = [], list(page_diffs_map)

    try:
        with db_connect() as conn:
            conn.execute(
                "UPDATE builds SET diff_pages = ? WHERE id = ?",
                ("\n".join(sorted(saved)) or None, build_id),
            )
            conn.commit()
    except Exception as exc:
        log.warning("%r: could not update the list of pages with a diff (%s)", name, exc)
    log.warning(
        "%r: preview imported, but %d of %d page diff(s) could not be saved", name, len(failed), len(page_diffs_map)
    )


def fetch_page_diff(build_id: int, page_path: str) -> str | None:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT diff_content FROM page_diffs WHERE build_id = ? AND page_path = ?",
            (build_id, canonical_page_path(page_path)),
        ).fetchone()
    return str(row["diff_content"]) if row else None


def import_build_archive_path(
    archive_path: Path,
    tag: str,
    display_name: str | None = None,
    source_ref: str | None = None,
    rewrite_host: str = DEFAULT_REWRITE_HOST,
    stage_callback: Callable[[str], None] | None = None,
    new_version: bool = True,
) -> ImportedBuild:
    """Import an archive as a preview.

    Uploading to a name that already exists creates the next version of that
    review and carries matching open comments forward. Pass new_version=False
    to overwrite the latest version in place instead (used for PR previews that
    are refreshed on every push).
    """
    review_key = slugify_tag(tag)
    resolved_name = (display_name or review_key).strip() or review_key
    stored_rewrite_host = ""
    if rewrite_host:
        stored_rewrite_host = normalize_rewrite_host(rewrite_host) or DEFAULT_REWRITE_HOST

    group = list_review_versions(review_key)
    latest = group[-1] if group else None
    if latest is not None and new_version and latest["archived_at"] is not None:
        # The review was deleted and is only waiting out the grace period, so a
        # new upload under the same name starts fresh instead of reviving it.
        for old_row in group:
            delete_build_and_comments(int(old_row["id"]), delete_storage=str(old_row["tag"]) != review_key)
        group = []
        latest = None

    overwrite = latest is not None and not new_version
    if latest is None:
        version = 1
        storage_tag = review_key
    elif overwrite:
        version = version_of(latest)
        storage_tag = str(latest["tag"])
    else:
        version = version_of(latest) + 1
        storage_tag = f"{review_key}{_VERSION_TAG_SEPARATOR}v{version}"

    site_dir = BUILDS_DIR / storage_tag / "site"
    had_existing_site = SITE_STORE.begin_replace(storage_tag)

    if stage_callback:
        stage_callback("Extracting preview files")

    def _progress(n: int, total: int) -> None:
        if stage_callback:
            stage_callback(f"Importing file {n} of {total}")

    try:
        changed_pages = SITE_STORE.replace_site_from_archive(archive_path, storage_tag, progress_callback=_progress if stage_callback else None)
    except Exception:
        SITE_STORE.rollback_replace(storage_tag, had_existing_site)
        raise

    page_diffs_map = _extract_diffs_from_archive(archive_path)
    diff_pages = "\n".join(sorted(page_diffs_map.keys())) or None

    now = now_ts()
    if stage_callback:
        stage_callback("Updating preview metadata")
    with db_connect() as conn:
        image_ref = source_ref or f"upload:{review_key}"
        if overwrite:
            build_id = int(latest["id"])
            conn.execute(
                """
                UPDATE builds
                SET image_ref = ?, display_name = ?, rewrite_host = ?, changed_pages = ?,
                    diff_pages = ?, archived_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (image_ref, resolved_name, stored_rewrite_host, changed_pages, diff_pages, now, build_id),
            )
        else:
            insert_sql = """
                INSERT INTO builds (image_ref, tag, display_name, rewrite_host, changed_pages, diff_pages,
                                    review_key, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            values = (
                image_ref, storage_tag, resolved_name, stored_rewrite_host, changed_pages, diff_pages,
                review_key, version, now, now,
            )
            if DB_BACKEND == "postgres":
                inserted = conn.execute(f"{insert_sql} RETURNING id", values).fetchone()
                if not inserted:
                    raise ValueError("failed to create preview metadata")
                build_id = int(inserted["id"])
            else:
                cur = conn.execute(insert_sql, values)
                build_id = int(cur.lastrowid)
        if latest is not None and not overwrite:
            if stage_callback:
                stage_callback("Carrying comments forward")
            carried, outdated = carry_comments_forward(conn, latest, build_id, storage_tag)
            _logging.getLogger(__name__).info(
                "%r v%d: carried %d comment(s) forward, %d left outdated", review_key, version, carried, outdated
            )
        conn.commit()

    # The preview exists now. The diffs come after it and can't fail the import.
    if stage_callback:
        stage_callback("Saving page diffs")
    save_page_diffs_best_effort(build_id, page_diffs_map, review_key)

    return ImportedBuild(
        build_id=build_id,
        tag=review_key,
        image_ref=image_ref,
        display_name=resolved_name,
        extracted_to=site_dir,
        version=version,
    )


def import_build_archive(
    archive_bytes: bytes,
    tag: str,
    display_name: str | None = None,
    source_ref: str | None = None,
    rewrite_host: str = DEFAULT_REWRITE_HOST,
) -> ImportedBuild:
    if not archive_bytes:
        raise ValueError("archive payload is empty")
    with tempfile.NamedTemporaryFile(prefix="review-site-", suffix=".tar", delete=False) as tmp:
        temp_archive = Path(tmp.name)
        tmp.write(archive_bytes)
    try:
        return import_build_archive_path(
            archive_path=temp_archive,
            tag=tag,
            display_name=display_name,
            source_ref=source_ref,
            rewrite_host=rewrite_host,
        )
    finally:
        temp_archive.unlink(missing_ok=True)


@dataclass
class ImportedBuild:
    build_id: int
    tag: str
    image_ref: str
    display_name: str
    extracted_to: Path
    version: int = 1

def inject_review_bundle(site_root: Path, build_id: int, tag: str) -> None:
    marker_pattern = re.compile(
        rf"{re.escape(INJECT_START)}.*?{re.escape(INJECT_END)}\n?",
        flags=re.DOTALL,
    )
    for html_file in site_root.rglob("*.html"):
        rel = html_file.relative_to(site_root).as_posix()
        page_path = canonical_page_path(f"/{rel}")
        context = {
            "buildId": build_id,
            "buildTag": tag,
            "pagePath": page_path,
        }
        inject = (
            f"{INJECT_START}\n"
            '<link rel="stylesheet" href="/_review/assets/review.css">\n'
            f"<script>window.REVIEW_CONTEXT={json.dumps(context)}</script>\n"
            '<script defer src="/_review/assets/review-client.js"></script>\n'
            f"{INJECT_END}\n"
        )
        content = html_file.read_text(encoding="utf-8", errors="replace")
        content = rewrite_docs_domain_urls(content)
        content = marker_pattern.sub("", content)
        body_idx = content.lower().rfind("</body>")
        if body_idx >= 0:
            content = f"{content[:body_idx]}{inject}{content[body_idx:]}"
        else:
            content = f"{content}\n{inject}"
        html_file.write_text(content, encoding="utf-8")


def update_build_changed_pages(build_id: int, pages: list[str]) -> bool:
    with db_connect() as conn:
        row = conn.execute("SELECT id FROM builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            return False
        cleaned = [p.strip() for p in pages if p.strip().startswith("/") and ".." not in p]
        changed_pages = "\n".join(cleaned) or None
        conn.execute("UPDATE builds SET changed_pages = ? WHERE id = ?", (changed_pages, build_id))
        conn.commit()
    return True


def version_of(row: Any) -> int:
    value = row["version"] if "version" in row.keys() else None
    return int(value or 1)


def review_key_of(row: Any) -> str:
    value = row["review_key"] if "review_key" in row.keys() else None
    return str(value or row["tag"])


# Highest version number of the review that the builds row aliased as "b" belongs to.
_LATEST_VERSION_SQL = (
    "(SELECT MAX(COALESCE(lv.version, 1)) FROM builds lv "
    "WHERE COALESCE(lv.review_key, lv.tag) = COALESCE(b.review_key, b.tag))"
)


def list_review_versions(review_key: str) -> list[Any]:
    """All versions of a review, oldest first."""
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT * FROM builds
            WHERE COALESCE(review_key, tag) = ?
            ORDER BY COALESCE(version, 1) ASC, id ASC
            """,
            (review_key,),
        ).fetchall()


def get_latest_build_for_key(review_key: str) -> Any | None:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT * FROM builds
            WHERE COALESCE(review_key, tag) = ?
            ORDER BY COALESCE(version, 1) DESC, id DESC
            LIMIT 1
            """,
            (review_key,),
        ).fetchone()


def get_build_version(review_key: str, version: int) -> Any | None:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT * FROM builds
            WHERE COALESCE(review_key, tag) = ? AND COALESCE(version, 1) = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (review_key, version),
        ).fetchone()


def review_context_for(build: Any) -> dict[str, Any]:
    """Version details the in-page review widget needs for the build being served."""
    key = review_key_of(build)
    rows = list_review_versions(key)
    version = version_of(build)
    latest_version = max((version_of(r) for r in rows), default=version)
    is_latest = version == latest_version
    return {
        "reviewKey": key,
        "version": version,
        "latestVersion": latest_version,
        "urlPrefix": review_path(key, version, is_latest).rstrip("/"),
        "versions": [
            {
                "version": version_of(r),
                "buildId": int(r["id"]),
                "createdAtHuman": format_ts(r["created_at"]),
            }
            for r in rows
        ],
    }


class _VisibleTextParser(HTMLParser):
    """Collects the text a reader sees, preferring what is inside <main>, which is
    the part of the page the comment widget lets people select."""

    _SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._main_depth = 0
        self._body_depth = 0
        self.main_parts: list[str] = []
        self.body_parts: list[str] = []
        self.all_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "main":
            self._main_depth += 1
        elif tag == "body":
            self._body_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "main":
            self._main_depth = max(0, self._main_depth - 1)
        elif tag == "body":
            self._body_depth = max(0, self._body_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.all_parts.append(data)
        if self._body_depth:
            self.body_parts.append(data)
        if self._main_depth:
            self.main_parts.append(data)


def _normalize_for_match(text: str) -> str:
    # Whitespace differs between a text selection and the page source (line breaks
    # between blocks, indentation), so matching ignores it entirely.
    return re.sub(r"\s+", "", text)


# How much page text before and after a highlight is saved with a comment. It is what
# tells apart repeats of the same words, such as a comment on one "the" of many.
CONTEXT_CHARS = 80
# Only the characters closest to the highlight are compared, since that is what says
# whether it is still the same spot. Either side matching this well is enough, so a
# neighbouring sentence being added or removed doesn't lose the comment.
_NEAR_CHARS = 30
_SIDE_MATCH = 0.7
# A selection this long that appears exactly once is trusted even if its surroundings changed.
_UNIQUE_SELECTION_CHARS = 40


def clean_context(value: Any) -> str | None:
    """Context text from a client, tidied up. None means no context was sent."""
    if not isinstance(value, str):
        return None
    return re.sub(r"\s+", " ", value).strip()[:CONTEXT_CHARS * 2]


def _common_suffix_len(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return n


def _locate_selection(
    page_text: str,
    selected_text: str,
    context_before: str | None,
    context_after: str | None,
) -> tuple[str, str] | None:
    """Find where a comment's highlighted text sits in a page of a newer version.

    The highlighted text has to appear on the page. When the comment saved the text
    around it, that decides which occurrence is meant and whether the surroundings
    still look the same. Returns the context to save at the new position, or None
    when the comment no longer fits.
    """
    collapsed = re.sub(r"\s+", " ", page_text).strip()
    # Matching ignores whitespace, so keep a way back from positions in the
    # stripped text to positions in the readable one.
    positions = [i for i, ch in enumerate(collapsed) if not ch.isspace()]
    stripped = "".join(collapsed[i] for i in positions)
    wanted = _normalize_for_match(selected_text)
    if not wanted:
        return None
    starts: list[int] = []
    found = stripped.find(wanted)
    while found != -1:
        starts.append(found)
        found = stripped.find(wanted, found + 1)
    if not starts:
        return None

    has_context = context_before is not None or context_after is not None
    if has_context:
        before = _normalize_for_match(context_before or "")
        after = _normalize_for_match(context_after or "")

        def side_scores(pos: int) -> list[float]:
            """How well each saved side of the context matches at pos. A side with
            nothing saved (the highlight was at the edge of the page) says nothing."""
            scores: list[float] = []
            if before:
                near = min(len(before), _NEAR_CHARS)
                around = stripped[max(0, pos - len(before)):pos]
                scores.append(min(_common_suffix_len(before, around), near) / near)
            if after:
                near = min(len(after), _NEAR_CHARS)
                around = stripped[pos + len(wanted):pos + len(wanted) + len(after)]
                scores.append(min(_common_prefix_len(after, around), near) / near)
            return scores

        def rank(pos: int) -> float:
            scores = side_scores(pos)
            return sum(scores) / len(scores) if scores else 1.0

        best = max(starts, key=lambda pos: (rank(pos), -pos))
        best_sides = side_scores(best)
        follows_context = not best_sides or max(best_sides) >= _SIDE_MATCH
        long_and_unique = len(starts) == 1 and len(wanted) >= _UNIQUE_SELECTION_CHARS
        if not follows_context and not long_and_unique:
            return None
    else:
        # Comments saved before context was kept can only go on the text.
        best = starts[0]

    start = positions[best]
    end = positions[best + len(wanted) - 1] + 1
    new_before = collapsed[:start].strip()[-CONTEXT_CHARS:].strip()
    new_after = collapsed[end:].strip()[:CONTEXT_CHARS].strip()
    return new_before, new_after


def _page_visible_text(storage_tag: str, page_path: str, whole_page: bool = False) -> str | None:
    trimmed = page_path.strip("/")
    if not trimmed:
        candidates = ["index.html"]
    else:
        candidates = [f"{trimmed}/index.html", trimmed if trimmed.lower().endswith(".html") else f"{trimmed}.html"]
    for rel in candidates:
        raw = SITE_STORE.read_file(storage_tag, rel)
        if raw is None:
            continue
        parser = _VisibleTextParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        if whole_page:
            # For a comment made outside <main>, such as in a footer: all of the page's text.
            return "".join(parser.body_parts or parser.all_parts)
        return "".join(parser.main_parts or parser.all_parts)
    return None


def _insert_comment_row(conn: DBConnectionProxy, values: dict[str, Any]) -> int:
    columns = list(values.keys())
    sql = f"INSERT INTO comments ({', '.join(columns)}) VALUES ({', '.join(['?'] * len(columns))})"
    params = tuple(values[c] for c in columns)
    if DB_BACKEND == "postgres":
        inserted = conn.execute(f"{sql} RETURNING id", params).fetchone()
        if not inserted:
            raise ValueError("failed to copy comment")
        return int(inserted["id"])
    return int(conn.execute(sql, params).lastrowid)


def carry_comments_forward(
    conn: DBConnectionProxy,
    previous_build: Any,
    new_build_id: int,
    new_storage_tag: str,
) -> tuple[int, int]:
    """Copy open preview comments from the previous version onto the new one when
    the text they were attached to still appears on the same page, in the same
    surroundings if the comment saved them (see _locate_selection).

    Returns (carried, outdated). Comments that can't be matched stay on the
    previous version and show up as outdated. Resolved comments stay where they
    are, as do comments on the diff view, whose row keys don't exist in a new
    version.
    """
    roots = conn.execute(
        """
        SELECT * FROM comments
        WHERE build_id = ? AND parent_id IS NULL AND resolved = 0
        ORDER BY id ASC
        """,
        (int(previous_build["id"]),),
    ).fetchall()
    page_text: dict[str, str | None] = {}
    carried = 0
    outdated = 0
    for root in roots:
        kind = str(root["kind"] or "preview") if "kind" in root.keys() else "preview"
        selected = str(root["selected_text"] or "")
        page_path = str(root["page_path"])
        if kind != "preview" or not _normalize_for_match(selected):
            outdated += 1
            continue
        # A comment made outside <main> is matched against the whole page, and its copy keeps a
        # note of that so the widget looks in the same place.
        whole_page = False
        selection_raw = root["selection_json"] if "selection_json" in root.keys() else None
        if selection_raw:
            try:
                whole_page = json.loads(str(selection_raw)).get("root") == "body"
            except Exception:
                whole_page = False
        cache_key = f"{page_path}\n{'body' if whole_page else 'main'}"
        if cache_key not in page_text:
            page_text[cache_key] = _page_visible_text(new_storage_tag, page_path, whole_page)
        text = page_text[cache_key]
        located = None
        if text is not None:
            located = _locate_selection(
                text,
                selected,
                root["context_before"] if "context_before" in root.keys() else None,
                root["context_after"] if "context_after" in root.keys() else None,
            )
        if located is None:
            outdated += 1
            continue
        # No selection or line numbers are copied. They describe the old page, so
        # the widget finds the new position again from the selected text.
        new_root_id = _insert_comment_row(
            conn,
            {
                "build_id": new_build_id,
                "page_path": page_path,
                "line_start": None,
                "line_end": None,
                "selected_text": root["selected_text"],
                "selection_json": json.dumps({"root": "body"}) if whole_page else None,
                "body": root["body"],
                "reviewer": root["reviewer"],
                "parent_id": None,
                "resolved": 0,
                "created_at": root["created_at"],
                "kind": "preview",
                "edited_at": root["edited_at"] if "edited_at" in root.keys() else None,
                "carried_from_id": int(root["id"]),
                # The text around the highlight as it reads in the new version.
                "context_before": located[0],
                "context_after": located[1],
            },
        )
        replies = conn.execute(
            "SELECT * FROM comments WHERE parent_id = ? ORDER BY created_at ASC, id ASC",
            (int(root["id"]),),
        ).fetchall()
        for reply in replies:
            _insert_comment_row(
                conn,
                {
                    "build_id": new_build_id,
                    "page_path": page_path,
                    "selected_text": reply["selected_text"] or "",
                    "body": reply["body"],
                    "reviewer": reply["reviewer"],
                    "parent_id": new_root_id,
                    "resolved": 0,
                    "created_at": reply["created_at"],
                    "kind": "preview",
                    "edited_at": reply["edited_at"] if "edited_at" in reply.keys() else None,
                },
            )
        carried += 1
    return carried, outdated


def count_outdated_comments(build: Any, page_path: str | None = None) -> int:
    """Open comments on earlier versions of this review that were not carried
    forward, optionally only those on one page. Only meaningful for the latest
    version, otherwise returns 0."""
    key = review_key_of(build)
    version = version_of(build)
    with db_connect() as conn:
        latest_version = conn.execute(
            "SELECT MAX(COALESCE(version, 1)) AS v FROM builds WHERE COALESCE(review_key, tag) = ?",
            (key,),
        ).fetchone()
        if not latest_version or int(latest_version["v"] or 1) != version:
            return 0
        page_clause = "AND c.page_path = ?" if page_path is not None else ""
        params: tuple[Any, ...] = (key, version) + ((page_path,) if page_path is not None else ())
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM comments c
            JOIN builds b ON b.id = c.build_id
            LEFT JOIN comments d ON d.carried_from_id = c.id
            WHERE COALESCE(b.review_key, b.tag) = ?
              AND COALESCE(b.version, 1) < ?
              AND c.parent_id IS NULL
              AND c.resolved = 0
              AND d.id IS NULL
              {page_clause}
            """,
            params,
        ).fetchone()
    return int(row["n"] or 0) if row else 0


def get_build_by_tag(tag: str) -> sqlite3.Row | None:
    with db_connect() as conn:
        return conn.execute("SELECT * FROM builds WHERE tag = ?", (tag,)).fetchone()


def get_build_by_id(build_id: int) -> sqlite3.Row | None:
    with db_connect() as conn:
        return conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()


def get_build_rewrite_host(build: sqlite3.Row) -> str:
    raw: Any = build["rewrite_host"] if "rewrite_host" in build.keys() else None
    if raw is None:
        return DEFAULT_REWRITE_HOST
    value = str(raw).strip()
    if not value:
        return ""
    return normalize_rewrite_host(value) or DEFAULT_REWRITE_HOST


def list_builds() -> list[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT
                b.*,
                SUM(CASE WHEN c.id IS NOT NULL AND c.parent_id IS NULL THEN 1 ELSE 0 END) AS comment_count,
                SUM(CASE WHEN c.id IS NOT NULL AND c.parent_id IS NULL AND c.resolved = 0 THEN 1 ELSE 0 END) AS open_comment_count,
                SUM(CASE WHEN c.id IS NOT NULL AND c.parent_id IS NULL AND d.id IS NULL THEN 1 ELSE 0 END) AS current_comment_count,
                SUM(CASE WHEN c.id IS NOT NULL AND c.parent_id IS NULL AND c.resolved = 0 AND d.id IS NULL THEN 1 ELSE 0 END) AS current_open_comment_count
            FROM builds b
            LEFT JOIN comments c ON c.build_id = b.id
            LEFT JOIN comments d ON d.carried_from_id = c.id
            GROUP BY b.id
            ORDER BY CASE WHEN b.archived_at IS NULL THEN 0 ELSE 1 END ASC,
                     COALESCE(b.updated_at, b.created_at) DESC,
                     b.id DESC
            """
        ).fetchall()


def list_reviews() -> list[dict[str, Any]]:
    """One entry per review, newest activity first. Each has the latest version's
    row, every version's row, and comment totals across all versions.

    Totals across versions skip a comment that was carried forward, since its
    copy on the newer version already stands in for it.
    """
    rows = list_builds()
    grouped: dict[str, list[Any]] = {}
    order: list[str] = []
    for row in rows:
        key = review_key_of(row)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)
    reviews: list[dict[str, Any]] = []
    for key in order:
        versions = sorted(grouped[key], key=lambda r: (version_of(r), int(r["id"])))
        reviews.append(
            {
                "key": key,
                "latest": versions[-1],
                "versions": versions,
                "total_comment_count": sum(int(r["current_comment_count"] or 0) for r in versions),
                "total_open_comment_count": sum(int(r["current_open_comment_count"] or 0) for r in versions),
            }
        )
    return reviews


def mark_build_archived(build_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute("SELECT id, tag, review_key FROM builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            return False
        now = now_ts()
        # Deleting a review schedules every one of its versions.
        conn.execute(
            """
            UPDATE builds
            SET archived_at = COALESCE(archived_at, ?), updated_at = ?
            WHERE COALESCE(review_key, tag) = ?
            """,
            (now, now, review_key_of(row)),
        )
        conn.commit()
    return True


def restore_archived_build(build_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute("SELECT id, tag, review_key FROM builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            return False
        conn.execute(
            """
            UPDATE builds
            SET archived_at = NULL, updated_at = ?
            WHERE COALESCE(review_key, tag) = ?
            """,
            (now_ts(), review_key_of(row)),
        )
        conn.commit()
    return True


def delete_build_and_comments(build_id: int, delete_storage: bool = True) -> bool:
    with db_connect() as conn:
        row = conn.execute("SELECT tag FROM builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            return False
        tag = str(row["tag"])
        conn.execute("DELETE FROM comments WHERE build_id = ?", (build_id,))
        conn.execute("DELETE FROM page_diffs WHERE build_id = ?", (build_id,))
        conn.execute("DELETE FROM builds WHERE id = ?", (build_id,))
        conn.commit()
    if delete_storage:
        try:
            SITE_STORE.delete_build(tag)
        except Exception:
            # The preview metadata is already removed; best-effort cleanup of storage is sufficient.
            pass
    return True


def delete_review(build_id: int) -> bool:
    """Permanently delete every version of the review this build belongs to."""
    with db_connect() as conn:
        row = conn.execute("SELECT id, tag, review_key FROM builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            return False
        ids = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM builds WHERE COALESCE(review_key, tag) = ?",
                (review_key_of(row),),
            ).fetchall()
        ]
    for version_build_id in ids:
        delete_build_and_comments(version_build_id)
    return True


def purge_expired_archived_builds() -> int:
    cutoff = now_ts() - SOFT_DELETE_GRACE_SECONDS
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id FROM builds WHERE archived_at IS NOT NULL AND archived_at <= ? ORDER BY archived_at ASC",
            (cutoff,),
        ).fetchall()
    purged = 0
    for row in rows:
        if delete_build_and_comments(int(row["id"])):
            purged += 1
    return purged


def row_to_comment_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["resolved"] = bool(item["resolved"])
    selection_raw = item.get("selection_json")
    if selection_raw:
        try:
            item["selection"] = json.loads(str(selection_raw))
        except Exception:
            item["selection"] = None
    else:
        item["selection"] = None
    item.pop("selection_json", None)
    item["created_at_human"] = format_ts(item.get("created_at"))
    item["resolved_at_human"] = format_ts(item.get("resolved_at"))
    item["edited_at_human"] = format_ts(item.get("edited_at")) if item.get("edited_at") else ""
    return item


def normalize_selection_payload(payload: Any) -> dict[str, Any] | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("selection must be an object")

    def parse_path(key: str) -> list[int]:
        value = payload.get(key)
        if not isinstance(value, list):
            raise ValueError(f"{key} must be an array")
        normalized: list[int] = []
        for part in value:
            if isinstance(part, bool) or not isinstance(part, int) or part < 0:
                raise ValueError(f"{key} must only contain non-negative integers")
            normalized.append(part)
        return normalized

    def parse_offset(key: str) -> int:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
        return value

    normalized_selection = {
        "startPath": parse_path("startPath"),
        "startOffset": parse_offset("startOffset"),
        "endPath": parse_path("endPath"),
        "endOffset": parse_offset("endOffset"),
    }
    # Positions are normally relative to the page's <main>. "body" means the selection was
    # outside it, such as in a header or footer, so positions are relative to the whole page.
    root = payload.get("root")
    if root is not None:
        if root != "body":
            raise ValueError("selection root must be body when it is given")
        normalized_selection["root"] = "body"
    return normalized_selection


def fetch_replies(conn: DBConnectionProxy, parent_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not parent_ids:
        return {}
    placeholders = ",".join(["?"] * len(parent_ids))
    rows = conn.execute(
        f"""
        SELECT *
        FROM comments
        WHERE parent_id IN ({placeholders})
        ORDER BY created_at ASC, id ASC
        """,
        parent_ids,
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        item = row_to_comment_dict(row)
        grouped.setdefault(int(item["parent_id"]), []).append(item)
    return grouped


def fetch_feedback(filters: dict[str, Any], sort_key: str = "created_at", sort_dir: str = "desc") -> list[dict[str, Any]]:
    clauses = ["c.parent_id IS NULL"]
    params: list[Any] = []

    build_id = filters.get("build_id")
    if build_id:
        # Any version's id selects the whole review, the version filter narrows it.
        clauses.append(
            "COALESCE(b.review_key, b.tag) = "
            "(SELECT COALESCE(rk.review_key, rk.tag) FROM builds rk WHERE rk.id = ?)"
        )
        params.append(build_id)
    version = filters.get("version", "latest")
    if version == "all":
        # A comment that was carried forward is represented by its copy, so skip the original.
        clauses.append("d.id IS NULL")
    elif isinstance(version, int):
        clauses.append("COALESCE(b.version, 1) = ?")
        params.append(version)
    else:
        clauses.append(f"COALESCE(b.version, 1) = {_LATEST_VERSION_SQL}")
    reviewer = filters.get("reviewer")
    if reviewer:
        clauses.append("LOWER(c.reviewer) LIKE LOWER(?)")
        params.append(f"%{reviewer}%")
    # Open and resolved say whether the comment has been dealt with. Outdated is separate:
    # the comment sits on an earlier version and has no copy on a newer one, whether it is
    # open or resolved. Picking several statuses shows comments matching any of them.
    outdated_sql = f"(d.id IS NULL AND COALESCE(b.version, 1) < {_LATEST_VERSION_SQL})"
    status_sql = {
        "open": "c.resolved = 0",
        "outdated": outdated_sql,
        "resolved": "c.resolved = 1",
    }
    statuses = [name for name in (filters.get("statuses") or []) if name in status_sql]
    if statuses and len(set(statuses)) < len(status_sql):
        clauses.append("(" + " OR ".join(status_sql[name] for name in dict.fromkeys(statuses)) + ")")
    from_ts = filters.get("from_ts")
    if from_ts is not None:
        clauses.append("c.created_at >= ?")
        params.append(from_ts)
    to_ts = filters.get("to_ts")
    if to_ts is not None:
        clauses.append("c.created_at <= ?")
        params.append(to_ts)

    sort_columns = {
        "preview": "b.display_name",
        "publication": "b.display_name",
        "version": "COALESCE(b.version, 1)",
        "page_path": "c.page_path",
        "reviewer": "c.reviewer",
        "created_at": "c.created_at",
        "resolved": "c.resolved",
    }
    order_column = sort_columns.get(sort_key, "c.created_at")
    order_dir = "ASC" if str(sort_dir).lower() == "asc" else "DESC"

    query = f"""
        SELECT
            c.*,
            COALESCE(b.review_key, b.tag) AS build_tag,
            b.display_name AS build_name,
            b.archived_at AS build_archived_at,
            COALESCE(b.version, 1) AS build_version,
            {_LATEST_VERSION_SQL} AS latest_version,
            ob.version AS carried_from_version,
            d.id AS carried_to_id
        FROM comments c
        JOIN builds b ON b.id = c.build_id
        LEFT JOIN comments d ON d.carried_from_id = c.id
        LEFT JOIN comments o ON o.id = c.carried_from_id
        LEFT JOIN builds ob ON ob.id = o.build_id
        WHERE {' AND '.join(clauses)}
        ORDER BY {order_column} {order_dir}, c.id DESC
    """

    with db_connect() as conn:
        rows = conn.execute(query, params).fetchall()
        comments = [row_to_comment_dict(row) for row in rows]
        parent_ids = [comment["id"] for comment in comments]
        replies = fetch_replies(conn, parent_ids)

    for comment in comments:
        comment["replies"] = replies.get(comment["id"], [])
        comment["is_latest_version"] = int(comment["build_version"]) == int(comment["latest_version"])
        # On an earlier version with no copy on a newer one, so it isn't on the latest version.
        comment["outdated"] = (
            comment.get("carried_to_id") is None
            and int(comment["build_version"]) < int(comment["latest_version"])
        )
    return comments


def fetch_comment(comment_id: int) -> dict[str, Any] | None:
    """One comment, or reply, with the details of the preview it belongs to. A comment
    that starts a thread comes with its replies."""
    with db_connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                c.*,
                COALESCE(b.review_key, b.tag) AS build_tag,
                b.display_name AS build_name,
                b.archived_at AS build_archived_at,
                COALESCE(b.version, 1) AS build_version,
                {_LATEST_VERSION_SQL} AS latest_version,
                ob.version AS carried_from_version,
                d.id AS carried_to_id
            FROM comments c
            JOIN builds b ON b.id = c.build_id
            LEFT JOIN comments d ON d.carried_from_id = c.id
            LEFT JOIN comments o ON o.id = c.carried_from_id
            LEFT JOIN builds ob ON ob.id = o.build_id
            WHERE c.id = ?
            """,
            (comment_id,),
        ).fetchone()
        if not row:
            return None
        item = row_to_comment_dict(row)
        is_root = item.get("parent_id") is None
        item["replies"] = fetch_replies(conn, [comment_id]).get(comment_id, []) if is_root else []
    item["is_latest_version"] = int(item["build_version"]) == int(item["latest_version"])
    item["outdated"] = (
        is_root
        and item.get("carried_to_id") is None
        and int(item["build_version"]) < int(item["latest_version"])
    )
    return item


def fetch_page_comments(build_id: int, page_path: str) -> list[dict[str, Any]]:
    with db_connect() as conn:
        roots = conn.execute(
            """
            SELECT c.*, ob.version AS carried_from_version
            FROM comments c
            LEFT JOIN comments o ON o.id = c.carried_from_id
            LEFT JOIN builds ob ON ob.id = o.build_id
            WHERE c.build_id = ? AND c.page_path = ? AND c.parent_id IS NULL
            ORDER BY c.created_at DESC, c.id DESC
            """,
            (build_id, page_path),
        ).fetchall()
        comments = [row_to_comment_dict(row) for row in roots]
        replies = fetch_replies(conn, [comment["id"] for comment in comments])
    for comment in comments:
        comment["replies"] = replies.get(comment["id"], [])
    return comments


def fetch_build_for_export(build_id: int) -> sqlite3.Row | None:
    with db_connect() as conn:
        return conn.execute(
            "SELECT id, tag, review_key, display_name FROM builds WHERE id = ?", (build_id,)
        ).fetchone()


def fetch_build_comments_for_export(build_id: int) -> list[dict[str, Any]]:
    """Every comment on every version of the review this build belongs to."""
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                c.*,
                COALESCE(b.review_key, b.tag) AS build_tag,
                b.display_name AS build_name,
                COALESCE(b.version, 1) AS build_version
            FROM comments c
            JOIN builds b ON b.id = c.build_id
            WHERE COALESCE(b.review_key, b.tag) =
                  (SELECT COALESCE(rk.review_key, rk.tag) FROM builds rk WHERE rk.id = ?)
            ORDER BY COALESCE(b.version, 1) ASC, c.created_at ASC, c.id ASC
            """,
            (build_id,),
        ).fetchall()
    return [row_to_comment_dict(row) for row in rows]


def create_comment(
    build_id: int,
    page_path: str,
    reviewer: str,
    body: str,
    selected_text: str = "",
    line_start: int | None = None,
    line_end: int | None = None,
    selection: dict[str, Any] | None = None,
    parent_id: int | None = None,
    kind: str = "preview",
    diff_start_key: str | None = None,
    diff_end_key: str | None = None,
    context_before: str | None = None,
    context_after: str | None = None,
) -> int:
    if kind not in ("preview", "diff"):
        raise ValueError("kind must be 'preview' or 'diff'")
    with db_connect() as conn:
        if parent_id is not None:
            parent = conn.execute("SELECT * FROM comments WHERE id = ?", (parent_id,)).fetchone()
            if not parent:
                raise ValueError("parent comment not found")
            build_id = int(parent["build_id"])
            page_path = str(parent["page_path"])
            kind = str(parent["kind"]) if "kind" in parent.keys() and parent["kind"] else "preview"
            diff_start_key = parent["diff_start_key"] if "diff_start_key" in parent.keys() else None
            diff_end_key = parent["diff_end_key"] if "diff_end_key" in parent.keys() else None
            parent_selection_raw = parent["selection_json"] if "selection_json" in parent.keys() else None
            if selection is None and parent_selection_raw:
                try:
                    selection = json.loads(str(parent_selection_raw))
                except Exception:
                    selection = None
        build = conn.execute(
            "SELECT id, tag, review_key, version, archived_at FROM builds WHERE id = ?", (build_id,)
        ).fetchone()
        if not build:
            raise ValueError("preview not found")
        build_archived_at = build["archived_at"] if "archived_at" in build.keys() else None
        if build_archived_at is not None:
            raise ValueError("preview is scheduled for deletion and is read-only")
        if parent_id is None:
            latest_version = conn.execute(
                "SELECT MAX(COALESCE(version, 1)) AS v FROM builds WHERE COALESCE(review_key, tag) = ?",
                (review_key_of(build),),
            ).fetchone()
            if latest_version and version_of(build) < int(latest_version["v"] or 1):
                raise ValueError(
                    "this is an older version, so new comments can't be added. "
                    "Reply to an existing comment or comment on the latest version"
                )
        selection_json = json.dumps(selection, separators=(",", ":")) if selection else None
        values = (
            build_id,
            page_path,
            line_start,
            line_end,
            selected_text,
            selection_json,
            body,
            reviewer,
            parent_id,
            kind,
            diff_start_key,
            diff_end_key,
            clean_context(context_before),
            clean_context(context_after),
            now_ts(),
        )
        if DB_BACKEND == "postgres":
            inserted = conn.execute(
                """
                INSERT INTO comments (
                    build_id, page_path, line_start, line_end, selected_text, selection_json, body, reviewer,
                    parent_id, kind, diff_start_key, diff_end_key, context_before, context_after, resolved, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                RETURNING id
                """,
                values,
            ).fetchone()
            if not inserted:
                raise ValueError("failed to create comment")
            comment_id = int(inserted["id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO comments (
                    build_id, page_path, line_start, line_end, selected_text, selection_json, body, reviewer,
                    parent_id, kind, diff_start_key, diff_end_key, context_before, context_after, resolved, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                values,
            )
            comment_id = int(cur.lastrowid)
        conn.commit()
        return comment_id


def set_comment_resolved(comment_id: int, resolved: bool) -> None:
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT b.archived_at
            FROM comments c
            JOIN builds b ON b.id = c.build_id
            WHERE c.id = ?
            """,
            (comment_id,),
        ).fetchone()
        if not row:
            raise ValueError("comment not found")
        archived_at = row["archived_at"] if "archived_at" in row.keys() else None
        if archived_at is not None:
            raise ValueError("preview is scheduled for deletion and is read-only")
        conn.execute(
            """
            UPDATE comments
            SET resolved = ?, resolved_at = ?
            WHERE id = ?
            """,
            (1 if resolved else 0, now_ts() if resolved else None, comment_id),
        )
        conn.commit()


def _writable_comment_row(conn: DBConnectionProxy, comment_id: int) -> Any:
    row = conn.execute(
        """
        SELECT c.id, b.archived_at
        FROM comments c
        JOIN builds b ON b.id = c.build_id
        WHERE c.id = ?
        """,
        (comment_id,),
    ).fetchone()
    if not row:
        raise ValueError("comment not found")
    archived_at = row["archived_at"] if "archived_at" in row.keys() else None
    if archived_at is not None:
        raise ValueError("preview is scheduled for deletion and is read-only")
    return row


def update_comment_body(comment_id: int, body: str) -> None:
    body = body.strip()
    if not body:
        raise ValueError("body is required")
    with db_connect() as conn:
        _writable_comment_row(conn, comment_id)
        conn.execute(
            "UPDATE comments SET body = ?, edited_at = ? WHERE id = ?",
            (body, now_ts(), comment_id),
        )
        conn.commit()


def delete_comment(comment_id: int) -> None:
    """Delete a comment. Deleting a top-level comment also deletes its replies."""
    with db_connect() as conn:
        _writable_comment_row(conn, comment_id)
        conn.execute("DELETE FROM comments WHERE parent_id = ?", (comment_id,))
        conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        conn.commit()
