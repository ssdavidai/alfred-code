from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

from .errors import PlanValidationError
from .util import content_hash, unique


LANE_BRANCH = {
    "I": "lane-1",
    "II": "lane-2",
    "III": "lane-3",
    "IV": "lane-4",
    "V": "lane-5",
    "VI": "lane-6",
    "VII": "lane-7",
    "phase0": "phase0",
}


def path_matches(path: str, pattern: str) -> bool:
    path = path.removeprefix("./").rstrip("/")
    pattern = pattern.removeprefix("./").rstrip("/")
    if pattern == "**":
        return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    if pattern.startswith("**/"):
        suffix = pattern[3:]
        return path == suffix or path.endswith("/" + suffix) or fnmatch.fnmatch(path, pattern)
    return path == pattern or fnmatch.fnmatch(path, pattern)


def patterns_may_overlap(left: str, right: str) -> bool:
    if left == right or path_matches(left, right) or path_matches(right, left):
        return True
    left_prefix = left[:-3].rstrip("/") if left.endswith("/**") else None
    right_prefix = right[:-3].rstrip("/") if right.endswith("/**") else None
    if left_prefix and right_prefix:
        return left_prefix.startswith(right_prefix + "/") or right_prefix.startswith(left_prefix + "/")
    return False


class LanePolicy:
    def __init__(self, source: Path, data: dict[str, Any]):
        self.source = source
        self.data = data
        self.lanes = data.get("lanes", {})
        self.forbidden = data.get("forbidden_zone", [])

    @classmethod
    def load(cls, source: Path) -> "LanePolicy":
        try:
            text = source.read_text()
        except OSError as exc:
            raise PlanValidationError([f"cannot load lane authority {source}: {exc}"]) from exc
        return cls.from_text(source, text)

    @classmethod
    def from_text(cls, source: Path, text: str) -> "LanePolicy":
        try:
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanValidationError([f"cannot load lane authority {source}: {exc}"]) from exc
        if not isinstance(data.get("lanes"), dict) or not data["lanes"]:
            raise PlanValidationError([f"lane authority {source} has no lanes"])
        return cls(source, data)

    def lane_for_path(self, path: str) -> list[str]:
        return [
            lane
            for lane, rules in self.lanes.items()
            if lane != "phase0" and any(path_matches(path, pattern) for pattern in rules.get("allowed", []))
        ]


