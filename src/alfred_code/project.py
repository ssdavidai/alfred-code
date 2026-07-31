from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .config import GitHubConfig
from .errors import AuthorityUnavailable, CommandError
from .states import PRODUCT_STATUS, PROJECT_STATUS
from .util import run_json


STAGES = [
    "Backlog",
    "Inbox",
    "Sprint queue",
    "Specifying",
    "Approval",
    "Queued",
    "Building",
    "Reviewing",
    "Ready to merge",
    "Blocked",
    "Needs splitting",
    "Done",
]

PROJECT_DESCRIPTION = (
    "Alfred product control: draggable backlog, explicit sprints, lane-safe agents, "
    "review gates, velocity, and token economics."
)

PROJECT_README = """# Alfred Product Control

This private board is the product authority for Alfred delivery. **Backlog** is an agent-silent idea pile. Move ordered candidates to **Inbox**, then drag the work you want into **Sprint queue**. Neither Backlog nor Inbox spends model tokens.

Start the queued sprint from the local Alfred dashboard or with `alfred-code sprint-start`. The controller freezes the initial order, assigns the native Sprint iteration, specifies each issue, estimates Fibonacci story points from live code evidence, and waits for the SHA-bound approval comment. Work added after start is measured separately.

Approved work takes the highest-priority runnable lane without bypassing dependencies or scoped agent permissions. Superset owns isolated execution workspaces; GitHub issues, pull requests, checks, and immutable controller events remain authoritative. Nothing merges automatically.

A sprint closes when every card is Done or terminally Blocked. Done means merged. Blocked cards return to the top of Inbox with their specification, point estimate, token history, and blocker intact. A 21-point estimate is routed to Needs splitting and cannot be approved as one delivery.
"""


