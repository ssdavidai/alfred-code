from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SECURITY_POLICY = "alfred-scoped-v1"
SCOPED_CLAUDE_AGENT_ID = "2dc16f0d-1e57-4f4b-9f3f-4e7835a921d1"
SCOPED_CODEX_AGENT_ID = "e75d43da-621f-449d-81ad-e3f92d553fd3"
SCOPED_AGENT_IDS = frozenset({SCOPED_CLAUDE_AGENT_ID, SCOPED_CODEX_AGENT_ID})
WORKER_RESULT = ".alfred-code-result.json"
REVIEW_RESULT = ".alfred-code-review.json"
CONTROL_FILES = frozenset({WORKER_RESULT, REVIEW_RESULT})


class AgentSecurityError(RuntimeError):
    pass


@dataclass(frozen=True)
class LaneManifest:
    workspace: Path
    role: str
    lane: str
    issue: int
    allowed: tuple[str, ...]
    verify: str
    controller_job: str

    @classmethod
    def load(cls, workspace: Path) -> "LaneManifest":
        lane_path = workspace / ".lane"
        try:
            value = json.loads(lane_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentSecurityError(f"missing or invalid enforced lane manifest at {lane_path}: {exc}") from exc
        if not isinstance(value, dict):
            raise AgentSecurityError(".lane must contain one JSON object")
        if value.get("security_policy") != SECURITY_POLICY:
            raise AgentSecurityError(f".lane does not declare required policy {SECURITY_POLICY}")
        role = str(value.get("role") or "")
        if role not in {"worker", "reviewer"}:
            raise AgentSecurityError(f"invalid agent role in .lane: {role!r}")
        allowed = value.get("allowed")
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise AgentSecurityError(".lane allowed must be an array of paths")
        for pattern in allowed:
            path = Path(pattern.removeprefix("./"))
            if path.is_absolute() or ".." in path.parts:
                raise AgentSecurityError(f"unsafe lane path {pattern!r}")
        return cls(
            workspace=workspace,
            role=role,
            lane=str(value.get("lane") or ""),
            issue=int(value.get("issue") or 0),
            allowed=tuple(allowed),
            verify=str(value.get("verify") or ""),
            controller_job=str(value.get("controller_job") or ""),
        )


def workspace_from_environment() -> Path:
    current = Path.cwd().resolve()
    configured = os.environ.get("SUPERSET_WORKSPACE_PATH", "").strip()
    workspace = Path(configured).expanduser().resolve() if configured else current
    if current != workspace:
        raise AgentSecurityError(
            f"scoped agent must start at its Superset workspace root; cwd={current}, workspace={workspace}"
        )
    if not (workspace / ".git").exists():
        raise AgentSecurityError(f"scoped agent workspace is not a Git checkout: {workspace}")
    return workspace


def path_matches(path: str, pattern: str) -> bool:
    path = path.removeprefix("./")
    pattern = pattern.removeprefix("./")
    if pattern == "**":
        return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    if pattern.startswith("**/"):
        suffix = pattern[3:]
        return path == suffix or path.endswith("/" + suffix)
    return fnmatch.fnmatchcase(path, pattern)


def path_allowed(path: str, manifest: LaneManifest) -> bool:
    normalized = path.removeprefix("./")
    if normalized in CONTROL_FILES:
        expected = WORKER_RESULT if manifest.role == "worker" else REVIEW_RESULT
        return normalized == expected
    if normalized == ".lane" or normalized.startswith(".git/") or normalized == ".git":
        return False
    return manifest.role == "worker" and any(path_matches(normalized, pattern) for pattern in manifest.allowed)


UNSAFE_ARGUMENTS = {
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-hook-trust",
    "--yolo",
    "--add-dir",
    "--settings",
    "--setting-sources",
    "--mcp-config",
    "--plugin-dir",
    "--plugin-url",
    "--agents",
    "--agent",
    "--sandbox",
    "-s",
    "--ask-for-approval",
    "-a",
    "--profile",
    "-p",
    "--config",
    "-c",
    "--cd",
    "-C",
    "--remote",
    "--remote-control",
    "--chrome",
    "--safe-mode",
}


def validate_provider_arguments(provider: str, arguments: list[str]) -> None:
    for index, argument in enumerate(arguments):
        lowered = argument.lower()
        if argument in UNSAFE_ARGUMENTS or lowered.startswith("--dangerously"):
            raise AgentSecurityError(f"{provider} launch rejected unsafe or policy-overriding argument {argument!r}")
        if lowered.startswith("--permission-mode=") and lowered.split("=", 1)[1] in {
            "bypasspermissions",
            "dontask",
        }:
            raise AgentSecurityError(f"{provider} launch rejected permission mode {argument!r}")
        if lowered.startswith("--sandbox=") or lowered.startswith("--ask-for-approval="):
            raise AgentSecurityError(f"{provider} launch rejected policy override {argument!r}")
        if argument == "--permission-mode" and index + 1 < len(arguments):
            raise AgentSecurityError("the Alfred policy owns Claude's permission mode")


def _toml_key(value: str) -> str:
    return json.dumps(value)


def _profile_path(pattern: str) -> str:
    value = pattern.removeprefix("./")
    if value.endswith("/**"):
        return value[:-3].rstrip("/")
    return value


def codex_profile(manifest: LaneManifest) -> str:
    writes = [_profile_path(pattern) for pattern in manifest.allowed] if manifest.role == "worker" else []
    writes.append(WORKER_RESULT if manifest.role == "worker" else REVIEW_RESULT)
    lines = [
        'default_permissions = "alfred_scoped"',
        'approval_policy = "never"',
        "",
        "[permissions.alfred_scoped]",
        f'description = "Alfred {manifest.role} lane {manifest.lane}: read repository, write approved paths only"',
        "",
        "[permissions.alfred_scoped.filesystem]",
        '":minimal" = "read"',
        "",
        '[permissions.alfred_scoped.filesystem.":workspace_roots"]',
        '"." = "read"',
    ]
    for path in dict.fromkeys(writes):
        if path:
            lines.append(f"{_toml_key(path)} = \"write\"")
    lines.extend(
        [
            '".git" = "read"',
            '".lane" = "read"',
            '".codex" = "read"',
            '".agents" = "read"',
            '"**/.env" = "deny"',
            '"**/.env.*" = "deny"',
            '"**/*credential*" = "deny"',
            '"**/*secret*" = "deny"',
            "",
            "[permissions.alfred_scoped.network]",
            "enabled = false",
            "",
            "[shell_environment_policy]",
            'inherit = "core"',
            "ignore_default_excludes = false",
            'exclude = ["AWS_*", "AZURE_*", "GH_*", "GITHUB_*", "SUPERSET_*", "*_KEY", "*_TOKEN", "*_SECRET"]',
            "",
            "[[hooks.PreToolUse]]",
            'matcher = ".*"',
            "",
            "[[hooks.PreToolUse.hooks]]",
            'type = "command"',
            f"command = {_toml_key(str(Path.home() / '.claude/bin/alfred-code-agent-guard'))}",
            "timeout = 5",
            'statusMessage = "Enforcing Alfred lane scope"',
            "",
        ]
    )
    return "\n".join(lines)


def claude_settings(manifest: LaneManifest, guard: Path) -> dict[str, Any]:
    tools = ["Bash", "Read", "Glob", "Grep"]
    if manifest.role == "worker":
        tools.extend(["Edit", "Write", "NotebookEdit"])
    permissions = {
        "defaultMode": "auto",
        "disableBypassPermissionsMode": "disable",
        "deny": ["mcp__*", "Agent", "WebFetch", "WebSearch"],
    }
    settings: dict[str, Any] = {
        "permissions": permissions,
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "allowAppleEvents": False,
            "excludedCommands": [],
            "filesystem": {
                "denyRead": ["~/"],
                "allowRead": [str(manifest.workspace)],
            },
            "network": {
                "allowedDomains": [],
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
            },
            "credentials": {
                "envVars": [
                    {"name": name, "mode": "deny"}
                    for name in (
                        "AWS_ACCESS_KEY_ID",
                        "AWS_SECRET_ACCESS_KEY",
                        "AWS_SESSION_TOKEN",
                        "GH_TOKEN",
                        "GITHUB_TOKEN",
                        "OPENAI_API_KEY",
                        "NPM_TOKEN",
                        "SUPERSET_API_KEY",
                    )
                ]
            },
        },
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": str(guard),
                            "timeout": 5,
                        }
                    ],
                }
            ]
        },
        "_alfred_tools": ",".join(tools),
    }
    return settings


