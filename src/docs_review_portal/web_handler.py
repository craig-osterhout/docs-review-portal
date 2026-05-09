#!/usr/bin/env python3
from __future__ import annotations

import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from docs_review_portal.config import HOST, PORT
from docs_review_portal.data import init_storage, purge_expired_archived_builds
from docs_review_portal import log_buffer
from docs_review_portal.web_api import ReviewApiMixin
from docs_review_portal.web_common import ReviewCommonMixin
from docs_review_portal.web_pages import ReviewPagesMixin
from docs_review_portal.web_preview import ReviewPreviewMixin


class ReviewHandler(
    ReviewApiMixin,
    ReviewPagesMixin,
    ReviewPreviewMixin,
    ReviewCommonMixin,
    BaseHTTPRequestHandler,
):
    server_version = "docs-review/0.1"

    def do_GET(self) -> None:  # noqa: N802
        try:
            self.route_get()
        except Exception as exc:  # pragma: no cover
            self._handle_exception(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self.route_post()
        except Exception as exc:  # pragma: no cover
            self._handle_exception(exc)

    def route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=False)
        if not path.startswith("/_review/assets/"):
            try:
                purge_expired_archived_builds()
            except Exception:
                pass

        if path.startswith("/_review/assets/"):
            self._serve_asset(path[len("/_review/assets/") :])
            return

        if path == "/":
            self._redirect("/previews")
            return
        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if path == "/manage":
            self._redirect("/previews")
            return
        if path == "/publications":
            self._redirect("/previews")
            return
        if re.match(r"^/(?:publications|previews)/\d+/comments\.csv$", path):
            self._export_preview_comments_csv(path)
            return
        if path == "/previews":
            self._render_previews_page()
            return
        if path == "/feedback":
            self._redirect("/comments")
            return
        if path == "/comments":
            self._render_comments_page(query)
            return
        if path.startswith("/builds/"):
            self._serve_build_route(path, parsed.query)
            return
        if path == "/api/builds":
            self._api_get_builds()
            return
        if path == "/api/comments":
            self._api_get_comments(query)
            return
        if path == "/logs":
            self._render_logs_page()
            return
        if self._serve_build_from_tagged_path(path):
            return
        if self._serve_build_from_context(path):
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Route not found")

    def route_post(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=False)
        try:
            purge_expired_archived_builds()
        except Exception:
            pass

        if (path.startswith("/publications/") or path.startswith("/previews/")) and path.endswith("/delete"):
            self._delete_preview(path)
            return
        if (path.startswith("/publications/") or path.startswith("/previews/")) and path.endswith("/destroy"):
            self._destroy_preview(path)
            return
        if (path.startswith("/publications/") or path.startswith("/previews/")) and path.endswith("/restore"):
            self._restore_preview(path)
            return
        if path.startswith("/comments/items/") and path.endswith("/resolve"):
            self._feedback_toggle_resolved(path)
            return
        if path.startswith("/comments/items/") and path.endswith("/reply"):
            self._feedback_reply(path)
            return
        if path == "/api/builds/upload":
            self._api_upload_build_archive(query)
            return
        if path == "/api/comments":
            self._api_create_comment()
            return
        if path.startswith("/api/comments/") and path.endswith("/resolve"):
            self._api_resolve_comment(path)
            return
        if path.startswith("/api/comments/") and path.endswith("/reply"):
            self._api_reply_comment(path)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Route not found")


def serve() -> None:
    log_buffer.setup()
    import logging
    from docs_review_portal.config import BUILD_VERSION, DATA_DIR, DB_BACKEND, SITE_STORAGE_BACKEND
    logging.getLogger(__name__).info(
        "starting: version=%s storage=%s data_dir=%s db=%s", BUILD_VERSION, SITE_STORAGE_BACKEND, DATA_DIR, DB_BACKEND
    )
    init_storage()
    try:
        purge_expired_archived_builds()
    except Exception:
        pass
    server = ThreadingHTTPServer((HOST, PORT), ReviewHandler)
    print(f"Docs review service running on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    serve()
