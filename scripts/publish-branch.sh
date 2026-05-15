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
ARCHIVE_REL="tmp/review-site/$NAME.tar.gz"
ARCHIVE="$DOCS_DIR/$ARCHIVE_REL"
CHUNKS_REL="tmp/review-site/chunks-$NAME"
CHUNKS_DIR="$DOCS_DIR/$CHUNKS_REL"
TMP_ROOT_REL="tmp/review-site"
CHUNK_SIZE=20971520  # 20 MiB — safely under the 32 MiB Cloud Run HTTP/1.1 body limit

cleanup() {
  (cd "$DOCS_DIR" && rm -rf "$OUT_REL" "$ARCHIVE_REL" "$CHUNKS_REL")
  (cd "$DOCS_DIR" && rmdir "$TMP_ROOT_REL" 2>/dev/null || true)
}
trap cleanup EXIT INT TERM

# Build static site output to a local folder.
rm -rf "$DOCS_DIR/$OUT_REL"
mkdir -p "$DOCS_DIR/$OUT_REL" "$(dirname "$ARCHIVE")"
(cd "$DOCS_DIR" && docker buildx bake release --set "release.output=type=local,dest=$OUT_REL")

# Generate .changed-pages list: URL paths for files added/modified vs origin/main.
# Only runs when a content/ directory and a git repo are present.
if [ -d "$DOCS_DIR/content" ] && git -C "$DOCS_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  printf 'Detecting changed pages...\n' >&2
  git -C "$DOCS_DIR" diff origin/main...HEAD --name-only --diff-filter=ACM -- content/ \
    | sed 's|^content||; s|/_index\.md$|/|; s|/index\.md$|/|; s|\.md$|/|; s|^/manuals/|/|' \
    | sort -u > "$DOCS_DIR/$OUT_REL/.changed-pages"
  CHANGED_COUNT="$(wc -l < "$DOCS_DIR/$OUT_REL/.changed-pages" | tr -d ' \t\n')"
  if [ "$CHANGED_COUNT" -eq 0 ]; then
    rm -f "$DOCS_DIR/$OUT_REL/.changed-pages"
    printf 'No changed pages detected (no diff against origin/main).\n' >&2
  else
    printf 'Found %s changed page(s).\n' "$CHANGED_COUNT" >&2
  fi
fi

# Package the built site as a .tar.gz upload payload.
(cd "$DOCS_DIR" && tar -C "$OUT_REL" -czf "$ARCHIVE_REL" .)

# Upload archive — split into 20 MiB chunks if the archive exceeds the server body limit.
ARCHIVE_BYTES="$(wc -c < "$ARCHIVE" | tr -d ' \t\n')"
if [ "$ARCHIVE_BYTES" -gt "$CHUNK_SIZE" ]; then
  mkdir -p "$CHUNKS_DIR"
  split -d -a 4 -b "$CHUNK_SIZE" "$ARCHIVE" "$CHUNKS_DIR/chunk-"
  TOTAL="$(ls "$CHUNKS_DIR"/chunk-* | wc -l | tr -d ' \t\n')"
  IDX=0
  for CHUNK in "$CHUNKS_DIR"/chunk-*; do
    IDX_HUMAN="$((IDX + 1))"
    printf 'Uploading part %d/%d...\n' "$IDX_HUMAN" "$TOTAL" >&2
    if [ "$IDX_HUMAN" -lt "$TOTAL" ]; then
      (cd "$DOCS_DIR" && curl --fail --show-error --ssl-no-revoke -o /dev/null \
        -X POST "$URL/api/builds/upload?name=$NAME&chunk=$IDX&chunks=$TOTAL&rewrite_host=$REWRITE_HOST" \
        -H "Content-Type: application/octet-stream" \
        --data-binary "@$CHUNK")
    else
      # Last chunk — server streams import progress until complete.
      printf 'Processing on server...\n' >&2
      (cd "$DOCS_DIR" && curl --fail --show-error --ssl-no-revoke --no-buffer \
        -X POST "$URL/api/builds/upload?name=$NAME&chunk=$IDX&chunks=$TOTAL&rewrite_host=$REWRITE_HOST" \
        -H "Content-Type: application/octet-stream" \
        --data-binary "@$CHUNK")
    fi
    IDX=$((IDX + 1))
  done
else
  # Single upload — server streams import progress until complete.
  printf 'Processing on server...\n' >&2
  (cd "$DOCS_DIR" && curl --fail --show-error --ssl-no-revoke --no-buffer \
    -X POST "$URL/api/builds/upload?name=$NAME&rewrite_host=$REWRITE_HOST" \
    -H "Content-Type: application/gzip" \
    --data-binary "@$ARCHIVE_REL")
fi
echo "Done."