def _quoted_config_segment(value: str) -> str:
    return json.dumps(value)


def _codex_disable_integrations(config_path: Path) -> list[str]:
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    arguments: list[str] = []
    for name in (config.get("mcp_servers") or {}):
        arguments.extend(["-c", f"mcp_servers.{_quoted_config_segment(str(name))}.enabled=false"])
    for name in (config.get("plugins") or {}):
        arguments.extend(["-c", f"plugins.{_quoted_config_segment(str(name))}.enabled=false"])
    return arguments


def _codex_legacy_sandbox_conflict(config_path: Path) -> str | None:
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return f"cannot validate base Codex config: {exc}"
    if "sandbox_mode" in config or "sandbox_workspace_write" in config:
        return (
            "base Codex config contains legacy sandbox settings that override named permission profiles; "
            "remove sandbox_mode/sandbox_workspace_write before running Alfred agents"
        )
    return None


def _provider_binary(provider: str) -> str:
    override = os.environ.get(f"ALFRED_CODE_REAL_{provider.upper()}", "").strip()
    if override:
        return override
    superset_wrapper = Path.home() / ".superset/bin" / provider
    if superset_wrapper.is_file() and os.access(superset_wrapper, os.X_OK):
        return str(superset_wrapper)
    path = os.environ.get("PATH", "")
    for directory in path.split(os.pathsep):
        candidate = Path(directory) / provider
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise AgentSecurityError(f"cannot find {provider} executable")


