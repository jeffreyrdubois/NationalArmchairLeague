"""What versions are there to update to?

Reads the tag list straight out of the GitHub Container Registry with the
Docker Registry v2 API. The package is public, so this works with the anonymous
pull token the registry hands to anyone who asks; a private package needs a
token with ``read:packages``, which the update page has a field for.

Tags come out of the registry as a flat, unordered list of strings, which is no
use for deciding what to install. They are sorted into the four kinds this
project publishes and labelled with what each one actually is — and for a
pull request build, that means asking GitHub for the pull request's title, so
the choice reads "PR #54 — Prize payouts" rather than "pr-54".
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx

from app.services.selfupdate import IMAGE_REPOSITORY

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Kinds, in the order the page offers them.
KIND_LATEST = "latest"
KIND_RELEASE = "release"
KIND_PR = "pr"
KIND_COMMIT = "commit"

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_PR_TAG = re.compile(r"^pr-(\d+)$")
_SHA_TAG = re.compile(r"^sha-([0-9a-f]{7,40})$")

# Registry answers are cached briefly: the page polls while an update runs, and
# a tag list that changes only when CI publishes does not need re-fetching on
# every poll.
_CACHE: dict = {"fetched_at": None, "tags": [], "error": ""}
CACHE_SECONDS = 120


def _registry_host_and_path(repository: str) -> tuple[str, str]:
    """Split 'ghcr.io/owner/name' into ('ghcr.io', 'owner/name')."""
    parts = repository.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0]):
        return parts[0], parts[1]
    return "registry-1.docker.io", repository


def fetch_tags(
    repository: str = IMAGE_REPOSITORY,
    token: str = "",
    username: str = "",
    timeout: float = 15.0,
) -> list[str]:
    """Every tag the registry knows about for this image.

    GHCR wants a bearer token even for a public image; it just gives one away
    for the asking. A personal access token is passed through as the basic-auth
    password when the package is private.
    """
    host, path = _registry_host_and_path(repository)
    auth = (username or "token", token) if token else None

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        bearer = client.get(
            f"https://{host}/token",
            params={"service": host, "scope": f"repository:{path}:pull"},
            auth=auth,
        )
        bearer.raise_for_status()
        access = bearer.json().get("token") or bearer.json().get("access_token")

        response = client.get(
            f"https://{host}/v2/{path}/tags/list",
            headers={"Authorization": f"Bearer {access}"},
            params={"n": 200},
        )
        if response.status_code in (401, 403):
            raise PermissionError(
                "The registry refused the request. If the package is private, "
                "save a GitHub token with read:packages below."
            )
        response.raise_for_status()
        return list(response.json().get("tags") or [])


def fetch_pull_requests(repo: str, token: str = "", timeout: float = 15.0) -> dict[int, dict]:
    """Open pull requests by number, so a pr- tag can be shown by its title.

    Best effort on purpose: this only decorates the list. An unauthenticated
    call that runs into GitHub's rate limit leaves the tags readable as plain
    `pr-54`, which is worse to read but perfectly usable.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                f"{GITHUB_API}/repos/{repo}/pulls",
                headers=headers,
                params={"state": "all", "sort": "updated", "direction": "desc", "per_page": 50},
            )
            response.raise_for_status()
            return {
                item["number"]: {
                    "title": item.get("title") or "",
                    "state": "merged" if item.get("merged_at") else item.get("state"),
                    "url": item.get("html_url") or "",
                    "branch": (item.get("head") or {}).get("ref") or "",
                }
                for item in response.json()
            }
    except Exception as exc:  # noqa: BLE001 - decoration only, never fatal
        logger.info("Could not read pull requests from GitHub: %s", exc)
        return {}


def _classify(tag: str, pulls: dict[int, dict]) -> dict | None:
    if tag == "latest":
        return {
            "tag": tag,
            "kind": KIND_LATEST,
            "label": "latest",
            "detail": "The newest build of main — what a normal update installs.",
            "sort": (0, 0),
        }

    semver = _SEMVER.match(tag)
    if semver:
        major, minor, patch = (int(part) for part in semver.groups())
        return {
            "tag": tag,
            "kind": KIND_RELEASE,
            "label": tag,
            "detail": "Tagged release.",
            "sort": (1, -(major * 1_000_000 + minor * 1_000 + patch)),
        }

    pr = _PR_TAG.match(tag)
    if pr:
        number = int(pr.group(1))
        info = pulls.get(number) or {}
        state = info.get("state") or ""
        title = info.get("title") or ""
        return {
            "tag": tag,
            "kind": KIND_PR,
            "label": f"PR #{number}" + (f" — {title}" if title else ""),
            "detail": (
                f"Unmerged pull request ({state})." if state and state != "merged"
                else "Pull request build."
            ),
            "url": info.get("url") or "",
            "state": state,
            "sort": (2, -number),
        }

    sha = _SHA_TAG.match(tag)
    if sha:
        return {
            "tag": tag,
            "kind": KIND_COMMIT,
            "label": tag,
            "detail": f"Build of commit {sha.group(1)[:7]}.",
            "sort": (3, 0),
        }
    return None


def list_versions(
    repository: str = IMAGE_REPOSITORY,
    github_repo: str = "",
    registry_token: str = "",
    registry_user: str = "",
    github_token: str = "",
    use_cache: bool = True,
) -> dict:
    """Every installable version, grouped and labelled for the update page."""
    now = datetime.now(timezone.utc)
    if (
        use_cache
        and _CACHE["fetched_at"]
        and (now - _CACHE["fetched_at"]).total_seconds() < CACHE_SECONDS
    ):
        tags, error = _CACHE["tags"], _CACHE["error"]
    else:
        error = ""
        try:
            tags = fetch_tags(repository, registry_token, registry_user)
        except Exception as exc:  # noqa: BLE001 - shown on the page, not raised
            logger.warning("Could not list image tags: %s", exc)
            tags, error = [], str(exc)[:300]
        _CACHE.update({"fetched_at": now, "tags": tags, "error": error})

    pulls = (
        fetch_pull_requests(github_repo, github_token)
        if github_repo and any(_PR_TAG.match(tag) for tag in tags)
        else {}
    )

    versions = [v for v in (_classify(tag, pulls) for tag in tags) if v]
    versions.sort(key=lambda v: (v["sort"], v["tag"]))
    return {
        "versions":  versions,
        "by_kind":   {
            kind: [v for v in versions if v["kind"] == kind]
            for kind in (KIND_LATEST, KIND_RELEASE, KIND_PR, KIND_COMMIT)
        },
        "error":     error,
        "fetched_at": _CACHE["fetched_at"],
        "repository": repository,
    }


def clear_cache() -> None:
    _CACHE.update({"fetched_at": None, "tags": [], "error": ""})
