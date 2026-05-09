from __future__ import annotations

import csv
import html
import io
import re
from http import HTTPStatus
from urllib.parse import urlencode, urlparse

from docs_review_portal.config import DEFAULT_REVIEWER
import time

from docs_review_portal import import_status
from docs_review_portal.data import (
    create_comment,
    delete_build_and_comments,
    fetch_build_comments_for_export,
    fetch_build_for_export,
    fetch_feedback,
    list_builds,
    mark_build_archived,
    restore_archived_build,
    set_comment_resolved,
)
from docs_review_portal.helpers import (
    build_path,
    build_url,
    compute_delete_at,
    format_ts,
    html_page,
    parse_local_datetime_to_ts,
    slugify_tag,
)


class ReviewPagesMixin:
    def _render_previews_page(self) -> None:
        rows = list_builds()
        build_rows = []
        for row in rows:
            build_id = int(row["id"])
            tag_raw = str(row["tag"])
            tag = html.escape(tag_raw)
            name_raw = (str(row["display_name"]) if row["display_name"] is not None else "").strip() or tag_raw
            name = html.escape(name_raw)
            preview_display = f"<strong>{name}</strong>"
            if name_raw != tag_raw:
                preview_display = f"{preview_display}<br><span class=\"subtle\">{tag}</span>"
            archived_raw = row["archived_at"] if "archived_at" in row.keys() else None
            archived_at = int(archived_raw) if archived_raw is not None else None
            delete_at = compute_delete_at(archived_at)
            if archived_at is not None and delete_at is not None:
                preview_display = (
                    f"{preview_display}<br>"
                    f"<span class=\"subtle archive-note\">Pending deletion on {html.escape(format_ts(delete_at))}</span>"
                )
            comments = int(row["comment_count"] or 0)
            open_comments = int(row["open_comment_count"] or 0)
            updated_at = int(row["updated_at"] or row["created_at"])
            action_path = f"/previews/{build_id}/delete"
            restore_path = f"/previews/{build_id}/restore"
            row_class = " class=\"is-archived\"" if archived_at is not None else ""
            destroy_path = f"/previews/{build_id}/destroy"
            if archived_at is not None:
                action_form = f"""
                    <form method="post" action="{restore_path}" onsubmit="return confirm('Restore this preview and keep it active?');">
                      <button type="submit" class="icon-action" title="Restore preview" aria-label="Restore preview">
                        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                          <path d="M3 12a9 9 0 1 0 3-6.7"></path>
                          <path d="M3 3v6h6"></path>
                        </svg>
                      </button>
                    </form>
                    <form method="post" action="{destroy_path}" onsubmit="return confirm('Permanently delete this preview and all comments right now? This cannot be undone.');">
                      <button type="submit" class="icon-action icon-danger" title="Delete immediately" aria-label="Delete immediately">
                        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                          <path d="M3 6h18"></path>
                          <path d="M8 6V4h8v2"></path>
                          <path d="M7 6l1 14h8l1-14"></path>
                          <path d="M10 11v6"></path>
                          <path d="M14 11v6"></path>
                        </svg>
                      </button>
                    </form>
                """
            else:
                action_form = f"""
                    <form method="post" action="{action_path}" onsubmit="return confirm('Schedule deletion for this preview and all associated comments in 7 days?');">
                      <button type="submit" class="icon-action icon-danger" title="Schedule delete" aria-label="Schedule delete">
                        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                          <path d="M3 6h18"></path>
                          <path d="M8 6V4h8v2"></path>
                          <path d="M7 6l1 14h8l1-14"></path>
                          <path d="M10 11v6"></path>
                          <path d="M14 11v6"></path>
                        </svg>
                      </button>
                    </form>
                """
            build_rows.append(
                f"""
                <tr{row_class}>
                  <td>{preview_display}</td>
                  <td>{format_ts(updated_at)}</td>
                  <td><a href="{build_url(tag_raw, base=self._public_base())}" target="_blank" rel="noreferrer">View preview</a><br>
                      <span class="subtle">{urlparse(self._public_base()).netloc}{build_path(tag_raw)}</span></td>
                  <td><a href="/comments?build_id={build_id}">{open_comments} open / {comments} total</a></td>
                  <td>
                    <div class="preview-actions">
                      <a class="icon-action" href="/previews/{build_id}/comments.csv" title="Export comments" aria-label="Export comments">
                        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                          <path d="M12 3v11"></path>
                          <path d="M7 10l5 5 5-5"></path>
                          <path d="M5 21h14"></path>
                        </svg>
                      </a>
                      {action_form}
                    </div>
                  </td>
                </tr>
                """
            )
        pending = import_status.get_all()
        now = time.time()

        def _fmt_elapsed(started_at: float) -> str:
            s = int(now - started_at)
            return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"

        pending_rows = []
        for entry in pending:
            name = html.escape(entry["display_name"])
            tag_esc = html.escape(entry["tag"])
            elapsed = _fmt_elapsed(entry["started_at"])
            if entry["failed"]:
                status_badge = '<span class="status archived">Failed</span>'
                stage_text = html.escape(entry["error"]) if entry["error"] else "See logs for details"
            else:
                status_badge = '<span class="status open">Importing</span>'
                stage_text = html.escape(entry["stage"])
            pending_rows.append(f"""
                <tr class="is-archived">
                  <td><strong>{name}</strong><br><span class="subtle">{tag_esc}</span></td>
                  <td>—</td>
                  <td>{status_badge}<br><span class="subtle">{stage_text}</span></td>
                  <td>—</td>
                  <td><span class="subtle">{elapsed} elapsed</span></td>
                </tr>""")

        all_rows = pending_rows + build_rows
        build_table = "\n".join(all_rows) if all_rows else "<tr><td colspan='5'>No previews uploaded yet.</td></tr>"
        auto_refresh = 3 if any(not e["failed"] for e in pending) else None

        body = f"""
        <section class="panel">
          <h1>Previews</h1>
        </section>
        <section class="panel">
          <table>
            <thead>
              <tr>
                <th>Preview</th>
                <th>Last updated</th>
                <th>Site</th>
                <th>Comments</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {build_table}
            </tbody>
          </table>
        </section>
        """
        self._send_html(HTTPStatus.OK, html_page("Previews", body, auto_refresh=auto_refresh))

    def _export_preview_comments_csv(self, path: str) -> None:
        match = re.match(r"^/(?:publications|previews)/(\d+)/comments\.csv$", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Export route not found")
            return
        build_id = int(match.group(1))
        build = fetch_build_for_export(build_id)
        if not build:
            self.send_error(HTTPStatus.NOT_FOUND, "Preview not found")
            return

        comments = fetch_build_comments_for_export(build_id)
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(
            [
                "comment_id",
                "build_id",
                "build_tag",
                "build_name",
                "page_path",
                "line_start",
                "line_end",
                "selected_text",
                "body",
                "reviewer",
                "resolved",
                "parent_id",
                "created_at_unix",
                "created_at_human",
                "resolved_at_unix",
                "resolved_at_human",
            ]
        )
        for comment in comments:
            writer.writerow(
                [
                    int(comment["id"]),
                    int(comment["build_id"]),
                    str(comment.get("build_tag") or ""),
                    str(comment.get("build_name") or comment.get("build_tag") or ""),
                    str(comment.get("page_path") or ""),
                    comment.get("line_start") or "",
                    comment.get("line_end") or "",
                    str(comment.get("selected_text") or ""),
                    str(comment.get("body") or ""),
                    str(comment.get("reviewer") or ""),
                    1 if bool(comment.get("resolved")) else 0,
                    comment.get("parent_id") or "",
                    int(comment.get("created_at") or 0),
                    str(comment.get("created_at_human") or ""),
                    int(comment["resolved_at"]) if comment.get("resolved_at") else "",
                    str(comment.get("resolved_at_human") or ""),
                ]
            )

        safe_tag = slugify_tag(str(build["tag"]))
        filename = f"{safe_tag}-comments.csv"
        self._send_csv(HTTPStatus.OK, filename, out.getvalue())

    def _render_comments_page(self, query: dict[str, list[str]]) -> None:
        builds = list_builds()
        selected_build_id = query.get("build_id", [""])[0]
        selected_reviewer = query.get("reviewer", [""])[0]
        selected_resolved = query.get("resolved", ["all"])[0]
        from_input = query.get("from", [""])[0]
        to_input = query.get("to", [""])[0]
        sort_key = query.get("sort", ["created_at"])[0]
        sort_dir = query.get("dir", ["desc"])[0].lower()
        if sort_dir not in ("asc", "desc"):
            sort_dir = "desc"
        filters = {
            "build_id": int(selected_build_id) if selected_build_id.isdigit() else None,
            "reviewer": selected_reviewer.strip() or None,
            "resolved": selected_resolved,
            "from_ts": parse_local_datetime_to_ts(from_input),
            "to_ts": parse_local_datetime_to_ts(to_input, end_of_minute=True),
        }
        comments = fetch_feedback(filters, sort_key=sort_key, sort_dir=sort_dir)

        options = ['<option value="">All previews</option>']
        for build in builds:
            build_id = str(build["id"])
            selected = " selected" if selected_build_id == build_id else ""
            build_tag = str(build["tag"])
            build_name = (str(build["display_name"]) if build["display_name"] is not None else "").strip() or build_tag
            archived_raw = build["archived_at"] if "archived_at" in build.keys() else None
            archived_at = int(archived_raw) if archived_raw is not None else None
            archive_suffix = " [pending delete]" if archived_at is not None else ""
            option_label = (
                (build_name if build_name == build_tag else f"{build_name} ({build_tag})")
                + archive_suffix
            )
            options.append(
                f'<option value="{build_id}"{selected}>{html.escape(option_label)}</option>'
            )

        def sort_link(column: str, label: str) -> str:
            next_dir = "desc" if sort_key == column and sort_dir == "asc" else "asc"
            params: dict[str, str] = {}
            if selected_build_id:
                params["build_id"] = selected_build_id
            if selected_reviewer:
                params["reviewer"] = selected_reviewer
            if selected_resolved:
                params["resolved"] = selected_resolved
            if from_input:
                params["from"] = from_input
            if to_input:
                params["to"] = to_input
            params["sort"] = column
            params["dir"] = next_dir
            arrow = ""
            if sort_key == column:
                arrow = " (↑)" if sort_dir == "asc" else " (↓)"
            href = f"/comments?{urlencode(params)}"
            return f'<a href="{html.escape(href)}">{html.escape(label)}{arrow}</a>'

        comment_rows: list[str] = []
        for comment in comments:
            resolved_label = "resolved" if comment["resolved"] else "open"
            location = build_url(str(comment["build_tag"]), str(comment["page_path"]), base=self._public_base())
            location = f"{location}#review-comment-{comment['id']}"
            archived_raw = comment.get("build_archived_at")
            archived_at = int(archived_raw) if archived_raw is not None else None
            delete_at = compute_delete_at(archived_at)
            row_class = " class=\"is-archived\"" if archived_at is not None else ""

            build_tag = str(comment["build_tag"])
            build_name = (str(comment["build_name"]) if comment["build_name"] is not None else "").strip() or build_tag
            preview_label = (
                html.escape(build_name)
                if build_name == build_tag
                else f"{html.escape(build_name)}<br><span class=\"subtle\">{html.escape(build_tag)}</span>"
            )
            if archived_at is not None and delete_at is not None:
                preview_label = (
                    f"{preview_label}<br>"
                    f"<span class=\"subtle archive-note\">Pending deletion on {html.escape(format_ts(delete_at))}</span>"
                )

            status_badges = [f'<span class="status {resolved_label}">{resolved_label}</span>']
            if archived_at is not None:
                status_badges.append('<span class="status archived">pending delete</span>')
            status_markup = " ".join(status_badges)

            comment_rows.append(
                f"""
                <tr id="review-comment-{comment['id']}"{row_class}>
                  <td>{preview_label}</td>
                  <td>
                    <a href="{html.escape(location)}" target="_blank" rel="noreferrer">{html.escape(str(comment['page_path']))}</a>
                    <div class="subtle">lines {comment.get('line_start') or '-'} - {comment.get('line_end') or '-'}</div>
                  </td>
                  <td>{html.escape(str(comment['reviewer']))}</td>
                  <td>{comment['created_at_human']}</td>
                  <td>{status_markup}</td>
                  <td>
                    <a href="{html.escape(location)}" target="_blank" rel="noreferrer">{html.escape(str(comment['body']))}</a>
                    {f"<blockquote>{html.escape(str(comment['selected_text']))}</blockquote>" if comment.get('selected_text') else ""}
                  </td>
                </tr>
                """
            )
        comments_markup = (
            "\n".join(comment_rows) if comment_rows else "<tr><td colspan='6'>No comments match the current filters.</td></tr>"
        )

        body = f"""
        <section class="panel">
          <h1>Comments</h1>
          <form class="filter-form" method="get" action="/comments">
            <label>Preview
              <select name="build_id">{''.join(options)}</select>
            </label>
            <label>Reviewer
              <input type="text" name="reviewer" value="{html.escape(selected_reviewer)}" placeholder="Name">
            </label>
            <label>Status
              <select name="resolved">
                <option value="all"{' selected' if selected_resolved == 'all' else ''}>All</option>
                <option value="unresolved"{' selected' if selected_resolved == 'unresolved' else ''}>Unresolved</option>
                <option value="resolved"{' selected' if selected_resolved == 'resolved' else ''}>Resolved</option>
              </select>
            </label>
            <label>From (UTC)
              <input type="datetime-local" name="from" value="{html.escape(from_input)}">
            </label>
            <label>To (UTC)
              <input type="datetime-local" name="to" value="{html.escape(to_input)}">
            </label>
            <input type="hidden" name="sort" value="{html.escape(sort_key)}">
            <input type="hidden" name="dir" value="{html.escape(sort_dir)}">
            <button type="submit">Apply</button>
          </form>
        </section>
        <section class="panel">
          <table>
            <thead>
              <tr>
                <th>{sort_link("preview", "Preview")}</th>
                <th>{sort_link("page_path", "Page")}</th>
                <th>{sort_link("reviewer", "Reviewer")}</th>
                <th>{sort_link("created_at", "Created")}</th>
                <th>{sort_link("resolved", "Status")}</th>
                <th>Comment</th>
              </tr>
            </thead>
            <tbody>
              {comments_markup}
            </tbody>
          </table>
        </section>
        """
        self._send_html(HTTPStatus.OK, html_page("Comments", body))

    def _delete_preview(self, path: str) -> None:
        match = re.match(r"^/(?:publications|previews)/(\d+)/delete$", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Action not found")
            return
        mark_build_archived(int(match.group(1)))
        self._redirect("/previews")

    def _restore_preview(self, path: str) -> None:
        match = re.match(r"^/(?:publications|previews)/(\d+)/restore$", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Action not found")
            return
        restore_archived_build(int(match.group(1)))
        self._redirect("/previews")

    def _destroy_preview(self, path: str) -> None:
        match = re.match(r"^/(?:publications|previews)/(\d+)/destroy$", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Action not found")
            return
        delete_build_and_comments(int(match.group(1)))
        self._redirect("/previews")

    def _feedback_toggle_resolved(self, path: str) -> None:
        match = re.match(r"^/comments/items/(\d+)/resolve$", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Comment not found")
            return
        form = self._read_form()
        resolved = form.get("resolved", "true").lower() == "true"
        try:
            set_comment_resolved(int(match.group(1)), resolved)
        except ValueError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._redirect("/comments")

    def _feedback_reply(self, path: str) -> None:
        match = re.match(r"^/comments/items/(\d+)/reply$", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Comment not found")
            return
        form = self._read_form()
        reviewer = (form.get("reviewer") or DEFAULT_REVIEWER).strip() or DEFAULT_REVIEWER
        body = (form.get("body") or "").strip()
        if not body:
            self._redirect("/comments")
            return
        try:
            create_comment(
                build_id=0,
                page_path="/",
                reviewer=reviewer,
                body=body,
                parent_id=int(match.group(1)),
            )
        except ValueError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._redirect("/comments")

