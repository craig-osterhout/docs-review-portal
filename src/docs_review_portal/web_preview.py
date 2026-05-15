from __future__ import annotations

import html
import mimetypes
import re
from http import HTTPStatus
from urllib.parse import urlparse

from docs_review_portal import import_status
from docs_review_portal.config import PREVIEW_CONTEXT_COOKIE, RESERVED_ROOT_SEGMENTS, STATIC_DIR
from docs_review_portal.data import SITE_STORE, get_build_by_tag, get_build_rewrite_host
from docs_review_portal.helpers import (
    build_path,
    canonical_page_path,
    html_page,
    inject_review_bundle_html,
    rewrite_docs_domain_urls,
    sanitize_rel_path,
)


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

    def _serve_build_from_tagged_path(self, path: str) -> bool:
        extracted = self._extract_tag_from_path(path)
        if not extracted:
            return False
        tag, req_path = extracted
        if not get_build_by_tag(tag):
            return False
        self._serve_build(tag, req_path)
        return True

    def _extract_tag_from_referer(self) -> str | None:
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
        tag, _ = extracted
        return tag if get_build_by_tag(tag) else None

    def _extract_tag_from_cookie(self) -> str | None:
        cookie = self.headers.get("Cookie", "")
        match = re.search(rf"(?:^|;\s*){re.escape(PREVIEW_CONTEXT_COOKIE)}=([A-Za-z0-9._-]+)", cookie)
        if not match:
            return None
        tag = match.group(1)
        return tag if get_build_by_tag(tag) else None

    def _serve_build_from_context(self, path: str) -> bool:
        if not path.startswith("/") or path in ("/", "/favicon.ico"):
            return False
        first_segment = path.split("/", 2)[1].lower()
        if first_segment in RESERVED_ROOT_SEGMENTS:
            return False

        tag = self._extract_tag_from_referer() or self._extract_tag_from_cookie()
        if not tag:
            return False
        self._serve_build(tag, path)
        return True

    def _serve_build(self, tag: str, req_path: str) -> None:
        build = get_build_by_tag(tag)
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
        resolved_rel = rel
        content = SITE_STORE.read_file(tag, resolved_rel)
        if content is None:
            fallback_rel = f"{rel.rstrip('/')}/index.html"
            if fallback_rel != resolved_rel:
                content = SITE_STORE.read_file(tag, fallback_rel)
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
                self.send_error(HTTPStatus.NOT_FOUND, "Page not found")
            return

        ctype = mimetypes.guess_type(resolved_rel)[0] or "application/octet-stream"
        if resolved_rel.lower().endswith(".html"):
            html_text = content.decode("utf-8", errors="replace")
            html_text = rewrite_docs_domain_urls(
                html_text,
                get_build_rewrite_host(build),
                build_path(tag),
            )
            html_text = inject_review_bundle_html(
                html_text,
                build_id=int(build["id"]),
                tag=str(build["tag"]),
                page_path=canonical_page_path(f"/{resolved_rel}"),
            )
            content = html_text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Set-Cookie", f"{PREVIEW_CONTEXT_COOKIE}={tag}; Path=/; SameSite=Lax")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

