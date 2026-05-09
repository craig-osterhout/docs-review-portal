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
- `http://localhost:8080/healthz`

## Add a preview (API)

```bash
curl -X POST "http://localhost:8080/api/builds/upload?name=my-docs-review&rewrite_host=docs.docker.com" \
  -H "Content-Type: application/gzip" \
  --data-binary "@tmp/review-site/my-docs-review.tar.gz"
```

Upload flow:

1. `POST /api/builds/upload?name=<name>[&rewrite_host=<host>]`
2. server stores and registers the preview

The app will:

1. write site files to the configured storage backend
2. register/update the preview in the database
3. inject review client at serve-time

Same preview name uploads overwrite preview files and metadata while preserving comments.

Generated docs are available at:

- `http://localhost:8080/<tag>/`

## Data model

Database tables (SQLite local, Postgres in cloud):

- `builds`: imported docs previews
- `comments`: root comments + replies (`parent_id`)

Comment records include:

- `build_id`, `page_path`
- `line_start`, `line_end`, `selected_text`
- `reviewer`, `resolved`, timestamps

## Persistence modes

The service supports two persistence modes.

Local mode (default):

- `REVIEW_SITE_STORAGE=filesystem`
- `REVIEW_DATABASE_URL` unset
- Uses `/app/data` (backed by the Docker volume in `compose.yaml`)

Cloud mode (durable):

- `REVIEW_SITE_STORAGE=gcs`
- `REVIEW_GCS_BUCKET=<your-bucket>`
- `REVIEW_GCS_PREFIX=<optional-prefix>`
- `REVIEW_DATABASE_URL=postgresql://...`

In cloud mode:

- site files are stored in GCS (durable object storage)
- metadata/comments are stored in Postgres (Cloud SQL)

This lets the same container run locally and in cloud environments with durable storage in both places.

