from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import threading
import time
from http import HTTPStatus
from pathlib import Path

_log = logging.getLogger(__name__)


def _stage_callback(tag: str, stage: str) -> None:
    import_status.update(tag, stage)
    _log.info("import %r: %s", tag, stage)


def _run_import(
    archive_path: Path,
    staging_dir: Path | None,
    tag: str,
    display_name: str,
    source_ref: str | None,
    rewrite_host: str,
) -> None:
    import_status.update(tag, "Starting import")
    _log.info("import started for %r (archive: %s, size: %d bytes)", tag, archive_path, archive_path.stat().st_size)
    try:
        result = import_build_archive_path(
            archive_path=archive_path,
            tag=tag,
            display_name=display_name,
            source_ref=source_ref,
            rewrite_host=rewrite_host,
            stage_callback=lambda stage: _stage_callback(tag, stage),
        )
        _log.info("import complete for %r (build_id: %d)", tag, result.build_id)
        import_status.complete(tag)
    except Exception as exc:
        _log.error("import failed for %r: %s", tag, exc, exc_info=True)
        import_status.fail(tag, str(exc))
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        else:
            archive_path.unlink(missing_ok=True)

import html as _html

from docs_review_portal import import_status, log_buffer
from docs_review_portal.config import (
    DEFAULT_REVIEWER,
    DEFAULT_REWRITE_HOST,
    DISABLE_REWRITE_HOST_VALUES,
    normalize_rewrite_host,
)
from docs_review_portal.data import (
    count_outdated_comments,
    create_comment,
    delete_comment,
    fetch_page_comments,
    fetch_page_diff,
    get_build_by_id,
    get_build_rewrite_host,
    get_latest_build_for_key,
    import_build_archive_path,
    list_builds,
    review_key_of,
    normalize_selection_payload,
    set_comment_resolved,
    update_build_changed_pages,
    update_comment_body,
)
from docs_review_portal.helpers import (
    build_url,
    canonical_page_path,
    compute_delete_at,
    format_ts,
    review_url,
    slugify_tag,
)



