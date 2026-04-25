#!/usr/bin/env python3
"""sentry_create_release.py — create/update a Sentry release and attach a deploy.

Usage:
    python scripts/sentry_create_release.py [--env production]

Environment variables:
    SENTRY_AUTH_TOKEN   — Sentry REST API token (read from Краб .env if absent)
    SENTRY_ENVIRONMENT  — deployment environment (default: production)

The script is idempotent: if the release already exists the refs are updated;
if the deploy already exists it is silently skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SENTRY_ORG = "po-zm"
SENTRY_BASE_URL = "https://de.sentry.io/api/0"
SENTRY_PROJECTS = ["krab-ear-backend", "krab-ear-agent"]
GITHUB_REPO = "Pavua/Krab-Ear"

# Path to Краб main .env which contains SENTRY_AUTH_TOKEN
KRAB_ENV_PATH = Path("/Users/pablito/Antigravity_AGENTS/Краб/.env")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file, ignoring comments and blanks."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _git_describe() -> str:
    """Return git describe output or 'krab-ear@unknown'."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            if version:
                return version
    except Exception:  # noqa: BLE001
        pass
    return "krab-ear@unknown"


def _git_head() -> str:
    """Return the full SHA of HEAD."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _sentry_request(
    method: str,
    path: str,
    token: str,
    payload: dict | None = None,
) -> tuple[int, dict]:
    """Send a request to the Sentry REST API.

    Returns (status_code, response_dict).
    """
    url = f"{SENTRY_BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body_raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(body_raw)
        except Exception:  # noqa: BLE001
            body = {"detail": body_raw}
        return exc.code, body


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def create_or_update_release(token: str, version: str, head_sha: str) -> bool:
    """Create the release (idempotent) and update commit refs."""
    payload: dict = {
        "version": version,
        "projects": SENTRY_PROJECTS,
    }
    if head_sha:
        payload["refs"] = [
            {
                "repository": GITHUB_REPO,
                "commit": head_sha,
            }
        ]

    # Try to create; 208 or 400 "already exists" are both fine
    status, body = _sentry_request(
        "POST",
        f"/organizations/{SENTRY_ORG}/releases/",
        token,
        payload,
    )

    if status in (200, 201):
        print(f"[sentry] Release created: {version}")
    elif status == 208:
        print(f"[sentry] Release already exists, updating refs: {version}")
        # Update refs on the existing release
        if head_sha:
            _sentry_request(
                "POST",
                f"/organizations/{SENTRY_ORG}/releases/{urllib.parse.quote(version, safe='')}/refs/",
                token,
                {
                    "refs": [
                        {
                            "repository": GITHUB_REPO,
                            "commit": head_sha,
                        }
                    ]
                },
            )
    elif status == 400 and "already exists" in str(body).lower():
        print(f"[sentry] Release already exists (400): {version}")
    else:
        print(f"[sentry] WARNING: unexpected status {status} creating release: {body}", file=sys.stderr)
        return False

    return True


def create_deploy(token: str, version: str, environment: str) -> bool:
    """Attach a deploy record to the release."""
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "environment": environment,
        "dateStarted": now,
        "dateFinished": now,
    }
    # url-encode version for path safety
    import urllib.parse
    encoded = urllib.parse.quote(version, safe="")
    status, body = _sentry_request(
        "POST",
        f"/organizations/{SENTRY_ORG}/releases/{encoded}/deploys/",
        token,
        payload,
    )

    if status in (200, 201):
        deploy_id = body.get("id", "?")
        print(f"[sentry] Deploy created: id={deploy_id} env={environment}")
        return True
    else:
        print(
            f"[sentry] WARNING: could not create deploy (status={status}): {body}",
            file=sys.stderr,
        )
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    import urllib.parse  # noqa: F401 (already imported above, keep here for clarity)

    parser = argparse.ArgumentParser(description="Create Sentry release + deploy")
    parser.add_argument(
        "--env",
        default=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        help="Deployment environment (default: production)",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Override release version (default: git describe)",
    )
    args = parser.parse_args()

    # --- Resolve auth token ---
    token = os.environ.get("SENTRY_AUTH_TOKEN", "")
    if not token:
        env_vars = _load_env_file(KRAB_ENV_PATH)
        token = env_vars.get("SENTRY_AUTH_TOKEN", "")
    if not token:
        print("[sentry] ERROR: SENTRY_AUTH_TOKEN not found", file=sys.stderr)
        return 1

    version = args.version or _git_describe()
    head_sha = _git_head()
    environment = args.env

    print(f"[sentry] version={version} sha={head_sha[:8] if head_sha else 'N/A'} env={environment}")

    ok1 = create_or_update_release(token, version, head_sha)
    ok2 = create_deploy(token, version, environment)

    if ok1 and ok2:
        print("[sentry] Release tracking complete.")
        return 0
    else:
        print("[sentry] Release tracking completed with warnings.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
