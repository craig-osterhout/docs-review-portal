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
    delete_build_and_comments,
    fetch_build_comments_for_export,
    fetch_build_for_export,
    fetch_feedback,
    get_build_by_id,
    list_builds,
    mark_build_archived,
    restore_archived_build,
    set_comment_resolved,
)
from docs_review_portal.helpers import (
    build_path,
    build_url,
    canonical_page_path,
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
                      <a class="icon-action" href="/previews/{build_id}/changed-pages" title="Changed pages" aria-label="Changed pages">
                        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                          <path d="M8 6h13"></path><path d="M8 12h13"></path><path d="M8 18h13"></path>
                          <path d="M3 6h.01"></path><path d="M3 12h.01"></path><path d="M3 18h.01"></path>
                        </svg>
                      </a>
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
        <section class="panel" style="display:flex;align-items:center;justify-content:space-between;gap:12px">
          <h1 style="margin:0">Previews</h1>
          <button type="button" id="open-upload-dialog">Upload preview</button>
        </section>
        <dialog id="upload-dialog" aria-labelledby="upload-dialog-title">
          <div class="dialog-header">
            <h2 id="upload-dialog-title">Upload preview</h2>
            <button type="button" class="dialog-close" id="close-upload-dialog" aria-label="Close">&times;</button>
          </div>
          <div class="tab-bar" role="tablist">
            <button type="button" class="tab-btn active" role="tab" aria-selected="true" aria-controls="tab-upload" data-tab="upload">Upload</button>
            <button type="button" class="tab-btn" role="tab" aria-selected="false" aria-controls="tab-cli" data-tab="cli">Command line</button>
          </div>
          <div id="tab-upload" class="tab-panel" role="tabpanel">
            <ol class="upload-steps">
              <li>
                <strong>Prepare archive</strong>
                <p class="subtle" style="margin:6px 0 4px">Docker docs site &mdash; run from the repo root:</p>
                <pre class="code-hint">docker buildx bake release --set "release.output=type=local,dest=/tmp/preview-site" &amp;&amp; \
git diff origin/main...HEAD --name-only --diff-filter=ACM -- content/ | sed 's|^content||;s|/_index\.md$|/|;s|/index\.md$|/|;s|\.md$|/|;s|^/manuals/|/|' | sort -u &gt; /tmp/preview-site/.changed-pages &amp;&amp; \
tar -C /tmp/preview-site -czf /tmp/my-preview.tar.gz . &amp;&amp; rm -rf /tmp/preview-site</pre>
                <p class="subtle" style="margin:8px 0 4px">Other static sites (output in <code>dist/</code>):</p>
                <pre class="code-hint">tar -C dist -czf /tmp/my-preview.tar.gz .</pre>
              </li>
              <li>
                <strong>Upload</strong>
                <form id="upload-form" class="grid-form" style="margin-top:10px">
                  <label>
                    Preview name
                    <input type="text" name="name" required placeholder="my-feature-branch">
                  </label>
                  <label>
                    Archive (.tar.gz)
                    <input type="file" name="archive" accept=".tar.gz,.tgz" required>
                  </label>
                  <label>
                    Rewrite host <span class="subtle">(optional)</span>
                    <input type="text" name="rewrite_host" placeholder="docs.docker.com">
                  </label>
                  <div style="align-self:end">
                    <button type="submit">Upload</button>
                  </div>
                </form>
                <div id="upload-progress" hidden>
                  <div class="upload-bar-track"><div class="upload-bar" id="upload-bar"></div></div>
                  <p id="upload-status" class="subtle" style="margin:4px 0 0"></p>
                </div>
              </li>
            </ol>
          </div>
          <div id="tab-cli" class="tab-panel" role="tabpanel" hidden>
            <p class="subtle" style="margin:16px 0 4px"><strong>Publish script</strong> &mdash; Docker docs site only. Builds, archives, and uploads in one step:</p>
            <pre class="code-hint">curl -fsSL https://raw.githubusercontent.com/craig-osterhout/docs-review-portal/refs/heads/main/scripts/publish-branch.sh | sh -s -- my-preview --docs-path ~/path/to/docs</pre>
            <hr class="dialog-rule">
            <p class="subtle" style="margin:14px 0 4px"><strong>API</strong> &mdash; any static site. Package your built output, then upload:</p>
            <p class="subtle" style="margin:10px 0 4px">1. Package:</p>
            <pre class="code-hint">tar -C dist -czf /tmp/my-preview.tar.gz .</pre>
            <p class="subtle" style="margin:12px 0 4px">2. Upload:</p>
            <pre class="code-hint">curl -X POST "{html.escape(self._public_base())}/api/builds/upload?name=my-preview" \
  -H "Content-Type: application/gzip" \
  --data-binary "@/tmp/my-preview.tar.gz"</pre>
            <p class="subtle" style="margin:10px 0 0">For archives larger than ~30 MiB, use the <strong>Upload tab</strong> instead &mdash; it handles chunking automatically.</p>
          </div>
        </dialog>
        <script src="/_review/assets/upload.js"></script>
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
        embed = (query.get("embed", ["0"])[0] or "0") == "1"
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
            if embed:
                params["embed"] = "1"
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
                    <a class="comment-page-link" href="{html.escape(location)}" target="_blank" rel="noreferrer">{html.escape(str(comment['page_path']))}</a>
                    <div class="subtle">lines {comment.get('line_start') or '-'} - {comment.get('line_end') or '-'}</div>
                  </td>
                  <td>{html.escape(str(comment['reviewer']))}</td>
                  <td>{comment['created_at_human']}</td>
                  <td>{status_markup}</td>
                  <td>
                    <a class="comment-page-link" href="{html.escape(location)}" target="_blank" rel="noreferrer">{html.escape(str(comment['body']))}</a>
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
            {'<input type="hidden" name="embed" value="1">' if embed else ''}
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

        tag = str(build["tag"])
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
            preview_link = html.escape(build_url(tag, p, base=self._public_base()))
            return f"""<button type="button" class="file-select-btn" title="{p_esc}">
                <span class="file-status-badge {status_cls}" title="{status_title}">{status_label}</span>
                <span class="file-path">{label_esc}</span>
              </button>
              <a class="icon-action open-preview-link" href="{preview_link}" target="_blank" rel="noreferrer" title="Open preview" aria-label="Open preview for {p_esc}">&#8599;</a>"""

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
             {f'<span class="subtle">({html.escape(tag)})</span>' if name_raw != tag else ''}</p>
          <p class="subtle" id="page-count">{len(pages)} page{"s" if len(pages) != 1 else ""}</p>

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
        <script>
        (function() {{
          var BUILD_ID = {build_id};
          var tag = {json.dumps(tag)};
          var baseUrl = {json.dumps(self._public_base())};
          var EMBED = {json.dumps(embed)};
          var list = document.getElementById('changed-pages-list');
          var diffViewer = document.getElementById('diff-viewer');
          var activePath = null;

          if (EMBED && window.parent && window.parent !== window) {{
            list.addEventListener('click', function(e) {{
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
            return baseUrl + '/' + tag + p;
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
                + '<a href="' + escHtml(buildUrlFor(pagePath)) + '" target="_blank" rel="noreferrer">Open preview</a></div>';
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
              }})
              .catch(function() {{
                if (activePath !== pagePath) {{ return; }}
                diffViewer.innerHTML = '<div class="diff-error subtle">Failed to load diff.</div>';
              }});
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

          function makeRow(oldNo, newNo, marker, rowClass) {{
            var tr = document.createElement('tr');
            if (rowClass) {{ tr.className = rowClass; }}
            var oldTd = document.createElement('td'); oldTd.className = 'diff-line-no'; oldTd.textContent = oldNo || '';
            var newTd = document.createElement('td'); newTd.className = 'diff-line-no'; newTd.textContent = newNo || '';
            var markTd = document.createElement('td'); markTd.className = 'diff-marker'; markTd.textContent = marker || '';
            var contentTd = document.createElement('td'); contentTd.className = 'diff-content';
            tr.appendChild(oldTd); tr.appendChild(newTd); tr.appendChild(markTd); tr.appendChild(contentTd);
            return {{tr: tr, contentTd: contentTd}};
          }}

          var DIFF_EDGE_CONTEXT = 3;
          var DIFF_COLLAPSE_MIN_HIDDEN = 3;

          function makeExpandRow(hiddenTrs) {{
            var tr = document.createElement('tr');
            tr.className = 'diff-row-expand';
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
            btn.addEventListener('click', function() {{
              hiddenTrs.forEach(function(hiddenTr) {{ tr.parentNode.insertBefore(hiddenTr, tr); }});
              tr.remove();
            }});
            td.appendChild(btn);
            tr.appendChild(td);
            return tr;
          }}

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
            head.forEach(function(tr) {{ tbody.appendChild(tr); }});
            tbody.appendChild(makeExpandRow(hidden));
            tail.forEach(function(tr) {{ tbody.appendChild(tr); }});
            buffer.length = 0;
          }}

          function renderDiff(text, pagePath) {{
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
            return wrap;
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
          if (firstItem) {{ selectFile(firstItem); }}
        }})();
        </script>
        """
        self._send_html(
            HTTPStatus.OK,
            html_page(f"Changed pages — {html.escape(name_raw)}", body, embed=embed),
        )