def build_provider_command(
    provider: str,
    arguments: list[str],
    manifest: LaneManifest,
    *,
    profile_name: str | None = None,
    guard: Path | None = None,
) -> list[str]:
    validate_provider_arguments(provider, arguments)
    binary = _provider_binary(provider)
    if provider == "codex":
        if not profile_name:
            raise AgentSecurityError("Codex scoped profile name is required")
        config_path = Path.home() / ".codex/config.toml"
        conflict = _codex_legacy_sandbox_conflict(config_path)
        if conflict:
            raise AgentSecurityError(conflict)
        command = [
            binary,
            "--strict-config",
            "--profile",
            profile_name,
            "--enable",
            "hooks",
        ]
        command.extend(_codex_disable_integrations(config_path))
        return [*command, *arguments]
    if provider == "claude":
        if guard is None:
            raise AgentSecurityError("Claude scoped guard path is required")
        settings = claude_settings(manifest, guard)
        tools = settings.pop("_alfred_tools")
        return [
            binary,
            "--permission-mode",
            "auto",
            "--settings",
            json.dumps(settings, separators=(",", ":")),
            "--tools",
            tools,
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--no-chrome",
            "--disable-slash-commands",
            *arguments,
        ]
    raise AgentSecurityError(f"unsupported scoped agent provider {provider!r}")