class ReviewApiMixin:
    def _api_get_builds(self) -> None:
        rows = list_builds()
        latest_versions: dict[str, int] = {}
        for row in rows:
            key = review_key_of(row)
            latest_versions[key] = max(latest_versions.get(key, 0), int(row["version"] or 1))
        payload = {
            "builds": [
                {
                    "id": int(row["id"]),
                    "image_ref": row["image_ref"],
                    "tag": row["tag"],
                    "review_key": review_key_of(row),
                    "version": int(row["version"] or 1),
                    "is_latest": int(row["version"] or 1) == latest_versions[review_key_of(row)],
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
                    "site_url": review_url(
                        review_key_of(row),
                        int(row["version"] or 1),
                        int(row["version"] or 1) == latest_versions[review_key_of(row)],
                        base=self._public_base(),
                    ),
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

            if chunk_idx == 0:
                import_status.start(tag, display_name, f"Uploading (1/{total_chunks})")
            else:
                import_status.update(tag, f"Uploading ({chunk_idx + 1}/{total_chunks})")

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

            _log.info("chunk %d/%d received for %r (%d bytes)", chunk_idx + 1, total_chunks, tag, dest.stat().st_size)
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
            import_status.start(tag, display_name, "Uploading")
            try:
                archive_path = self._read_body_to_temp_file()
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

        source_ref = (query.get("source_ref", [""])[0] or "").strip() or None

        # Run import in a background thread so health checks stay responsive, but
        # keep this request connection open with streaming progress so Cloud Run
        # allocates full CPU for the duration.
        t = threading.Thread(
            target=_run_import,
            args=(archive_path, staging_dir, tag, display_name, source_ref, rewrite_host),
            daemon=True,
        )
        t.start()

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        while t.is_alive():
            entries = import_status.get_all()
            stage = next((e["stage"] for e in entries if e["tag"] == tag), "working")
            self._write_chunk(f"status: {stage}\n".encode())
            t.join(timeout=2)

        failed = import_status.get_failed(tag)
        if failed:
            self._write_chunk(f"error: {failed['error']}\n".encode())
        else:
            imported = get_latest_build_for_key(tag)
            result = json.dumps({
                "tag": tag,
                "name": display_name,
                "version": int(imported["version"] or 1) if imported else 1,
                "rewrite_host": rewrite_host or None,
                "site_url": build_url(tag, base=self._public_base()),
            })
            self._write_chunk(f"result: {result}\n".encode())
        self._write_chunk(b"")

    def _api_get_comments(self, query: dict[str, list[str]]) -> None:
        build_raw = query.get("build_id", [""])[0]
        page_path = query.get("page_path", [""])[0] or "/"
        if not build_raw.isdigit():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "build_id is required"})
            return
        build_id = int(build_raw)
        canonical = canonical_page_path(page_path)
        comments = fetch_page_comments(build_id, canonical)
        build = get_build_by_id(build_id)
        # Outdated comments on this page, and on the whole review (other pages included).
        outdated_page_count = count_outdated_comments(build, canonical) if build else 0
        outdated_count = count_outdated_comments(build) if build else 0
        self._send_json(
            HTTPStatus.OK,
            {
                "comments": comments,
                "outdated_count": outdated_count,
                "outdated_page_count": outdated_page_count,
            },
        )

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
            kind = str(payload.get("kind") or "preview")
            diff_start_key = payload.get("diff_start_key")
            diff_end_key = payload.get("diff_end_key")
            context_before = payload.get("context_before")
            context_after = payload.get("context_after")
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
                kind=kind,
                diff_start_key=str(diff_start_key) if diff_start_key else None,
                diff_end_key=str(diff_end_key) if diff_end_key else None,
                context_before=context_before,
                context_after=context_after,
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

    def _render_logs_page(self, query: dict[str, list[str]] | None = None) -> None:
        from urllib.parse import urlencode

        from docs_review_portal.config import LOG_RETENTION_DAYS
        from docs_review_portal.helpers import html_page

        query = query or {}
        page_size = 200
        level = (query.get("level", ["all"])[0] or "all").lower()
        if level not in ("all", "warning", "error"):
            level = "all"
        search = (query.get("q", [""])[0] or "").strip()
        instance = (query.get("instance", [""])[0] or "").strip()
        page_raw = query.get("page", ["1"])[0]
        page_number = int(page_raw) if page_raw.isdigit() and int(page_raw) > 0 else 1

        notice = ""
        instances: list[str] = []
        has_older = False
        try:
            entries, has_older = log_buffer.query_entries(
                level=level, search=search, instance=instance,
                limit=page_size, offset=(page_number - 1) * page_size,
            )
            instances = log_buffer.list_instances()
        except Exception:
            # The saved logs can't be read right now, so show what this process remembers.
            notice = (
                '<p class="subtle archive-note">Saved logs are unavailable, so this shows only '
                "recent entries from this instance.</p>"
            )
            entries = list(reversed(log_buffer.get_entries()))
            if level != "all":
                wanted = ("WARNING", "ERROR", "CRITICAL") if level == "warning" else ("ERROR", "CRITICAL")
                entries = [e for e in entries if e["level"] in wanted]
            if search:
                needle = search.lower()
                entries = [e for e in entries if needle in e["msg"].lower() or needle in e["logger"].lower()]
            page_number = 1

        level_class = {"CRITICAL": "archived", "ERROR": "archived", "WARNING": "open", "INFO": "resolved"}
        rows = "".join(
            f"<tr>"
            f"<td>{_html.escape(e['ts_human'])}</td>"
            f"<td><span class=\"status {level_class.get(e['level'], '')}\">{_html.escape(e['level'])}</span></td>"
            f"<td><span class=\"subtle\">{_html.escape(e.get('instance') or '')}</span></td>"
            f"<td>{_html.escape(e['logger'])}</td>"
            f"<td><pre class=\"log-message\">{_html.escape(e['msg'])}</pre></td>"
            f"</tr>"
            for e in entries
        )

        def page_link(number: int, label: str) -> str:
            params = {"level": level, "q": search, "instance": instance, "page": str(number)}
            params = {k: v for k, v in params.items() if v and not (k == "level" and v == "all")}
            return f'<a href="/logs?{_html.escape(urlencode(params))}">{label}</a>'

        pager = []
        if page_number > 1:
            pager.append(page_link(page_number - 1, "&larr; Newer"))
        if has_older:
            pager.append(page_link(page_number + 1, "Older &rarr;"))
        pager_markup = f'<p class="log-pager">{" ".join(pager)}</p>' if pager else ""

        instance_options = ['<option value="">All instances</option>']
        for name in instances + ([instance] if instance and instance not in instances else []):
            selected = " selected" if name == instance else ""
            current = " (this instance)" if name == log_buffer.INSTANCE_ID else ""
            instance_options.append(
                f'<option value="{_html.escape(name)}"{selected}>{_html.escape(name)}{current}</option>'
            )
        level_options = "".join(
            f'<option value="{value}"{" selected" if level == value else ""}>{label}</option>'
            for value, label in (("all", "All levels"), ("warning", "Warnings and errors"), ("error", "Errors only"))
        )

        body = f"""
        <section class="panel">
          <h1>System logs</h1>
          <p class="subtle">Logs are saved for {LOG_RETENTION_DAYS} days and survive restarts.
             This instance is <code>{_html.escape(log_buffer.INSTANCE_ID)}</code>.</p>
          {notice}
          <form class="filter-form" method="get" action="/logs">
            <label>Level
              <select name="level">{level_options}</select>
            </label>
            <label>Instance
              <select name="instance">{''.join(instance_options)}</select>
            </label>
            <label>Search
              <input type="text" name="q" value="{_html.escape(search)}" placeholder="Message or logger">
            </label>
            <button type="submit">Apply</button>
          </form>
        </section>
        <section class="panel">
          <table>
            <thead>
              <tr>
                <th>Time (UTC)</th>
                <th>Level</th>
                <th>Instance</th>
                <th>Logger</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {rows or '<tr><td colspan="5">No log entries match.</td></tr>'}
            </tbody>
          </table>
          {pager_markup}
        </section>"""
        self._send_html(HTTPStatus.OK, html_page("System logs", body))

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

    def _api_edit_comment(self, path: str) -> None:
        match = re.match(r"^/api/comments/(\d+)/edit$", path)
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Comment not found"})
            return
        payload = self._read_json()
        body = str(payload.get("body", "")).strip()
        if not body:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "body is required"})
            return
        try:
            update_comment_body(int(match.group(1)), body)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def _api_delete_comment(self, path: str) -> None:
        match = re.match(r"^/api/comments/(\d+)/delete$", path)
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Comment not found"})
            return
        try:
            delete_comment(int(match.group(1)))
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def _api_update_changed_pages(self, path: str) -> None:
        match = re.match(r"^/api/builds/(\d+)/changed-pages$", path)
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        build_id = int(match.group(1))
        if not get_build_by_id(build_id):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Preview not found"})
            return
        payload = self._read_json()
        pages = payload.get("pages")
        if not isinstance(pages, list):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "pages must be an array"})
            return
        if len(pages) > 5000:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "too many pages"})
            return
        if not all(isinstance(p, str) for p in pages):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "each page must be a string"})
            return
        update_build_changed_pages(build_id, pages)
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _api_get_page_diff(self, path: str, query: dict[str, list[str]]) -> None:
        match = re.match(r"^/api/builds/(\d+)/page-diff$", path)
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        build_id = int(match.group(1))
        page_path = query.get("page", [""])[0] or "/"
        diff_content = fetch_page_diff(build_id, page_path)
        if diff_content is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Diff not found for this page"})
            return
        data = diff_content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

