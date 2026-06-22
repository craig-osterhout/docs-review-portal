"""Background poller that imports PR previews from GitHub Actions artifacts."""
from __future__ import annotations

import io
import json
import logging
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_ARTIFACT_NAME_PREFIX = "preview-pr-"


def _api_get(url: str, token: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "docs-review-portal",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _download_artifact_zip(repo: str, artifact_id: int, token: str) -> bytes:
    """Download a GitHub artifact zip, handling the redirect to blob storage.

    GitHub returns a 302 to a short-lived signed URL (Azure Blob Storage).
    The Authorization header must not be forwarded to the redirect target.
    """
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        f"{_GITHUB_API}/repos/{repo}/actions/artifacts/{artifact_id}/zip",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "docs-review-portal",
        },
    )
    redirect_url: str | None = None
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            redirect_url = exc.headers.get("Location")
        else:
            raise

    if not redirect_url:
        raise RuntimeError(f"artifact download for {repo}/{artifact_id} returned no redirect URL")

    with urllib.request.urlopen(redirect_url, timeout=120) as resp:
        return resp.read()


def _parse_github_ts(value: str | None) -> int:
    if not value:
        return 0
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return 0


def _poll_repo(repo: str, token: str, rewrite_host: str) -> None:
    from docs_review_portal import import_status
    from docs_review_portal.config import DEFAULT_REWRITE_HOST
    from docs_review_portal.data import get_build_by_tag, import_build_archive_path
    from docs_review_portal.helpers import slugify_tag

    resolved_rewrite_host = rewrite_host or DEFAULT_REWRITE_HOST

    try:
        prs = _api_get(f"{_GITHUB_API}/repos/{repo}/pulls?state=open&per_page=100", token)
    except Exception as exc:
        _log.warning("github_poller: failed to list open PRs for %s: %s", repo, exc)
        return

    for pr in prs:
        pr_number = int(pr["number"])
        head_sha = str(pr["head"]["sha"])
        tag = slugify_tag(f"{repo}-pr-{pr_number}")

        if import_status.is_active(tag):
            continue

        try:
            runs_data = _api_get(
                f"{_GITHUB_API}/repos/{repo}/actions/runs"
                f"?head_sha={head_sha}&event=pull_request&status=completed&per_page=20",
                token,
            )
        except Exception as exc:
            _log.debug("github_poller: failed to get runs for %s#%d: %s", repo, pr_number, exc)
            continue

        artifact_info: dict[str, Any] | None = None
        for run in runs_data.get("workflow_runs", []):
            run_id = int(run["id"])
            try:
                artifacts_data = _api_get(
                    f"{_GITHUB_API}/repos/{repo}/actions/runs/{run_id}/artifacts",
                    token,
                )
            except Exception as exc:
                _log.debug("github_poller: failed to get artifacts for run %d: %s", run_id, exc)
                continue
            for artifact in artifacts_data.get("artifacts", []):
                if artifact["name"] == f"{_ARTIFACT_NAME_PREFIX}{pr_number}":
                    artifact_info = artifact
                    break
            if artifact_info:
                break

        if not artifact_info:
            continue

        artifact_ts = _parse_github_ts(artifact_info.get("updated_at") or artifact_info.get("created_at"))
        existing = get_build_by_tag(tag)
        if existing is not None:
            existing_ts = int(existing["updated_at"] or existing["created_at"] or 0)
            if artifact_ts <= existing_ts:
                continue

        artifact_id = int(artifact_info["id"])
        _log.info(
            "github_poller: importing %s#%d (artifact=%d, new=%s)",
            repo, pr_number, artifact_id, is_new,
        )

        try:
            zip_bytes = _download_artifact_zip(repo, artifact_id, token)
        except Exception as exc:
            _log.warning("github_poller: failed to download artifact for %s#%d: %s", repo, pr_number, exc)
            continue

        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                tar_names = [n for n in zf.namelist() if n.endswith(".tar.gz")]
                if not tar_names:
                    _log.warning("github_poller: no .tar.gz in artifact zip for %s#%d", repo, pr_number)
                    continue
                tar_bytes = zf.read(tar_names[0])
        except Exception as exc:
            _log.warning("github_poller: failed to extract artifact zip for %s#%d: %s", repo, pr_number, exc)
            continue

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp.write(tar_bytes)
            archive_path = Path(tmp.name)

        display_name = f"PR #{pr_number}"
        import_status.start(tag, display_name, "Importing from GitHub")

        def _do_import(
            _path: Path = archive_path,
            _tag: str = tag,
            _name: str = display_name,
            _sha: str = head_sha,
            _rh: str = resolved_rewrite_host,
        ) -> None:
            try:
                result = import_build_archive_path(
                    archive_path=_path,
                    tag=_tag,
                    display_name=_name,
                    source_ref=_sha,
                    rewrite_host=_rh,
                    stage_callback=lambda stage: import_status.update(_tag, stage),
                )
                _log.info("github_poller: import complete for %s (build_id=%d)", _tag, result.build_id)
                import_status.complete(_tag)
            except Exception as exc:
                _log.error("github_poller: import failed for %s: %s", _tag, exc, exc_info=True)
                import_status.fail(_tag, str(exc))
            finally:
                _path.unlink(missing_ok=True)

        threading.Thread(target=_do_import, daemon=True).start()


def start_poller(watches: list[dict], poll_interval: int) -> None:
    """Entry point for the background poller daemon thread."""
    repo_names = [w.get("repo", "") for w in watches if w.get("repo")]
    _log.info("github_poller: started, repos=%s, interval=%ds", repo_names, poll_interval)
    while True:
        for watch in watches:
            repo = (watch.get("repo") or "").strip()
            token = (watch.get("token") or "").strip()
            rewrite_host = (watch.get("rewrite_host") or "").strip()
            if not repo or not token:
                _log.warning("github_poller: skipping watch entry missing repo or token")
                continue
            try:
                _poll_repo(repo, token, rewrite_host)
            except Exception as exc:
                _log.error("github_poller: unhandled error for %s: %s", repo, exc, exc_info=True)
        time.sleep(poll_interval)
