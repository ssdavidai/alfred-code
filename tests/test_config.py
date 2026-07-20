from pathlib import Path

import pytest

from alfred_code.config import load_config
from alfred_code.errors import ConfigurationError


def test_parallel_planner_default_is_bounded(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")

    assert config.max_parallel_planners == 3
    assert config.planner_command[config.planner_command.index("--model") + 1] == "sonnet"
    assert config.planner_command[config.planner_command.index("--effort") + 1] == "high"


def test_parallel_planner_bound_is_validated(tmp_path: Path) -> None:
    path = tmp_path / "controller.toml"
    path.write_text("max_parallel_planners = 9\n")

    with pytest.raises(ConfigurationError, match="between 1 and 8"):
        load_config(path)