def launch(provider: str, arguments: list[str]) -> int:
    workspace = workspace_from_environment()
    manifest = LaneManifest.load(workspace)
    env = dict(os.environ)
    env["ALFRED_CODE_SECURITY_POLICY"] = SECURITY_POLICY
    env["ALFRED_CODE_AGENT_ROLE"] = manifest.role
    if provider == "codex":
        digest = hashlib.sha256(f"{workspace}:{os.getpid()}".encode()).hexdigest()[:16]
        profile_name = f"alfred-scoped-{digest}"
        profile_path = Path.home() / ".codex" / f"{profile_name}.config.toml"
        profile_path.write_text(codex_profile(manifest))
        profile_path.chmod(0o600)
        try:
            command = build_provider_command(provider, arguments, manifest, profile_name=profile_name)
            return subprocess.call(command, cwd=workspace, env=env)
        finally:
            profile_path.unlink(missing_ok=True)
    guard = Path.home() / ".claude/bin/alfred-code-agent-guard"
    if not guard.is_file() or not os.access(guard, os.X_OK):
        raise AgentSecurityError(f"required Claude security guard is unavailable: {guard}")
    command = build_provider_command(provider, arguments, manifest, guard=guard)
    return subprocess.call(command, cwd=workspace, env=env)


DESTRUCTIVE_SHELL_PATTERNS = (
    r"(^|[;&|]\s*)(sudo\s+)?(rm|rmdir|unlink|shred|trash)\b",
    r"\bfind\b[^\n]*(?:-delete|-exec\s+(?:rm|rmdir|unlink))",
    r"\b(?:git\s+)?(?:reset\s+--hard|clean\s+-|restore\b|checkout\s+--|branch\s+-D|stash\s+(?:drop|clear)|worktree\s+(?:remove|prune))",
    r"\bgit\s+(?:add|commit|push|checkout|switch|merge|rebase|cherry-pick|revert|tag|branch|update-ref)\b",
    r"\bgit\s+push\b[^\n]*(?:--force|-f\b|--delete|:\w)",
    r"\bgh\s+(?:repo\s+delete|pr\s+(?:merge|close)|issue\s+(?:close|delete)|release\s+delete)\b",
    r"\bgh\s+api\b[^\n]*(?:-X|--method)\s+(?:DELETE|PATCH|PUT)\b",
    r"\b(?:terraform|tofu)\s+(?:apply|destroy)\b",
    r"\b(?:kubectl|helm)\s+(?:apply|delete|patch|replace|upgrade|uninstall)\b",
    r"\bdocker\b[^\n]*(?:system\s+prune|volume\s+(?:rm|prune)|image\s+prune|compose\s+down)\b",
    r"\b(?:drop|truncate)\s+(?:table|database|schema)\b",
    r"\b(?:delete|update)\s+from\b",
    r"\b(?:mkfs|diskutil\s+erase|dd\b[^\n]*\bof=|shutdown|reboot|halt)\b",
    r"(^|[;&|]\s*)(kill|killall|pkill|launchctl\s+(?:bootout|unload))\b",
    r"\bcurl\b[^\n]*\|\s*(?:ba|z|k)?sh\b",
    r"\b(?:chmod|chown)\s+(?:-[^\s]*R[^\s]*\s+)?[/~]",
    r"\b(?:npm|pnpm|yarn)\s+(?:install|add|remove|uninstall)\b",
    r"\b(?:pip|pip3|python(?:3)?\s+-m\s+pip)\s+(?:install|uninstall)\b",
    r"\b(?:brew|apt|apt-get|dnf|yum)\s+(?:install|remove|uninstall|upgrade)\b",
    r"(^|[;&|]\s*)(?:mv|sed\s+-i|perl\s+-i)\b",
)

READ_ONLY_SHELL_COMMANDS = frozenset(
    {
        "cat",
        "file",
        "find",
        "head",
        "jq",
        "ls",
        "pwd",
        "rg",
        "sed",
        "stat",
        "tail",
        "wc",
    }
)
READ_ONLY_GIT_COMMANDS = frozenset(
    {"diff", "grep", "log", "ls-files", "rev-parse", "show", "status"}
)


