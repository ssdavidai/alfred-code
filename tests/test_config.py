from pathlib import Path

import pytest

from alfred_code.config import load_config
from alfred_code.errors import ConfigurationError


def test_parallel_planner_default_is_bounded(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")

    assert config.max_parallel_planners == 3
    assert config.auto_replan_max_attempts == 2
    assert config.planner_command[:2] == ("codex", "exec")
    assert config.planner_command[config.planner_command.index("--model") + 1] == "gpt-5.6-sol"
    assert config.planner_command[config.planner_command.index("--profile") + 1] == "alfred-planner"
    assert 'model_reasoning_effort="high"' in config.planner_command


@pytest.mark.parametrize(
    "command,error",
    [
        ('["claude", "-p"]', "codex exec"),
        (
            '["codex", "exec", "--profile", "alfred-planner", "--model", "gpt-5.6-sol", "--sandbox", "danger-full-access", "--ephemeral", "--strict-config", "--json"]',
            "permission profile",
        ),
        (
            '["codex", "exec", "--profile", "alfred-planner", "--model", "gpt-5.6-terra", "--ephemeral", "--strict-config", "--json"]',
            "gpt-5.6-sol",
        ),
    ],
)
def test_unsafe_or_wrong_planner_is_rejected(
    tmp_path: Path, command: str, error: str
) -> None:
    path = tmp_path / "controller.toml"
    path.write_text(f"planner_command = {command}\n")

    with pytest.raises(ConfigurationError, match=error):
        load_config(path)


def test_parallel_planner_bound_is_validated(tmp_path: Path) -> None:
    path = tmp_path / "controller.toml"
    path.write_text("max_parallel_planners = 9\n")

    with pytest.raises(ConfigurationError, match="between 1 and 8"):
        load_config(path)


def test_auto_replan_bound_is_validated(tmp_path: Path) -> None:
    path = tmp_path / "controller.toml"
    path.write_text("auto_replan_max_attempts = 6\n")

    with pytest.raises(ConfigurationError, match="between 0 and 5"):
        load_config(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("approvers", "[]"),
        ("approvers", '["intruder"]'),
        ("approvers", '["ssdavidai", "intruder"]'),
        ("reviewers", "[]"),
        ("reviewers", '["intruder"]'),
        ("reviewers", '["ssdavidai", "intruder"]'),
    ],
)
def test_github_comment_authority_cannot_be_reconfigured(
    tmp_path: Path, field: str, value: str
) -> None:
    path = tmp_path / "controller.toml"
    path.write_text(f"[github]\n{field} = {value}\n")

    with pytest.raises(ConfigurationError, match="only the trusted GitHub operator"):
        load_config(path)


def test_github_comment_authority_is_canonicalized_to_ssdavidai(tmp_path: Path) -> None:
    path = tmp_path / "controller.toml"
    path.write_text(
        '[github]\napprovers = ["SSDavidAI"]\nreviewers = ["ssdavidai"]\n'
    )

    config = load_config(path)

    assert config.github.approvers == ("ssdavidai",)
    assert config.github.reviewers == ("ssdavidai",)
