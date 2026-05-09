from __future__ import annotations

import html
import json
import tempfile
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from docs_review_portal.config import PUBLIC_PORT
from docs_review_portal.helpers import html_page


class ReviewCommonMixin:
    def _public_base(self) -> str:
        host = self.headers.get("Host") or f"localhost:{PUBLIC_PORT}"
        scheme = self.headers.get("X-Forwarded-Proto") or ("https" if "443" in host else "http")
        return f"{scheme}://{host}"
    def _handle_exception(self, exc: Exception) -> None:
        if self.path.startswith("/api/"):
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        body = html_page("Error", f"<h1>Request failed</h1><p>{html.escape(str(exc))}</p>")
        self._send_html(HTTPStatus.INTERNAL_SERVER_ERROR, body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, status: HTTPStatus, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_csv(self, status: HTTPStatus, filename: str, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode())
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _redirect(self, location: str, status: HTTPStatus = HTTPStatus.SEE_OTHER) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.end_headers()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length > 0 else b""

    def _read_body_to_temp_file(self) -> Path:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("request body is empty")
        with tempfile.NamedTemporaryFile(prefix="review-upload-", suffix=".tar", delete=False) as tmp:
            path = Path(tmp.name)
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                tmp.write(chunk)
                remaining -= len(chunk)
        if remaining > 0:
            path.unlink(missing_ok=True)
            raise ValueError("incomplete request body")
        return path

    def _read_form(self) -> dict[str, str]:
        body = self._read_body()
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        return {key: values[0] for key, values in fields.items()}

    def _read_json(self) -> dict[str, Any]:
        body = self._read_body()
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

