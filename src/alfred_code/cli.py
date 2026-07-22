from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .agent_security import SCOPED_CLAUDE_AGENT_ID, SCOPED_CODEX_AGENT_ID
from .config import (
    DEFAULT_CONFIG,
    DEFAULT_PLANNER_COMMAND,
    ControllerConfig,
    load_config,
    planner_profile_path,
    validate_planner_profile,
)
from .controller import Controller
from .db import Database
from .errors import AlfredCodeError, AuthorityUnavailable
from .github import GitHubClient
from .legacy import LegacyImporter
from .notify import ConsoleNotifier, DurableNotifier, SlackNotifier
from .planner import Planner
from .plans import LanePolicy, PlanValidator
from .project import ProjectBoard
from .superset import SupersetClient
from .superset_agents import inspect_agent_configs, provision_agent_configs
from .sprints import SprintManager
from .util import atomic_write, run
from .worktrees import audit_worktrees


EXAMPLE_CONFIG = f"""# Alfred Code v2 control plane. Safe/read-only until apply = true.
repo_path = "~/dev/alfred"
state_dir = "~/.alfred-code-state-v2"
apply = false
poll_seconds = 60
max_parallel_planners = 3
auto_replan_max_attempts = 2
sprint_duration_days = 14
planner_command = {json.dumps(list(DEFAULT_PLANNER_COMMAND))}
planner_timeout_seconds = 900

[github]
repo = "ssdavidai/alfred"
owner = "ssdavidai"
intake_label = "alfred-code"
approval_command = "/approve-plan"
# Immutable authorization boundary: startup fails if either list differs.
approvers = ["ssdavidai"]
reviewers = ["ssdavidai"]
project_title = "Alfred Product Control"
# Set this after `alfred-code project-setup` succeeds.
# project_number = 1

[superset]
cli = "/Users/ssd/.superset/bin/superset"
project_name = "alfred"
worker_agent = "{SCOPED_CLAUDE_AGENT_ID}"
reviewer_agent = "{SCOPED_CODEX_AGENT_ID}"
workspace_prefix = "alfred-code"
api_key_env = "SUPERSET_API_KEY"
# Workspace deletion is intentionally disabled. Enable only after auditing cleanup.
cleanup_merged_workspaces = false
# Failed exact-SHA reviews get at most two scoped repairs before escalation.
review_repair_max_attempts = 2

[slack]
enabled = false
webhook_env = "ALFRED_CODE_SLACK_WEBHOOK"
channel = ""
"""


