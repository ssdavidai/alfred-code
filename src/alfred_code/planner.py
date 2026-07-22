from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .audit import AuditLog
from .config import ControllerConfig
from .errors import CommandError, PlanValidationError
from .github import GitHubClient
from .plans import LanePolicy, PlanValidator
from .util import content_hash, run


def extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    candidates = [stripped]
    if "```" in stripped:
        pieces = stripped.split("```")
        candidates.extend(piece.removeprefix("json").strip() for piece in pieces[1::2])
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        for index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise PlanValidationError([f"planner returned no JSON object (output length: {len(text)})"])


def plan_json_schema(issue_number: int, base_sha: str) -> dict[str, Any]:
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "required": [
            "issue",
            "base_sha",
            "summary",
            "risk",
            "story_points",
            "points_evidence",
            "issue_dependencies",
            "jobs",
        ],
        "additionalProperties": False,
        "properties": {
            "issue": {"type": "integer", "const": issue_number},
            "base_sha": {"type": "string", "const": base_sha},
            "summary": {"type": "string", "minLength": 1},
            "risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "story_points": {"type": "integer", "enum": [1, 2, 3, 5, 8, 13, 21]},
            "points_evidence": {"type": "string", "minLength": 1},
            "issue_dependencies": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "uniqueItems": True,
            },
            "jobs": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": [
                        "id",
                        "lane",
                        "title",
                        "branch",
                        "paths",
                        "verify",
                        "contracts_read",
                        "contracts_changed",
                        "depends_on",
                        "acceptance",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "lane": {"type": "string"},
                        "title": {"type": "string", "minLength": 1},
                        "branch": {"type": "string", "minLength": 1},
                        "paths": {**string_array, "minItems": 1},
                        "verify": {"type": "string", "minLength": 1},
                        "contracts_read": string_array,
                        "contracts_changed": string_array,
                        "depends_on": string_array,
                        "acceptance": {**string_array, "minItems": 1},
                    },
                },
            },
        },
    }


def structured_planner_command(
    command: tuple[str, ...],
    issue_number: int,
    base_sha: str,
    *,
    schema_path: Path | None = None,
) -> list[str]:
    argv = list(command)
    provider = planner_provider(command)
    if provider == "claude":
        if "--output-format" not in argv:
            argv.extend(["--output-format", "json"])
    if provider == "claude" and "--json-schema" not in argv:
        argv.extend(
            [
                "--json-schema",
                json.dumps(plan_json_schema(issue_number, base_sha), separators=(",", ":")),
            ]
        )
    if provider == "codex":
        if schema_path is None:
            raise ValueError("Codex planner requires a JSON schema file")
        if argv[-1:] == ["-"]:
            argv.pop()
        if "--output-schema" not in argv:
            argv.extend(["--output-schema", str(schema_path)])
        if "--json" not in argv:
            argv.append("--json")
        argv.append("-")
    return argv


def planner_provider(command: tuple[str, ...]) -> str:
    name = Path(command[0]).name
    if name in {"claude", "codex"}:
        return name
    return "custom"


def planner_model(command: tuple[str, ...]) -> str:
    for index, argument in enumerate(command[:-1]):
        if argument in {"--model", "-m"}:
            return command[index + 1]
    return "configured default"


