#!/bin/sh
set -eu

# Defaults: current directory docs path + docs.docker.com host.
RAW_NAME=""
DOCS_DIR="${REVIEW_DOCS_DIR:-$PWD}"
REWRITE_HOST="${REVIEW_REWRITE_HOST:-docs.docker.com}"

while [ $# -gt 0 ]; do
  case "$1" in
    --docs-path)
      shift
      [ $# -gt 0 ] || { echo "--docs-path requires a value" >&2; exit 1; }
      DOCS_DIR="$1"
      ;;
    --rewrite-host|--host)
      shift
      [ $# -gt 0 ] || { echo "--rewrite-host requires a value" >&2; exit 1; }
      REWRITE_HOST="$1"
      ;;
    -h|--help)
      echo "usage: sh publish-branch.sh <name> [--docs-path <path>] [--rewrite-host <host>]" >&2
      exit 0
      ;;
    -*)
      echo "unknown option: $1" >&2
      exit 1
      ;;
    *)
      [ -z "$RAW_NAME" ] || { echo "unexpected extra argument: $1" >&2; exit 1; }
      RAW_NAME="$1"
      ;;
  esac
  shift
done

RAW_NAME="${RAW_NAME:-${REVIEW_NAME:-}}"
[ -n "$RAW_NAME" ] || { echo "usage: sh publish-branch.sh <name> [--docs-path <path>] [--rewrite-host <host>]" >&2; exit 1; }
# Normalize to a URL-safe slug used by the service as publication tag.
NAME="$(printf "%s" "$RAW_NAME" | tr "[:upper:]" "[:lower:]" | sed -E 's#[^a-z0-9._-]+#-#g; s#(^[-._]+|[-._]+$)##g')"
[ -n "$NAME" ] || { echo "name produced an empty slug" >&2; exit 1; }

# Docs repo root comes from --docs-path, REVIEW_DOCS_DIR, or current directory.
[ -d "$DOCS_DIR" ] || { echo "docs dir not found: $DOCS_DIR" >&2; exit 1; }
# Rewrite host comes from --rewrite-host or REVIEW_REWRITE_HOST and defaults to docs.docker.com.
[ -n "$REWRITE_HOST" ] || { echo "--rewrite-host cannot be empty" >&2; exit 1; }

URL="${REVIEW_SERVICE_URL:-http://localhost:8080}"
OUT_REL="tmp/review-site/$NAME"
OUT="$DOCS_DIR/$OUT_REL"
ARCHIVE_REL="tmp/review-site/$NAME.tar.gz"
ARCHIVE="$DOCS_DIR/$ARCHIVE_REL"
TMP_ROOT_REL="tmp/review-site"

cleanup() {
  (cd "$DOCS_DIR" && rm -rf "$OUT_REL" "$ARCHIVE_REL")
  (cd "$DOCS_DIR" && rmdir "$TMP_ROOT_REL" 2>/dev/null || true)
}
trap cleanup EXIT INT TERM

# Build static site output to a local folder.
rm -rf "$OUT"
mkdir -p "$OUT" "$(dirname "$ARCHIVE")"
(cd "$DOCS_DIR" && docker buildx bake release --set "release.output=type=local,dest=$OUT_REL")
# Package the built site as a .tar.gz upload payload.
(cd "$DOCS_DIR" && tar -C "$OUT_REL" -czf "$ARCHIVE_REL" .)
# Upload archive and wait for server processing to complete.
(cd "$DOCS_DIR" && curl --fail --show-error -X POST "$URL/api/builds/upload?name=$NAME&rewrite_host=$REWRITE_HOST" -H "Content-Type: application/gzip" --data-binary "@$ARCHIVE_REL")
echo "Submitted."
