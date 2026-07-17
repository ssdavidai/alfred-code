from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import select
import shlex
import subprocess
import sys
import tomllib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SECURITY_POLICY = "alfred-scoped-v1"
LAUNCH_REVISION = 22
SCOPED_CLAUDE_AGENT_ID = "2dc16f0d-1e57-4f4b-9f3f-4e7835a921d1"
SCOPED_CODEX_AGENT_ID = "e75d43da-621f-449d-81ad-e3f92d553fd3"
SCOPED_AGENT_IDS = frozenset({SCOPED_CLAUDE_AGENT_ID, SCOPED_CODEX_AGENT_ID})
WORKER_RESULT = ".alfred-code-result.json"
REVIEW_RESULT = ".alfred-code-review.json"
WORKER_RESULT_TEMP = ".alfred-code-result.json.tmp"
LAUNCH_STATUS = ".alfred-code-launch.json"
LAUNCH_STATUS_TEMP = ".alfred-code-launch.json.tmp"
CONTROL_FILES = frozenset({WORKER_RESULT, REVIEW_RESULT})
RUNTIME_CONTROL_FILES = frozenset(
    {
        ".lane",
        ".lane.tmp",
        "node_modules",
        WORKER_RESULT,
        WORKER_RESULT_TEMP,
        REVIEW_RESULT,
        LAUNCH_STATUS,
        LAUNCH_STATUS_TEMP,
    }
)


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
    mode: str = "build"
    head_sha: str = ""
    handoff_token: str = ""
    attempt: int = 0

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
        mode = str(value.get("mode") or "build")
        if mode not in {"build", "repair"}:
            raise AgentSecurityError(f"invalid agent mode in .lane: {mode!r}")
        head_sha = str(value.get("head_sha") or "")
        handoff_token = str(value.get("handoff_token") or "")
        attempt = int(value.get("attempt") or 0)
        if mode == "repair":
            if role != "worker":
                raise AgentSecurityError("repair mode is valid only for worker lanes")
            if not re.fullmatch(r"[0-9a-f]{40,64}", head_sha):
                raise AgentSecurityError("repair mode requires an exact lowercase Git head SHA")
            if not re.fullmatch(r"[0-9a-f]{32,64}", handoff_token):
                raise AgentSecurityError("repair mode requires a controller handoff token")
            if attempt < 1:
                raise AgentSecurityError("repair mode requires a positive attempt number")
        return cls(
            workspace=workspace,
            role=role,
            lane=str(value.get("lane") or ""),
            issue=int(value.get("issue") or 0),
            allowed=tuple(allowed),
            verify=str(value.get("verify") or ""),
            controller_job=str(value.get("controller_job") or ""),
            mode=mode,
            head_sha=head_sha,
            handoff_token=handoff_token,
            attempt=attempt,
        )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_launch_status(workspace: Path, status: str, **details: Any) -> dict[str, Any]:
    if status not in {"retrying", "running", "completed", "exited", "failed"}:
        raise ValueError(f"unknown scoped-agent launch status: {status}")
    value = {"schema": 1, "revision": LAUNCH_REVISION, "status": status, **details}
    temporary = workspace / LAUNCH_STATUS_TEMP
    target = workspace / LAUNCH_STATUS
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, target)
    return value


def _write_json_atomic(target: Path, value: dict[str, Any]) -> None:
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, target)


def _record_startup_failure(reason: str, exit_code: int = 78) -> None:
    workspace = Path.cwd().resolve()
    if not (workspace / ".lane").is_file():
        return
    try:
        write_launch_status(
            workspace,
            "failed",
            exit_code=exit_code,
            reason=reason[:500],
            finished_at=_utcnow(),
        )
    except OSError:
        pass


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
    if pattern.endswith("/"):
        prefix = pattern.rstrip("/")
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


def _git_metadata_paths(workspace: Path) -> tuple[Path, ...]:
    marker = workspace / ".git"
    if marker.is_dir():
        return (marker.resolve(),)
    try:
        first_line = marker.read_text().splitlines()[0]
    except (OSError, IndexError):
        return ()
    if not first_line.startswith("gitdir:"):
        return ()
    git_dir = Path(first_line.split(":", 1)[1].strip()).expanduser()
    if not git_dir.is_absolute():
        git_dir = marker.parent / git_dir
    git_dir = git_dir.resolve()
    paths = [git_dir]
    try:
        common_value = (git_dir / "commondir").read_text().strip()
    except OSError:
        common_value = ""
    if common_value:
        common_dir = Path(common_value).expanduser()
        if not common_dir.is_absolute():
            common_dir = git_dir / common_dir
        paths.append(common_dir.resolve())
    return tuple(dict.fromkeys(paths))


