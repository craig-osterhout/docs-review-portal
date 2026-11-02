# Docs Review Portal

Single-container review service with:

- `Previews` page with browser-based archive upload, `Export comments` + `Delete` actions, site links, and comment counts
- `Comments` page for centralized comment management with sortable columns and filters
- Inline page comments on generated docs with line-range selection, replies, and resolve/unresolve
- Changed pages tracking — automatically detect which pages changed on a branch, with an editable list per preview
- Direct site archive upload API with chunked upload support for large archives
- Soft-delete workflow for previews/comments with a 7-day recovery window before permanent deletion

Code layout:

```text
docs-review-portal/
|-- .dockerignore                  # Docker build exclusions
|-- .gitignore                     # Git exclusions (includes internal-only docs)
|-- compose.yaml                   # Local Docker Compose service definition
|-- Dockerfile                     # Hardened container build for the service
|-- README.md                      # Public project documentation
|-- README.internal.md             # Internal deployment notes (gitignored)
|-- requirements.txt               # Pinned Python dependencies for reproducible builds
|-- scripts/                       # Local helper scripts
|   `-- publish-branch.sh          # Builds docs site and uploads preview archive
|-- static/                        # Static frontend assets served by the app
|   |-- app.css                    # UI styles for management pages
|   |-- review-client.js           # In-page comment widget client script
|   `-- review.css                 # Styles for in-page review widget
`-- src/                           # Application source code
    |-- main.py                    # Thin process entrypoint
    `-- docs_review_portal/        # Python package for the review portal
        |-- __init__.py            # Package marker
        |-- __main__.py            # Module entrypoint (`python -m docs_review_portal`)
        |-- config.py              # Environment/config constants and derived paths
        |-- data.py                # Storage backends, schema init, builds/comments data operations
        |-- helpers.py             # Shared URL/path/time/html helper functions
        |-- web_api.py             # JSON API endpoints (build upload/list, comments)
        |-- web_common.py          # Shared request/response parsing and send helpers
        |-- web_handler.py         # HTTP routing and server bootstrap
        |-- web_pages.py           # HTML page rendering and non-API form handlers
        `-- web_preview.py         # Preview/static asset serving and tag/context resolution
```

## Publish from docs repo

```bash
sh ./publish-branch.sh my-docs-review
```

Run from anywhere by passing a docs repo path:

```bash
sh ./publish-branch.sh my-docs-review --docs-path ~/Documents/docker.github.io
```

Override rewrite host for this upload:

```bash
sh ./publish-branch.sh my-docs-review --docs-path ~/Documents/docker.github.io --rewrite-host docs.example.com
```

This command:

1. takes the preview name you provide
2. builds and exports the static site
3. generates a `.changed-pages` list by diffing `content/` against `origin/main` (if run from a feature branch inside the docs git repo)
4. packages everything to `.tar.gz`
5. uploads it to `POST /api/builds/upload?name=<name>`
6. waits for server processing to complete

The script normalizes the provided name to a URL-safe slug for preview routing.

Optional override:

- `REVIEW_SERVICE_URL` (default `http://localhost:8080`)
- `REVIEW_NAME` (if you prefer env var over CLI arg)
- `REVIEW_DOCS_DIR` (default docs repo path if `--docs-path` is not provided)
- `REVIEW_REWRITE_HOST` (default rewrite host if `--rewrite-host` is not provided)

## Run the service

Authenticate to Docker Hardened Images first:

```bash
docker login dhi.io
```

```bash
cd docs-review-portal
docker compose up -d --build
```

Local testing defaults to:

- metadata/comments: SQLite at `/app/data/review.db`
- uploaded site files: local filesystem under `/app/data/builds`

Local data persistence is stored in the Docker named volume `review_data`.

Open:

- `http://localhost:8080/previews`
- `http://localhost:8080/comments`
- `http://localhost:8080/logs`
- `http://localhost:8080/healthz`

## System logs

The **System logs** page (`/logs`) shows the app's logs, saved in the database so they survive restarts and are shared by every instance. Entries are kept for 30 days (set `REVIEW_LOG_RETENTION_DAYS` to change this). The page filters by level, instance and text, and each entry shows which instance wrote it. If the database can't be written to, the app keeps running and logs still go to stdout.

## Upload a preview

The **Upload preview** button on the `/previews` page opens the upload section of the Help page, which has the publish script command to copy. It builds, packages and uploads the docs site for you:

```bash
REVIEW_SERVICE_URL=https://<service-url> \
  bash <(curl -fsSL https://raw.githubusercontent.com/craig-osterhout/docs-review-portal/refs/heads/main/scripts/publish-branch.sh) <name> \
  --docs-path /path/to/local/docs
```

