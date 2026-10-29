from __future__ import annotations

import html
import mimetypes
import re
from http import HTTPStatus
from urllib.parse import urlparse

from docs_review_portal import import_status
from docs_review_portal.config import (
    PREVIEW_CONTEXT_COOKIE,
    PREVIEW_VERSION_COOKIE,
    RESERVED_ROOT_SEGMENTS,
    STATIC_DIR,
)
from docs_review_portal.data import (
    SITE_STORE,
    get_build_rewrite_host,
    get_build_version,
    get_latest_build_for_key,
    list_review_versions,
    review_context_for,
    version_of,
)
from docs_review_portal.helpers import (
    build_path,
    canonical_page_path,
    html_page,
    inject_review_bundle_html,
    review_path,
    rewrite_docs_domain_urls,
    sanitize_rel_path,
)

# Older versions of a review are served under /<name>/_v/<n>/, the latest at /<name>/.
_VERSION_SEGMENT = re.compile(r"^/_v/(\d+)(/.*)?$")


class ReviewPreviewMixin:
    def _send_upload_in_progress(self, tag: str) -> None:
        body = html_page(
            "Upload in progress",
            f"""<section class="panel">
              <h1>Upload in progress</h1>
              <p>The preview <strong>{html.escape(tag)}</strong> is currently being updated.</p>
              <p class="subtle">This page will refresh automatically when it&rsquo;s ready.</p>
            </section>""",
            auto_refresh=3,
        )
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Retry-After", "3")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_asset(self, rel_path: str) -> None:
        rel = sanitize_rel_path(rel_path)
        if rel is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid path")
            return
        file_path = (STATIC_DIR / rel).resolve()
        try:
            file_path.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid path")
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Asset not found")
            return
        content = file_path.read_bytes()
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_build_route(self, path: str, raw_query: str = "") -> None:
        match = re.match(r"^/builds/([^/]+)(/.*)?$", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Preview not found")
            return
        tag = match.group(1)
        req_path = match.group(2) or "/"
        location = build_path(tag, req_path)
        if raw_query:
            location = f"{location}?{raw_query}"
        self._redirect(location, status=HTTPStatus.TEMPORARY_REDIRECT)

    def _extract_tag_from_path(self, path: str) -> tuple[str, str] | None:
        stripped = path.lstrip("/")
        if not stripped:
            return None
        tag_part, _, remainder = stripped.partition("/")
        if tag_part.lower() in RESERVED_ROOT_SEGMENTS:
            return None
        if not re.fullmatch(r"[A-Za-z0-9._-]+", tag_part):
            return None
        req_path = f"/{remainder}" if remainder else "/"
        return tag_part, req_path

    def _split_version(self, req_path: str) -> tuple[int | None, str]:
        match = _VERSION_SEGMENT.match(req_path)
        if not match:
            return None, req_path
        return int(match.group(1)), match.group(2) or "/"

    def _build_exists(self, tag: str, version: int | None) -> bool:
        if version is None:
            return get_latest_build_for_key(tag) is not None
        return get_build_version(tag, version) is not None

    def _serve_build_from_tagged_path(self, path: str) -> bool:
        extracted = self._extract_tag_from_path(path)
        if not extracted:
            return False
        tag, req_path = extracted
        version, req_path = self._split_version(req_path)
        if not self._build_exists(tag, version):
            return False
        self._serve_build(tag, req_path, version)
        return True

    def _extract_context_from_referer(self) -> tuple[str, int | None] | None:
        referer = self.headers.get("Referer", "").strip()
        if not referer:
            return None
        try:
            ref_path = urlparse(referer).path
        except Exception:
            return None
        extracted = self._extract_tag_from_path(ref_path)
        if not extracted:
            return None
        tag, remainder = extracted
        version, _ = self._split_version(remainder)
        return (tag, version) if self._build_exists(tag, version) else None

    def _extract_context_from_cookie(self) -> tuple[str, int | None] | None:
        cookie = self.headers.get("Cookie", "")
        match = re.search(rf"(?:^|;\s*){re.escape(PREVIEW_CONTEXT_COOKIE)}=([A-Za-z0-9._-]+)", cookie)
        if not match:
            return None
        tag = match.group(1)
        version_match = re.search(rf"(?:^|;\s*){re.escape(PREVIEW_VERSION_COOKIE)}=(\d+)", cookie)
        version = int(version_match.group(1)) if version_match else None
        return (tag, version) if self._build_exists(tag, version) else None

    def _serve_build_from_context(self, path: str) -> bool:
        if not path.startswith("/") or path in ("/", "/favicon.ico"):
            return False
        first_segment = path.split("/", 2)[1].lower()
        if first_segment in RESERVED_ROOT_SEGMENTS:
            return False

        context = self._extract_context_from_referer() or self._extract_context_from_cookie()
        if not context:
            return False
        tag, version = context
        self._serve_build(tag, path, version)
        return True

    def _send_page_not_found(self, build, review: dict, req_path: str, rel: str) -> None:
        """404 for a page that isn't in this version. When the review has other
        versions, say which of them do have the page."""
        last_segment = rel.rsplit("/", 1)[-1]
        looks_like_page = "." not in last_segment or last_segment.lower().endswith(".html")
        if len(review["versions"]) <= 1 or not looks_like_page:
            self.send_error(HTTPStatus.NOT_FOUND, "Page not found")
            return

        def has_page(storage_tag: str) -> bool:
            candidates = [rel, f"{rel.rstrip('/')}/index.html"] if rel else ["index.html"]
            return any(SITE_STORE.read_file(storage_tag, c) is not None for c in candidates)

        key = review["reviewKey"]
        current = int(review["version"])
        latest = int(review["latestVersion"])
        page_suffix = req_path if req_path.startswith("/") else f"/{req_path}"
        elsewhere = []
        for row in reversed(list_review_versions(key)):
            number = version_of(row)
            if number != current and has_page(str(row["tag"])):
                href = html.escape(review_path(key, number, number == latest, page_suffix))
                label = f"Version {number}" + (" (latest)" if number == latest else "")
                elsewhere.append(f'<li><a href="{href}">{label}</a></li>')
        home = html.escape(review_path(key, current, current == latest))
        if elsewhere:
            where = (
                '<p class="subtle">This page exists in:</p>'
                f'<ul>{"".join(elsewhere)}</ul>'
            )
        else:
            where = '<p class="subtle">It doesn\'t exist in any other version either.</p>'
        body = html_page(
            "Page not found",
            f"""<section class="panel">
              <h1>404 Page not found</h1>
              <p>The page <strong>{html.escape(page_suffix)}</strong> doesn&rsquo;t exist in
                 version {current} of <strong>{html.escape(key)}</strong>.</p>
              {where}
              <p><a href="{home}">Go to the home page of version {current}</a></p>
            </section>""",
        )
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_build(self, tag: str, req_path: str, version: int | None = None) -> None:
        """Serve a file of a review. tag is the review name, and version picks an
        older version (None means the latest)."""
        build = get_latest_build_for_key(tag) if version is None else get_build_version(tag, version)
        if not build:
            if import_status.is_active(tag):
                self._send_upload_in_progress(tag)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown preview tag")
            return
        rel = sanitize_rel_path(req_path)
        if rel is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid path")
            return
        if req_path.endswith("/") or rel == "":
            rel = f"{rel}/index.html" if rel else "index.html"
        # Files are stored under the version's own storage tag, which differs from
        # the review name for versions after the first.
        storage_tag = str(build["tag"])
        review = review_context_for(build)
        url_prefix = review_path(
            review["reviewKey"], review["version"], review["version"] == review["latestVersion"]
        )
        resolved_rel = rel
        content = SITE_STORE.read_file(storage_tag, resolved_rel)
        if content is None:
            fallback_rel = f"{rel.rstrip('/')}/index.html"
            if fallback_rel != resolved_rel:
                content = SITE_STORE.read_file(storage_tag, fallback_rel)
                if content is not None:
                    resolved_rel = fallback_rel
        if content is None:
            if rel == "assets/review.css":
                self._serve_asset("review.css")
                return
            if rel == "assets/review-client.js":
                self._serve_asset("review-client.js")
                return
            if import_status.is_active(tag):
                self._send_upload_in_progress(tag)
            else:
                self._send_page_not_found(build, review, req_path, rel)
            return

        ctype = mimetypes.guess_type(resolved_rel)[0] or "application/octet-stream"
        if resolved_rel.lower().endswith(".html"):
            html_text = content.decode("utf-8", errors="replace")
            html_text = rewrite_docs_domain_urls(
                html_text,
                get_build_rewrite_host(build),
                url_prefix,
            )
            changed_pages_raw = build["changed_pages"] if "changed_pages" in build.keys() else None
            changed_pages_list = [ln.strip() for ln in str(changed_pages_raw).splitlines() if ln.strip()] if changed_pages_raw else []
            page_path = canonical_page_path(f"/{resolved_rel}")
            diff_pages_raw = build["diff_pages"] if "diff_pages" in build.keys() else None
            diff_pages_set = {ln.strip() for ln in str(diff_pages_raw).splitlines() if ln.strip()} if diff_pages_raw else set()
            has_diff = page_path in diff_pages_set
            html_text = inject_review_bundle_html(
                html_text,
                build_id=int(build["id"]),
                tag=review["reviewKey"],
                page_path=page_path,
                changed_pages=changed_pages_list,
                has_diff=has_diff,
                review=review,
            )
            content = html_text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Set-Cookie", f"{PREVIEW_CONTEXT_COOKIE}={review['reviewKey']}; Path=/; SameSite=Lax")
        if review["version"] == review["latestVersion"]:
            self.send_header("Set-Cookie", f"{PREVIEW_VERSION_COOKIE}=; Path=/; SameSite=Lax; Max-Age=0")
        else:
            self.send_header("Set-Cookie", f"{PREVIEW_VERSION_COOKIE}={review['version']}; Path=/; SameSite=Lax")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

