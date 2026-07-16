from __future__ import annotations

from pathlib import Path
from typing import Any

from .github import GitHubClient
from .util import run


def audit_worktrees(repo_path: Path, github: GitHubClient | None = None) -> list[dict[str, Any]]:
    output = run(["git", "worktree", "list", "--porcelain"], cwd=repo_path)
    records: list[dict[str, Any]] = []
    current: dict[str, Any] = {}

    def flush() -> None:
        nonlocal current
        if not current:
            return
        path = Path(current["path"])
        status = run(["git", "status", "--porcelain"], cwd=path).splitlines()
        current["dirty_files"] = len(status)
        current["main"] = path.resolve() == repo_path.resolve()
        branch = current.get("branch", "")
        if github and branch and not current["main"]:
            pr = github.pr_for_branch(branch)
            current["pr"] = (
                {"number": pr.number, "state": pr.state, "url": pr.url, "head_sha": pr.head_sha}
                if pr
                else None
            )
        records.append(current)
        current = {}

    for line in output.splitlines() + [""]:
        if not line:
            flush()
        elif line.startswith("worktree "):
            flush()
            current["path"] = line.removeprefix("worktree ")
        elif line.startswith("HEAD "):
            current["head"] = line.removeprefix("HEAD ")
        elif line.startswith("branch refs/heads/"):
            current["branch"] = line.removeprefix("branch refs/heads/")
        elif line == "detached":
            current["branch"] = "(detached)"
        elif line.startswith("locked"):
            current["locked"] = True
        elif line.startswith("prunable"):
            current["prunable"] = True
    return records
