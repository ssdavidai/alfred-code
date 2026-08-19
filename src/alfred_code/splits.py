from __future__ import annotations

from typing import Any

from .config import ControllerConfig
from .db import Database
from .github import GitHubClient
from .project import ProjectBoard


MAX_SPLIT_CHILDREN = 12
CHILD_MARKER = "<!-- alfred-code-split-child:{issue_number}:{plan_hash}:{job_id} -->"


def _lines(values: list[str]) -> str:
    return "\n".join(f"- `{value}`" for value in values) if values else "- None"


class IssueSplitter:
    """Materialize a 21-point plan as operator-authorized GitHub sub-issues."""

    def __init__(
        self,
        config: ControllerConfig,
        database: Database,
        project: ProjectBoard,
        github: GitHubClient,
    ):
        self.config = config
        self.database = database
        self.project = project
        self.github = github

    @staticmethod
    def proposed_children(
        issue_number: int,
        plan_hash: str,
        plan: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if int(plan.get("story_points") or 0) != 21:
            raise RuntimeError("only a 21-point plan may be split")
        jobs = plan.get("jobs") or []
        if not isinstance(jobs, list) or not jobs:
            raise RuntimeError("the current plan has no proposed jobs to split")
        if len(jobs) > MAX_SPLIT_CHILDREN:
            raise RuntimeError(
                f"the plan proposes {len(jobs)} children; the safe limit is {MAX_SPLIT_CHILDREN}"
            )
        children: list[dict[str, Any]] = []
        for job in jobs:
            job_id = str(job.get("id") or "").strip()
            title = str(job.get("title") or "").strip()
            lane = str(job.get("lane") or "").strip()
            if not job_id or not title or not lane:
                raise RuntimeError("every proposed child requires a job id, title, and lane")
            children.append(
                {
                    "job_id": job_id,
                    "marker": CHILD_MARKER.format(
                        issue_number=issue_number,
                        plan_hash=plan_hash,
                        job_id=job_id,
                    ),
                    "title": title,
                    "lane": lane,
                    "paths": [str(value) for value in job.get("paths") or []],
                    "verify": str(job.get("verify") or ""),
                    "contracts_read": [
                        str(value) for value in job.get("contracts_read") or []
                    ],
                    "contracts_changed": [
                        str(value) for value in job.get("contracts_changed") or []
                    ],
                    "depends_on": [str(value) for value in job.get("depends_on") or []],
                    "acceptance": [str(value) for value in job.get("acceptance") or []],
                }
            )
        return children

    @staticmethod
    def child_body(
        parent_issue_number: int,
        plan_hash: str,
        plan: dict[str, Any],
        spec: dict[str, Any],
    ) -> str:
        acceptance = spec["acceptance"] or [
            "Re-specify this bounded child against the latest source before implementation."
        ]
        return "\n".join(
            [
                spec["marker"],
                f"# Split child of #{parent_issue_number}",
                "",
                "This issue was created by an explicit operator action because the parent plan was estimated at 21 points. It is **not approved work**. It starts in **Inbox** and will be independently specified from the latest source only after it is prioritized into a sprint.",
                "",
                f"Source plan: `{plan_hash}`",
                f"Source base SHA: `{plan.get('base_sha') or 'unknown'}`",
                f"Proposed lane: `{spec['lane']}`",
                f"Proposed job identity: `{spec['job_id']}`",
                "",
                "## Parent planning context",
                "",
                str(plan.get("summary") or "No parent summary was recorded."),
                "",
                "## Proposed paths",
                "",
                _lines(spec["paths"]),
                "",
                "## Contracts read",
                "",
                _lines(spec["contracts_read"]),
                "",
                "## Contracts changed",
                "",
                _lines(spec["contracts_changed"]),
                "",
                "## Proposed dependencies",
                "",
                _lines(spec["depends_on"]),
                "",
                "## Acceptance evidence",
                "",
                "\n".join(f"- {value}" for value in acceptance),
                "",
                "## Proposed verification",
                "",
                f"`{spec['verify'] or 'To be determined during independent specification.'}`",
            ]
        )

    def split(self, issue_number: int) -> dict[str, Any]:
        if not self.config.github.project_number:
            raise RuntimeError("issue splitting requires the configured GitHub project")
        issue = self.database.get_issue(issue_number)
        current = self.database.current_plan(issue_number)
        if issue is None or current is None:
            raise KeyError(f"issue #{issue_number} has no current plan")
        plan_hash = str(current["plan_hash"])
        plan = current["plan"]
        if (
            current.get("status") != "needs_split"
            or issue.get("product_stage") != "needs_split"
        ):
            raise RuntimeError("only the current Needs splitting plan can create child issues")

        proposed = self.proposed_children(issue_number, plan_hash, plan)
        self.database.begin_issue_split(issue_number, plan_hash, proposed)
        split = self.database.issue_split(issue_number, plan_hash) or {}
        if split.get("status") == "completed":
            return split

        missing_markers = {
            child["marker"]
            for child in split.get("children") or []
            if not child.get("child_issue_number")
        }
        adopted = self.github.issues_by_markers(missing_markers)

        for child in split.get("children") or []:
            if child.get("status") == "ready":
                continue
            spec = child["spec"]
            job_id = str(child["job_id"])
            try:
                if child.get("child_issue_number"):
                    child_issue = self.github.issue(int(child["child_issue_number"]))
                else:
                    child_issue = adopted.get(str(child["marker"]))
                    if child_issue is None:
                        child_issue = self.github.create_issue(
                            title=f"[Split of #{issue_number}] {spec['title']}"[:256],
                            body=self.child_body(issue_number, plan_hash, plan, spec),
                        )
                    self.database.record_split_child_created(
                        issue_number,
                        plan_hash,
                        job_id,
                        int(child_issue["number"]),
                        str(child_issue["url"]),
                    )

                if not child.get("linked_at"):
                    self.github.add_sub_issue(issue_number, int(child_issue["number"]))
                    self.database.record_split_child_linked(issue_number, plan_hash, job_id)

                self.database.upsert_issue(child_issue)
                self.database.set_product_stage(
                    int(child_issue["number"]),
                    "inbox",
                    reason=f"operator split of #{issue_number}",
                )
                self.project.sync_issue(
                    project_number=int(self.config.github.project_number),
                    issue_url=str(child_issue["url"]),
                    controller_state="observed",
                    product_stage="inbox",
                )
                self.database.record_split_child_projected(issue_number, plan_hash, job_id)
            except Exception as exc:
                error = str(exc)
                self.database.fail_split_child(issue_number, plan_hash, job_id, error)
                self.database.fail_issue_split(issue_number, plan_hash, error)
                raise

        ready = self.database.issue_split(issue_number, plan_hash) or {}
        try:
            summary_url = self.github.post_split_summary(
                issue_number,
                plan_hash,
                ready.get("children") or [],
            )
            return self.database.complete_issue_split(issue_number, plan_hash, summary_url)
        except Exception as exc:
            self.database.fail_issue_split(issue_number, plan_hash, str(exc))
            raise
