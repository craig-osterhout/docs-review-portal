from __future__ import annotations

import os
import re
from pathlib import Path


def normalize_rewrite_host(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.startswith("//"):
        raw = raw[2:]
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    raw = raw.split("/", 1)[0].strip().lower().strip(".")
    return raw or None


SOURCE_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = SOURCE_ROOT.parent
_ver_file = APP_ROOT / "BUILD_VERSION"
BUILD_VERSION = _ver_file.read_text().strip() if _ver_file.exists() else "dev"
DATA_DIR = Path(os.environ.get("REVIEW_DATA_DIR", APP_ROOT / "data")).resolve()
BUILDS_DIR = DATA_DIR / "builds"
LOCAL_CACHE_DIR = Path(os.environ.get("REVIEW_LOCAL_CACHE_DIR", "/app/review-cache")).resolve()
DB_PATH = DATA_DIR / "review.db"
STATIC_DIR = APP_ROOT / "static"
DATABASE_URL = (os.environ.get("REVIEW_DATABASE_URL") or "").strip()
DB_BACKEND = "postgres" if DATABASE_URL.lower().startswith(("postgres://", "postgresql://")) else "sqlite"
SITE_STORAGE_BACKEND = (os.environ.get("REVIEW_SITE_STORAGE", "filesystem") or "filesystem").strip().lower()
GCS_BUCKET = (os.environ.get("REVIEW_GCS_BUCKET") or "").strip()
GCS_PREFIX = (os.environ.get("REVIEW_GCS_PREFIX", "docs-review") or "docs-review").strip().strip("/")

HOST = os.environ.get("REVIEW_BIND", "0.0.0.0")
# Cloud Run sets PORT; REVIEW_PORT remains as a backwards-compatible fallback.
PORT = int(os.environ.get("PORT") or os.environ.get("REVIEW_PORT", "8080"))
PUBLIC_PORT = int(os.environ.get("REVIEW_PUBLIC_PORT", str(PORT)))
DEFAULT_REVIEWER = os.environ.get("REVIEW_DEFAULT_REVIEWER", "anonymous")
DEFAULT_REWRITE_HOST = "docs.docker.com"
DISABLE_REWRITE_HOST_VALUES = {"0", "false", "no", "off", "none", "disable", "disabled"}
PREVIEW_CONTEXT_COOKIE = "review_preview_tag"
SOFT_DELETE_GRACE_SECONDS = 7 * 24 * 60 * 60
RESERVED_ROOT_SEGMENTS = {
    "_review",
    "api",
    "previews",
    "comments",
    "healthz",
    "manage",
    "publications",
    "feedback",
    "builds",
}

INJECT_START = "<!-- DOCS_REVIEW_START -->"
INJECT_END = "<!-- DOCS_REVIEW_END -->"
INJECT_MARKER_PATTERN = re.compile(
    rf"{re.escape(INJECT_START)}.*?{re.escape(INJECT_END)}\n?",
    flags=re.DOTALL,
)
PREVIOUS_SITE_DIRNAME = "site.previous"