def _trusted_toolchain_paths() -> tuple[Path, ...]:
    candidates = (
        Path("/opt/homebrew/opt/node@22/bin"),
        Path("/opt/homebrew/bin"),
        Path("/opt/homebrew/lib"),
        Path("/opt/homebrew/opt"),
        Path("/opt/homebrew/Cellar"),
        Path("/opt/homebrew/share"),
        Path("/opt/homebrew/Frameworks"),
        Path("/opt/homebrew/etc/openssl@3"),
        Path("/Applications/ChatGPT.app/Contents/Resources/rg"),
        Path("/Applications/Codex.app/Contents/Resources/rg"),
        Path.home() / ".claude/bin/alfred-code-npm-shell",
        Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback",
        Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/git",
    )
    return tuple(path.resolve() for path in candidates if path.exists())


def _npm_shell_path() -> Path:
    # Resolve the installed symlink so a deny-by-default home policy can grant
    # the exact executable target without exposing the rest of ~/.claude/bin.
    return (Path.home() / ".claude/bin/alfred-code-npm-shell").resolve()


def _toolchain_path_value(paths: tuple[Path, ...]) -> str:
    executable_dirs = [
        path if path.is_dir() else path.parent
        for path in paths
        if path.name in {"bin", "fallback"} or path.name == "rg"
    ]
    executable_dirs.extend(Path(value) for value in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"))
    return os.pathsep.join(str(path) for path in dict.fromkeys(executable_dirs))


def _verification_roots(manifest: LaneManifest) -> tuple[str, ...]:
    roots = {"."}
    for match in re.finditer(r"(?:^|&&)\s*cd\s+([^\s;&|]+)", manifest.verify):
        value = match.group(1).strip("'\"")
        path = Path(value)
        if value and not path.is_absolute() and ".." not in path.parts:
            roots.add(path.as_posix())
    return tuple(sorted(roots))


def _verification_write_paths(manifest: LaneManifest) -> tuple[str, ...]:
    roots = _verification_roots(manifest)
    outputs = (".pytest_cache", ".cache", "coverage", "dist", "build", ".next", "node_modules/.cache")
    return tuple(
        (Path(root) / output).as_posix()
        for root in sorted(roots)
        for output in outputs
    )


def _verification_dependency_paths(manifest: LaneManifest) -> tuple[Path, ...]:
    paths: list[Path] = []
    for root in _verification_roots(manifest):
        base = manifest.workspace if root == "." else manifest.workspace / root
        for name in ("node_modules", ".venv", "venv"):
            candidate = base / name
            if candidate.is_symlink():
                paths.append(candidate.resolve())
    return tuple(dict.fromkeys(paths))


def _git_origin_url(checkout: Path) -> str:
    for git_dir in reversed(_git_metadata_paths(checkout)):
        config = git_dir / "config"
        try:
            lines = config.read_text().splitlines()
        except OSError:
            continue
        in_origin = False
        for raw_line in lines:
            line = raw_line.strip()
            if line.startswith("[") and line.endswith("]"):
                in_origin = line[1:-1].strip().lower() == 'remote "origin"'
                continue
            if not in_origin or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().lower() == "url":
                return value.strip()
    return ""


def _normalized_git_origin(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if normalized.startswith("git@github.com:"):
        normalized = "https://github.com/" + normalized.split(":", 1)[1]
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized.lower()


def _node_modules_supports(package_root: Path, node_modules: Path) -> bool:
    try:
        package = json.loads((package_root / "package.json").read_text())
        target = node_modules.resolve(strict=True)
    except (OSError, RuntimeError, json.JSONDecodeError):
        return False
    if not target.is_dir() or not isinstance(package, dict):
        return False
    required: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        values = package.get(section, {})
        if isinstance(values, dict):
            required.update(str(name) for name in values)
    return all((target / Path(*name.split("/"))).exists() for name in required)


def _dependency_candidates(manifest: LaneManifest, package_root: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []
    explicit = os.environ.get("ALFRED_CODE_NODE_MODULES", "").strip()
    if explicit:
        candidates.extend(Path(value).expanduser() for value in explicit.split(os.pathsep) if value)

    git_paths = _git_metadata_paths(manifest.workspace)
    common_dir = git_paths[-1] if git_paths else None
    source_checkout = common_dir.parent if common_dir and common_dir.name == ".git" else None
    source_origin = _normalized_git_origin(_git_origin_url(source_checkout)) if source_checkout else ""
    relative_root = package_root.relative_to(manifest.workspace)
    if source_checkout and source_origin:
        try:
            siblings = source_checkout.parent.iterdir()
        except OSError:
            siblings = ()
        for sibling in siblings:
            if sibling == source_checkout or not (sibling / ".git").exists():
                continue
            if _normalized_git_origin(_git_origin_url(sibling)) != source_origin:
                continue
            candidates.append(sibling / relative_root / "node_modules")

    usable: list[tuple[float, Path]] = []
    for candidate in dict.fromkeys(candidates):
        if not _node_modules_supports(package_root, candidate):
            continue
        try:
            target = candidate.resolve(strict=True)
            usable.append((target.stat().st_mtime, target))
        except (OSError, RuntimeError):
            continue
    usable.sort(key=lambda item: item[0], reverse=True)
    return tuple(dict.fromkeys(target for _, target in usable))


def prepare_dependency_overlay(manifest: LaneManifest) -> Path | None:
    """Create a root Node resolution fallback without replacing repository files."""
    overlay = manifest.workspace / "node_modules"
    if os.path.lexists(overlay):
        return None
    for root in _verification_roots(manifest):
        if root == ".":
            continue
        package_root = manifest.workspace / root
        local_modules = package_root / "node_modules"
        if not local_modules.is_symlink() or local_modules.exists():
            continue
        candidates = _dependency_candidates(manifest, package_root)
        if not candidates:
            continue
        overlay.symlink_to(candidates[0], target_is_directory=True)
        return overlay
    return None


def codex_profile(
    manifest: LaneManifest,
    *,
    profile_path: Path | None = None,
    hook_trust_hash: str | None = None,
    toolchain_paths: tuple[Path, ...] | None = None,
    git_metadata_paths: tuple[Path, ...] | None = None,
) -> str:
    writes = [_profile_path(pattern) for pattern in manifest.allowed] if manifest.role == "worker" else []
    writes.append(WORKER_RESULT if manifest.role == "worker" else REVIEW_RESULT)
    verification_writes = _verification_write_paths(manifest)
    base_toolchains = _trusted_toolchain_paths() if toolchain_paths is None else toolchain_paths
    npm_shell = _npm_shell_path()
    toolchains = tuple(dict.fromkeys((*base_toolchains, npm_shell)))
    git_paths = _git_metadata_paths(manifest.workspace) if git_metadata_paths is None else git_metadata_paths
    dependency_paths = _verification_dependency_paths(manifest)
    lines = [
        'default_permissions = "alfred_scoped"',
        'approval_policy = "never"',
        "",
        "[permissions.alfred_scoped]",
        f'description = "Alfred {manifest.role} lane {manifest.lane}: read repository, write approved paths only"',
        "",
        "[permissions.alfred_scoped.filesystem]",
        '":minimal" = "read"',
        '":tmpdir" = "write"',
        '":slash_tmp" = "write"',
    ]
    for path in dict.fromkeys((*toolchains, *git_paths, *dependency_paths)):
        lines.append(f"{_toml_key(str(path))} = \"read\"")
    lines.extend(["", '[permissions.alfred_scoped.filesystem.":workspace_roots"]', '"." = "read"'])
    for path in dict.fromkeys(writes):
        if path:
            lines.append(f"{_toml_key(path)} = \"write\"")
    for path in verification_writes:
        lines.append(f"{_toml_key(path)} = \"write\"")
    lines.extend(
        [
            '".git" = "read"',
            '".lane" = "read"',
            '".codex" = "read"',
            '".agents" = "read"',
            '"**/.env" = "deny"',
            '"**/.env.local" = "deny"',
            '"**/.env.*.local" = "deny"',
            '"**/.env.development" = "deny"',
            '"**/.env.production" = "deny"',
            '"**/.env.staging" = "deny"',
            '"**/.env.test" = "deny"',
            '"**/credentials.json" = "deny"',
            '"**/secrets.json" = "deny"',
            '"**/*.pem" = "deny"',
            '"**/*.key" = "deny"',
            '"**/id_rsa*" = "deny"',
            '"**/.npmrc" = "deny"',
            '"**/.pypirc" = "deny"',
            "",
            "[permissions.alfred_scoped.network]",
            "enabled = false",
            "",
            "[shell_environment_policy]",
            'inherit = "core"',
            "ignore_default_excludes = false",
            'exclude = ["AWS_*", "AZURE_*", "GH_*", "GITHUB_*", "SUPERSET_*", "*_KEY", "*_TOKEN", "*_SECRET"]',
            "",
            "[shell_environment_policy.set]",
            f"PATH = {_toml_key(_toolchain_path_value(toolchains))}",
            'PYTHONDONTWRITEBYTECODE = "1"',
            'GIT_CONFIG_GLOBAL = "/dev/null"',
            'GIT_CONFIG_NOSYSTEM = "1"',
            'GIT_CONFIG_COUNT = "1"',
            'GIT_CONFIG_KEY_0 = "core.excludesFile"',
            'GIT_CONFIG_VALUE_0 = "/dev/null"',
            'npm_config_scripts_prepend_node_path = "false"',
            f"npm_config_script_shell = {_toml_key(str(npm_shell))}",
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
    if bool(profile_path) != bool(hook_trust_hash):
        raise AgentSecurityError("Codex hook profile path and trust hash must be supplied together")
    if profile_path and hook_trust_hash:
        key = f"{profile_path.resolve()}:pre_tool_use:0:0"
        lines.extend(
            [
                f"[hooks.state.{_toml_key(key)}]",
                f"trusted_hash = {_toml_key(hook_trust_hash)}",
                "",
            ]
        )
    return "\n".join(lines)


def _codex_hook_override(guard: Path) -> str:
    return (
        "hooks.PreToolUse=[{matcher=\".*\",hooks=[{type=\"command\",command="
        f"{_toml_key(str(guard))},timeout=5,statusMessage=\"Enforcing Alfred lane scope\""
        "}]}]"
    )


def _read_app_server_response(process: subprocess.Popen[str], request_id: int, timeout: float) -> dict[str, Any]:
    if process.stdout is None:
        raise AgentSecurityError("Codex hook trust probe has no stdout")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([process.stdout], [], [], remaining)
        if not readable:
            break
        line = process.stdout.readline()
        if not line:
            break
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == request_id:
            return payload
    raise AgentSecurityError(f"Codex hook trust probe timed out waiting for response {request_id}")


def codex_hook_trust_hash(binary: str, guard: Path, workspace: Path) -> str:
    command = [
        binary,
        "--strict-config",
        "--enable",
        "hooks",
        "-c",
        "mcp_servers={}",
        "-c",
        "plugins={}",
        "-c",
        "features={hooks=true}",
        "-c",
        _codex_hook_override(guard),
        "app-server",
        "--listen",
        "stdio://",
    ]
    env = dict(os.environ)
    env.pop("ALFRED_CODE_SECURITY_POLICY", None)
    try:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise AgentSecurityError(f"cannot start Codex hook trust probe: {exc}") from exc
    try:
        if process.stdin is None:
            raise AgentSecurityError("Codex hook trust probe has no stdin")
        process.stdin.write(
            json.dumps(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "alfred-code-controller", "version": "1"},
                        "capabilities": {},
                    },
                }
            )
            + "\n"
        )
        process.stdin.flush()
        initialized = _read_app_server_response(process, 1, 15)
        if "error" in initialized:
            raise AgentSecurityError(f"Codex hook trust probe initialization failed: {initialized['error']}")
        process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        process.stdin.write(
            json.dumps(
                {"id": 2, "method": "hooks/list", "params": {"cwds": [str(workspace)]}}
            )
            + "\n"
        )
        process.stdin.flush()
        response = _read_app_server_response(process, 2, 15)
        for entry in response.get("result", {}).get("data", []):
            for hook in entry.get("hooks", []):
                if (
                    hook.get("source") == "sessionFlags"
                    and hook.get("eventName") == "preToolUse"
                    and hook.get("command") == str(guard)
                ):
                    current_hash = str(hook.get("currentHash") or "")
                    if current_hash.startswith("sha256:"):
                        return current_hash
        raise AgentSecurityError("Codex did not report the scoped PreToolUse hook during trust probe")
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=2)


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
                "allowRead": [
                    str(path)
                    for path in dict.fromkeys(
                        (
                            manifest.workspace,
                            *_verification_dependency_paths(manifest),
                            _npm_shell_path(),
                        )
                    )
                ],
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


def _codex_isolation_arguments() -> list[str]:
    # Replace the complete integration tables. Per-entry dotted overrides can
    # be interpreted as quoted literal table names by some Codex builds and
    # fail configuration loading before the scoped profile is active.
    return [
        "-c",
        "mcp_servers={}",
        "-c",
        "plugins={}",
        "-c",
        "features={hooks=true}",
        "-c",
        "memories={generate_memories=false,use_memories=false}",
    ]


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
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise AgentSecurityError(f"configured {provider} executable is unavailable: {candidate}")
    trusted_candidates: dict[str, tuple[Path, ...]] = {
        "codex": (
            Path("/Applications/Codex.app/Contents/Resources/codex"),
            Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
            Path.home() / ".local/bin/codex",
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
        ),
        "claude": (
            Path.home() / ".local/bin/claude",
            Path("/opt/homebrew/bin/claude"),
            Path("/usr/local/bin/claude"),
        ),
    }
    for candidate in trusted_candidates.get(provider, ()):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    path = os.environ.get("PATH", "")
    for directory in path.split(os.pathsep):
        if not directory or Path(directory).expanduser().resolve() == Path.home() / ".superset/bin":
            continue
        candidate = Path(directory) / provider
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise AgentSecurityError(f"cannot find {provider} executable")


def provider_self_check(provider: str) -> dict[str, Any]:
    if provider not in {"claude", "codex"}:
        raise AgentSecurityError(f"unsupported scoped agent provider {provider!r}")
    binary = _provider_binary(provider)
    try:
        checked = subprocess.run(
            [binary, "--version"],
            text=True,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentSecurityError(f"cannot execute {provider} provider at {binary}: {exc}") from exc
    if checked.returncode != 0:
        detail = (checked.stderr or checked.stdout or f"exit code {checked.returncode}").strip()[:300]
        raise AgentSecurityError(f"{provider} provider self-check failed at {binary}: {detail}")
    return {
        "provider": provider,
        "provider_binary": binary,
        "provider_version": (checked.stdout or checked.stderr).strip()[:300],
    }


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
        conflict = _codex_legacy_sandbox_conflict(Path.home() / ".codex/config.toml")
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
        command.extend(_codex_isolation_arguments())
        command.extend(
            [
                "-c",
                f"projects={{{_toml_key(str(manifest.workspace))}={{trust_level=\"trusted\"}}}}",
                "exec",
            ]
        )
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
    started_at = _utcnow()
    if manifest.mode == "repair":
        _write_json_atomic(
            workspace / WORKER_RESULT,
            {
                "status": "retrying",
                "revision": LAUNCH_REVISION,
                "head_sha": manifest.head_sha,
                "handoff_token": manifest.handoff_token,
                "attempt": manifest.attempt,
            },
        )
    write_launch_status(
        workspace,
        "running",
        provider=provider,
        role=manifest.role,
        controller_job=manifest.controller_job,
        pid=os.getpid(),
        started_at=started_at,
        mode=manifest.mode,
        head_sha=manifest.head_sha or None,
        attempt=manifest.attempt or None,
    )
    env = dict(os.environ)
    env["ALFRED_CODE_SECURITY_POLICY"] = SECURITY_POLICY
    env["ALFRED_CODE_AGENT_ROLE"] = manifest.role
    env["PATH"] = _toolchain_path_value(_trusted_toolchain_paths())
    env["npm_config_scripts_prepend_node_path"] = "false"
    env["npm_config_script_shell"] = str(_npm_shell_path())
    try:
        npm_shell = _npm_shell_path()
        if not npm_shell.is_file() or not os.access(npm_shell, os.X_OK):
            raise AgentSecurityError(f"required npm verification shell is unavailable: {npm_shell}")
        prepare_dependency_overlay(manifest)
        if provider == "codex":
            digest = hashlib.sha256(f"{workspace}:{os.getpid()}".encode()).hexdigest()[:16]
            profile_name = f"alfred-scoped-{digest}"
            profile_path = Path.home() / ".codex" / f"{profile_name}.config.toml"
            guard = Path.home() / ".claude/bin/alfred-code-agent-guard"
            if not guard.is_file() or not os.access(guard, os.X_OK):
                raise AgentSecurityError(f"required Codex security guard is unavailable: {guard}")
            hook_trust_hash = codex_hook_trust_hash(
                _provider_binary("codex"), guard, workspace
            )
            profile_path.write_text(
                codex_profile(
                    manifest,
                    profile_path=profile_path,
                    hook_trust_hash=hook_trust_hash,
                )
            )
            profile_path.chmod(0o600)
            try:
                command = build_provider_command(
                    provider, arguments, manifest, profile_name=profile_name
                )
                exit_code = subprocess.call(command, cwd=workspace, env=env)
            finally:
                profile_path.unlink(missing_ok=True)
        else:
            guard = Path.home() / ".claude/bin/alfred-code-agent-guard"
            if not guard.is_file() or not os.access(guard, os.X_OK):
                raise AgentSecurityError(f"required Claude security guard is unavailable: {guard}")
            command = build_provider_command(provider, arguments, manifest, guard=guard)
            exit_code = subprocess.call(command, cwd=workspace, env=env)
    except Exception as exc:
        write_launch_status(
            workspace,
            "failed",
            provider=provider,
            role=manifest.role,
            controller_job=manifest.controller_job,
            exit_code=78,
            reason=f"{type(exc).__name__}: {exc}"[:500],
            started_at=started_at,
            finished_at=_utcnow(),
            mode=manifest.mode,
            head_sha=manifest.head_sha or None,
            attempt=manifest.attempt or None,
        )
        raise
    result_path = workspace / (WORKER_RESULT if manifest.role == "worker" else REVIEW_RESULT)
    result = None
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            result = None
    if manifest.role == "reviewer":
        valid_handoff = (
            isinstance(result, dict)
            and str(result.get("verdict") or "").lower() in {"pass", "fail"}
            and bool(str(result.get("head_sha") or ""))
        )
    else:
        valid_handoff = (
            isinstance(result, dict)
            and str(result.get("status") or "") in {"ready", "blocked"}
            and (
                manifest.mode != "repair"
                or (
                    str(result.get("head_sha") or "") == manifest.head_sha
                    and str(result.get("handoff_token") or "")
                    == manifest.handoff_token
                    and type(result.get("attempt")) is int
                    and result["attempt"] == manifest.attempt
                )
            )
        )
    write_launch_status(
        workspace,
        "completed" if valid_handoff else "exited",
        provider=provider,
        role=manifest.role,
        controller_job=manifest.controller_job,
        exit_code=exit_code,
        reason=(
            "agent returned after writing a valid result marker"
            if valid_handoff
            else "agent exited before writing its required result marker"
        ),
        started_at=started_at,
        finished_at=_utcnow(),
        mode=manifest.mode,
        head_sha=manifest.head_sha or None,
        attempt=manifest.attempt or None,
    )
    return exit_code


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
    r"(^|[;&|]\s*)(?:cp|tee|truncate)\b",
    r"\b(?:make|gmake|ninja)\s+(?:clean|distclean|clobber|mrproper)\b",
    r"\b(?:python(?:3)?|node|ruby|perl)\b[^\n]*(?:\.unlink\s*\(|\.rmdir\s*\(|\.write_(?:text|bytes)\s*\(|open\s*\([^)]*,\s*['\"](?:w|x|a))",
    r"(^|[;&|]\s]*)(?:eval|source)\b",
    r"(^|[^>])>(?!>)(?![ \t]*(?:&[012]\b|/dev/(?:null|stdout|stderr)\b))",
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
        patch = str(
            tool_input.get("patch")
            or tool_input.get("input")
            or tool_input.get("patch_text")
            or tool_input.get("patchText")
            or next(
                (
                    value
                    for value in tool_input.values()
                    if isinstance(value, str) and "*** Begin Patch" in value
                ),
                "",
            )
        )
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
    if values and values[0] == "--self-check":
        try:
            provider = provider_self_check(values[1]) if len(values) == 2 else {}
            if len(values) > 2:
                raise AgentSecurityError("self-check accepts at most one provider")
        except AgentSecurityError as exc:
            print(f"Alfred scoped-agent self-check failed: {exc}", file=sys.stderr)
            return 70
        print(
            json.dumps(
                {
                    "ok": True,
                    "policy": SECURITY_POLICY,
                    "python": list(sys.version_info[:3]),
                    **provider,
                },
                sort_keys=True,
            )
        )
        return 0
    if not values or values[0] not in {"claude", "codex"}:
        print("usage: alfred-code-agent claude|codex [provider arguments]", file=sys.stderr)
        return 64
    try:
        return launch(values[0], values[1:])
    except AgentSecurityError as exc:
        _record_startup_failure(f"AgentSecurityError: {exc}")
        print(f"Alfred scoped-agent launch refused: {exc}", file=sys.stderr)
        return 78
    except Exception as exc:
        _record_startup_failure(f"{type(exc).__name__}: {exc}")
        print(f"Alfred scoped-agent launch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
