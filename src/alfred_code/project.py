from __future__ import annotations

from typing import Any

from .config import GitHubConfig
from .errors import AuthorityUnavailable, CommandError
from .states import PROJECT_STATUS
from .util import run_json


STAGES = [
    "Inbox",
    "Specifying",
    "Approval",
    "Queued",
    "Building",
    "Reviewing",
    "Ready to merge",
    "Blocked",
    "Done",
]


class ProjectBoard:
    def __init__(self, config: GitHubConfig, binary: str = "gh"):
        self.config = config
        self.binary = binary

    def _json(self, args: list[str]) -> Any:
        try:
            return run_json([self.binary, *args])
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
        return self._json(
            ["project", "view", str(number), "--owner", self.config.owner, "--format", "json"]
        )

    def fields(self, number: int) -> dict[str, dict[str, Any]]:
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
        return {str(field.get("name")): field for field in values}

    def _item(self, number: int, issue_url: str) -> dict[str, Any] | None:
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
        for item in values:
            content = item.get("content") or {}
            if content.get("url") == issue_url:
                return item
        return None

    def _edit(self, project_id: str, item_id: str, field: dict[str, Any], value: str) -> None:
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
        if field.get("type") == "ProjectV2SingleSelectField" or field.get("dataType") == "SINGLE_SELECT":
            option = next(
                (option for option in field.get("options", []) if str(option.get("name")) == value),
                None,
            )
            if option is None:
                raise AuthorityUnavailable(f"project field {field.get('name')} has no option {value!r}")
            args.extend(["--single-select-option-id", str(option["id"])])
        else:
            args.extend(["--text", value])
        self._json([*args, "--format", "json"])

    def sync_issue(
        self,
        *,
        project_number: int,
        issue_url: str,
        controller_state: str,
        plan_hash: str = "",
        risk: str = "",
        lanes: list[str] | None = None,
        runtime: str = "",
    ) -> None:
        project = self._json(
            ["project", "view", str(project_number), "--owner", self.config.owner, "--format", "json"]
        )
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
        fields = self.fields(project_number)
        values = {
            "Control stage": PROJECT_STATUS.get(controller_state, "Inbox"),
            "Plan hash": plan_hash[:12],
            "Risk": risk,
            "Lane set": ", ".join(lanes or []),
            "Runtime": runtime,
        }
        for name, value in values.items():
            if value and name in fields:
                self._edit(str(project["id"]), str(item["id"]), fields[name], value)