class ProjectBoard:
    def __init__(self, config: GitHubConfig, binary: str = "gh"):
        self.config = config
        self.binary = binary
        self._projects: dict[int, dict[str, Any]] = {}
        self._fields: dict[int, dict[str, dict[str, Any]]] = {}
        self._items: dict[int, dict[str, dict[str, Any]]] = {}
        self._ordered_items: dict[int, list[dict[str, Any]]] = {}

    def _json(self, args: list[str]) -> Any:
        try:
            return run_json([self.binary, *args])
        except CommandError as exc:
            raise AuthorityUnavailable(str(exc)) from exc

    def _graphql(self, query: str, variables: dict[str, Any]) -> Any:
        try:
            return run_json(
                [self.binary, "api", "graphql", "--input", "-"],
                input_text=json.dumps({"query": query, "variables": variables}),
            )
        except CommandError as exc:
            raise AuthorityUnavailable(str(exc)) from exc

    @staticmethod
    def _values(value: Any, key: str) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and isinstance(value.get(key), list):
            return value[key]
        return []

    def projects(self) -> list[dict[str, Any]]:
        return self._values(
            self._json(["project", "list", "--owner", self.config.owner, "--limit", "100", "--format", "json"]),
            "projects",
        )

    def setup(self) -> dict[str, Any]:
        project = next(
            (item for item in self.projects() if item.get("title") == self.config.project_title),
            None,
        )
        if project is None:
            project = self._json(
                [
                    "project",
                    "create",
                    "--owner",
                    self.config.owner,
                    "--title",
                    self.config.project_title,
                    "--format",
                    "json",
                ]
            )
        number = int(project["number"])
        fields = self.fields(number)
        wanted = {
            "Control stage": ("SINGLE_SELECT", STAGES),
            "Risk": ("SINGLE_SELECT", ["low", "medium", "high"]),
            "Plan hash": ("TEXT", []),
            "Lane set": ("TEXT", []),
            "Runtime": ("TEXT", []),
            "Story points": ("NUMBER", []),
        }
        for name, (data_type, options) in wanted.items():
            if name in fields:
                continue
            args = [
                "project",
                "field-create",
                str(number),
                "--owner",
                self.config.owner,
                "--name",
                name,
                "--data-type",
                data_type,
                "--format",
                "json",
            ]
            if options:
                args.extend(["--single-select-options", ",".join(options)])
            self._json(args)
        self._fields.pop(number, None)
        fields = self.fields(number)
        if "Control stage" in fields:
            self._ensure_select_options(fields["Control stage"], STAGES)
        self._fields.pop(number, None)
        fields = self.fields(number)
        if "Sprint" not in fields:
            self._create_iteration_field(str(project["id"]), "Sprint")
        self._fields.pop(number, None)
        self._json(
            [
                "project",
                "edit",
                str(number),
                "--owner",
                self.config.owner,
                "--description",
                PROJECT_DESCRIPTION,
                "--readme",
                PROJECT_README,
                "--format",
                "json",
            ]
        )
        return self._json(
            ["project", "view", str(number), "--owner", self.config.owner, "--format", "json"]
        )

    def fields(self, number: int) -> dict[str, dict[str, Any]]:
        if number in self._fields:
            return self._fields[number]
        values = self._values(
            self._json(
                [
                    "project",
                    "field-list",
                    str(number),
                    "--owner",
                    self.config.owner,
                    "--limit",
                    "100",
                    "--format",
                    "json",
                ]
            ),
            "fields",
        )
        self._fields[number] = {str(field.get("name")): field for field in values}
        return self._fields[number]

    def refresh(self, number: int, *, force: bool = False) -> None:
        if (
            not force
            and number in self._projects
            and number in self._fields
            and number in self._items
        ):
            return

        # Fetch a complete snapshot before replacing the last known-good one.
        # A rate-limit failure must not destroy the cache and trigger follow-up
        # queries from fields(), _item(), or sync_issue().
        project = self._json(
            ["project", "view", str(number), "--owner", self.config.owner, "--format", "json"]
        )
        field_values = self._values(
            self._json(
                [
                    "project",
                    "field-list",
                    str(number),
                    "--owner",
                    self.config.owner,
                    "--limit",
                    "100",
                    "--format",
                    "json",
                ]
            ),
            "fields",
        )
        values = self._values(
            self._json(
                [
                    "project",
                    "item-list",
                    str(number),
                    "--owner",
                    self.config.owner,
                    "--limit",
                    "500",
                    "--format",
                    "json",
                ]
            ),
            "items",
        )
        fields = {str(field.get("name")): field for field in field_values}
        items = {
            str((item.get("content") or {}).get("url")): item
            for item in values
            if (item.get("content") or {}).get("url")
        }

        self._projects[number] = project
        self._fields[number] = fields
        self._ordered_items[number] = values
        self._items[number] = items

    def _item(self, number: int, issue_url: str) -> dict[str, Any] | None:
        if number in self._items:
            return self._items[number].get(issue_url)
        values = self._values(
            self._json(
                [
                    "project",
                    "item-list",
                    str(number),
                    "--owner",
                    self.config.owner,
                    "--limit",
                    "500",
                    "--format",
                    "json",
                ]
            ),
            "items",
        )
        self._items[number] = {}
        self._ordered_items[number] = values
        for item in values:
            content = item.get("content") or {}
            url = content.get("url")
            if url:
                self._items[number][str(url)] = item
        return self._items[number].get(issue_url)

    def _edit(self, project_id: str, item_id: str, field: dict[str, Any], value: Any) -> None:
        args = [
            "project",
            "item-edit",
            "--id",
            item_id,
            "--project-id",
            project_id,
            "--field-id",
            str(field["id"]),
        ]
        field_type = str(field.get("type") or field.get("dataType") or "")
        if value is None or value == "":
            args.append("--clear")
        elif field_type in {"ProjectV2SingleSelectField", "SINGLE_SELECT"}:
            option = next(
                (option for option in field.get("options", []) if str(option.get("name")) == value),
                None,
            )
            if option is None:
                raise AuthorityUnavailable(f"project field {field.get('name')} has no option {value!r}")
            args.extend(["--single-select-option-id", str(option["id"])])
        elif field_type == "NUMBER" or str(field.get("dataType")) == "NUMBER" or field.get("name") == "Story points":
            args.extend(["--number", str(value)])
        elif field_type == "ProjectV2IterationField" or str(field.get("dataType")) == "ITERATION":
            args.extend(["--iteration-id", str(value)])
        else:
            args.extend(["--text", value])
        self._json([*args, "--format", "json"])

    def _ensure_select_options(self, field: dict[str, Any], wanted: list[str]) -> None:
        existing = list(field.get("options") or [])
        names = [str(option.get("name") or "") for option in existing]
        if set(wanted) <= set(names):
            return
        colors = ["GRAY", "BLUE", "PURPLE", "YELLOW", "ORANGE", "PINK", "GREEN", "RED"]
        options = []
        for index, name in enumerate([*names, *[value for value in wanted if value not in names]]):
            previous = next((option for option in existing if option.get("name") == name), {})
            option = {
                "name": name,
                "color": str(previous.get("color") or colors[index % len(colors)]).upper(),
                "description": str(previous.get("description") or ""),
            }
            if previous.get("id"):
                option["id"] = str(previous["id"])
            options.append(option)
        self._graphql(
            """
            mutation($input:UpdateProjectV2FieldInput!) {
              updateProjectV2Field(input:$input) { projectV2Field { ... on ProjectV2SingleSelectField { id name } } }
            }
            """,
            {"input": {"fieldId": str(field["id"]), "singleSelectOptions": options}},
        )

    def _create_iteration_field(self, project_id: str, name: str) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()
        result = self._graphql(
            """
            mutation($input:CreateProjectV2FieldInput!) {
              createProjectV2Field(input:$input) {
                projectV2Field { ... on ProjectV2IterationField { id name } }
              }
            }
            """,
            {
                "input": {
                    "projectId": project_id,
                    "dataType": "ITERATION",
                    "name": name,
                    "iterationConfiguration": {
                        "startDate": today,
                        "duration": 14,
                        "iterations": [],
                    },
                }
            },
        )
        return ((result.get("data") or {}).get("createProjectV2Field") or {}).get(
            "projectV2Field"
        ) or {}

    def ensure_sprint_iteration(
        self,
        project_number: int,
        *,
        title: str,
        starts_at: str,
        duration_days: int,
    ) -> str:
        self.refresh(project_number, force=True)
        field = self.fields(project_number).get("Sprint")
        if field is None:
            project = self._projects[project_number]
            self._create_iteration_field(str(project["id"]), "Sprint")
            self._fields.pop(project_number, None)
            field = self.fields(project_number).get("Sprint")
        if field is None:
            raise AuthorityUnavailable("Sprint iteration field could not be created")
        result = self._graphql(
            """
            query($id:ID!) {
              node(id:$id) { ... on ProjectV2IterationField {
                id configuration {
                  duration
                  iterations { id title startDate duration }
                  completedIterations { id title startDate duration }
                }
              } }
            }
            """,
            {"id": str(field["id"])},
        )
        node = (result.get("data") or {}).get("node") or {}
        configuration = node.get("configuration") or {}
        iterations = [
            dict(value)
            for value in [
                *(configuration.get("completedIterations") or []),
                *(configuration.get("iterations") or []),
            ]
        ]
        existing = next((value for value in iterations if value.get("title") == title), None)
        if existing:
            return str(existing["id"])
        start_date = starts_at[:10]
        active_iterations = [dict(value) for value in configuration.get("iterations") or []]
        active_iterations.append(
            {"title": title, "startDate": start_date, "duration": duration_days}
        )
        anchor = min(
            [str(value.get("startDate")) for value in active_iterations if value.get("startDate")]
            or [start_date]
        )
        self._graphql(
            """
            mutation($input:UpdateProjectV2FieldInput!) {
              updateProjectV2Field(input:$input) { projectV2Field { ... on ProjectV2IterationField { id } } }
            }
            """,
            {
                "input": {
                    "fieldId": str(field["id"]),
                    "iterationConfiguration": {
                        "startDate": anchor,
                        "duration": duration_days,
                        "iterations": [
                            {
                                "title": str(value["title"]),
                                "startDate": str(value["startDate"]),
                                "duration": int(value.get("duration") or duration_days),
                            }
                            for value in active_iterations
                        ],
                    },
                }
            },
        )
        refreshed = self._graphql(
            """
            query($id:ID!) { node(id:$id) { ... on ProjectV2IterationField {
              configuration { iterations { id title } completedIterations { id title } }
            } } }
            """,
            {"id": str(field["id"])},
        )
        current = ((refreshed.get("data") or {}).get("node") or {}).get("configuration") or {}
        found = next(
            (
                value
                for value in [
                    *(current.get("iterations") or []),
                    *(current.get("completedIterations") or []),
                ]
                if value.get("title") == title
            ),
            None,
        )
        if not found:
            raise AuthorityUnavailable(f"GitHub did not persist iteration {title!r}")
        return str(found["id"])

    def delivery_items(self, project_number: int) -> list[dict[str, Any]]:
        if project_number not in self._ordered_items:
            self.refresh(project_number)
        result = []
        for rank, item in enumerate(self._ordered_items.get(project_number, [])):
            content = item.get("content") or {}
            if not content.get("url") or content.get("number") is None:
                continue
            result.append(
                {
                    "item_id": str(item.get("id") or ""),
                    "issue_url": str(content["url"]),
                    "issue_number": int(content["number"]),
                    "stage": str(item.get("control stage") or ""),
                    "rank": rank,
                    "story_points": item.get("story points"),
                    "sprint": item.get("sprint"),
                }
            )
        return result

    def move_to_top(self, project_number: int, issue_urls: list[str]) -> None:
        project = self._projects.get(project_number)
        if project is None:
            self.refresh(project_number)
            project = self._projects[project_number]
        for issue_url in reversed(issue_urls):
            item = self._item(project_number, issue_url)
            if not item:
                continue
            self._graphql(
                """
                mutation($input:UpdateProjectV2ItemPositionInput!) {
                  updateProjectV2ItemPosition(input:$input) { clientMutationId }
                }
                """,
                {
                    "input": {
                        "projectId": str(project["id"]),
                        "itemId": str(item["id"]),
                        "afterId": None,
                    }
                },
            )

    def sync_issue(
        self,
        *,
        project_number: int,
        issue_url: str,
        controller_state: str,
        product_stage: str = "legacy_active",
        plan_hash: str = "",
        risk: str = "",
        lanes: list[str] | None = None,
        runtime: str = "",
        story_points: int | None = None,
        iteration_id: str | None = None,
        iteration_title: str | None = None,
    ) -> None:
        project = self._projects.get(project_number)
        if project is None:
            project = self._json(
                ["project", "view", str(project_number), "--owner", self.config.owner, "--format", "json"]
            )
            self._projects[project_number] = project
        item = self._item(project_number, issue_url)
        if item is None:
            item = self._json(
                [
                    "project",
                    "item-add",
                    str(project_number),
                    "--owner",
                    self.config.owner,
                    "--url",
                    issue_url,
                    "--format",
                    "json",
                ]
            )
            self._items.setdefault(project_number, {})[issue_url] = item
        fields = self.fields(project_number)
        values = {
            "Control stage": (
                PRODUCT_STATUS[product_stage]
                if product_stage in PRODUCT_STATUS
                else PROJECT_STATUS.get(controller_state, "Inbox")
            ),
            "Plan hash": plan_hash[:12],
            "Risk": risk,
            "Lane set": ", ".join(lanes or []),
            "Runtime": runtime,
            "Story points": story_points,
            "Sprint": iteration_id,
        }
        for name, value in values.items():
            if name not in fields:
                continue
            key = name.lower()
            comparison = iteration_title if name == "Sprint" else value
            if value is None and item.get(key) in {None, ""}:
                continue
            if comparison is not None and str(item.get(key) or "") == str(comparison):
                continue
            self._edit(str(project["id"]), str(item["id"]), fields[name], value)
            item[key] = value