The script handles chunking automatically for large archives. To upload an archive for any other static site, use the API below.

## Versions

Uploading to a preview name that already exists creates a new version of that preview instead of overwriting it. Older versions keep their files and comments, so you can restructure a review (move, add or delete pages) without losing the context of earlier feedback.

- `/<name>/` always shows the latest version. Older versions live at `/<name>/_v/<n>/`.
- Switching to a version that doesn't have the current page (it was added, moved or removed in between) shows a 404 that lists the versions which do have it.
- The comments panel has a version dropdown. Older versions show a banner and do not accept new comments, but their comments can still be replied to, resolved, edited and deleted.
- When a new version is uploaded, open comments from the previous version are carried forward if the text they were attached to is still on the same page. Each new comment also saves a short stretch of text on either side of the highlight. When the same words appear several times on a page, that surrounding text picks the right occurrence, and a comment is carried forward only if the text next to it on at least one side still matches. Comments made before this was added are matched on the highlighted text alone. Comments that can't be matched (reworded text, moved or removed pages, diff comments) stay on the old version and are marked **outdated**.
- `/previews` shows one row per preview with its latest version. The versions icon in **Actions** lists every version. The comments column shows counts for the latest version and across all versions.
- `/comments` shows the latest version by default. The **Version** filter switches to all versions or a single one.
- Export comments includes every version, with `version` and `carried_from_comment_id` columns.
- Deleting a preview schedules all of its versions. Uploading to the name of a preview that is waiting to be deleted starts a fresh preview.
- PR previews imported by the GitHub poller are still overwritten in place on each push, so they don't gain a version per commit.

Existing previews become version 1 of their own preview, so nothing changes for them until a new version is uploaded.

## Changed pages

Each preview can store a list of URLs that changed on the branch, so reviewers know where to focus, and a diff for each of those pages.

**Automatic** — `publish-branch.sh` generates a `.changed-pages` file and a diff for each page, comparing against the point where the branch split off `origin/main`, and bundles them into the archive. It includes uncommitted and new files. If almost every page shows as changed, `origin/main` in that clone is probably stale or points at a fork.

**Manual** — the list can be replaced with the API, `POST /api/builds/<id>/changed-pages`. The list icon in the **Actions** column (or `/previews/<id>/changed-pages`) opens the list and the diffs, but doesn't edit them.

**In the review widget** — when a preview has changed pages, a diff icon appears in the comment panel header. It opens the changed pages as a tree with the diff for each page, where reviewers can also comment on the diff.

The **Help** page in the portal covers all of this for reviewers and authors.

## Add a preview (API)

```bash
curl -X POST "http://localhost:8080/api/builds/upload?name=my-docs-review&rewrite_host=docs.docker.com" \
  -H "Content-Type: application/gzip" \
  --data-binary "@my-docs-review.tar.gz"
```

The response streams server-side progress lines and finishes with a `result:` JSON line on success or `error:` on failure.

**Query parameters:**

- `name` (required) — preview name, normalized to a URL-safe slug
- `rewrite_host` — hostname whose absolute URLs should be rewritten to relative paths (e.g. `docs.docker.com`); pass `off` or `none` to disable rewriting
- `source_ref` — optional label stored as the build's image ref (e.g. a git SHA)
- `mode` — `new_version` (the default) creates the next version when the name already exists, `overwrite` replaces the latest version in place and keeps its comments. For a chunked upload it is read from the last chunk's request

**Chunked upload for large archives:**

Archives larger than ~30 MiB should be split and uploaded in chunks:

```bash
split -d -a 4 -b 20971520 my-docs-review.tar.gz chunks/chunk-
# then for each chunk (0-indexed):
curl -X POST "http://localhost:8080/api/builds/upload?name=my-docs-review&chunk=0&chunks=3" \
  -H "Content-Type: application/octet-stream" \
  --data-binary "@chunks/chunk-0000"
```

The `publish-branch.sh` script handles chunking automatically.

**Upload flow:**

1. server assembles chunks (if applicable) and stores site files to the configured storage backend
2. registers/updates the preview in the database
3. injects the review client at serve-time

Same preview name uploads overwrite preview files and metadata while preserving comments.

Generated docs are available at `http://localhost:8080/<tag>/`.

## API reference

