from __future__ import annotations

import gzip
import hashlib
import html
import mimetypes
import re
import threading
from collections import OrderedDict
from http import HTTPStatus
from urllib.parse import urlparse

from docs_review_portal import import_status
from docs_review_portal.config import (
    FILE_CACHE_MB,
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

# Types the standard library doesn't know, or gets wrong. Fonts in particular are otherwise
# sent as generic binary files.
for _ext, _type in {
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".mjs": "text/javascript",
    ".webmanifest": "application/manifest+json",
    ".map": "application/json",
    ".wasm": "application/wasm",
}.items():
    mimetypes.add_type(_type, _ext)

# How long a browser may reuse a file without asking again. Fonts and images almost never
# change at the same address. Stylesheets and scripts change more often, and pages are never
# reused because they carry the comment widget's live details.
_CACHE_STATIC_MEDIA = "public, max-age=3600"
_CACHE_OTHER_FILES = "public, max-age=300"
_CACHE_PAGES = "no-cache"
_COMPRESS_MIN_BYTES = 1024
_FILE_CACHE_LARGEST = 4 * 1024 * 1024


def _is_compressible(content_type: str) -> bool:
    return (
        content_type.startswith("text/")
        or content_type in ("application/javascript", "application/json", "application/xml", "image/svg+xml")
        or content_type.endswith(("+json", "+xml"))
    )


def _cache_control(content_type: str, is_page: bool) -> str:
    if is_page:
        return _CACHE_PAGES
    if content_type.startswith(("font/", "image/", "video/", "audio/")) and content_type != "image/svg+xml":
        return _CACHE_STATIC_MEDIA
    return _CACHE_OTHER_FILES


class _FileCache:
    """The most recently used preview files, kept in memory up to a size limit."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._items: OrderedDict[tuple, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: tuple) -> bytes | None:
        with self._lock:
            data = self._items.get(key)
            if data is not None:
                self._items.move_to_end(key)
            return data

    def put(self, key: tuple, data: bytes) -> None:
        if self._max_bytes <= 0 or len(data) > min(_FILE_CACHE_LARGEST, self._max_bytes):
            return
        with self._lock:
            if key in self._items:
                self._size -= len(self._items.pop(key))
            self._items[key] = data
            self._size += len(data)
            while self._size > self._max_bytes and self._items:
                _, evicted = self._items.popitem(last=False)
                self._size -= len(evicted)


_FILE_CACHE = _FileCache(FILE_CACHE_MB * 1024 * 1024)


class ReviewPreviewMixin:
    def _accepts_gzip(self) -> bool:
        for part in self.headers.get("Accept-Encoding", "").split(","):
            name, _, params = part.strip().partition(";")
            if name.strip().lower() in ("gzip", "x-gzip"):
                quality = re.search(r"q\s*=\s*([0-9.]+)", params)
                return not (quality and float(quality.group(1)) == 0)
        return False

    def _etag_matches(self, etag: str) -> bool:
        header = self.headers.get("If-None-Match", "")
        if not header:
            return False
        if header.strip() == "*":
            return True
        wanted = etag[2:] if etag.startswith("W/") else etag
        return any(
            (candidate.strip()[2:] if candidate.strip().startswith("W/") else candidate.strip()) == wanted
            for candidate in header.split(",")
        )

    def _send_not_modified(self, etag: str, cache_control: str) -> None:
        self.send_response(HTTPStatus.NOT_MODIFIED)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache_control)
        self.end_headers()

    def _send_file_response(
        self,
        content: bytes,
        content_type: str,
        cache_control: str,
        etag: str | None = None,
        cookies: list[str] | None = None,
    ) -> None:
        """Send a file with caching headers, compressed when the browser accepts it."""
        encoding = None
        if _is_compressible(content_type) and len(content) >= _COMPRESS_MIN_BYTES and self._accepts_gzip():
            content = gzip.compress(content, compresslevel=6, mtime=0)
            encoding = "gzip"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        if etag:
            self.send_header("ETag", etag)
        if _is_compressible(content_type):
            self.send_header("Vary", "Accept-Encoding")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

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
        info = file_path.stat()
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        etag = f'W/"asset-{info.st_mtime_ns:x}-{info.st_size:x}"'
        cache_control = _cache_control(ctype, is_page=False)
        if self._etag_matches(etag):
            self._send_not_modified(etag, cache_control)
            return
        self._send_file_response(file_path.read_bytes(), ctype, cache_control, etag=etag)

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

        # A link or a script in a preview page sometimes asks for an address that wasn't
        # rewritten to include the preview's name. The page that made the request says which
        # preview it was, and the cookie remembers the last preview opened as a backup for
        # requests that don't say. The cookie must not be used when a person navigates to an
        # address themselves, for example one for a preview that no longer exists, or they
        # would be shown a page of whichever preview they opened last.
        context = self._extract_context_from_referer()
        if not context and not self._is_navigation():
            context = self._extract_context_from_cookie()
        if not context:
            return False
        tag, version = context
        self._serve_build(tag, path, version)
        return True

    def _is_navigation(self) -> bool:
        """True when a browser is opening a page in its own right, such as an address
        typed in, a bookmark or a link from another site, as opposed to a page loading
        its own images and scripts. Browsers say which in the Sec-Fetch headers."""
        mode = self.headers.get("Sec-Fetch-Mode", "").lower()
        dest = self.headers.get("Sec-Fetch-Dest", "").lower()
        return mode == "navigate" or dest in ("document", "iframe", "frame")

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
        stamp = int(build["updated_at"] or build["created_at"])
        is_page_request = req_path.endswith("/") or "." not in rel.rsplit("/", 1)[-1] or rel.lower().endswith(".html")

        # Anything that isn't a page can be tagged without reading it, because a build's
        # files only change when the build is updated, and that changes the tag.
        etag = None
        if not is_page_request:
            digest = hashlib.blake2b(rel.encode("utf-8"), digest_size=8).hexdigest()
            etag = f'W/"{int(build["id"]):x}-{stamp:x}-{digest}"'
            guessed = mimetypes.guess_type(rel)[0] or "application/octet-stream"
            if self._etag_matches(etag):
                self._send_not_modified(etag, _cache_control(guessed, is_page=False))
                return

        review_info: dict = {}

        def review() -> dict:
            # Only pages need the review's versions and URL prefix, so assets skip the lookup.
            if not review_info:
                review_info.update(review_context_for(build))
            return review_info

        def read_file(rel_path: str) -> bytes | None:
            key = (int(build["id"]), storage_tag, rel_path, stamp)
            data = _FILE_CACHE.get(key)
            if data is None:
                data = SITE_STORE.read_file(storage_tag, rel_path)
                if data is not None:
                    _FILE_CACHE.put(key, data)
            return data

        resolved_rel = rel
        content = read_file(resolved_rel)
        if content is None:
            fallback_rel = f"{rel.rstrip('/')}/index.html"
            if fallback_rel != resolved_rel:
                content = read_file(fallback_rel)
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
                self._send_page_not_found(build, review(), req_path, rel)
            return

        ctype = mimetypes.guess_type(resolved_rel)[0] or "application/octet-stream"
        if not resolved_rel.lower().endswith(".html"):
            # Not a page: no comment widget, no cookies, and a browser may reuse it.
            tag_for_file = etag or f'W/"{int(build["id"]):x}-{stamp:x}-{hashlib.blake2b(resolved_rel.encode("utf-8"), digest_size=8).hexdigest()}"'
            self._send_file_response(content, ctype, _cache_control(ctype, is_page=False), etag=tag_for_file)
            return

        review_data = review()
        url_prefix = review_path(
            review_data["reviewKey"], review_data["version"], review_data["version"] == review_data["latestVersion"]
        )
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
            tag=review_data["reviewKey"],
            page_path=page_path,
            changed_pages=changed_pages_list,
            has_diff=has_diff,
            review=review_data,
        )
        cookies = [f"{PREVIEW_CONTEXT_COOKIE}={review_data['reviewKey']}; Path=/; SameSite=Lax"]
        if review_data["version"] == review_data["latestVersion"]:
            cookies.append(f"{PREVIEW_VERSION_COOKIE}=; Path=/; SameSite=Lax; Max-Age=0")
        else:
            cookies.append(f"{PREVIEW_VERSION_COOKIE}={review_data['version']}; Path=/; SameSite=Lax")
        self._send_file_response(
            html_text.encode("utf-8"), ctype, _cache_control(ctype, is_page=True), cookies=cookies
        )
