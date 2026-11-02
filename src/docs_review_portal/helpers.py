from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from urllib.parse import unquote

from .config import (
    DEFAULT_REWRITE_HOST,
    INJECT_END,
    INJECT_MARKER_PATTERN,
    INJECT_START,
    PUBLIC_PORT,
    SOFT_DELETE_GRACE_SECONDS,
    normalize_rewrite_host,
)


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def format_ts(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def compute_delete_at(archived_at: int | None) -> int | None:
    if not archived_at:
        return None
    return int(archived_at) + SOFT_DELETE_GRACE_SECONDS


def parse_local_datetime_to_ts(value: str | None, end_of_minute: bool = False) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end_of_minute:
        dt = dt.replace(second=59)
    return int(dt.timestamp())


def slugify_tag(tag: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", tag.strip())
    cleaned = cleaned.strip(".-_")
    if not cleaned:
        raise ValueError("Tag cannot be empty after normalization")
    return cleaned


def sanitize_rel_path(path: str) -> str | None:
    path = unquote(path)
    path = path.replace("\\", "/")
    if path.startswith("/"):
        path = path[1:]
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts)


def canonical_page_path(path: str) -> str:
    if not path or path == "/":
        return "/"
    if not path.startswith("/"):
        path = f"/{path}"
    if path.endswith("/index.html"):
        trimmed = path[: -len("/index.html")]
        return trimmed if trimmed else "/"
    if path == "/index.html":
        return "/"
    if path != "/" and path.endswith("/"):
        return path[:-1]
    return path


def rewrite_docs_domain_urls(
    content: str,
    rewrite_host: str | None = DEFAULT_REWRITE_HOST,
    preview_prefix: str = "/",
) -> str:
    # Keep generated docs local by rewriting absolute links from the preview rewrite host.
    prefix = preview_prefix if preview_prefix.startswith("/") else f"/{preview_prefix}"
    if not prefix.endswith("/"):
        prefix = f"{prefix}/"
    prefix_no_slash = prefix[1:] if prefix.startswith("/") else prefix

    normalized_host = normalize_rewrite_host(rewrite_host)
    if not normalized_host:
        normalized_host = None
    if normalized_host:
        absolute_pattern = re.compile(rf"https?://{re.escape(normalized_host)}(/)?", re.IGNORECASE)
        protocol_rel_pattern = re.compile(rf"//{re.escape(normalized_host)}(/)?", re.IGNORECASE)
        content = absolute_pattern.sub(prefix, content)
        content = protocol_rel_pattern.sub(prefix, content)

    if prefix != "/":
        attr_pattern_quoted = re.compile(
            r'(?P<lead>\b(?:href|src|action|poster)\s*=\s*["\'])/(?P<path>(?!/)[^"\']*)',
            re.IGNORECASE,
        )
        attr_pattern_unquoted = re.compile(
            # Minified HTML often leaves quotes off, including for a link to the site root
            # (href=/), so the path after the slash may be empty here just as it can be above.
            r'(?P<lead>\b(?:href|src|action|poster)\s*=\s*)/(?P<path>(?!/)[^\s"\'`>]*)',
            re.IGNORECASE,
        )
        css_url_pattern = re.compile(r'url\((?P<quote>["\']?)/(?P<path>(?!/)[^)"\']*)(?P=quote)\)')

        def _rewrite_path(path: str) -> str:
            lowered = path.lower()
            if (
                lowered.startswith("_review/")
                or lowered.startswith("api/")
                or lowered.startswith("previews")
                or lowered.startswith("comments")
                or lowered.startswith("healthz")
                or lowered.startswith("manage")
                or lowered.startswith("publications")
                or lowered.startswith("feedback")
                or lowered.startswith(prefix_no_slash.lower())
            ):
                return f"/{path}"
            return f"{prefix}{path}"

        content = attr_pattern_quoted.sub(
            lambda m: f"{m.group('lead')}{_rewrite_path(m.group('path'))}",
            content,
        )
        content = attr_pattern_unquoted.sub(
            lambda m: f"{m.group('lead')}{_rewrite_path(m.group('path'))}",
            content,
        )
        content = css_url_pattern.sub(
            lambda m: f"url({m.group('quote')}{_rewrite_path(m.group('path'))}{m.group('quote')})",
            content,
        )
    return content


def inject_review_bundle_html(
    content: str,
    build_id: int,
    tag: str,
    page_path: str,
    changed_pages: list[str] | None = None,
    has_diff: bool = False,
    review: dict | None = None,
) -> str:
    context = {
        "buildId": build_id,
        "buildTag": tag,
        "pagePath": page_path,
        "changedPages": changed_pages or [],
        "hasDiff": has_diff,
    }
    if review:
        # Version details for reviews that have more than one version: the review
        # key, which version this page belongs to, and the URL prefix to stay inside it.
        context.update(review)
    inject = (
        f"{INJECT_START}\n"
        '<link rel="stylesheet" href="/_review/assets/review.css">\n'
        f"<script>window.REVIEW_CONTEXT={json.dumps(context)}</script>\n"
        '<script defer src="/_review/assets/review-client.js"></script>\n'
        f"{INJECT_END}\n"
    )
    content = INJECT_MARKER_PATTERN.sub("", content)
    body_idx = content.lower().rfind("</body>")
    if body_idx >= 0:
        return f"{content[:body_idx]}{inject}{content[body_idx:]}"
    return f"{content}\n{inject}"


def build_path(tag: str, path: str = "/") -> str:
    safe_tag = slugify_tag(tag)
    suffix = path if path.startswith("/") else f"/{path}"
    if suffix == "/":
        return f"/{safe_tag}/"
    return f"/{safe_tag}{suffix}"


def review_path(key: str, version: int | None = None, is_latest: bool = True, path: str = "/") -> str:
    """URL path for a page of a review. The latest version lives at /<key>/ and
    older versions live under /<key>/_v/<n>/ so links keep working after new uploads."""
    safe_key = slugify_tag(key)
    suffix = path if path.startswith("/") else f"/{path}"
    base = f"/{safe_key}" if is_latest or not version else f"/{safe_key}/_v/{int(version)}"
    return f"{base}/" if suffix == "/" else f"{base}{suffix}"


def review_url(
    key: str,
    version: int | None = None,
    is_latest: bool = True,
    path: str = "/",
    base: str | None = None,
) -> str:
    root = base.rstrip("/") if base else f"http://localhost:{PUBLIC_PORT}"
    return f"{root}{review_path(key, version, is_latest, path)}"


def build_host(tag: str) -> str:
    return f"localhost:{PUBLIC_PORT}{build_path(tag)}"


def build_url(tag: str, path: str = "/", base: str | None = None) -> str:
    root = base.rstrip("/") if base else f"http://localhost:{PUBLIC_PORT}"
    return f"{root}{build_path(tag, path)}"


def html_page(title: str, body: str, auto_refresh: int | None = None, embed: bool = False) -> str:
    refresh = f'  <meta http-equiv="refresh" content="{auto_refresh}">\n' if auto_refresh else ""
    header = (
        ""
        if embed
        else """<header class="topbar">
    <nav class="nav">
      <a href="/previews">Previews</a>
      <a href="/comments">Comments</a>
      <a href="/logs">System logs</a>
      <a href="/help">Help</a>
    </nav>
  </header>
  """
    )
    main_class = "app-shell app-shell-embed" if embed else "app-shell"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="/_review/assets/app.css">
</head>
<body>
  {header}<main class="{main_class}">
    {body}
  </main>
</body>
</html>
"""


_URL_PATTERN = re.compile(r"https?://[^\s<>]+", flags=re.IGNORECASE)


def linkify_html(text: str) -> str:
    """Escape text and turn http(s) URLs in it into links that open in a new tab."""
    parts: list[str] = []
    last = 0
    for match in _URL_PATTERN.finditer(text):
        url = match.group(0)
        # Leave trailing punctuation (and unbalanced closing brackets) outside the link.
        while url and (url[-1] in ".,;:!?'\"" or (url[-1] in ")]}" and url.count(url[-1]) > url.count({")": "(", "]": "[", "}": "{"}[url[-1]]))):
            url = url[:-1]
        if not re.match(r"https?://[^/\s]", url, flags=re.IGNORECASE):
            continue
        parts.append(html.escape(text[last:match.start()]))
        parts.append(
            f'<a class="comment-link" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">'
            f"{html.escape(url)}</a>"
        )
        last = match.start() + len(url)
    parts.append(html.escape(text[last:]))
    return "".join(parts)
