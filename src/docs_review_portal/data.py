from __future__ import annotations

import json
import logging as _logging
import mimetypes
import re
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
    rewrite_docs_domain_urls,
    slugify_tag,
)
class DBConnectionProxy:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def _prepare_sql(self, statement: str) -> str:
        if DB_BACKEND == "postgres":
            return statement.replace("?", "%s")
        return statement

    def execute(self, statement: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        return self._conn.execute(self._prepare_sql(statement), params)

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


class SiteStore:
    def init_storage(self) -> None:
        raise NotImplementedError

    def begin_replace(self, tag: str) -> bool:
        raise NotImplementedError

    def rollback_replace(self, tag: str, had_existing_site: bool) -> None:
        raise NotImplementedError

    def replace_site_from_archive(self, archive_path: Path, tag: str, progress_callback: Callable[[int, int], None] | None = None) -> None:
        raise NotImplementedError

    def read_file(self, tag: str, rel_path: str) -> bytes | None:
        raise NotImplementedError

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

    def replace_site_from_archive(self, archive_path: Path, tag: str, progress_callback: Callable[[int, int], None] | None = None) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import logging
        import threading

        _log = logging.getLogger(__name__)
        dest = self._site_dir(tag, "site")
        dest.mkdir(parents=True, exist_ok=True)

        # Count files first (fast — no extraction)
        total = sum(1 for m in tarfile.open(archive_path, mode="r:*").getmembers() if m.isreg())
        _log.info("replace_site_from_archive %r: writing %d files in parallel to %s", tag, total, dest)

        completed = [0]
        counter_lock = threading.Lock()
        # Semaphore limits how many file payloads are in memory simultaneously.
        sem = threading.Semaphore(64)

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

    def replace_site_from_archive(self, archive_path: Path, tag: str, progress_callback: Callable[[int, int], None] | None = None) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        site_prefix = self._snapshot_prefix(tag, "site")

        uploads: list[tuple[str, bytes, str]] = []
        with tarfile.open(archive_path, mode="r:*") as archive:
            for member in archive.getmembers():
                if not member.isreg():
                    continue
                rel = Path(member.name)
                parts = [part for part in rel.parts if part not in ("", ".")]
                if not parts or ".." in parts:
                    continue
                rel_str = Path(*parts).as_posix()
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

    def read_file(self, tag: str, rel_path: str) -> bytes | None:
        blob = self._bucket.blob(f"{self._snapshot_prefix(tag, 'site')}{rel_path}")
        try:
            return blob.download_as_bytes()
        except self._not_found:
            return None

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

    def replace_site_from_archive(self, archive_path: Path, tag: str, progress_callback: Callable[[int, int], None] | None = None) -> None:
        dest = self._backing / tag / "site"
        fuse = _parse_fuse_mount(self._backing)
        if fuse:
            bucket, mount_point = fuse
            gcs_prefix = dest.relative_to(mount_point).as_posix()
            self._log.info("uploading %r to gs://%s/%s/ via GCS SDK...", tag, bucket, gcs_prefix)
            _upload_archive_to_gcs(archive_path, bucket, gcs_prefix, self._log)
        else:
            self._log.info("extracting %r to local backing store...", tag)
            extract_site_archive(archive_path, dest)
            self._log.info("extracted %r to local backing store", tag)

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
                """
            )
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(builds)").fetchall()}
            if "updated_at" not in columns:
                conn.execute("ALTER TABLE builds ADD COLUMN updated_at INTEGER")
            if "rewrite_host" not in columns:
                conn.execute("ALTER TABLE builds ADD COLUMN rewrite_host TEXT")
            comment_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(comments)").fetchall()}
            if "selection_json" not in comment_columns:
                conn.execute("ALTER TABLE comments ADD COLUMN selection_json TEXT")
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
                ALTER TABLE comments ADD COLUMN IF NOT EXISTS selection_json TEXT;
                """
            )

        conn.execute("UPDATE builds SET updated_at = created_at WHERE updated_at IS NULL")
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
) -> None:
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


def extract_site_archive(archive_path: Path, destination: Path) -> None:
    import time as _time
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    # r|gz streaming mode: single decompression pass (r:gz decompresses twice —
    # once for getmembers(), again for each extractfile() call).
    with tarfile.open(archive_path, mode="r|gz") as archive:
        for i, member in enumerate(archive):
            rel = Path(member.name)
            parts = [part for part in rel.parts if part not in ("", ".")]
            if not parts or ".." in parts:
                continue
            rel = Path(*parts)
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


