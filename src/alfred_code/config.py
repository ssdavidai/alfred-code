from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_security import (
    SCOPED_AGENT_IDS,
    SCOPED_CLAUDE_AGENT_ID,
    SCOPED_CODEX_AGENT_ID,
)
from .errors import ConfigurationError


DEFAULT_CONFIG = Path("~/.config/alfred-code/controller.toml").expanduser()
PLANNER_PROFILE_NAME = "alfred-planner"

DEFAULT_PLANNER_COMMAND = (
    "codex",
    "exec",
    "--profile",
    PLANNER_PROFILE_NAME,
    "--model",
    "gpt-5.6-sol",
    "-c",
    'model_reasoning_effort="high"',
    "-c",
    'web_search="disabled"',
    "-c",
    "mcp_servers={}",
    "-c",
    "plugins={}",
    "-c",
    "memories={generate_memories=false,use_memories=false}",
    "--ephemeral",
    "--strict-config",
    "--json",
)


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
    worker_agent: str = SCOPED_CLAUDE_AGENT_ID
    reviewer_agent: str = SCOPED_CODEX_AGENT_ID
    workspace_prefix: str = "alfred-code"
    api_key_env: str = "SUPERSET_API_KEY"
    cleanup_merged_workspaces: bool = False
    worker_launch_timeout_seconds: int = 120
    worker_progress_timeout_seconds: int = 1200
    review_repair_max_attempts: int = 2


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
    max_parallel_planners: int = 3
    planner_command: tuple[str, ...] = DEFAULT_PLANNER_COMMAND
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
    planner = raw.get("planner_command", list(DEFAULT_PLANNER_COMMAND))
    if not isinstance(planner, list) or not planner or not all(isinstance(x, str) for x in planner):
        raise ConfigurationError("planner_command must be a non-empty array of strings")
    _validate_planner_command(planner)
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
        max_parallel_planners=int(raw.get("max_parallel_planners", 3)),
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
            worker_agent=str(superset_raw.get("worker_agent", SCOPED_CLAUDE_AGENT_ID)),
            reviewer_agent=str(superset_raw.get("reviewer_agent", SCOPED_CODEX_AGENT_ID)),
            workspace_prefix=str(superset_raw.get("workspace_prefix", "alfred-code")),
            api_key_env=str(superset_raw.get("api_key_env", "SUPERSET_API_KEY")),
            cleanup_merged_workspaces=bool(superset_raw.get("cleanup_merged_workspaces", False)),
            worker_launch_timeout_seconds=int(
                superset_raw.get("worker_launch_timeout_seconds", 120)
            ),
            worker_progress_timeout_seconds=int(
                superset_raw.get("worker_progress_timeout_seconds", 1200)
            ),
            review_repair_max_attempts=int(
                superset_raw.get("review_repair_max_attempts", 2)
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
    if not 1 <= config.max_parallel_planners <= 8:
        raise ConfigurationError("max_parallel_planners must be between 1 and 8")
    if config.superset.worker_launch_timeout_seconds < 30:
        raise ConfigurationError("superset.worker_launch_timeout_seconds must be at least 30")
    if config.superset.worker_progress_timeout_seconds < 60:
        raise ConfigurationError("superset.worker_progress_timeout_seconds must be at least 60")
    if not 0 <= config.superset.review_repair_max_attempts <= 10:
        raise ConfigurationError(
            "superset.review_repair_max_attempts must be between 0 and 10"
        )
    if config.superset.worker_agent not in SCOPED_AGENT_IDS:
        raise ConfigurationError(
            "superset.worker_agent must be an Alfred scoped Superset agent UUID; "
            "run `alfred-code agents-provision` instead of selecting a built-in preset"
        )
    if config.superset.reviewer_agent not in SCOPED_AGENT_IDS:
        raise ConfigurationError(
            "superset.reviewer_agent must be an Alfred scoped Superset agent UUID; "
            "run `alfred-code agents-provision` instead of selecting a built-in preset"
        )
    if not config.github.repo.count("/") == 1:
        raise ConfigurationError("github.repo must be OWNER/REPO")
    return config


def env_secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _option_value(command: list[str], *options: str) -> str | None:
    for index, argument in enumerate(command[:-1]):
        if argument in options:
            return command[index + 1]
    return None


def _validate_planner_command(command: list[str]) -> None:
    """Fail closed unless the planner is the pinned, read-only Codex role."""
    if Path(command[0]).name != "codex" or len(command) < 2 or command[1] != "exec":
        raise ConfigurationError("planner_command must invoke `codex exec`")
    model = _option_value(command, "--model", "-m")
    if model != "gpt-5.6-sol":
        raise ConfigurationError("planner_command model must be gpt-5.6-sol")
    if _option_value(command, "--profile", "-p") != PLANNER_PROFILE_NAME:
        raise ConfigurationError(
            f"planner_command must use the {PLANNER_PROFILE_NAME} permission profile"
        )
    if any(argument in {"--sandbox", "-s"} for argument in command):
        raise ConfigurationError(
            "planner_command must use its scoped permission profile, not a broad sandbox mode"
        )
    required = {"--ephemeral", "--strict-config", "--json"}
    missing = sorted(required.difference(command))
    if missing:
        raise ConfigurationError(
            f"planner_command is missing required isolation arguments: {', '.join(missing)}"
        )
    overrides = [
        command[index + 1]
        for index, argument in enumerate(command[:-1])
        if argument in {"-c", "--config"}
    ]
    if 'model_reasoning_effort="high"' not in overrides:
        raise ConfigurationError("planner_command must pin model_reasoning_effort to high")
    if 'web_search="disabled"' not in overrides:
        raise ConfigurationError("planner_command must disable web search")
    required_overrides = {
        "mcp_servers={}",
        "plugins={}",
        "memories={generate_memories=false,use_memories=false}",
    }
    if missing_overrides := sorted(required_overrides.difference(overrides)):
        raise ConfigurationError(
            f"planner_command is missing isolation overrides: {', '.join(missing_overrides)}"
        )
    serialized = " ".join(command).lower()
    forbidden = (
        "dangerously-bypass",
        "--yolo",
        "--full-auto",
        "danger-full-access",
        "workspace-write",
        "--ignore-rules",
        "approval_policy",
        "sandbox_mode",
    )
    if any(value in serialized for value in forbidden):
        raise ConfigurationError("planner_command contains a forbidden permission override")


def planner_profile_path(command: tuple[str, ...]) -> Path:
    values = list(command)
    profile = _option_value(values, "--profile", "-p")
    if not profile:
        raise ConfigurationError("planner_command has no permission profile")
    return Path.home() / ".codex" / f"{profile}.config.toml"


def validate_planner_profile(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
        profile = tomllib.loads(payload.decode())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot read Codex planner profile {path}: {exc}") from exc
    name = profile.get("default_permissions")
    if name != "alfred_planner" or profile.get("approval_policy") != "never":
        raise ConfigurationError("Codex planner profile has unsafe authority settings")
    policy = profile.get("permissions", {}).get(name, {})
    filesystem = policy.get("filesystem", {})
    workspace = filesystem.get(":workspace_roots", {})
    if filesystem.get(":minimal") != "read" or workspace.get(".") != "read":
        raise ConfigurationError("Codex planner profile cannot read its scoped workspace")
    if policy.get("network", {}).get("enabled") is not False:
        raise ConfigurationError("Codex planner profile must disable network access")
    if any(value == "write" for value in _nested_values(filesystem)):
        raise ConfigurationError("Codex planner profile must not grant writable filesystem paths")
    for pattern in ("**/.env", "**/credentials.json", "**/secrets.json", "**/*.pem", "**/*.key"):
        if workspace.get(pattern) != "deny":
            raise ConfigurationError(
                f"Codex planner profile must deny credential pattern {pattern}"
            )
    return {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "permissions": name,
        "network": "disabled",
        "workspace": "read-only",
    }


def _nested_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        values: list[Any] = []
        for nested in value.values():
            values.extend(_nested_values(nested))
        return values
    if isinstance(value, list):
        values = []
        for nested in value:
            values.extend(_nested_values(nested))
        return values
    return [value]
