import subprocess
from unittest.mock import patch

import pytest

from alfred_code.errors import CommandError
from alfred_code.util import run


def test_failed_command_preserves_stdout_when_provider_uses_it_for_errors() -> None:
    completed = subprocess.CompletedProcess(
        ["provider"],
        1,
        stdout='{"is_error":true,"result":"monthly spend limit"}\n',
        stderr="",
    )

    with patch("alfred_code.util.subprocess.run", return_value=completed):
        with pytest.raises(CommandError, match="monthly spend limit"):
            run(["provider"])


def test_failed_command_prefers_stderr_when_both_streams_have_content() -> None:
    completed = subprocess.CompletedProcess(
        ["provider"],
        1,
        stdout="partial normal output",
        stderr="authoritative error",
    )

    with patch("alfred_code.util.subprocess.run", return_value=completed):
        with pytest.raises(CommandError, match="authoritative error") as captured:
            run(["provider"])

    assert "partial normal output" not in str(captured.value)
