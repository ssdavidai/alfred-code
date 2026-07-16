from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigurationError


DEFAULT_CONFIG = Path("~/.config/alfred-code/controller.toml").expanduser()


@dataclass(frozen=True)
class GitHubConfig:
    repo: str = "ssdavidai/alfred"
    owner: str = "ssdavidai"
    auto_intake: bool = False
    intake_label: str = "alfred-code"
    approval_command: str = "/approve-plan"
    rejection_command: str = "/reject-plan"
    approvers: tuple[str, ...] = ("ssdavidai",)
    reviewers: tuple[str, ...] = ("ssdavidai",)
    project_number: int | None = None
    project_title: str = "Alfred Product Control"


@dataclass(frozen=True)
class SupersetConfig:
    cli: str = "/Users/ssd/.superset/bin/superset"
    project_name: str = "alfred"
    worker_agent: str = "Claude"
    reviewer_agent: str = "Codex"
    workspace_prefix: str = "alfred-code"
    api_key_env: str = "SUPERSET_API_KEY"
    cleanup_merged_workspaces: bool = False
    worker_progress_timeout_seconds: int = 1200


@dataclass(frozen=True)
class SlackConfig:
    enabled: bool = False
    webhook_env: str = "ALFRED_CODE_SLACK_WEBHOOK"
    channel: str = ""


@dataclass(frozen=True)
class ControllerConfig:
    repo_path: Path = Path("~/dev/alfred").expanduser()
    state_dir: Path = Path("~/.alfred-code-state-v2").expanduser()
    apply: bool = False
    poll_seconds: int = 60
    planner_command: tuple[str, ...] = (
        "claude",
        "-p",
        "--safe-mode",
        "--permission-mode",
        "plan",
        "--tools",
        "",
        "--no-session-persistence",
        "--no-chrome",
        "--disable-slash-commands",
    )
    planner_timeout_seconds: int = 900
    github: GitHubConfig = field(default_factory=GitHubConfig)
    superset: SupersetConfig = field(default_factory=SupersetConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)

    @property
    def database_path(self) -> Path:
        return self.state_dir / "control-plane.sqlite3"

    @property
    def lane_policy_path(self) -> Path:
        return self.repo_path / "scripts/hooks/lanes.json"


def _section(source: dict[str, Any], name: str) -> dict[str, Any]:
    value = source.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"[{name}] must be a TOML table")
    return value


def load_config(path: Path | None = None) -> ControllerConfig:
    path = (path or DEFAULT_CONFIG).expanduser()
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"cannot read {path}: {exc}") from exc

    github_raw = _section(raw, "github")
    superset_raw = _section(raw, "superset")
    slack_raw = _section(raw, "slack")
    repo_path = Path(raw.get("repo_path", "~/dev/alfred")).expanduser().resolve()
    state_dir = Path(raw.get("state_dir", "~/.alfred-code-state-v2")).expanduser().resolve()
    planner = raw.get(
        "planner_command",
        [
            "claude",
            "-p",
            "--safe-mode",
            "--permission-mode",
            "plan",
            "--tools",
            "",
            "--no-session-persistence",
            "--no-chrome",
            "--disable-slash-commands",
        ],
    )
    if not isinstance(planner, list) or not planner or not all(isinstance(x, str) for x in planner):
        raise ConfigurationError("planner_command must be a non-empty array of strings")
    approvers = github_raw.get("approvers", ["ssdavidai"])
    if not isinstance(approvers, list) or not all(isinstance(x, str) for x in approvers):
        raise ConfigurationError("github.approvers must be an array of strings")
    reviewers = github_raw.get("reviewers", ["ssdavidai"])
    if not isinstance(reviewers, list) or not all(isinstance(x, str) for x in reviewers):
        raise ConfigurationError("github.reviewers must be an array of strings")

    config = ControllerConfig(
        repo_path=repo_path,
        state_dir=state_dir,
        apply=bool(raw.get("apply", False)),
        poll_seconds=int(raw.get("poll_seconds", 60)),
        planner_command=tuple(planner),
        planner_timeout_seconds=int(raw.get("planner_timeout_seconds", 900)),
        github=GitHubConfig(
            repo=str(github_raw.get("repo", "ssdavidai/alfred")),
            owner=str(github_raw.get("owner", "ssdavidai")),
            auto_intake=bool(github_raw.get("auto_intake", False)),
            intake_label=str(github_raw.get("intake_label", "alfred-code")),
            approval_command=str(github_raw.get("approval_command", "/approve-plan")),
            rejection_command=str(github_raw.get("rejection_command", "/reject-plan")),
            approvers=tuple(approvers),
            reviewers=tuple(reviewers),
            project_number=(int(github_raw["project_number"]) if github_raw.get("project_number") else None),
            project_title=str(github_raw.get("project_title", "Alfred Product Control")),
        ),
        superset=SupersetConfig(
            cli=str(superset_raw.get("cli", "/Users/ssd/.superset/bin/superset")),
            project_name=str(superset_raw.get("project_name", "alfred")),
            worker_agent=str(superset_raw.get("worker_agent", "Claude")),
            reviewer_agent=str(superset_raw.get("reviewer_agent", "Codex")),
            workspace_prefix=str(superset_raw.get("workspace_prefix", "alfred-code")),
            api_key_env=str(superset_raw.get("api_key_env", "SUPERSET_API_KEY")),
            cleanup_merged_workspaces=bool(superset_raw.get("cleanup_merged_workspaces", False)),
            worker_progress_timeout_seconds=int(
                superset_raw.get("worker_progress_timeout_seconds", 1200)
            ),
        ),
        slack=SlackConfig(
            enabled=bool(slack_raw.get("enabled", False)),
            webhook_env=str(slack_raw.get("webhook_env", "ALFRED_CODE_SLACK_WEBHOOK")),
            channel=str(slack_raw.get("channel", "")),
        ),
    )
    if config.poll_seconds < 10:
        raise ConfigurationError("poll_seconds must be at least 10")
    if config.superset.worker_progress_timeout_seconds < 60:
        raise ConfigurationError("superset.worker_progress_timeout_seconds must be at least 60")
    if not config.github.repo.count("/") == 1:
        raise ConfigurationError("github.repo must be OWNER/REPO")
    return config


def env_secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None