- Requests and responses are JSON, apart from the upload, which streams plain text, and the CSV export.
- Every JSON response has an `ok` field. Errors are `{"ok": false, "error": "message"}` with a 4xx status. Older fields such as `status` are kept so existing scripts keep working.
- Times are unix seconds, with a `_human` copy where it helps.
- There is no authentication.
- Previews can't be deleted, scheduled for deletion or restored through the API. Those are actions for a person in the web UI.
- A preview has an `id` for each version. A review is the name, such as `my-docs-review`, with all of its versions. Any version's id selects its review where a review is wanted.

### Previews and reviews

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/builds` | Every preview, one entry per version, with comment counts, `review_key`, `version`, `is_latest` and `site_url` |
| `GET` | `/api/builds/<id>` | One preview, with its `changed_pages` and `diff_pages`, and its review with every version and the comment totals across versions |
| `GET` | `/api/reviews` | One entry per review, with its versions and comment totals |
| `GET` | `/api/reviews/<name>` | One review by name |
| `GET` | `/api/builds/<id>/changed-pages` | The pages listed as changed, and which of them have a stored diff |
| `POST` | `/api/builds/<id>/changed-pages` | Replace the changed pages list. Body: `{"pages": ["/path/", ...]}` |
| `GET` | `/api/builds/<id>/page-diff?page=<path>` | The stored diff for a page, as plain text |
| `POST` | `/api/builds/upload` | Upload a site archive, see "Add a preview (API)" |
| `GET` | `/api/imports` | Uploads in progress or recently failed, optionally for one `name`. Progress is held in memory by the instance doing the import, so with several instances this lists only the one that answered |

### Comments

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/comments?build_id=<id>&page_path=<path>` | The comments on one page, with replies, as the in-page widget loads them |
| `GET` | `/api/comments` | Comments across pages, with the filters below |
| `GET` | `/api/comments/<id>` | One comment or reply. A comment that starts a thread includes its replies |
| `POST` | `/api/comments` | Create a comment. Not allowed on an older version of a review, replies are |
| `POST` | `/api/comments/<id>/reply` | Reply to a comment. Body: `{"reviewer": "...", "body": "..."}` |
| `POST` | `/api/comments/<id>/resolve` | Resolve or unresolve. Body: `{"resolved": true}` |
| `POST` | `/api/comments/<id>/edit` | Change the text. Body: `{"body": "..."}` |
| `POST` | `/api/comments/<id>/delete` | Delete a comment and its replies |
| `GET` | `/api/builds/<id>/comments.csv` | Every comment and reply on every version of the review, as CSV, with `version` and `carried_from_comment_id` columns |

Filters for `GET /api/comments` without a `page_path`. All are optional.

| Parameter | Meaning |
|-----------|---------|
| `build_id` or `review` | Limit to one review, by any version's id or by name. Leave both out for every review |
| `version` | `latest` (the default), `all`, or a version number |
| `status` | `open`, `outdated` or `resolved`. Repeat it to match any of them |
| `reviewer` | Part of the reviewer's name |
| `from`, `to` | Unix time, or a date such as `2026-10-01` or `2026-10-01T12:00` |
| `sort`, `dir` | `preview`, `page_path`, `reviewer`, `created_at`, `resolved` or `version`, and `asc` or `desc` (the default) |
| `limit`, `offset` | Up to 1000 per request, 200 by default. The response has `total` and `has_more` |

A comment is open if it is unresolved, resolved if it is resolved, and outdated if it sits on an earlier version and has no copy on the latest one. A comment can be both open and outdated, or resolved and outdated.

### System logs and health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/logs` | Saved log entries, newest first. Filters: `level` (`all`, `warning`, `error`), `q` (text), `instance`, `limit` (up to 1000) and `page`. If the saved logs can't be read, the answer comes from this instance's memory and `source` says so |
| `GET` | `/healthz` | `{"status": "ok", "version": "<app version>"}` |

## Soft-delete workflow

Deleting a preview from the UI archives it rather than removing it immediately. Archived previews:

- are hidden from the previews list
- retain all comments
- are permanently deleted after 7 days
- can be restored within the 7-day window via the **Restore** button
- can be permanently deleted immediately via the **Delete immediately** button

## Data model

Database tables (SQLite local, Postgres in cloud):

- `builds`: imported docs previews
- `comments`: root comments + replies (`parent_id`)

Build records include `changed_pages TEXT` — a newline-separated list of URL paths that changed on the branch, populated automatically from `.changed-pages` files in uploaded archives or set manually via the UI/API.

Comment records include:

- `build_id`, `page_path`
- `line_start`, `line_end`, `selected_text`
- `reviewer`, `resolved`, timestamps

## GitHub Actions integration