def emit(value: Any, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    else:
        print(value)


def build(config: ControllerConfig) -> tuple[Controller, Database]:
    database = Database(config.database_path)
    github = GitHubClient(config.github)
    superset = SupersetClient(config.superset)
    policy = LanePolicy.load(config.lane_policy_path)
    planner = Planner(config, github, PlanValidator(policy))
    channel = SlackNotifier(config.slack) if config.slack.enabled else ConsoleNotifier()
    notifier = DurableNotifier(database, channel)
    project = ProjectBoard(config.github) if config.github.project_number else None
    controller = Controller(
        config,
        database,
        github,
        superset,
        planner,
        notifier,
        project=project,
        audit=AuditLog(config.state_dir / "controller.jsonl"),
    )
    return controller, database


def doctor(config: ControllerConfig) -> tuple[dict[str, Any], bool]:
    report: dict[str, Any] = {
        "apply": config.apply,
        "scheduler": {
            "max_parallel_planners": config.max_parallel_planners,
            "auto_replan_max_attempts": config.auto_replan_max_attempts,
            "sprint_duration_days": config.sprint_duration_days,
        },
        "checks": {},
    }
    healthy = True

    def check(name: str, callback: Any, *, required: bool = True) -> None:
        nonlocal healthy
        try:
            value = callback()
        except Exception as exc:
            report["checks"][name] = {"ok": False, "error": str(exc), "type": type(exc).__name__}
            if required:
                healthy = False
        else:
            report["checks"][name] = {"ok": True, "detail": value}

    check("repository", lambda: {
        "path": str(config.repo_path),
        "top_level": run(["git", "rev-parse", "--show-toplevel"], cwd=config.repo_path).strip(),
        "head": run(["git", "rev-parse", "HEAD"], cwd=config.repo_path).strip(),
        "dirty": bool(run(["git", "status", "--porcelain"], cwd=config.repo_path).strip()),
    })
    check("lane_policy", lambda: {
        "path": str(config.lane_policy_path),
        "lanes": sorted(LanePolicy.load(config.lane_policy_path).lanes),
    })
    def database_check() -> dict[str, Any]:
        database = Database(config.database_path)
        try:
            version = database.connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        finally:
            database.close()
        return {"path": str(config.database_path), "schema": version}

    check("database", database_check)
    def planner_check() -> dict[str, Any]:
        binary = shutil.which(config.planner_command[0])
        if not binary:
            raise FileNotFoundError(config.planner_command[0])
        return {
            "command": list(config.planner_command),
            "binary": binary,
            "profile": validate_planner_profile(planner_profile_path(config.planner_command)),
        }

    check("planner", planner_check)
    check("github", lambda: GitHubClient(config.github).doctor())
    check("superset", lambda: SupersetClient(config.superset).doctor())
    check("superset_scoped_agents", inspect_agent_configs)
    if config.slack.enabled:
        check("slack", lambda: {"channel": SlackNotifier(config.slack).destination})
    if config.github.project_number:
        check(
            "github_project",
            lambda: ProjectBoard(config.github)._json(
                [
                    "project",
                    "view",
                    str(config.github.project_number),
                    "--owner",
                    config.github.owner,
                    "--format",
                    "json",
                ]
            ),
        )
    else:
        report["checks"]["github_project"] = {
            "ok": False,
            "required": False,
            "error": "project_number is not configured; execution still works but the PM board will not sync",
        }
    report["healthy"] = healthy
    report["execution_enabled"] = healthy and config.apply
    return report, healthy


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="alfred-code", description="Alfred deterministic control plane")
    root.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = root.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create a safe disabled configuration and database")
    init.add_argument("--print", action="store_true", dest="print_only")

    sub.add_parser("doctor", help="Verify local, GitHub, Superset, and project authority")
    sub.add_parser("status", help="Show durable issue/job state and recent events")

    plan = sub.add_parser("plan", help="Generate and validate one issue plan")
    plan.add_argument("issue", type=int)
    plan.add_argument("--publish", action="store_true", help="Persist and comment the plan on GitHub")

    once = sub.add_parser("run-once", help="Perform one reconciliation cycle")
    once.add_argument("--apply", action="store_true", help="Allow planning and Superset launches")
    once.add_argument("--dry-run", action="store_true", help="Force read-only observation")

    serve = sub.add_parser("serve", help="Run the reconciliation loop with a singleton lock")
    serve.add_argument("--apply", action="store_true", help="Allow planning and Superset launches")
    serve.add_argument("--dry-run", action="store_true", help="Force read-only observation")

    dashboard = sub.add_parser(
        "dashboard", help="Run the read-only local operations dashboard"
    )
    dashboard.add_argument("--host", default="127.0.0.1", help="Loopback address to bind")
    dashboard.add_argument("--port", type=int, default=7331, help="Local port to bind")
    dashboard.add_argument("--open", action="store_true", help="Open the dashboard in a browser")

    migrate = sub.add_parser("migrate-legacy", help="Import old JSON and log evidence without trusting its state")
    migrate.add_argument("--legacy-dir", type=Path, default=Path("~/.alfred-code-state").expanduser())

    sub.add_parser("project-setup", help="Create or adopt the GitHub PM project and fields")
    sprint_start = sub.add_parser(
        "sprint-start", help="Start the ordered cards currently in the GitHub Sprint queue"
    )
    sprint_start.add_argument("--title", help="Sprint title; defaults to Sprint N")
    sprint_start.add_argument("--duration-days", type=int, help="Override configured duration")
    sub.add_parser("sprint-status", help="Show active and historical sprint state")
    sub.add_parser("worktrees-audit", help="Read-only audit of every target-repository worktree")
    sub.add_parser("agents-provision", help="Provision and verify Alfred-only scoped Superset agent presets")
    return root


def resolved_apply(config: ControllerConfig, args: argparse.Namespace) -> ControllerConfig:
    if getattr(args, "dry_run", False):
        return replace(config, apply=False)
    if getattr(args, "apply", False):
        return replace(config, apply=True)
    return config


