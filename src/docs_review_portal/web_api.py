from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
from http import HTTPStatus
from pathlib import Path

_log = logging.getLogger(__name__)


def _run_import(
    archive_path: Path,
    staging_dir: Path | None,
    tag: str,
    display_name: str,
    source_ref: str | None,
    rewrite_host: str,
) -> None:
    try:
        import_build_archive_path(
            archive_path=archive_path,
            tag=tag,
            display_name=display_name,
            source_ref=source_ref,
            rewrite_host=rewrite_host,
        )
    except Exception as exc:
        _log.error("import failed for %r: %s", tag, exc)
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        else:
            archive_path.unlink(missing_ok=True)

from docs_review_portal.config import (
    DEFAULT_REVIEWER,
    DEFAULT_REWRITE_HOST,
    DISABLE_REWRITE_HOST_VALUES,
    normalize_rewrite_host,
)
from docs_review_portal.data import (
    create_comment,
    fetch_page_comments,
    get_build_rewrite_host,
    import_build_archive_path,
    list_builds,
    normalize_selection_payload,
    set_comment_resolved,
)
from docs_review_portal.helpers import (
    build_url,
    canonical_page_path,
    compute_delete_at,
    format_ts,
    slugify_tag,
)


class ReviewApiMixin:
    def _api_get_builds(self) -> None:
        rows = list_builds()
        payload = {
            "builds": [
                {
                    "id": int(row["id"]),
                    "image_ref": row["image_ref"],
                    "tag": row["tag"],
                    "display_name": row["display_name"],
                    "created_at": int(row["created_at"]),
                    "created_at_human": format_ts(int(row["created_at"])),
                    "updated_at": int(row["updated_at"] or row["created_at"]),
                    "updated_at_human": format_ts(int(row["updated_at"] or row["created_at"])),
                    "comment_count": int(row["comment_count"] or 0),
                    "open_comment_count": int(row["open_comment_count"] or 0),
                    "rewrite_host": get_build_rewrite_host(row) or None,
                    "archived_at": int(row["archived_at"]) if row["archived_at"] is not None else None,
                    "delete_after": (
                        compute_delete_at(int(row["archived_at"]))
                        if row["archived_at"] is not None
                        else None
                    ),
                    "site_url": build_url(str(row["tag"])),
                }
                for row in rows
            ]
        }
        self._send_json(HTTPStatus.OK, payload)

    def _api_upload_build_archive(self, query: dict[str, list[str]]) -> None:
        name = (query.get("name", [""])[0] or "").strip()
        if not name:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "name query parameter is required"},
            )
            return
        try:
            tag = slugify_tag(name)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        display_name = name
        rewrite_host_raw = (query.get("rewrite_host", [""])[0] or "").strip()
        if not rewrite_host_raw:
            rewrite_host = DEFAULT_REWRITE_HOST
        elif rewrite_host_raw.lower() in DISABLE_REWRITE_HOST_VALUES:
            rewrite_host = ""
        else:
            rewrite_host = normalize_rewrite_host(rewrite_host_raw) or ""
            if not rewrite_host:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "rewrite_host must be a hostname, URL, or one of: off, none"},
                )
                return

        chunk_str = (query.get("chunk", [""])[0] or "").strip()
        chunks_str = (query.get("chunks", [""])[0] or "").strip()
        staging_dir: Path | None = None

        if chunk_str or chunks_str:
            try:
                chunk_idx = int(chunk_str)
                total_chunks = int(chunks_str)
                if total_chunks < 1 or chunk_idx < 0 or chunk_idx >= total_chunks:
                    raise ValueError("chunk index out of range")
            except (ValueError, TypeError):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "chunk and chunks must be integers with 0 <= chunk < chunks"},
                )
                return

            staging_dir = Path(tempfile.gettempdir()) / f"review-upload-{tag}"
            staging_dir.mkdir(exist_ok=True)

            try:
                chunk_path = self._read_body_to_temp_file()
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            dest = staging_dir / f"{chunk_idx:06d}.bin"
            try:
                chunk_path.rename(dest)
            except Exception:
                chunk_path.unlink(missing_ok=True)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "failed to store chunk"})
                return

            if chunk_idx < total_chunks - 1:
                self._send_json(HTTPStatus.OK, {"status": "chunk received", "chunk": chunk_idx, "chunks": total_chunks})
                return

            missing = [i for i in range(total_chunks) if not (staging_dir / f"{i:06d}.bin").exists()]
            if missing:
                shutil.rmtree(staging_dir, ignore_errors=True)
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"missing chunks: {missing[:5]}"})
                return

            archive_path = staging_dir / "assembled.tar.gz"
            try:
                with archive_path.open("wb") as out:
                    for i in range(total_chunks):
                        chunk_file = staging_dir / f"{i:06d}.bin"
                        with chunk_file.open("rb") as src:
                            shutil.copyfileobj(src, out)
                        chunk_file.unlink()
            except Exception as exc:
                shutil.rmtree(staging_dir, ignore_errors=True)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"failed to assemble chunks: {exc}"})
                return
        else:
            try:
                archive_path = self._read_body_to_temp_file()
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

        source_ref = (query.get("source_ref", [""])[0] or "").strip() or None

        threading.Thread(
            target=_run_import,
            args=(archive_path, staging_dir, tag, display_name, source_ref, rewrite_host),
            daemon=True,
        ).start()

        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "tag": tag,
                "name": display_name,
                "rewrite_host": rewrite_host or None,
                "site_url": build_url(tag),
            },
        )

    def _api_get_comments(self, query: dict[str, list[str]]) -> None:
        build_raw = query.get("build_id", [""])[0]
        page_path = query.get("page_path", [""])[0] or "/"
        if not build_raw.isdigit():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "build_id is required"})
            return
        comments = fetch_page_comments(int(build_raw), canonical_page_path(page_path))
        self._send_json(HTTPStatus.OK, {"comments": comments})

    def _api_create_comment(self) -> None:
        payload = self._read_json()
        try:
            build_id = int(payload.get("build_id"))
            reviewer = str(payload.get("reviewer", DEFAULT_REVIEWER)).strip() or DEFAULT_REVIEWER
            body = str(payload.get("body", "")).strip()
            if not body:
                raise ValueError("body is required")
            page_path = canonical_page_path(str(payload.get("page_path", "/")))
            selected_text = str(payload.get("selected_text", "")).strip()
            selection = normalize_selection_payload(payload.get("selection"))
            line_start = payload.get("line_start")
            line_end = payload.get("line_end")
            parent_id = payload.get("parent_id")
            if line_start is not None:
                line_start = int(line_start)
            if line_end is not None:
                line_end = int(line_end)
            if parent_id is not None:
                parent_id = int(parent_id)
            comment_id = create_comment(
                build_id=build_id,
                page_path=page_path,
                reviewer=reviewer,
                body=body,
                selected_text=selected_text,
                line_start=line_start,
                line_end=line_end,
                selection=selection,
                parent_id=parent_id,
            )
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.CREATED, {"comment_id": comment_id})

    def _api_resolve_comment(self, path: str) -> None:
        match = re.match(r"^/api/comments/(\d+)/resolve$", path)
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Comment not found"})
            return
        payload = self._read_json()
        resolved = bool(payload.get("resolved", True))
        try:
            set_comment_resolved(int(match.group(1)), resolved)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok", "resolved": resolved})

    def _api_reply_comment(self, path: str) -> None:
        match = re.match(r"^/api/comments/(\d+)/reply$", path)
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Comment not found"})
            return
        payload = self._read_json()
        reviewer = str(payload.get("reviewer", DEFAULT_REVIEWER)).strip() or DEFAULT_REVIEWER
        body = str(payload.get("body", "")).strip()
        if not body:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "body is required"})
            return
        try:
            comment_id = create_comment(
                build_id=0,
                page_path="/",
                reviewer=reviewer,
                body=body,
                parent_id=int(match.group(1)),
            )
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.CREATED, {"comment_id": comment_id})

