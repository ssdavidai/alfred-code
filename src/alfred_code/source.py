from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import AuthorityUnavailable, CommandError
from .util import run


@dataclass(frozen=True)
class SourceSync:
    """Verified state of the controller's dedicated source checkout."""

    branch: str
    before_sha: str
    head_sha: str
    changed: bool


def _require_detached_clean_checkout(repo: Path) -> str:
    try:
        branch = run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=repo).strip()
    except CommandError:
        branch = ""
    if branch:
        raise AuthorityUnavailable(
            "controller repo_path must be a dedicated detached checkout; "
            f"refusing to move attached branch {branch!r}"
        )

    dirty = run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        timeout=60,
    ).strip()
    if dirty:
        preview = "\n".join(dirty.splitlines()[:20])
        raise AuthorityUnavailable(
            "controller source checkout is dirty; refusing to discard or build over local work"
            + (f"\n{preview}" if preview else "")
        )
    return run(["git", "rev-parse", "HEAD"], cwd=repo, timeout=30).strip()


def sync_default_branch_checkout(
    repo: Path,
    *,
    branch: str = "main",
) -> SourceSync:
    """Safely make a clean detached checkout match the remote default branch.

    No reset, clean, stash, deletion, or force operation is permitted. Any local
    state causes a fail-closed error instead of being overwritten.
    """

    before_sha = _require_detached_clean_checkout(repo)
    run(["git", "fetch", "--no-tags", "origin", branch], cwd=repo, timeout=300)
    remote_sha = run(
        ["git", "rev-parse", f"refs/remotes/origin/{branch}"],
        cwd=repo,
        timeout=30,
    ).strip()

    if before_sha != remote_sha:
        run(["git", "switch", "--detach", "--quiet", remote_sha], cwd=repo, timeout=120)

    head_sha = _require_detached_clean_checkout(repo)
    if head_sha != remote_sha:
        raise AuthorityUnavailable(
            "controller source checkout did not reach the fetched default branch "
            f"({head_sha[:12]} != {remote_sha[:12]})"
        )
    return SourceSync(
        branch=branch,
        before_sha=before_sha,
        head_sha=head_sha,
        changed=before_sha != head_sha,
    )


def verify_default_branch_checkout(
    repo: Path,
    expected_sha: str,
    *,
    branch: str = "main",
) -> None:
    """Fail closed unless both the checkout and fetched remote ref are exact."""

    head_sha = _require_detached_clean_checkout(repo)
    remote_sha = run(
        ["git", "rev-parse", f"refs/remotes/origin/{branch}"],
        cwd=repo,
        timeout=30,
    ).strip()
    if head_sha != expected_sha or remote_sha != expected_sha:
        raise AuthorityUnavailable(
            "GitHub, the fetched default-branch ref, and the planner checkout disagree "
            f"(GitHub {expected_sha[:12]}, origin/{branch} {remote_sha[:12]}, "
            f"checkout {head_sha[:12]})"
        )