The service can poll one or more GitHub repositories for PR preview artifacts and import them automatically. This lets contributors open a PR, wait a few minutes, and find a live preview link posted as a PR comment — no manual upload needed.

### How it works

1. A GitHub Actions workflow in your docs repo builds the site on every PR open/update, uploads the result as a named artifact (`preview-pr-{number}`), and posts a PR comment with the preview URL.
2. The review service polls the GitHub API every `GITHUB_POLL_INTERVAL` seconds, finds new or updated artifacts, downloads them, and imports the preview.

### Configuration

Set `GITHUB_WATCHES` to a JSON array of repos to watch:

```json
[{"repo": "myorg/mydocs", "token": "ghp_xxx"}]
```

Each entry supports:

| Field | Required | Description |
|-------|----------|-------------|
| `repo` | yes | `owner/repo` of the GitHub repository to watch |
| `token` | yes | GitHub fine-grained PAT with `contents:read`, `actions:read` |
| `rewrite_host` | no | Hostname to rewrite to relative paths (e.g. `docs.example.com`); defaults to `docs.docker.com` |

Set `REVIEW_PORTAL_URL` to the public URL of this service so the PR comment contains a working link:

```
REVIEW_PORTAL_URL=https://review.internal.example.com
```

If `GITHUB_WATCHES` is not set, the poller does not start and all existing behavior is unchanged.

### Required GitHub Actions workflow

Your docs repo needs a workflow that builds the site and uploads the artifact with the name `preview-pr-{number}`. Here is a generic example to adapt:

```yaml
name: Preview
on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write

jobs:
  preview:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      # Build your static site. Adjust this step for your site generator.
      # The built output must be written to ./out/
      - name: Build site
        run: hugo --minify -d ./out

      - name: Package preview
        run: |
          # Generate a list of changed pages (adjust content path as needed).
          # Each line is a URL path, e.g. /guides/install/
          git diff origin/${{ github.base_ref }}...HEAD --name-only -- content/ \
            | sed 's|^content||; s|\.md$|/|; /^\//!s|^|/|' \
            > ./out/.changed-pages

          tar -czf preview.tar.gz -C ./out .

      - uses: actions/upload-artifact@v4
        with:
          name: preview-pr-${{ github.event.pull_request.number }}
          path: preview.tar.gz
          retention-days: 1

      - name: Comment preview link
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          body="A preview of this PR will be available at ${{ vars.REVIEW_PORTAL_URL }}/pr-${{ github.event.pull_request.number }}/ within 15 minutes."
          gh pr comment "${{ github.event.pull_request.number }}" --edit-last --body "$body" 2>/dev/null \
            || gh pr comment "${{ github.event.pull_request.number }}" --body "$body"
```

The artifact name `preview-pr-{number}` is the convention the poller looks for. The `.changed-pages` file inside the archive is optional but enables the changed-pages feature in the review UI.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | HTTP listen port (Cloud Run sets this automatically) |
| `REVIEW_BIND` | `0.0.0.0` | HTTP bind address |
| `REVIEW_DATA_DIR` | `/app/data` | Root directory for site files and SQLite DB |
| `REVIEW_LOCAL_CACHE_DIR` | `/app/review-cache` | Local cache dir (filesystem mode only) |
| `REVIEW_SITE_STORAGE` | `filesystem` | Storage backend: `filesystem` or `gcs` |
| `REVIEW_GCS_BUCKET` | — | GCS bucket name (gcs mode only) |
| `REVIEW_GCS_PREFIX` | `docs-review` | Key prefix within the GCS bucket (gcs mode only) |
| `REVIEW_DATABASE_URL` | — | Postgres connection string; if unset uses SQLite |
| `REVIEW_DEFAULT_REVIEWER` | `anonymous` | Fallback reviewer name used when a comment is submitted via the API without a `reviewer` field |
| `GITHUB_WATCHES` | — | JSON array of `{repo, token}` objects; enables GitHub PR polling (see above) |
| `GITHUB_POLL_INTERVAL` | `300` | Seconds between GitHub poll cycles |

## Persistence modes

**Local mode (default):**

- `REVIEW_SITE_STORAGE=filesystem`, `REVIEW_DATABASE_URL` unset
- Site files stored under `REVIEW_DATA_DIR/builds/`
- SQLite database at `REVIEW_DATA_DIR/review.db`
- Backed by the Docker named volume `review_data` in `compose.yaml`

**Cloud mode:**

- `REVIEW_SITE_STORAGE=gcs` with `REVIEW_GCS_BUCKET` set
- `REVIEW_DATABASE_URL` set to a Postgres connection string
- Site files stored in GCS, metadata/comments in Postgres

