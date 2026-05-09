# Docs Review Portal

Single-container review service with:

- `Previews` page with `Export comments` + `Delete` actions, site links, and comment counts
- `Comments` page for centralized comment management with sortable columns and filters
- Inline page comments on generated docs with line-range selection, replies, and resolve/unresolve
- Direct site archive upload API
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
3. packages the export to `.tar.gz`
4. uploads it to `POST /api/builds/upload?name=<name>`
5. waits for server processing to complete

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

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/builds` | List all builds with comment counts |
| `POST` | `/api/builds/upload` | Upload a site archive (see above) |
| `GET` | `/api/comments?build_id=<id>&page_path=<path>` | Fetch comments for a page |
| `POST` | `/api/comments` | Create a comment |
| `POST` | `/api/comments/<id>/resolve` | Resolve or unresolve a comment |
| `POST` | `/api/comments/<id>/reply` | Reply to a comment |
| `GET` | `/healthz` | Health check |

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

Comment records include:

- `build_id`, `page_path`
- `line_start`, `line_end`, `selected_text`
- `reviewer`, `resolved`, timestamps

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