def import_build_archive_path(
    archive_path: Path,
    tag: str,
    display_name: str | None = None,
    source_ref: str | None = None,
    rewrite_host: str = DEFAULT_REWRITE_HOST,
    stage_callback: Callable[[str], None] | None = None,
) -> ImportedBuild:
    resolved_tag = slugify_tag(tag)
    resolved_name = (display_name or resolved_tag).strip() or resolved_tag
    site_dir = BUILDS_DIR / resolved_tag / "site"
    stored_rewrite_host = ""
    if rewrite_host:
        stored_rewrite_host = normalize_rewrite_host(rewrite_host) or DEFAULT_REWRITE_HOST
    had_existing_site = SITE_STORE.begin_replace(resolved_tag)

    if stage_callback:
        stage_callback("Extracting preview files")

    def _progress(n: int, total: int) -> None:
        if stage_callback:
            stage_callback(f"Importing file {n} of {total}")

    try:
        SITE_STORE.replace_site_from_archive(archive_path, resolved_tag, progress_callback=_progress if stage_callback else None)
    except Exception:
        SITE_STORE.rollback_replace(resolved_tag, had_existing_site)
        raise

    now = now_ts()
    if stage_callback:
        stage_callback("Updating preview metadata")
    with db_connect() as conn:
        existing = conn.execute("SELECT id FROM builds WHERE tag = ?", (resolved_tag,)).fetchone()
        image_ref = source_ref or f"upload:{resolved_tag}"
        if existing:
            build_id = int(existing["id"])
            conn.execute(
                """
                UPDATE builds
                SET image_ref = ?, display_name = ?, rewrite_host = ?, archived_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (image_ref, resolved_name, stored_rewrite_host, now, build_id),
            )
        else:
            if DB_BACKEND == "postgres":
                inserted = conn.execute(
                    """
                    INSERT INTO builds (image_ref, tag, display_name, rewrite_host, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    (image_ref, resolved_tag, resolved_name, stored_rewrite_host, now, now),
                ).fetchone()
                if not inserted:
                    raise ValueError("failed to create preview metadata")
                build_id = int(inserted["id"])
            else:
                cur = conn.execute(
                    """
                    INSERT INTO builds (image_ref, tag, display_name, rewrite_host, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (image_ref, resolved_tag, resolved_name, stored_rewrite_host, now, now),
                )
                build_id = int(cur.lastrowid)
        conn.commit()

    return ImportedBuild(
        build_id=build_id,
        tag=resolved_tag,
        image_ref=image_ref,
        display_name=resolved_name,
        extracted_to=site_dir,
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
                SUM(CASE WHEN c.id IS NOT NULL AND c.parent_id IS NULL AND c.resolved = 0 THEN 1 ELSE 0 END) AS open_comment_count
            FROM builds b
            LEFT JOIN comments c ON c.build_id = b.id
            GROUP BY b.id
            ORDER BY CASE WHEN b.archived_at IS NULL THEN 0 ELSE 1 END ASC,
                     COALESCE(b.updated_at, b.created_at) DESC,
                     b.id DESC
            """
        ).fetchall()


def mark_build_archived(build_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute("SELECT id, archived_at FROM builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            return False
        now = now_ts()
        conn.execute(
            """
            UPDATE builds
            SET archived_at = COALESCE(archived_at, ?), updated_at = ?
            WHERE id = ?
            """,
            (now, now, build_id),
        )
        conn.commit()
    return True


def restore_archived_build(build_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute("SELECT id FROM builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            return False
        conn.execute(
            """
            UPDATE builds
            SET archived_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (now_ts(), build_id),
        )
        conn.commit()
    return True


def delete_build_and_comments(build_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute("SELECT tag FROM builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            return False
        tag = str(row["tag"])
        conn.execute("DELETE FROM comments WHERE build_id = ?", (build_id,))
        conn.execute("DELETE FROM builds WHERE id = ?", (build_id,))
        conn.commit()
    try:
        SITE_STORE.delete_build(tag)
    except Exception:
        # The preview metadata is already removed; best-effort cleanup of storage is sufficient.
        pass
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

    return {
        "startPath": parse_path("startPath"),
        "startOffset": parse_offset("startOffset"),
        "endPath": parse_path("endPath"),
        "endOffset": parse_offset("endOffset"),
    }


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
        clauses.append("c.build_id = ?")
        params.append(build_id)
    reviewer = filters.get("reviewer")
    if reviewer:
        clauses.append("LOWER(c.reviewer) LIKE LOWER(?)")
        params.append(f"%{reviewer}%")
    resolved = filters.get("resolved")
    if resolved == "resolved":
        clauses.append("c.resolved = 1")
    elif resolved == "unresolved":
        clauses.append("c.resolved = 0")
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
            b.tag AS build_tag,
            b.display_name AS build_name,
            b.archived_at AS build_archived_at
        FROM comments c
        JOIN builds b ON b.id = c.build_id
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
    return comments


def fetch_page_comments(build_id: int, page_path: str) -> list[dict[str, Any]]:
    with db_connect() as conn:
        roots = conn.execute(
            """
            SELECT *
            FROM comments
            WHERE build_id = ? AND page_path = ? AND parent_id IS NULL
            ORDER BY created_at DESC, id DESC
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
        return conn.execute("SELECT id, tag, display_name FROM builds WHERE id = ?", (build_id,)).fetchone()


def fetch_build_comments_for_export(build_id: int) -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                c.*,
                b.tag AS build_tag,
                b.display_name AS build_name
            FROM comments c
            JOIN builds b ON b.id = c.build_id
            WHERE c.build_id = ?
            ORDER BY c.created_at ASC, c.id ASC
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
) -> int:
    with db_connect() as conn:
        if parent_id is not None:
            parent = conn.execute("SELECT * FROM comments WHERE id = ?", (parent_id,)).fetchone()
            if not parent:
                raise ValueError("parent comment not found")
            build_id = int(parent["build_id"])
            page_path = str(parent["page_path"])
            parent_selection_raw = parent["selection_json"] if "selection_json" in parent.keys() else None
            if selection is None and parent_selection_raw:
                try:
                    selection = json.loads(str(parent_selection_raw))
                except Exception:
                    selection = None
        build = conn.execute("SELECT id, archived_at FROM builds WHERE id = ?", (build_id,)).fetchone()
        if not build:
            raise ValueError("preview not found")
        build_archived_at = build["archived_at"] if "archived_at" in build.keys() else None
        if build_archived_at is not None:
            raise ValueError("preview is scheduled for deletion and is read-only")
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
            now_ts(),
        )
        if DB_BACKEND == "postgres":
            inserted = conn.execute(
                """
                INSERT INTO comments (
                    build_id, page_path, line_start, line_end, selected_text, selection_json, body, reviewer,
                    parent_id, resolved, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
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
                    parent_id, resolved, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
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