class PlanValidator:
    def __init__(self, policy: LanePolicy):
        self.policy = policy

    def validate(
        self,
        raw: dict[str, Any],
        *,
        issue_number: int,
        base_sha: str,
        issue_body_hash: str = "",
        decision_context_hash: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        problems: list[str] = []
        if not isinstance(raw, dict):
            raise PlanValidationError(["planner output must be a JSON object"])
        try:
            planned_issue = int(raw.get("issue"))
        except (TypeError, ValueError):
            planned_issue = -1
        if planned_issue != issue_number:
            problems.append(f"plan issue {raw.get('issue')!r} does not match #{issue_number}")
        if raw.get("base_sha") != base_sha:
            problems.append("plan base_sha does not match the freshly observed default branch SHA")
        jobs = raw.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            problems.append("jobs must be a non-empty array")
            jobs = []

        normalized_jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_branches: set[str] = set()
        for index, job in enumerate(jobs):
            label = f"jobs[{index}]"
            if not isinstance(job, dict):
                problems.append(f"{label} must be an object")
                continue
            job_id = str(job.get("id") or "").strip()
            lane = str(job.get("lane") or "").strip()
            title = str(job.get("title") or "").strip()
            branch = str(job.get("branch") or "").strip()
            paths = job.get("paths")
            verify = str(job.get("verify") or "").strip()
            depends = job.get("depends_on", [])
            contracts_read = job.get("contracts_read", [])
            contracts_changed = job.get("contracts_changed", [])
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", job_id):
                problems.append(f"{label}.id must use lowercase letters, digits, dot, underscore, or dash")
            elif not re.search(rf"(^|[-_.]){issue_number}($|[-_.])", job_id):
                problems.append(f"{label}.id must include issue number {issue_number} as a delimited token")
            elif job_id in seen_ids:
                problems.append(f"duplicate job id {job_id}")
            seen_ids.add(job_id)
            if lane not in self.policy.lanes:
                problems.append(f"{label}.lane {lane!r} is not defined by {self.policy.source}")
            if not title:
                problems.append(f"{label}.title is required")
            expected_prefix = LANE_BRANCH.get(lane)
            if expected_prefix and not re.fullmatch(rf"{re.escape(expected_prefix)}/{issue_number}-[a-z0-9][a-z0-9-]*", branch):
                problems.append(f"{label}.branch must match {expected_prefix}/{issue_number}-<slug>")
            elif branch in seen_branches:
                problems.append(f"duplicate job branch {branch}")
            seen_branches.add(branch)
            if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and path.strip() for path in paths):
                problems.append(f"{label}.paths must be a non-empty array of repository-relative paths or globs")
                paths = []
            paths = unique(path.removeprefix("./") for path in paths)
            if any(path.startswith("/") or ".." in Path(path).parts for path in paths):
                problems.append(f"{label}.paths must stay inside the repository")
            if lane in self.policy.lanes:
                allowed = self.policy.lanes[lane].get("allowed", [])
                for path in paths:
                    if not any(path_matches(path, pattern) for pattern in allowed):
                        problems.append(f"{label} path {path!r} is outside lane {lane}")
                    if lane != "phase0" and any(path_matches(path, pattern) for pattern in self.policy.forbidden):
                        problems.append(f"{label} path {path!r} is Phase-0-owned")
            if lane in self.policy.lanes:
                expected_verify = str(self.policy.lanes[lane].get("verify") or "").strip()
                if not expected_verify:
                    problems.append(f"lane {lane} has no authoritative verification command")
                else:
                    # Planner output is untrusted. The live lane policy, not the model or issue text,
                    # owns the only shell command the trusted controller may execute.
                    verify = expected_verify
            elif not verify:
                problems.append(f"{label}.verify is required")
            if not isinstance(depends, list) or not all(isinstance(value, str) for value in depends):
                problems.append(f"{label}.depends_on must be an array of job IDs")
                depends = []
            if not isinstance(contracts_read, list) or not all(isinstance(value, str) for value in contracts_read):
                problems.append(f"{label}.contracts_read must be an array of paths")
                contracts_read = []
            if not isinstance(contracts_changed, list) or not all(isinstance(value, str) for value in contracts_changed):
                problems.append(f"{label}.contracts_changed must be an array of paths")
                contracts_changed = []
            if contracts_changed and lane != "phase0":
                problems.append(f"{label} changes contracts but is not the phase0 job")
            normalized_jobs.append(
                {
                    "id": job_id,
                    "lane": lane,
                    "title": title,
                    "branch": branch,
                    "paths": paths,
                    "verify": verify,
                    "contracts_read": unique(contracts_read),
                    "contracts_changed": unique(contracts_changed),
                    "depends_on": unique(depends),
                    "acceptance": unique(str(x).strip() for x in job.get("acceptance", []) if str(x).strip()),
                }
            )

        # Phase-0 sequencing is controller policy, not model judgment. A valid
        # phase-0 job is always the root, so discard model-supplied dependencies
        # on that job and canonically add it to every downstream lane. The rest
        # of the graph is still validated below for cycles and unknown jobs.
        phase0_ids = {job["id"] for job in normalized_jobs if job["lane"] == "phase0"}
        if len(phase0_ids) > 1:
            problems.append("phase0 appears more than once; contract changes require one root job")
        for job in normalized_jobs:
            if job["lane"] == "phase0":
                job["depends_on"] = []
        if len(phase0_ids) == 1:
            phase0_id = next(iter(phase0_ids))
            for job in normalized_jobs:
                if job["lane"] != "phase0":
                    job["depends_on"] = unique([*job["depends_on"], phase0_id])

        ids = {job["id"] for job in normalized_jobs}
        graph = {job["id"]: job["depends_on"] for job in normalized_jobs}
        for job_id, dependencies in graph.items():
            for dependency in dependencies:
                if dependency not in ids:
                    problems.append(f"job {job_id} depends on unknown job {dependency}")
                if dependency == job_id:
                    problems.append(f"job {job_id} depends on itself")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(job_id: str) -> None:
            if job_id in visiting:
                problems.append(f"dependency cycle includes {job_id}")
                return
            if job_id in visited:
                return
            visiting.add(job_id)
            for dependency in graph.get(job_id, []):
                if dependency in graph:
                    visit(dependency)
            visiting.remove(job_id)
            visited.add(job_id)

        for job_id in graph:
            visit(job_id)

        def transitively_depends_on(job_id: str, dependency_id: str) -> bool:
            pending = list(graph.get(job_id, []))
            inspected: set[str] = set()
            while pending:
                candidate = pending.pop()
                if candidate == dependency_id:
                    return True
                if candidate in inspected:
                    continue
                inspected.add(candidate)
                pending.extend(graph.get(candidate, []))
            return False

        for left_index, left in enumerate(normalized_jobs):
            for right in normalized_jobs[left_index + 1 :]:
                same_lane = left["lane"] == right["lane"]
                sequential = transitively_depends_on(
                    left["id"], right["id"]
                ) or transitively_depends_on(right["id"], left["id"])
                if same_lane and not sequential:
                    problems.append(
                        f"jobs {left['id']} and {right['id']} share lane {left['lane']} without a dependency chain"
                    )
                for left_path in left["paths"]:
                    for right_path in right["paths"]:
                        if patterns_may_overlap(left_path, right_path) and not (
                            same_lane and sequential
                        ):
                            problems.append(
                                f"jobs {left['id']} and {right['id']} may both write {left_path!r}/{right_path!r}"
                            )

        if phase0_ids:
            for job in normalized_jobs:
                if job["lane"] != "phase0" and not phase0_ids.intersection(job["depends_on"]):
                    problems.append(f"job {job['id']} must depend directly on the phase0 contract job")

        if problems:
            raise PlanValidationError(unique(problems))

        normalized = {
            "schema": 1,
            "issue": issue_number,
            "base_sha": base_sha,
            "issue_body_hash": issue_body_hash,
            "summary": str(raw.get("summary") or "").strip(),
            "risk": str(raw.get("risk") or "medium").strip().lower(),
            "jobs": normalized_jobs,
        }
        if decision_context_hash is not None:
            normalized["decision_context_hash"] = decision_context_hash
        return normalized, content_hash(normalized)
