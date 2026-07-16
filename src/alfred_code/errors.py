class AlfredCodeError(RuntimeError):
    """Base exception with an operator-readable message."""


class ConfigurationError(AlfredCodeError):
    pass


class CommandError(AlfredCodeError):
    def __init__(self, command: list[str], returncode: int, stderr: str):
        self.command = command
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(
            f"command failed ({returncode}): {' '.join(command)}"
            + (f"\n{self.stderr}" if self.stderr else "")
        )


class PlanValidationError(AlfredCodeError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid plan:\n- " + "\n- ".join(problems))


class AuthorityUnavailable(AlfredCodeError):
    """Raised when a live authority cannot be refreshed safely."""