def serve(config: ControllerConfig) -> int:
    controller, database = build(config)
    lock_path = config.state_dir / "controller.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("w")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"another controller owns {lock_path}", file=sys.stderr)
        return 73
    stop = False

    def request_stop(signum: int, frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop:
        try:
            result = controller.run_once()
            emit(result)
        except Exception as exc:
            print(f"reconciliation failed safely: {exc}", file=sys.stderr)
        for _ in range(config.poll_seconds):
            if stop:
                break
            time.sleep(1)
    database.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config_path = args.config.expanduser()
    if args.command == "init":
        if args.print_only:
            print(EXAMPLE_CONFIG, end="")
            return 0
        if config_path.exists():
            print(f"preserved existing config: {config_path}")
        else:
            atomic_write(config_path, EXAMPLE_CONFIG, mode=0o600)
            print(f"created safe disabled config: {config_path}")
        config = load_config(config_path)
        database = Database(config.database_path)
        database.close()
        print(f"initialized durable state: {config.database_path}")
        return 0

    if args.command == "agents-provision":
        try:
            emit(provision_agent_configs())
            return 0
        except (AlfredCodeError, OSError, ValueError, KeyError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    try:
        config = load_config(config_path)
        if args.command == "doctor":
            report, healthy = doctor(config)
            emit(report)
            return 0 if healthy else 1
        if args.command == "project-setup":
            result = ProjectBoard(config.github).setup()
            emit(
                {
                    "project": result,
                    "next": f"set github.project_number = {result['number']} in {config_path}",
                }
            )
            return 0
        if args.command in {"sprint-start", "sprint-status"}:
            database = Database(config.database_path)
            try:
                if args.command == "sprint-start":
                    project = ProjectBoard(config.github)
                    result = SprintManager(
                        config,
                        database,
                        project,
                        GitHubClient(config.github),
                    ).start(
                        title=args.title,
                        duration_days=args.duration_days,
                    )
                    emit({"sprint": result, "next": "controller will specify queued cards"})
                else:
                    sprints = database.list_sprints()
                    emit(
                        {
                            "active": database.active_sprint(),
                            "sprints": [
                                {
                                    **sprint,
                                    "items": database.sprint_items(int(sprint["id"])),
                                }
                                for sprint in sprints
                            ],
                        }
                    )
            finally:
                database.close()
            return 0
        if args.command == "worktrees-audit":
            emit(audit_worktrees(config.repo_path, GitHubClient(config.github)))
            return 0
        if args.command == "dashboard":
            from .dashboard import serve_dashboard

            return serve_dashboard(
                config,
                host=args.host,
                port=args.port,
                open_browser=args.open,
            )
        effective = resolved_apply(config, args)
        if args.command in {"run-once", "serve"} and effective.apply:
            report, healthy = doctor(effective)
            if not healthy:
                emit(report)
                print("refusing apply mode until every required doctor check is healthy", file=sys.stderr)
                return 1
        controller, database = build(effective)
        if args.command == "status":
            emit(
                {
                    "issues": database.list_issues(),
                    "jobs": database.list_jobs(),
                    "leases": [dict(row) for row in database.connection.execute("SELECT * FROM lane_leases")],
                    "events": database.events(100),
                    "sprints": [
                        {
                            **sprint,
                            "items": database.sprint_items(int(sprint["id"])),
                        }
                        for sprint in database.list_sprints()
                    ],
                }
            )
        elif args.command == "plan":
            issue = controller.github.issue(args.issue)
            database.upsert_issue(issue)
            plan, plan_hash = controller.planner.plan_issue(args.issue)
            if args.publish:
                database.save_plan(args.issue, plan_hash, plan)
                url = controller.github.post_plan(args.issue, plan, plan_hash)
            else:
                url = None
            emit({"plan_hash": plan_hash, "plan": plan, "published": args.publish, "url": url})
        elif args.command == "run-once":
            emit(controller.run_once())
        elif args.command == "serve":
            database.close()
            return serve(resolved_apply(config, args))
        elif args.command == "migrate-legacy":
            emit(LegacyImporter(database, args.legacy_dir.expanduser()).run())
        database.close()
        return 0
    except (AlfredCodeError, OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
