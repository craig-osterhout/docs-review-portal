from __future__ import annotations

import html
from http import HTTPStatus

from docs_review_portal.config import SOFT_DELETE_GRACE_SECONDS
from docs_review_portal.helpers import html_page

# The page is plain HTML written out here. Values that depend on the running service,
# such as its address and how long things are kept, are filled in where a token like
# __BASE__ appears, so the commands shown can be copied and run as they are.

_PUBLISH_COMMAND = """REVIEW_SERVICE_URL=__BASE__ \\
  bash <(curl -fsSL https://raw.githubusercontent.com/craig-osterhout/docs-review-portal/refs/heads/main/scripts/publish-branch.sh) <name> \\
  --docs-path /path/to/local/docs"""

_UPLOAD_CURL = """curl -X POST "__BASE__/api/builds/upload?name=my-preview" \\
  -H "Content-Type: application/gzip" \\
  --data-binary "@/tmp/my-preview.tar.gz\""""


def _code(text: str) -> str:
    return f'<pre class="code-hint" data-copy>{html.escape(text)}</pre>'


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="help-table"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _endpoint(method: str, path: str) -> str:
    return f"<code>{method} {html.escape(path)}</code>"


def _sections(base: str) -> list[tuple[str, str, str]]:
    delete_days = SOFT_DELETE_GRACE_SECONDS // (24 * 60 * 60)
    publish = _code(_PUBLISH_COMMAND.replace("__BASE__", base))
    upload_curl = _code(_UPLOAD_CURL.replace("__BASE__", base))

    changed = """
      <p>Previews uploaded with the publish script list the pages that changed on the branch and show a diff for
      each one. Click the diff icon at the top of the comments panel, then pick a page to see its diff. You can
      highlight text in a diff and comment on it.</p>
      <p>If nearly every page shows as changed, run <code>git fetch</code> and check that
      <code>origin</code> points at the main docs repository, then upload again.</p>
    """

    versions = """
      <p>Uploading again with a name that already exists adds a new <strong>version</strong> of that preview. The
      earlier versions stay available, with their comments.</p>
      <ul>
        <li>The <strong>Version</strong> dropdown in the comments panel switches between versions.</li>
        <li>You can't add new comments to an older version, but you can still reply to the ones on it.</li>
        <li>Open comments are carried over to the new version when the text they were attached to is still on
        the same page. The ones that can't be carried over stay on the old version and are marked
        <strong>outdated</strong>.</li>
      </ul>
    """

    statuses = """
      <p>Every comment is open or resolved. A comment is also <strong>outdated</strong> when it was left on an
      earlier version and didn't carry over to the latest one.</p>
      _STATUS_TABLE_
    """.replace(
        "_STATUS_TABLE_",
        _table(
            ["Status", "Meaning"],
            [
                ["Open", "Not resolved yet."],
                ["Resolved", "Marked resolved by someone."],
                ["Outdated", "Left on an earlier version and not carried over to the latest one."],
            ],
        ),
    )

    upload = """
      <p>The easiest way is the publish script, which builds the docs site, packages it and uploads it.
      Replace <code>&lt;name&gt;</code> with a name for the preview, such as your branch name, and
      <code>/path/to/local/docs</code> with the path to your local docs repository.</p>
      _PUBLISH_
      <ul>
        <li>Docker needs to be running, because the script builds the site with it.</li>
        <li>Running it again with the same name adds a new version.</li>
      </ul>
      <p>Any other static site can be uploaded with the API, see below.</p>
    """.replace("_PUBLISH_", publish)

    managing = """
      <p>The icons in the <strong>Actions</strong> column of the <a href="/previews">Previews</a> page open the
      changed pages and diffs, export a preview's comments as a CSV file, and delete a preview.</p>
      <ul>
        <li>Deleting a preview schedules it for removal. It is greyed out and removed after __DAYS__ days, and
        <strong>Restore</strong> brings it back before then.</li>
        <li><strong>Delete immediately</strong> removes it for good.</li>
        <li>Deleting removes all of the preview's versions and comments.</li>
        <li>Deleting previews can only be done in the web page, not through the API.</li>
      </ul>
    """.replace("__DAYS__", str(delete_days))

    api_notes = """
      <ul>
        <li>Requests and responses are JSON, except the upload, which streams plain text, and the CSV export.</li>
        <li>Every JSON response has an <code>ok</code> field. Errors look like
        <code>{"ok": false, "error": "message"}</code> and come with a 4xx status.</li>
        <li>There is no authentication.</li>
        <li>A preview has an <code>id</code> for each version. A <strong>review</strong> is the name with all its
        versions, and any version's id selects it.</li>
        <li>There is no API to delete, schedule for deletion or restore previews. Those are actions for a
        person in the web page.</li>
      </ul>
    """

    api_previews = _table(
        ["Method and path", "Description"],
        [
            [_endpoint("GET", "/api/builds"), "Every preview, one entry per version, with comment counts, <code>review_key</code>, <code>version</code>, <code>is_latest</code> and <code>site_url</code>."],
            [_endpoint("GET", "/api/builds/<id>"), "One preview with its <code>changed_pages</code> and <code>diff_pages</code>, and its review with every version and comment totals."],
            [_endpoint("GET", "/api/reviews"), "One entry per review, with its versions and comment totals."],
            [_endpoint("GET", "/api/reviews/<name>"), "One review by name."],
            [_endpoint("GET", "/api/builds/<id>/changed-pages"), "The pages listed as changed, and which have a stored diff."],
            [_endpoint("POST", "/api/builds/<id>/changed-pages"), 'Replace the changed pages list. Body: <code>{"pages": ["/path/", ...]}</code>.'],
            [_endpoint("GET", "/api/builds/<id>/page-diff?page=<path>"), "The stored diff for a page, as plain text."],
            [_endpoint("GET", "/api/imports"), "Uploads in progress or recently failed, optionally for one <code>name</code>."],
        ],
    )

    api_upload = (
        "<p><code>POST /api/builds/upload</code> takes a <code>.tar.gz</code> of the built site as the request body.</p>"
        + upload_curl
        + _table(
            ["Query parameter", "Meaning"],
            [
                ["<code>name</code>", "Required. The preview name, turned into a web safe name."],
                ["<code>mode</code>", "<code>new_version</code> (the default) creates the next version when the name exists. <code>overwrite</code> replaces the latest version in place and keeps its comments."],
                ["<code>rewrite_host</code>", "A host whose absolute links should become relative, such as <code>docs.docker.com</code>. Use <code>off</code> to turn it off."],
                ["<code>source_ref</code>", "An optional label stored with the preview, such as a git commit."],
                ["<code>chunk</code> and <code>chunks</code>", "For archives over about 30 MiB. Send the file in parts, each with its zero based <code>chunk</code> number and the total <code>chunks</code>. The publish script does this for you."],
            ],
        )
        + "<p>The response streams progress lines that start with <code>status:</code>, and ends with a "
        "<code>result:</code> line holding JSON on success, or an <code>error:</code> line on failure.</p>"
    )

    api_comments = _table(
        ["Method and path", "Description"],
        [
            [_endpoint("GET", "/api/comments?build_id=<id>&page_path=<path>"), "The comments on one page, with replies."],
            [_endpoint("GET", "/api/comments"), "Comments across pages, with the filters below."],
            [_endpoint("GET", "/api/comments/<id>"), "One comment or reply. A comment that starts a thread includes its replies."],
            [_endpoint("POST", "/api/comments"), "Create a comment. Not allowed on an older version, replies are."],
            [_endpoint("POST", "/api/comments/<id>/reply"), 'Reply. Body: <code>{"reviewer": "...", "body": "..."}</code>.'],
            [_endpoint("POST", "/api/comments/<id>/resolve"), 'Resolve or reopen. Body: <code>{"resolved": true}</code>.'],
            [_endpoint("POST", "/api/comments/<id>/edit"), 'Change the text. Body: <code>{"body": "..."}</code>.'],
            [_endpoint("POST", "/api/comments/<id>/delete"), "Delete a comment and its replies."],
            [_endpoint("GET", "/api/builds/<id>/comments.csv"), "Every comment and reply on every version of the review, as CSV."],
        ],
    ) + "<h3>Filters for listing comments</h3><p>Used by <code>GET /api/comments</code> when no <code>page_path</code> is given. All are optional.</p>" + _table(
        ["Parameter", "Meaning"],
        [
            ["<code>build_id</code> or <code>review</code>", "Limit to one review, by any version's id or by name."],
            ["<code>version</code>", "<code>latest</code> (the default), <code>all</code>, or a version number."],
            ["<code>status</code>", "<code>open</code>, <code>outdated</code> or <code>resolved</code>. Repeat it to match any of them."],
            ["<code>reviewer</code>", "Part of the reviewer's name."],
            ["<code>from</code> and <code>to</code>", "Unix time, or a date such as <code>2026-10-01</code>."],
            ["<code>sort</code> and <code>dir</code>", "<code>preview</code>, <code>page_path</code>, <code>reviewer</code>, <code>created_at</code>, <code>resolved</code> or <code>version</code>, and <code>asc</code> or <code>desc</code>."],
            ["<code>limit</code> and <code>offset</code>", "Up to 1000 per request, 200 by default. The response has <code>total</code> and <code>has_more</code>."],
        ],
    )

    api_other = _table(
        ["Method and path", "Description"],
        [
            [_endpoint("GET", "/api/logs"), "Log entries, newest first. Filters: <code>level</code> (<code>all</code>, <code>warning</code>, <code>error</code>), <code>q</code> to search the text, <code>limit</code> and <code>page</code>."],
        ],
    )

    api = (
        api_notes
        + "<h3>Previews and reviews</h3>" + api_previews
        + "<h3>Uploading</h3>" + api_upload
        + "<h3>Comments</h3>" + api_comments
        + "<h3>System logs</h3>" + api_other
    )

    return [
        ("changed-pages", "Changed pages and diffs", changed),
        ("versions", "Versions", versions),
        ("statuses", "Comment statuses", statuses),
        ("upload", "Uploading a preview", upload),
        ("managing", "Managing previews", managing),
        ("api", "API reference", api),
    ]


class ReviewHelpMixin:
    def _render_help_page(self) -> None:
        base = self._public_base()
        sections = _sections(base)
        toc = "".join(f'<li><a href="#{sid}">{html.escape(title)}</a></li>' for sid, title, _ in sections)
        content = "".join(
            f'<section class="panel help-section" id="{sid}"><h2>{html.escape(title)}</h2>{body}</section>'
            for sid, title, body in sections
        )
        body = f"""
        <section class="panel">
          <h1 style="margin:0 0 4px">Help</h1>
          <p class="subtle" style="margin:0">How to review, upload and manage previews, and how to use the API.</p>
        </section>
        <div class="help-layout">
          <nav class="help-toc panel" aria-label="On this page">
            <strong>On this page</strong>
            <ul>{toc}</ul>
          </nav>
          <div class="help-content">{content}</div>
        </div>
        <script src="/_review/assets/help.js"></script>
        """
        self._send_html(HTTPStatus.OK, html_page("Help", body))
