from __future__ import annotations

import csv
import html
import io
import json
import re
from http import HTTPStatus
from urllib.parse import urlencode, urlparse

from docs_review_portal.config import DEFAULT_REVIEWER
import time

from docs_review_portal import import_status
from docs_review_portal.data import (
    create_comment,
    delete_review,
    fetch_build_comments_for_export,
    fetch_build_for_export,
    fetch_feedback,
    get_build_by_id,
    list_reviews,
    mark_build_archived,
    restore_archived_build,
    review_context_for,
    review_key_of,
    set_comment_resolved,
    version_of,
)
from docs_review_portal.helpers import (
    build_path,
    canonical_page_path,
    compute_delete_at,
    format_ts,
    html_page,
    linkify_html,
    parse_local_datetime_to_ts,
    review_path,
    review_url,
    slugify_tag,
)


class ReviewPagesMixin:
    def _render_previews_page(self) -> None:
        reviews = list_reviews()
        build_rows = []
        for review in reviews:
            # A review with several versions shows as one row for its latest version.
            row = review["latest"]
            versions = review["versions"]
            version_count = len(versions)
            latest_version = version_of(row)
            build_id = int(row["id"])
            tag_raw = str(review["key"])
            tag = html.escape(tag_raw)
            name_raw = (str(row["display_name"]) if row["display_name"] is not None else "").strip() or tag_raw
            name = html.escape(name_raw)
            preview_display = f"<strong>{name}</strong>"
            if version_count > 1:
                preview_display = f'{preview_display} <span class="version-badge">v{latest_version}</span>'
            if name_raw != tag_raw:
                preview_display = f"{preview_display}<br><span class=\"subtle\">{tag}</span>"
            changed_pages_raw = row["changed_pages"] if "changed_pages" in row.keys() else None
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
            site_href = html.escape(review_url(tag_raw, latest_version, True, base=self._public_base()))
            comments_cell = f'<a href="/comments?build_id={build_id}">{open_comments} open / {comments} total</a>'
            versions_action = ""
            delete_scope = "this preview and all associated comments"
            if version_count > 1:
                comments_cell = (
                    f'<a href="/comments?build_id={build_id}">{open_comments} open / {comments} total</a>'
                    f'<br><a class="subtle" href="/comments?build_id={build_id}&amp;version=all">'
                    f'{review["total_open_comment_count"]} open / {review["total_comment_count"]} total across all versions</a>'
                )
                delete_scope = f"this preview, all {version_count} of its versions and all associated comments"
                version_items = []
                for version_row in reversed(versions):
                    v_num = version_of(version_row)
                    v_id = int(version_row["id"])
                    v_is_latest = v_num == latest_version
                    v_href = html.escape(review_url(tag_raw, v_num, v_is_latest, base=self._public_base()))
                    v_open = int(version_row["open_comment_count"] or 0)
                    v_total = int(version_row["comment_count"] or 0)
                    v_date = html.escape(format_ts(int(version_row["created_at"])))
                    version_items.append(
                        f'''<li class="version-menu-item">
                          <div class="version-menu-title"><strong>v{v_num}</strong>{' <span class="version-badge">latest</span>' if v_is_latest else ''}
                            <span class="subtle">{v_date}</span></div>
                          <div class="version-menu-links">
                            <a href="{v_href}">View preview</a>
                            <a href="/comments?build_id={v_id}&amp;version={v_num}">{v_open} open / {v_total} total</a>
                          </div>
                        </li>'''
                    )
                versions_action = f'''
                      <details class="version-menu">
                        <summary class="icon-action" title="Versions" aria-label="Versions">
                          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                            <path d="M12 3l9 5-9 5-9-5z"></path>
                            <path d="M3 13l9 5 9-5"></path>
                            <path d="M3 17.5l9 5 9-5"></path>
                          </svg>
                        </summary>
                        <ul class="version-menu-list">{"".join(version_items)}</ul>
                      </details>'''
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
                    <form method="post" action="{destroy_path}" onsubmit="return confirm('Permanently delete {delete_scope} right now? This cannot be undone.');">
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
                    <form method="post" action="{action_path}" onsubmit="return confirm('Schedule deletion for {delete_scope} in 7 days?');">
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
                  <td><a href="{site_href}">View preview</a><br>
                      <span class="subtle">{urlparse(self._public_base()).netloc}{build_path(tag_raw)}</span></td>
                  <td>{comments_cell}</td>
                  <td>
                    <div class="preview-actions">
                      {versions_action}
                      <a class="icon-action" href="/previews/{build_id}/changed-pages" title="Changed pages" aria-label="Changed pages">
                        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                          <path d="M8 6h13"></path><path d="M8 12h13"></path><path d="M8 18h13"></path>
                          <path d="M3 6h.01"></path><path d="M3 12h.01"></path><path d="M3 18h.01"></path>
                        </svg>
                      </a>
                      <a class="icon-action" href="/previews/{build_id}/comments.csv" title="{'Export comments from all versions' if version_count > 1 else 'Export comments'}" aria-label="Export comments">
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
        <section class="panel" style="display:flex;align-items:center;justify-content:space-between;gap:12px">
          <h1 style="margin:0">Previews</h1>
          <a class="button-link" href="/help#upload">Upload preview</a>
        </section>
        <script>
          function closeVersionMenus(except) {{
            document.querySelectorAll('details.version-menu[open]').forEach(function (d) {{
              if (d !== except) {{ d.open = false; }}
            }});
          }}
          document.addEventListener('click', function (e) {{
            document.querySelectorAll('details.version-menu[open]').forEach(function (d) {{
              if (!d.contains(e.target)) {{ d.open = false; }}
            }});
          }});
          // The menu is placed on the screen, not inside the table, so the table's own
          // sideways scrolling can't cut it off.
          document.addEventListener('toggle', function (e) {{
            var d = e.target;
            if (!d.classList || !d.classList.contains('version-menu')) {{ return; }}
            var list = d.querySelector('.version-menu-list');
            if (!d.open || !list) {{ return; }}
            var r = d.querySelector('summary').getBoundingClientRect();
            list.style.right = Math.max(10, window.innerWidth - r.right) + 'px';
            var top = r.bottom + 6;
            if (top + list.offsetHeight > window.innerHeight - 10 && r.top - list.offsetHeight - 6 > 10) {{
              top = r.top - list.offsetHeight - 6;
            }}
            list.style.top = top + 'px';
          }}, true);
          window.addEventListener('scroll', function () {{ closeVersionMenus(null); }}, true);
          window.addEventListener('resize', function () {{ closeVersionMenus(null); }});
        </script>
        <section class="panel">
          <div class="table-scroll">
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
          </div>
        </section>
        """
        self._send_html(HTTPStatus.OK, html_page("Previews", body, auto_refresh=auto_refresh))

    def _export_preview_comments_csv(self, path: str) -> None:
        match = re.match(r"^/(?:publications|previews|api/builds)/(\d+)/comments\.csv$", path)
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
                "version",
                "carried_from_comment_id",
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
                    int(comment.get("build_version") or 1),
                    comment.get("carried_from_id") or "",
                ]
            )

        safe_tag = slugify_tag(review_key_of(build))
        filename = f"{safe_tag}-comments.csv"
        self._send_csv(HTTPStatus.OK, filename, out.getvalue())

    def _render_comments_page(self, query: dict[str, list[str]]) -> None:
        reviews = list_reviews()
        selected_build_id = query.get("build_id", [""])[0]
        version_input = (query.get("version", ["latest"])[0] or "latest").strip().lower()
        version_filter: str | int = int(version_input) if version_input.isdigit() else (
            "all" if version_input == "all" else "latest"
        )
        selected_reviewer = query.get("reviewer", [""])[0]
        status_labels = {"open": "Open", "outdated": "Outdated", "resolved": "Resolved"}
        status_names = tuple(status_labels)
        selected_statuses = [name for name in query.get("status", []) if name in status_names]
        legacy_resolved = query.get("resolved", [""])[0]
        if not selected_statuses and legacy_resolved:
            # Older links used a single resolved filter.
            selected_statuses = {"unresolved": ["open"], "resolved": ["resolved"]}.get(legacy_resolved, [])
        selected_statuses = list(dict.fromkeys(selected_statuses))
        # What the status dropdown shows when closed. Nothing chosen means every status.
        if not selected_statuses or len(selected_statuses) == len(status_names):
            status_summary = "All statuses"
        else:
            status_summary = ", ".join(status_labels[name] for name in status_names if name in selected_statuses)
        from_input = query.get("from", [""])[0]
        to_input = query.get("to", [""])[0]
        sort_key = query.get("sort", ["created_at"])[0]
        sort_dir = query.get("dir", ["desc"])[0].lower()
        embed = (query.get("embed", ["0"])[0] or "0") == "1"
        if sort_dir not in ("asc", "desc"):
            sort_dir = "desc"
        filters = {
            "build_id": int(selected_build_id) if selected_build_id.isdigit() else None,
            "reviewer": selected_reviewer.strip() or None,
            "statuses": selected_statuses,
            "from_ts": parse_local_datetime_to_ts(from_input),
            "to_ts": parse_local_datetime_to_ts(to_input, end_of_minute=True),
            "version": version_filter,
        }
        comments = fetch_feedback(filters, sort_key=sort_key, sort_dir=sort_dir)

        options = ['<option value="">All previews</option>']
        selected_review = None
        for review in reviews:
            # The option's value is the latest version's id, any version's id selects the review.
            build = review["latest"]
            build_id = str(build["id"])
            is_selected = selected_build_id in {str(v["id"]) for v in review["versions"]}
            if is_selected:
                selected_review = review
            selected = " selected" if is_selected else ""
            build_tag = str(review["key"])
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

        version_options = [
            f'<option value="latest"{" selected" if version_filter == "latest" else ""}>Latest version</option>',
            f'<option value="all"{" selected" if version_filter == "all" else ""}>All versions</option>',
        ]
        if selected_review is not None:
            for version_row in reversed(selected_review["versions"]):
                v_num = version_of(version_row)
                version_options.append(
                    f'<option value="{v_num}"{" selected" if version_filter == v_num else ""}>Version {v_num}</option>'
                )

        def sort_link(column: str, label: str) -> str:
            next_dir = "desc" if sort_key == column and sort_dir == "asc" else "asc"
            params: list[tuple[str, str]] = []
            if selected_build_id:
                params.append(("build_id", selected_build_id))
            if selected_reviewer:
                params.append(("reviewer", selected_reviewer))
            params.extend(("status", name) for name in selected_statuses)
            if version_input != "latest":
                params.append(("version", version_input))
            if from_input:
                params.append(("from", from_input))
            if to_input:
                params.append(("to", to_input))
            if embed:
                params.append(("embed", "1"))
            params.append(("sort", column))
            params.append(("dir", next_dir))
            arrow = ""
            if sort_key == column:
                arrow = " (↑)" if sort_dir == "asc" else " (↓)"
            href = f"/comments?{urlencode(params)}"
            return f'<a href="{html.escape(href)}">{html.escape(label)}{arrow}</a>'

        comment_rows: list[str] = []
        for comment in comments:
            resolved_label = "resolved" if comment["resolved"] else "open"
            is_diff_comment = comment.get("kind") == "diff"
            location = review_url(
                str(comment["build_tag"]),
                int(comment["build_version"]),
                bool(comment["is_latest_version"]),
                str(comment["page_path"]),
                base=self._public_base(),
            )
            hash_fragment = "review-diff-comment" if is_diff_comment else "review-comment"
            location = f"{location}#{hash_fragment}-{comment['id']}"
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
            version_cell = f'v{int(comment["build_version"])}'
            if comment.get("carried_from_version") is not None:
                version_cell += (
                    f'<br><span class="subtle">carried from v{int(comment["carried_from_version"])}</span>'
                )
            if archived_at is not None and delete_at is not None:
                preview_label = (
                    f"{preview_label}<br>"
                    f"<span class=\"subtle archive-note\">Pending deletion on {html.escape(format_ts(delete_at))}</span>"
                )

            status_badges = [f'<span class="status {resolved_label}">{resolved_label}</span>']
            if comment.get("outdated"):
                status_badges.append(
                    '<span class="status outdated" title="On an earlier version, with no copy on the latest one">outdated</span>'
                )

            if archived_at is not None:
                status_badges.append('<span class="status archived">pending delete</span>')
            status_markup = " ".join(status_badges)

            if is_diff_comment:
                page_meta = '<div class="subtle">diff comment</div>'
            elif comment.get("line_start") or comment.get("line_end"):
                page_meta = f"<div class=\"subtle\">lines {comment.get('line_start') or '-'} - {comment.get('line_end') or '-'}</div>"
            else:
                # Carried forward from an earlier version, so there are no line numbers.
                page_meta = ""
            replies = comment.get("replies") or []
            thread_markup = ""
            if replies:
                reply_count_label = f"{len(replies)} {'reply' if len(replies) == 1 else 'replies'}"
                reply_items = "".join(
                    f"""
                    <li class="thread-reply">
                      <span class="thread-avatar" aria-hidden="true">{html.escape((str(reply.get('reviewer') or '?').strip() or '?')[:1].upper())}</span>
                      <div class="thread-reply-main">
                        <div class="thread-reply-head">
                          <strong>{html.escape(str(reply.get('reviewer') or 'reviewer'))}</strong>
                          <span class="subtle">{html.escape(str(reply.get('created_at_human') or ''))}</span>
                          {'<span class="subtle">(edited)</span>' if reply.get('edited_at') else ''}
                        </div>
                        <div class="thread-reply-body">{linkify_html(str(reply.get('body') or ''))}</div>
                      </div>
                    </li>
                    """
                    for reply in replies
                )
                thread_markup = f"""
                    <div class="comment-thread">
                      <div class="comment-thread-label">{reply_count_label}</div>
                      <ol class="thread-replies">{reply_items}</ol>
                    </div>
                """
            edited_markup = ' <span class="subtle">(edited)</span>' if comment.get("edited_at") else ""
            comment_rows.append(
                f"""
                <tr id="review-comment-{comment['id']}"{row_class}>
                  <td>{preview_label}</td>
                  <td>{version_cell}</td>
                  <td>
                    <a class="comment-page-link" href="{html.escape(location)}">{html.escape(str(comment['page_path']))}</a>
                    {page_meta}
                  </td>
                  <td>{html.escape(str(comment['reviewer']))}</td>
                  <td>{comment['created_at_human']}</td>
                  <td>{status_markup}</td>
                  <td>
                    <div class="comment-body">{linkify_html(str(comment['body']))}{edited_markup}</div>
                    {f"<blockquote>{html.escape(str(comment['selected_text']))}</blockquote>" if comment.get('selected_text') else ""}
                    {thread_markup}
                  </td>
                </tr>
                """
            )
        comments_markup = (
            "\n".join(comment_rows) if comment_rows else "<tr><td colspan='7'>No comments match the current filters.</td></tr>"
        )

        body = f"""
        <section class="panel">
          <h1>Comments</h1>
          <form class="filter-form" method="get" action="/comments">
            <label>Preview
              <select name="build_id">{''.join(options)}</select>
            </label>
            <label>Version
              <select name="version">{''.join(version_options)}</select>
            </label>
            <label>Reviewer
              <input type="text" name="reviewer" value="{html.escape(selected_reviewer)}" placeholder="Name">
            </label>
            <div class="status-filter">
              <span class="status-filter-title">Status</span>
              <details class="status-dropdown">
                <summary id="status-dropdown-label">{html.escape(status_summary)}</summary>
                <div class="status-dropdown-menu">
                  {''.join(
                      f'<label><input type="checkbox" name="status" value="{name}"'
                      f'{" checked" if name in selected_statuses or not selected_statuses else ""}> {html.escape(status_labels[name])}</label>'
                      for name in status_names
                  )}
                </div>
              </details>
            </div>
            <label>From (UTC)
              <input type="datetime-local" name="from" value="{html.escape(from_input)}">
            </label>
            <label>To (UTC)
              <input type="datetime-local" name="to" value="{html.escape(to_input)}">
            </label>
            <input type="hidden" name="sort" value="{html.escape(sort_key)}">
            <input type="hidden" name="dir" value="{html.escape(sort_dir)}">
            {'<input type="hidden" name="embed" value="1">' if embed else ''}
            <button type="submit">Apply</button>
          </form>
        </section>
        <section class="panel">
          <div class="table-scroll">
          <table class="table-wide">
            <thead>
              <tr>
                <th>{sort_link("preview", "Preview")}</th>
                <th>{sort_link("version", "Version")}</th>
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
          </div>
        </section>
        <script>
        (function() {{
          var dropdown = document.querySelector('details.status-dropdown');
          if (!dropdown) {{ return; }}
          var label = document.getElementById('status-dropdown-label');
          var boxes = dropdown.querySelectorAll('input[name="status"]');
          function refreshLabel() {{
            var names = [];
            boxes.forEach(function(box) {{
              if (box.checked) {{ names.push(box.parentNode.textContent.trim()); }}
            }});
            label.textContent = (names.length === 0 || names.length === boxes.length) ? 'All statuses' : names.join(', ');
          }}
          boxes.forEach(function(box) {{ box.addEventListener('change', refreshLabel); }});
          document.addEventListener('click', function(e) {{
            if (dropdown.open && !dropdown.contains(e.target)) {{ dropdown.open = false; }}
          }});
          document.addEventListener('keydown', function(e) {{
            if (e.key === 'Escape' && dropdown.open) {{ dropdown.open = false; }}
          }});
        }})();
        </script>
        {f'''<script>
        (function() {{
          if (window.parent && window.parent !== window) {{
            document.addEventListener('click', function(e) {{
              var link = e.target.closest && e.target.closest('.comment-page-link');
              if (!link) {{ return; }}
              e.preventDefault();
              if (window.parent.__reviewCloseModal) {{ window.parent.__reviewCloseModal(); }}
              window.parent.location.href = link.href;
            }});
          }}
        }})();
        </script>''' if embed else ''}
        """
        self._send_html(HTTPStatus.OK, html_page("Comments", body, embed=embed))

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
        delete_review(int(match.group(1)))
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

    def _render_changed_pages_page(self, path: str, query: dict[str, list[str]] | None = None) -> None:
        match = re.match(r"^/(?:publications|previews)/(\d+)/changed-pages$", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Route not found")
            return
        build_id = int(match.group(1))
        build = get_build_by_id(build_id)
        if not build:
            self.send_error(HTTPStatus.NOT_FOUND, "Preview not found")
            return

        query = query or {}
        embed = (query.get("embed", ["0"])[0] or "0") == "1"
        select_raw = (query.get("select", [""])[0] or "").strip()

        review = review_context_for(build)
        tag = str(review["reviewKey"])
        url_prefix = str(review["urlPrefix"])
        version_label = (
            f"version {review['version']}" if len(review["versions"]) > 1 else ""
        )
        # New comments go on the latest version only. Older ones allow replies.
        can_comment = review["version"] == review["latestVersion"]
        name_raw = (str(build["display_name"]) if build["display_name"] is not None else "").strip() or tag
        raw = build["changed_pages"] if "changed_pages" in build.keys() else None
        pages = [ln.strip() for ln in str(raw).splitlines() if ln.strip()] if raw else []
        diff_raw = build["diff_pages"] if "diff_pages" in build.keys() else None
        diff_pages_set = {ln.strip() for ln in str(diff_raw).splitlines() if ln.strip()} if diff_raw else set()

        selected_page = None
        if select_raw:
            select_canon = canonical_page_path(select_raw)
            for p in pages:
                if canonical_page_path(p) == select_canon:
                    selected_page = p
                    break

        highlight_raw = (query.get("highlight", [""])[0] or "").strip()
        highlight_comment_id = int(highlight_raw) if highlight_raw.isdigit() else None

        def build_page_tree(paths: list[str]) -> dict:
            root: dict = {}
            for p in paths:
                segments = [s for s in p.strip("/").split("/") if s]
                if not segments:
                    continue
                node = root
                for i, seg in enumerate(segments):
                    entry = node.setdefault(seg, {"children": {}, "page": None})
                    if i == len(segments) - 1:
                        entry["page"] = p
                    node = entry["children"]
            return root

        def render_file_row(p: str, label: str) -> str:
            has_diff = canonical_page_path(p) in diff_pages_set
            p_esc = html.escape(p)
            label_esc = html.escape(label)
            status_cls = "status-modified" if has_diff else "status-added"
            status_label = "M" if has_diff else "A"
            status_title = "Modified &mdash; diff available" if has_diff else "Added &mdash; no diff available"
            preview_link = html.escape(
                review_url(
                    tag,
                    int(review["version"]),
                    review["version"] == review["latestVersion"],
                    p,
                    base=self._public_base(),
                )
            )
            return f"""<button type="button" class="file-select-btn" title="{p_esc}">
                <span class="file-status-badge {status_cls}" title="{status_title}">{status_label}</span>
                <span class="file-path">{label_esc}</span>
              </button>
              <a class="icon-action open-preview-link" href="{preview_link}" title="Open preview" aria-label="Open preview for {p_esc}">&#8599;</a>"""

        def render_tree(node: dict) -> str:
            dirs = sorted((seg for seg, e in node.items() if e["children"]))
            files = sorted((seg for seg, e in node.items() if not e["children"]))
            parts = []
            for seg in dirs:
                entry = node[seg]
                page = entry["page"]
                if page is not None:
                    has_diff = canonical_page_path(page) in diff_pages_set
                    header = (
                        f'<div class="changed-pages-item tree-row" data-path="{html.escape(page)}" '
                        f'data-has-diff="{"1" if has_diff else "0"}">{render_file_row(page, seg)}</div>'
                    )
                else:
                    header = f'<div class="tree-dir-label">{html.escape(seg)}/</div>'
                parts.append(
                    f'<li class="tree-dir">{header}<ul class="tree-children">{render_tree(entry["children"])}</ul></li>'
                )
            for seg in files:
                page = node[seg]["page"]
                has_diff = canonical_page_path(page) in diff_pages_set
                parts.append(
                    f'<li class="changed-pages-item tree-row" data-path="{html.escape(page)}" '
                    f'data-has-diff="{"1" if has_diff else "0"}">{render_file_row(page, seg)}</li>'
                )
            return "\n".join(parts)

        items_html = render_tree(build_page_tree(pages)) if pages else (
            '<li class="changed-pages-empty subtle"><em>No pages listed yet.</em></li>'
        )

        body = f"""
        <section class="panel">
          {'' if embed else '<p><a href="/previews">&larr; Back to previews</a></p>'}
          <h1>Changed pages</h1>
          <p>Preview: <strong>{html.escape(name_raw)}</strong>
             {f'<span class="subtle">({html.escape(tag)})</span>' if name_raw != tag else ''}
             {f'<span class="version-badge">{html.escape(version_label)}</span>' if version_label else ''}</p>
          <p class="subtle" id="page-count">{len(pages)} page{"s" if len(pages) != 1 else ""}</p>
          {'' if can_comment else '<p class="subtle">New comments are turned off on older versions. You can still reply to existing comments.</p>'}

          <div class="diff-page-layout">
            <div class="diff-sidebar">
              <ul class="changed-pages-list" id="changed-pages-list">
                {items_html}
              </ul>
            </div>
            <div class="diff-viewer" id="diff-viewer">
              <div class="diff-empty subtle"><em>Select a page to view its diff.</em></div>
            </div>
          </div>
        </section>
        {"" if embed else f'''<script>
          window.REVIEW_CONTEXT = {{
            mode: "diff",
            buildId: {build_id},
            buildTag: {json.dumps(tag)},
            urlPrefix: {json.dumps(url_prefix)},
            version: {review["version"]},
            latestVersion: {review["latestVersion"]},
            versions: {json.dumps(review["versions"])},
            pagePath: {json.dumps(selected_page)},
          }};
        </script>
        <link rel="stylesheet" href="/_review/assets/review.css">
        <script defer src="/_review/assets/review-client.js"></script>'''}
        <script>
        (function() {{
          var BUILD_ID = {build_id};
          var urlPrefix = {json.dumps(url_prefix)};
          var baseUrl = {json.dumps(self._public_base())};
          var EMBED = {json.dumps(embed)};
          var HIGHLIGHT_COMMENT_ID = {json.dumps(highlight_comment_id)};
          var CAN_COMMENT = {json.dumps(can_comment)};
          var REVIEWER_KEY = 'docs-review-reviewer';
          var HAS_PARENT_WIDGET = EMBED && window.parent && window.parent !== window;
          var list = document.getElementById('changed-pages-list');
          var diffViewer = document.getElementById('diff-viewer');
          var activePath = null;

          // Per-render diff-comment state — reset at the top of each renderDiff() call.
          var rowsByKey = {{}};
          var orderedKeys = [];

          function getReviewerName() {{
            try {{ return (window.sessionStorage.getItem(REVIEWER_KEY) || '').trim(); }} catch (_) {{ return ''; }}
          }}

          function setReviewerName(name) {{
            try {{ window.sessionStorage.setItem(REVIEWER_KEY, name); }} catch (_) {{}}
          }}

          // The comments list itself lives in a review-client.js widget — the
          // preview page's own widget when this is embedded in its modal, or a
          // local one loaded on this same page when viewed standalone (see
          // below). Either way it calls back in here to reveal a specific row,
          // and we call back out to it after changing pages or adding a
          // comment, so it always reflects whichever file is open here.
          function notifyParentActivePage(pagePath) {{
            if (HAS_PARENT_WIDGET && window.parent.__reviewSetActivePage) {{
              window.parent.__reviewSetActivePage(pagePath);
            }} else if (!EMBED && window.__reviewSetActivePage) {{
              window.__reviewSetActivePage(pagePath);
            }}
          }}

          if (HAS_PARENT_WIDGET) {{
            document.addEventListener('click', function(e) {{
              var link = e.target.closest && e.target.closest('.open-preview-link');
              if (!link) {{ return; }}
              e.preventDefault();
              if (window.parent.__reviewCloseModal) {{ window.parent.__reviewCloseModal(); }}
              window.parent.location.href = link.href;
            }});
          }}

          function escHtml(v) {{
            return String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
          }}

          function buildUrlFor(p) {{
            return baseUrl + urlPrefix + p;
          }}

          function wireSelect(li) {{
            var btn = li.querySelector('.file-select-btn');
            if (btn) {{
              btn.addEventListener('click', function() {{ selectFile(li); }});
            }}
          }}

          function selectFile(li) {{
            var pagePath = li.dataset.path;
            activePath = pagePath;
            list.querySelectorAll('.changed-pages-item').forEach(function(item) {{
              item.classList.toggle('is-selected', item === li);
            }});
            if (li.scrollIntoView) {{ li.scrollIntoView({{block: 'nearest'}}); }}
            if (li.dataset.hasDiff !== '1') {{
              diffViewer.innerHTML = '<div class="diff-empty subtle"><em>No diff available for this page &mdash; it looks like a new page.</em><br>'
                + '<a class="open-preview-link" href="' + escHtml(buildUrlFor(pagePath)) + '">Open preview</a></div>';
              notifyParentActivePage(pagePath);
              return;
            }}
            diffViewer.innerHTML = '<div class="diff-loading subtle">Loading diff&hellip;</div>';
            fetch('/api/builds/' + BUILD_ID + '/page-diff?page=' + encodeURIComponent(pagePath))
              .then(function(r) {{
                if (!r.ok) {{ throw new Error('not found'); }}
                return r.text();
              }})
              .then(function(text) {{
                if (activePath !== pagePath) {{ return; }}
                diffViewer.innerHTML = '';
                diffViewer.appendChild(renderDiff(text, pagePath));
                // renderDiff() must finish first — it (re)populates rowsByKey,
                // which the parent widget highlights rows against once it
                // calls back into __applyCommentHighlights.
                notifyParentActivePage(pagePath);
                if (HIGHLIGHT_COMMENT_ID) {{
                  var targetId = HIGHLIGHT_COMMENT_ID;
                  HIGHLIGHT_COMMENT_ID = null;
                  revealCommentById(pagePath, targetId);
                }}
              }})
              .catch(function() {{
                if (activePath !== pagePath) {{ return; }}
                diffViewer.innerHTML = '<div class="diff-error subtle">Failed to load diff.</div>';
                notifyParentActivePage(pagePath);
              }});
          }}

          function revealCommentById(pagePath, commentId) {{
            fetch('/api/comments?build_id=' + BUILD_ID + '&page_path=' + encodeURIComponent(pagePath))
              .then(function(r) {{ return r.ok ? r.json() : {{comments: []}}; }})
              .then(function(data) {{
                if (activePath !== pagePath) {{ return; }}
                var target = (data.comments || []).filter(function(c) {{ return Number(c.id) === Number(commentId); }})[0];
                if (target) {{ window.__revealAndScrollToDiffKey(target.diff_start_key) || window.__revealAndScrollToDiffKey(target.diff_end_key); }}
              }})
              .catch(function() {{}});
          }}

          // ---- word-level diff (LCS over whitespace-separated tokens) ----
          function tokenize(str) {{
            return str.match(/\\s+|[^\\s]+/g) || [];
          }}

          function lcsDiff(a, b) {{
            var n = a.length, m = b.length;
            var dp = new Array(n + 1);
            var i, j;
            for (i = 0; i <= n; i++) {{ dp[i] = new Array(m + 1).fill(0); }}
            for (i = n - 1; i >= 0; i--) {{
              for (j = m - 1; j >= 0; j--) {{
                dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
              }}
            }}
            var oldOps = [], newOps = [], common = 0;
            i = 0; j = 0;
            while (i < n && j < m) {{
              if (a[i] === b[j]) {{
                oldOps.push({{t: 'eq', v: a[i]}});
                newOps.push({{t: 'eq', v: b[j]}});
                common++; i++; j++;
              }} else if (dp[i + 1][j] >= dp[i][j + 1]) {{
                oldOps.push({{t: 'del', v: a[i]}}); i++;
              }} else {{
                newOps.push({{t: 'add', v: b[j]}}); j++;
              }}
            }}
            while (i < n) {{ oldOps.push({{t: 'del', v: a[i]}}); i++; }}
            while (j < m) {{ newOps.push({{t: 'add', v: b[j]}}); j++; }}
            return {{oldOps: oldOps, newOps: newOps, common: common, total: Math.max(n, m)}};
          }}

          function renderLineContent(ops, kind) {{
            var frag = document.createDocumentFragment();
            ops.forEach(function(op) {{
              var span = document.createElement('span');
              if (op.t === kind) {{
                span.className = 'diff-word-' + kind;
              }}
              span.textContent = op.v;
              frag.appendChild(span);
            }});
            return frag;
          }}

          function diffLineKey(oldNo, newNo) {{
            if (oldNo && newNo) {{ return oldNo + ':' + newNo; }}
            if (oldNo) {{ return 'o' + oldNo; }}
            if (newNo) {{ return 'n' + newNo; }}
            return '';
          }}

          function makeRow(oldNo, newNo, marker, rowClass) {{
            var tr = document.createElement('tr');
            if (rowClass) {{ tr.className = rowClass; }}
            var key = diffLineKey(oldNo, newNo);
            if (key) {{
              tr.dataset.diffKey = key;
              rowsByKey[key] = tr;
              orderedKeys.push(key);
            }}
            var oldTd = document.createElement('td'); oldTd.className = 'diff-line-no'; oldTd.textContent = oldNo || '';
            var newTd = document.createElement('td'); newTd.className = 'diff-line-no'; newTd.textContent = newNo || '';
            var markTd = document.createElement('td'); markTd.className = 'diff-marker'; markTd.textContent = marker || '';
            var contentTd = document.createElement('td'); contentTd.className = 'diff-content';
            tr.appendChild(oldTd); tr.appendChild(newTd); tr.appendChild(markTd); tr.appendChild(contentTd);
            return {{tr: tr, contentTd: contentTd}};
          }}

          var DIFF_EDGE_CONTEXT = 3;
          var DIFF_COLLAPSE_MIN_HIDDEN = 3;
          var diffHiddenGroupCounter = 0;

          function expandHiddenGroup(groupId) {{
            document.querySelectorAll('[data-diff-hidden-group="' + groupId + '"].diff-row-hidden').forEach(function(el) {{
              el.classList.remove('diff-row-hidden');
            }});
            var expandRow = document.querySelector('.diff-row-expand[data-diff-hidden-group="' + groupId + '"]');
            if (expandRow) {{ expandRow.remove(); }}
          }}

          function makeExpandRow(hiddenTrs, groupId) {{
            var tr = document.createElement('tr');
            tr.className = 'diff-row-expand';
            tr.dataset.diffHiddenGroup = String(groupId);
            var td = document.createElement('td');
            td.colSpan = 4;
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'diff-expand-btn';
            var icon = document.createElement('span');
            icon.className = 'diff-expand-icons';
            icon.textContent = '\\u2303\\u2304';
            btn.appendChild(icon);
            btn.appendChild(document.createTextNode(
              'Show ' + hiddenTrs.length + ' unchanged line' + (hiddenTrs.length === 1 ? '' : 's')
            ));
            btn.addEventListener('click', function() {{ expandHiddenGroup(groupId); }});
            td.appendChild(btn);
            tr.appendChild(td);
            return tr;
          }}

          // Collapsed rows stay in the DOM (just hidden via CSS) instead of
          // being detached into a JS-side array — that way review-client.js
          // can find and reveal a specific commented row with a plain DOM
          // query, with no coupling back to this page's render-time state.
          function flushContext(buffer, tbody) {{
            if (!buffer.length) {{ return; }}
            var hiddenCount = buffer.length - DIFF_EDGE_CONTEXT * 2;
            if (hiddenCount <= DIFF_COLLAPSE_MIN_HIDDEN) {{
              buffer.forEach(function(tr) {{ tbody.appendChild(tr); }});
              buffer.length = 0;
              return;
            }}
            var head = buffer.slice(0, DIFF_EDGE_CONTEXT);
            var hidden = buffer.slice(DIFF_EDGE_CONTEXT, buffer.length - DIFF_EDGE_CONTEXT);
            var tail = buffer.slice(buffer.length - DIFF_EDGE_CONTEXT);
            var groupId = ++diffHiddenGroupCounter;
            head.forEach(function(tr) {{ tbody.appendChild(tr); }});
            hidden.forEach(function(tr) {{
              tr.classList.add('diff-row-hidden');
              tr.dataset.diffHiddenGroup = String(groupId);
              tbody.appendChild(tr);
            }});
            tbody.appendChild(makeExpandRow(hidden, groupId));
            tail.forEach(function(tr) {{ tbody.appendChild(tr); }});
            buffer.length = 0;
          }}

          function renderDiff(text, pagePath) {{
            rowsByKey = {{}};
            orderedKeys = [];

            var wrap = document.createElement('div');
            wrap.className = 'diff-file-wrap';
            var titleBar = document.createElement('div');
            titleBar.className = 'diff-file-title';
            titleBar.textContent = pagePath;
            wrap.appendChild(titleBar);

            if (text.indexOf('Binary files') === 0 || /\\nBinary files /.test(text)) {{
              var binMsg = document.createElement('div');
              binMsg.className = 'diff-empty subtle';
              binMsg.textContent = 'Binary file — no diff to display.';
              wrap.appendChild(binMsg);
              return wrap;
            }}

            var table = document.createElement('table');
            table.className = 'diff-table';
            var tbody = document.createElement('tbody');
            table.appendChild(tbody);

            var lines = text.split('\\n');
            if (lines.length && lines[lines.length - 1] === '') {{ lines.pop(); }}

            var i = 0;
            var sawHunk = false;
            while (i < lines.length) {{
              var line = lines[i];
              var hunkMatch = /^@@ -(\\d+)(?:,(\\d+))? \\+(\\d+)(?:,(\\d+))? @@.*$/.exec(line);
              if (!hunkMatch) {{ i++; continue; }}

              sawHunk = true;
              var oldNo = parseInt(hunkMatch[1], 10);
              var newNo = parseInt(hunkMatch[3], 10);
              var hunkRow = document.createElement('tr');
              hunkRow.className = 'diff-row-hunk';
              var hunkTd = document.createElement('td');
              hunkTd.colSpan = 4;
              hunkTd.textContent = line;
              hunkRow.appendChild(hunkTd);
              tbody.appendChild(hunkRow);
              i++;

              var ctxBuffer = [];
              while (i < lines.length && lines[i].charAt(0) !== '@') {{
                var dels = [], adds = [];
                while (i < lines.length && lines[i].charAt(0) === '-') {{ dels.push(lines[i].slice(1)); i++; }}
                while (i < lines.length && lines[i].charAt(0) === '+') {{ adds.push(lines[i].slice(1)); i++; }}

                if (dels.length || adds.length) {{
                  flushContext(ctxBuffer, tbody);
                  var pairCount = Math.min(dels.length, adds.length);
                  var k;
                  for (k = 0; k < pairCount; k++) {{
                    var d = lcsDiff(tokenize(dels[k]), tokenize(adds[k]));
                    var similar = d.total === 0 || (d.common / d.total) >= 0.25;
                    var rDel = makeRow(oldNo++, '', '-', 'diff-row-del');
                    var rAdd = makeRow('', newNo++, '+', 'diff-row-add');
                    if (similar) {{
                      rDel.contentTd.appendChild(renderLineContent(d.oldOps, 'del'));
                      rAdd.contentTd.appendChild(renderLineContent(d.newOps, 'add'));
                    }} else {{
                      rDel.contentTd.textContent = dels[k];
                      rAdd.contentTd.textContent = adds[k];
                    }}
                    tbody.appendChild(rDel.tr);
                    tbody.appendChild(rAdd.tr);
                  }}
                  for (k = pairCount; k < dels.length; k++) {{
                    var rd = makeRow(oldNo++, '', '-', 'diff-row-del');
                    rd.contentTd.textContent = dels[k];
                    tbody.appendChild(rd.tr);
                  }}
                  for (k = pairCount; k < adds.length; k++) {{
                    var ra = makeRow('', newNo++, '+', 'diff-row-add');
                    ra.contentTd.textContent = adds[k];
                    tbody.appendChild(ra.tr);
                  }}
                  continue;
                }}

                if (i >= lines.length) {{ break; }}
                if (lines[i].charAt(0) === '\\\\') {{ i++; continue; }}
                var ctx = lines[i];
                var content = ctx.length ? ctx.slice(1) : '';
                var rc = makeRow(oldNo++, newNo++, '', '');
                rc.contentTd.textContent = content;
                ctxBuffer.push(rc.tr);
                i++;
              }}
              flushContext(ctxBuffer, tbody);
            }}

            if (!sawHunk) {{
              var empty = document.createElement('div');
              empty.className = 'diff-empty subtle';
              empty.textContent = 'No changes to display.';
              wrap.appendChild(empty);
              return wrap;
            }}

            wrap.appendChild(table);
            wireDiffSelection(table, pagePath);
            return wrap;
          }}

          // ---- diff comment row highlighting (comments themselves are owned by the review widget) ----

          function keyRange(startKey, endKey) {{
            var startIdx = orderedKeys.indexOf(startKey);
            var endIdx = orderedKeys.indexOf(endKey);
            if (startIdx === -1 || endIdx === -1) {{ return null; }}
            if (startIdx > endIdx) {{ var tmp = startIdx; startIdx = endIdx; endIdx = tmp; }}
            return orderedKeys.slice(startIdx, endIdx + 1);
          }}

          function applyCommentHighlights(comments) {{
            document.querySelectorAll('.diff-row-commented, .diff-row-commented-resolved').forEach(function(tr) {{
              tr.classList.remove('diff-row-commented', 'diff-row-commented-resolved');
            }});
            comments.forEach(function(comment) {{
              var keys = keyRange(comment.diff_start_key, comment.diff_end_key);
              if (!keys) {{ return; }}
              keys.forEach(function(key) {{
                var tr = rowsByKey[key];
                if (tr) {{ tr.classList.add(comment.resolved ? 'diff-row-commented-resolved' : 'diff-row-commented'); }}
              }});
            }});
          }}

          // Exposed for the parent window's comment widget (review-client.js) to
          // call into — it owns the comments list, this page only owns the diff.
          window.__applyCommentHighlights = applyCommentHighlights;

          window.__revealAndScrollToDiffKey = function(key) {{
            if (!key) {{ return false; }}
            var row = rowsByKey[key];
            if (!row) {{ return false; }}
            if (row.classList.contains('diff-row-hidden')) {{
              expandHiddenGroup(row.getAttribute('data-diff-hidden-group'));
            }}
            row.scrollIntoView({{block: 'center'}});
            row.classList.add('diff-row-flash');
            setTimeout(function() {{ row.classList.remove('diff-row-flash'); }}, 2000);
            return true;
          }};

          // ---- selection-to-comment floating button + composer ----
          var floatingAddBtn = null;
          var diffComposer = null;

          function closestDiffRow(node) {{
            while (node && node.nodeType !== 1) {{ node = node.parentNode; }}
            return node && node.closest ? node.closest('tr[data-diff-key]') : null;
          }}

          function removeFloatingAddBtn() {{
            if (floatingAddBtn) {{ floatingAddBtn.remove(); floatingAddBtn = null; }}
          }}

          var composerCleanup = null;
          var COMPOSER_SIZE_KEY = 'docs-review-composer-size';

          function removeDiffComposer() {{
            if (diffComposer) {{ diffComposer.remove(); diffComposer = null; }}
            if (composerCleanup) {{ composerCleanup(); composerCleanup = null; }}
          }}

          function loadComposerSize() {{
            try {{
              var saved = JSON.parse(window.localStorage.getItem(COMPOSER_SIZE_KEY) || 'null');
              if (saved && isFinite(saved.width) && isFinite(saved.height)) {{ return saved; }}
            }} catch (e) {{}}
            return null;
          }}

          function saveComposerSize(width, height) {{
            try {{
              window.localStorage.setItem(COMPOSER_SIZE_KEY, JSON.stringify({{
                width: Math.round(width),
                height: Math.round(height),
              }}));
            }} catch (e) {{}}
          }}

          // Keeps the composer fully on screen. The position is pulled back first and
          // the max size is then limited to the room left, so a composer the user made
          // large shrinks when the window gets smaller and grows back when it gets larger.
          function fitComposerToViewport(el) {{
            var margin = 10;
            var left = Math.max(margin, Math.min(parseFloat(el.style.left) || 0, window.innerWidth - el.offsetWidth - margin));
            var top = Math.max(margin, Math.min(parseFloat(el.style.top) || 0, window.innerHeight - el.offsetHeight - margin));
            el.style.left = Math.round(left) + 'px';
            el.style.top = Math.round(top) + 'px';
            el.style.maxWidth = Math.max(0, window.innerWidth - left - margin) + 'px';
            el.style.maxHeight = Math.max(0, window.innerHeight - top - margin) + 'px';
          }}

          // Applies the size saved from an earlier resize and saves new sizes, but only
          // while the user is dragging the resize handle. Shrinks forced by the viewport
          // are never saved, so the preferred size survives a temporarily small window.
          function setupComposerResize(el, rect) {{
            var margin = 10;
            var saved = loadComposerSize();
            if (saved) {{
              el.style.width = Math.min(saved.width, window.innerWidth - 2 * margin) + 'px';
              el.style.height = Math.min(saved.height, window.innerHeight - 2 * margin) + 'px';
            }}
            document.body.appendChild(el);
            var top = rect.bottom + 8;
            if (top + el.offsetHeight > window.innerHeight - margin) {{
              top = Math.max(margin, rect.top - el.offsetHeight - 8);
            }}
            el.style.left = Math.round(rect.left) + 'px';
            el.style.top = Math.round(top) + 'px';
            fitComposerToViewport(el);

            var dragging = false;
            function onPointerDown() {{ dragging = true; }}
            function onPointerUp() {{ dragging = false; }}
            function onWindowResize() {{ fitComposerToViewport(el); }}
            var observer = new ResizeObserver(function() {{
              if (dragging) {{ saveComposerSize(el.offsetWidth, el.offsetHeight); }}
            }});
            el.addEventListener('pointerdown', onPointerDown);
            window.addEventListener('pointerup', onPointerUp);
            window.addEventListener('pointercancel', onPointerUp);
            window.addEventListener('mouseup', onPointerUp);
            window.addEventListener('resize', onWindowResize);
            observer.observe(el);
            composerCleanup = function() {{
              observer.disconnect();
              window.removeEventListener('pointerup', onPointerUp);
              window.removeEventListener('pointercancel', onPointerUp);
              window.removeEventListener('mouseup', onPointerUp);
              window.removeEventListener('resize', onWindowResize);
            }};
          }}

          function wireDiffSelection(table, pagePath) {{
            table.addEventListener('mouseup', function() {{
              setTimeout(function() {{ handleDiffSelection(table, pagePath); }}, 0);
            }});
          }}

          function handleDiffSelection(table, pagePath) {{
            removeDiffComposer();
            if (!CAN_COMMENT) {{
              removeFloatingAddBtn();
              return;
            }}
            var selection = window.getSelection();
            if (!selection || selection.isCollapsed || selection.rangeCount === 0) {{
              removeFloatingAddBtn();
              return;
            }}
            var range = selection.getRangeAt(0);
            if (!table.contains(range.commonAncestorContainer)) {{
              removeFloatingAddBtn();
              return;
            }}
            var startRow = closestDiffRow(range.startContainer);
            var endRow = closestDiffRow(range.endContainer);
            if (!startRow || !endRow) {{
              removeFloatingAddBtn();
              return;
            }}
            var startIdx = orderedKeys.indexOf(startRow.dataset.diffKey);
            var endIdx = orderedKeys.indexOf(endRow.dataset.diffKey);
            var startKey = startIdx <= endIdx ? startRow.dataset.diffKey : endRow.dataset.diffKey;
            var endKey = startIdx <= endIdx ? endRow.dataset.diffKey : startRow.dataset.diffKey;
            var selectedText = selection.toString();

            removeFloatingAddBtn();
            var rect = range.getBoundingClientRect();
            floatingAddBtn = document.createElement('button');
            floatingAddBtn.type = 'button';
            floatingAddBtn.className = 'diff-add-comment-btn';
            floatingAddBtn.textContent = 'Add comment';
            floatingAddBtn.style.top = Math.max(8, rect.top - 34) + 'px';
            floatingAddBtn.style.left = Math.max(8, rect.left) + 'px';
            floatingAddBtn.addEventListener('mousedown', function(e) {{ e.preventDefault(); }});
            floatingAddBtn.addEventListener('click', function() {{
              showDiffComposer(rect, pagePath, startKey, endKey, selectedText);
            }});
            document.body.appendChild(floatingAddBtn);
          }}

          function showDiffComposer(rect, pagePath, startKey, endKey, selectedText) {{
            removeFloatingAddBtn();
            removeDiffComposer();
            diffComposer = document.createElement('div');
            diffComposer.className = 'diff-add-comment-composer';
            diffComposer.innerHTML =
              '<input type="text" class="diff-composer-reviewer" placeholder="Your name" value="' + escHtml(getReviewerName()) + '">' +
              '<textarea class="diff-composer-body" placeholder="Leave a comment" rows="3"></textarea>' +
              '<div class="diff-composer-error subtle" hidden></div>' +
              '<div class="diff-composer-actions">' +
                '<button type="button" class="diff-composer-cancel">Cancel</button>' +
                '<button type="button" class="diff-composer-submit">Comment</button>' +
              '</div>';
            setupComposerResize(diffComposer, rect);
            var reviewerInput = diffComposer.querySelector('.diff-composer-reviewer');
            var textarea = diffComposer.querySelector('.diff-composer-body');
            var errorEl = diffComposer.querySelector('.diff-composer-error');
            if (getReviewerName()) {{ textarea.focus(); }} else {{ reviewerInput.focus(); }}
            diffComposer.querySelector('.diff-composer-cancel').addEventListener('click', function() {{
              removeDiffComposer();
              window.getSelection().removeAllRanges();
            }});
            diffComposer.querySelector('.diff-composer-submit').addEventListener('click', function() {{
              var reviewer = reviewerInput.value.trim();
              var body = textarea.value.trim();
              if (!reviewer) {{
                errorEl.textContent = 'Enter your name to comment.';
                errorEl.hidden = false;
                reviewerInput.focus();
                return;
              }}
              if (!body) {{
                errorEl.textContent = 'Comment text is required.';
                errorEl.hidden = false;
                textarea.focus();
                return;
              }}
              setReviewerName(reviewer);
              fetch('/api/comments', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                  build_id: BUILD_ID,
                  page_path: pagePath,
                  reviewer: reviewer,
                  body: body,
                  selected_text: selectedText,
                  kind: 'diff',
                  diff_start_key: startKey,
                  diff_end_key: endKey,
                }}),
              }})
                .then(function(r) {{ return r.json(); }})
                .then(function(data) {{
                  removeDiffComposer();
                  window.getSelection().removeAllRanges();
                  if (data.error) {{ window.alert(data.error); return; }}
                  // Triggers the parent widget's refreshComments(), which calls
                  // back into __applyCommentHighlights with the full, correct list.
                  notifyParentActivePage(pagePath);
                }})
                .catch(function() {{ window.alert('Failed to save comment.'); }});
            }});
          }}

          list.querySelectorAll('.changed-pages-item').forEach(wireSelect);

          var initialSelect = {json.dumps(selected_page)};
          var initialItem = null;
          if (initialSelect) {{
            list.querySelectorAll('.changed-pages-item').forEach(function(li) {{
              if (!initialItem && li.dataset.path === initialSelect) {{ initialItem = li; }}
            }});
          }}
          var firstItem = initialItem || list.querySelector('.changed-pages-item');
          if (firstItem) {{
            // The standalone (non-embed) case loads its own review-client.js
            // widget, which reads window.REVIEW_CONTEXT.pagePath once,
            // synchronously, as soon as its deferred script runs — before
            // selectFile()'s own async fetch would otherwise get around to
            // setting it via notifyParentActivePage(). Set it now so the
            // widget doesn't bail out for lacking a page path on first load.
            if (!EMBED && window.REVIEW_CONTEXT) {{
              window.REVIEW_CONTEXT.pagePath = firstItem.dataset.path;
            }}
            selectFile(firstItem);
          }}
        }})();
        </script>
        """
        self._send_html(
            HTTPStatus.OK,
            html_page(f"Changed pages — {html.escape(name_raw)}", body, embed=embed),
        )