def destructive_shell_reason(command: str) -> str | None:
    for pattern in DESTRUCTIVE_SHELL_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE | re.MULTILINE):
            return "destructive shell or external-state mutation is prohibited for Alfred agents"
    return None


def read_only_shell_command(command: str) -> bool:
    if not command.strip() or re.search(r"[;&|><`\n\r]", command):
        return False
    try:
        arguments = shlex.split(command)
    except ValueError:
        return False
    if not arguments:
        return False
    for argument in arguments[1:]:
        if argument.startswith(("/", "~/", "../")) or "/../" in argument:
            return False
    executable = Path(arguments[0]).name
    if executable == "git":
        if len(arguments) < 2 or any(
            argument in {"-C", "--git-dir", "--work-tree"}
            or argument.startswith(("--git-dir=", "--work-tree="))
            for argument in arguments[1:]
        ):
            return False
        return arguments[1] in READ_ONLY_GIT_COMMANDS
    if executable == "find" and any(
        argument in {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
        for argument in arguments[1:]
    ):
        return False
    return executable in READ_ONLY_SHELL_COMMANDS


def _tool_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
    if tool_name.lower() in {"apply_patch", "applypatch"}:
        patch = str(tool_input.get("patch") or tool_input.get("input") or "")
        paths.extend(re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE))
        paths.extend(re.findall(r"^\*\*\* Move to: (.+)$", patch, re.MULTILINE))
    return paths


def _relative_tool_path(path: str, workspace: Path) -> str | None:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        return candidate.resolve(strict=False).relative_to(workspace).as_posix()
    except ValueError:
        return None


def guard_reason(payload: dict[str, Any]) -> str | None:
    workspace_value = os.environ.get("SUPERSET_WORKSPACE_PATH") or payload.get("cwd") or os.getcwd()
    workspace = Path(str(workspace_value)).expanduser().resolve()
    try:
        manifest = LaneManifest.load(workspace)
    except AgentSecurityError as exc:
        return str(exc)
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return "tool input is not an object"
    lowered_tool = tool_name.lower()
    if lowered_tool.startswith("mcp__") or any(token in lowered_tool for token in ("computer", "browser", "slack")):
        return f"external control tool {tool_name!r} is outside the Alfred build scope"
    if lowered_tool in {"read", "glob", "grep"}:
        for path in _tool_paths(tool_name, tool_input):
            if _relative_tool_path(path, workspace) is None:
                return f"read from {path!r} is outside the assigned Superset workspace"
    if lowered_tool in {"edit", "write", "notebookedit", "notebook_edit", "apply_patch", "applypatch"}:
        for path in _tool_paths(tool_name, tool_input):
            relative = _relative_tool_path(path, workspace)
            if relative is None or not path_allowed(relative, manifest):
                return f"write to {path!r} is outside approved {manifest.role} scope"
        if not _tool_paths(tool_name, tool_input):
            return f"cannot prove {tool_name} stays inside the approved lane"
    if lowered_tool in {"bash", "shell", "exec", "exec_command"}:
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        destructive = destructive_shell_reason(command)
        if destructive:
            return destructive
        if command.strip() != manifest.verify.strip() and not read_only_shell_command(command):
            return "shell commands are limited to read-only inspection or the exact controller-enforced verification command"
    return None


def guard_main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}
    reason = guard_reason(payload if isinstance(payload, dict) else {})
    if reason:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"Alfred scoped-agent policy blocked this action: {reason}",
                    }
                }
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if not values or values[0] not in {"claude", "codex"}:
        print("usage: alfred-code-agent claude|codex [provider arguments]", file=sys.stderr)
        return 64
    try:
        return launch(values[0], values[1:])
    except AgentSecurityError as exc:
        print(f"Alfred scoped-agent launch refused: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