def _codex_jsonl_result(text: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            events.append(value)
    if not events or not any(event.get("type") == "turn.completed" for event in events):
        return None
    messages = [
        str(item.get("text") or "")
        for event in events
        if event.get("type") == "item.completed"
        and isinstance((item := event.get("item")), dict)
        and item.get("type") == "agent_message"
    ]
    if not messages:
        raise PlanValidationError(["Codex planner completed without a final agent message"])
    usage_events = [event for event in events if event.get("type") == "turn.completed"]
    usage = usage_events[-1].get("usage") if usage_events else {}
    if not isinstance(usage, dict):
        usage = {}
    thread = next((event for event in events if event.get("type") == "thread.started"), {})
    return extract_json(messages[-1]), {
        "provider": "codex",
        "session_id": str(thread.get("thread_id") or ""),
        "num_turns": len(usage_events),
        "usage": usage,
    }


def extract_planner_result(text: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return the structured plan and an optional provider usage envelope."""
    codex = _codex_jsonl_result(text)
    if codex is not None:
        return codex
    try:
        envelope = json.loads(text.strip())
    except json.JSONDecodeError:
        return extract_json(text), None
    if not isinstance(envelope, dict):
        return extract_json(text), None
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured, envelope
    result = envelope.get("result")
    if isinstance(result, dict):
        return result, envelope
    if isinstance(result, str):
        return extract_json(result), envelope
    return envelope, None


@dataclass(frozen=True)
class PreparedPlan:
    """Immutable, read-only planner input captured at one repository/GitHub snapshot."""

    issue_number: int
    base_sha: str
    issue_body_hash: str
    decision_context_hash: str
    policy: LanePolicy
    prompt: str


class Planner:
    def __init__(self, config: ControllerConfig, github: GitHubClient, validator: PlanValidator):
        self.config = config
        self.github = github
        self.validator = validator
        self.telemetry = AuditLog(config.state_dir / "planner-telemetry.jsonl")

    def _record_usage(
        self,
        issue_number: int,
        envelope: dict[str, Any],
        *,
        started_at: str,
        duration_ms: int,
    ) -> None:
        usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
        provider = str(envelope.get("provider") or planner_provider(self.config.planner_command))
        model = str(envelope.get("model") or planner_model(self.config.planner_command))
        model_usage = (
            envelope.get("modelUsage") if isinstance(envelope.get("modelUsage"), dict) else {}
        )
        if not model_usage and provider == "codex":
            model_usage = {model: usage}
        self.telemetry.write(
            "planner.usage",
            issue_number=issue_number,
            provider=provider,
            model=model,
            session_id=str(envelope.get("session_id") or ""),
            started_at=started_at,
            duration_ms=int(envelope.get("duration_ms") or duration_ms),
            duration_api_ms=int(envelope.get("duration_api_ms") or 0),
            num_turns=int(envelope.get("num_turns") or 0),
            usage=usage,
            model_usage=model_usage,
        )

    def repository_evidence(self, base_sha: str) -> str:
        repo = self.config.repo_path
        tree = run(["git", "ls-tree", "-r", "--name-only", base_sha], cwd=repo, timeout=120)
        files = tree.splitlines()
        evidence_files = [
            path
            for path in files
            if path.endswith(("CONTRACT.md", "package.json", "pyproject.toml", "schema.sql"))
            or path in {"scripts/hooks/lanes.json", "docker-compose.yaml", "Makefile"}
        ][:40]
        sections = ["REPOSITORY FILE TREE (live git object):", "\n".join(files[:5000])]
        remaining = 250_000
        for path in evidence_files:
            if remaining <= 0:
                break
            try:
                content = run(["git", "show", f"{base_sha}:{path}"], cwd=repo, timeout=30)
            except CommandError:
                continue
            excerpt = content[: min(12_000, remaining)]
            remaining -= len(excerpt)
            sections.extend([f"\n--- {path} (live at {base_sha[:12]}) ---", excerpt])
        return "\n".join(sections)

    def policy_at(self, base_sha: str) -> tuple[LanePolicy, str]:
        relative = self.config.lane_policy_path.relative_to(self.config.repo_path).as_posix()
        text = run(["git", "show", f"{base_sha}:{relative}"], cwd=self.config.repo_path, timeout=30)
        return LanePolicy.from_text(Path(f"{base_sha}:{relative}"), text), text

    def prompt(
        self,
        issue: dict[str, Any],
        base_sha: str,
        policy_text: str,
        decision_comments: list[dict[str, str]],
        *,
        open_prs: list[dict[str, Any]] | None = None,
        evidence: str | None = None,
    ) -> str:
        if open_prs is None:
            open_prs = self.github.open_prs()
        if evidence is None:
            evidence = self.repository_evidence(base_sha)
        return f"""You are specifying GitHub issue #{issue['number']} for deterministic multi-agent execution.

The plan is executable input, not prose. Trust the live source tree, tests, git objects, and current GitHub state. Markdown documents are claims: use them to find intent, but verify every claim against code and configuration. Do not modify anything. Do not create branches or issues. Return exactly one JSON object and no commentary.

ISSUE TITLE:
{issue.get('title', '')}

ISSUE BODY:
{issue.get('body', '')}

OPERATOR DECISION COMMENTS:
{json.dumps(decision_comments, indent=2)}

PINNED DEFAULT-BRANCH SHA:
{base_sha}

OPEN PULL REQUESTS:
{json.dumps(open_prs, indent=2)}

LANE AUTHORITY (enforced later by code):
{policy_text}

{evidence}

Required schema:
{{
  "issue": {issue['number']},
  "base_sha": "{base_sha}",
  "summary": "specific implementation summary",
  "risk": "low|medium|high",
  "story_points": 5,
  "points_evidence": "short evidence-based estimate using lanes, contracts, jobs, risk, and verification scope",
  "issue_dependencies": [123],
  "jobs": [
    {{
      "id": "api-{issue['number']}",
      "lane": "I|II|III|IV|V|phase0",
      "title": "bounded deliverable",
      "branch": "lane-N/{issue['number']}-slug or phase0/{issue['number']}-slug",
      "paths": ["smallest accurate repository path or glob"],
      "verify": "real lane-specific verification command",
      "contracts_read": ["contract paths inspected"],
      "contracts_changed": ["contract paths changed, phase0 only"],
      "depends_on": ["job-id"],
      "acceptance": ["observable behavior and test evidence"]
    }}
  ]
}}

Estimate the whole issue, not individual jobs. Use this calibration: 1 is a trivial bounded change; 2 is a small single-lane change; 3 is a normal single-lane implementation with tests; 5 has multiple bounded jobs or meaningful contract reads; 8 crosses lanes or contracts; 13 is a large coordinated delivery; 21 means the issue is too large for one sprint and must be split. Only list issue_dependencies that must be Done before implementation can safely begin.

Rules: normally use one job per lane, and always one agent per job. Jobs in different lanes must not have overlapping write paths. If a trusted `alfred-code-auto-replan` evidence block reports a lane scope-limit failure, decompose only that lane into multiple small jobs: give each a distinct ID and branch, order them with an explicit dependency chain, keep every individual job below the reported cap, and repeat a write path only when sequential work genuinely must modify the same file. The controller still permits only one active agent in a lane. If that evidence reports an absent cross-lane contract, add one phase0 contract job before its consumers; do not let a lane invent the interface. If it reports missing declared Python dependencies, retain the authoritative full verification because the controller provisions those dependencies in an isolated environment. Preserve completed PR work listed in the evidence instead of planning it again. Use phase0 only for forbidden-zone or contract changes, and make every downstream lane depend directly on it. Identify all impacted lanes from actual code. Do not invent lanes VI/VII unless the live authority defines them. Every verify command must exercise the real package. Never include merge, deletion, deployment, or secret-reading steps.
"""

    def prepare_plans(self, issue_numbers: list[int]) -> list[PreparedPlan]:
        """Capture shared repository evidence once before parallel model execution."""
        if not issue_numbers:
            return []
        base_sha = self.github.default_branch_sha()
        run(["git", "fetch", "--no-tags", "origin", base_sha], cwd=self.config.repo_path, timeout=120)
        policy, policy_text = self.policy_at(base_sha)
        evidence = self.repository_evidence(base_sha)
        open_prs = self.github.open_prs()
        prepared: list[PreparedPlan] = []
        for issue_number in issue_numbers:
            issue = self.github.issue(issue_number)
            decision_comments = self.github.decision_comments(issue_number)
            decision_context_hash = content_hash(
                {
                    "body": str(issue.get("body") or ""),
                    "comments": decision_comments,
                }
            )
            prepared.append(
                PreparedPlan(
                    issue_number=issue_number,
                    base_sha=base_sha,
                    issue_body_hash=content_hash(str(issue.get("body") or "")),
                    decision_context_hash=decision_context_hash,
                    policy=policy,
                    prompt=self.prompt(
                        issue,
                        base_sha,
                        policy_text,
                        decision_comments,
                        open_prs=open_prs,
                        evidence=evidence,
                    ),
                )
            )
        return prepared

    def execute_plan(self, prepared: PreparedPlan) -> tuple[dict[str, Any], str]:
        """Run only the isolated model command; safe to call in a bounded worker thread."""
        started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        started = time.monotonic()
        provider = planner_provider(self.config.planner_command)
        if provider == "codex":
            with TemporaryDirectory(prefix=f"alfred-code-plan-{prepared.issue_number}-") as directory:
                schema_path = Path(directory) / "plan.schema.json"
                schema_path.write_text(
                    json.dumps(plan_json_schema(prepared.issue_number, prepared.base_sha))
                )
                output = run(
                    structured_planner_command(
                        self.config.planner_command,
                        prepared.issue_number,
                        prepared.base_sha,
                        schema_path=schema_path,
                    ),
                    cwd=self.config.repo_path,
                    input_text=prepared.prompt,
                    timeout=self.config.planner_timeout_seconds,
                )
        else:
            output = run(
                structured_planner_command(
                    self.config.planner_command,
                    prepared.issue_number,
                    prepared.base_sha,
                ),
                cwd=self.config.repo_path,
                input_text=prepared.prompt,
                timeout=self.config.planner_timeout_seconds,
            )
        raw, envelope = extract_planner_result(output)
        if envelope is not None:
            self._record_usage(
                prepared.issue_number,
                envelope,
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        return PlanValidator(prepared.policy).validate(
            raw,
            issue_number=prepared.issue_number,
            base_sha=prepared.base_sha,
            issue_body_hash=prepared.issue_body_hash,
            decision_context_hash=prepared.decision_context_hash,
        )

    def plan_issue(self, issue_number: int) -> tuple[dict[str, Any], str]:
        return self.execute_plan(self.prepare_plans([issue_number])[0])

    def revalidate(self, plan: dict[str, Any], expected_hash: str) -> None:
        run(
            ["git", "fetch", "--no-tags", "origin", plan["base_sha"]],
            cwd=self.config.repo_path,
            timeout=120,
        )
        policy, _ = self.policy_at(plan["base_sha"])
        normalized, actual_hash = PlanValidator(policy).validate(
            plan,
            issue_number=int(plan["issue"]),
            base_sha=str(plan["base_sha"]),
            issue_body_hash=str(plan.get("issue_body_hash") or ""),
            decision_context_hash=plan.get("decision_context_hash"),
        )
        if actual_hash != expected_hash or normalized != plan:
            raise PlanValidationError(
                [f"stored plan does not reproduce its immutable hash {expected_hash}"]
            )
