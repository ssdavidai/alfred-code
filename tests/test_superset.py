import base64
import json
import tempfile
import unittest
from pathlib import Path

from alfred_code.config import SupersetConfig
from alfred_code.superset import SupersetClient, worker_workspace_name


class FakeSuperset(SupersetClient):
    def __init__(self):
        super().__init__(SupersetConfig(cli="superset"))
        self.calls = []

    def _json(self, arguments, timeout=180):
        self.calls.append(arguments)
        if arguments[:2] == ["workspaces", "list"]:
            return []
        if arguments[:2] == ["projects", "list"]:
            return [{"id": "project-1", "name": "alfred"}]
        if arguments[:2] == ["workspaces", "create"]:
            return {
                "workspace": {"id": "workspace-1", "name": arguments[arguments.index("--name") + 1], "branch": arguments[arguments.index("--branch") + 1]},
                "agent": {"id": "agent-1"},
            }
        return {}


class SupersetTests(unittest.TestCase):
    def test_worker_workspace_names_are_stable_and_unique_per_sequential_job(self):
        model = worker_workspace_name(
            "alfred-code", 316, "I", "ctrl-run-model-316-r3-r3"
        )
        request = worker_workspace_name(
            "alfred-code", 316, "I", "ctrl-run-request-316-r3-r3"
        )

        self.assertEqual(
            model,
            worker_workspace_name(
                "alfred-code", 316, "I", "ctrl-run-model-316-r3-r3"
            ),
        )
        self.assertNotEqual(model, request)
        self.assertIn("ctrl-run-model-316-r3-r3", model)
        self.assertIn("ctrl-run-request-316-r3-r3", request)

    def test_worker_creation_is_atomic_and_writes_lane_metadata(self):
        client = FakeSuperset()
        job = {
            "job_id": "api",
            "lane": "I",
            "branch": "lane-1/4-api",
            "paths": ["api/**"],
            "verify_command": "pytest",
        }
        workspace, agent = client.create_worker(
            repo_path=Path("/repo"), issue_number=4, job=job, prompt="build"
        )
        self.assertEqual(workspace.id, "workspace-1")
        self.assertEqual(agent, "agent-1")
        create = next(call for call in client.calls if call[:2] == ["workspaces", "create"])
        self.assertIn("--agent", create)
        self.assertIn("--prompt", create)
        command = create[create.index("--command") + 1]
        encoded = command.split()[2]
        lane = json.loads(base64.b64decode(encoded))
        self.assertEqual(lane["controller_job"], "api")
        self.assertEqual(lane["allowed"], ["api/**"])
        self.assertEqual(lane["role"], "worker")
        self.assertEqual(lane["security_policy"], "alfred-scoped-v1")

    def test_sprint_integration_workspace_has_no_autonomous_agent(self):
        client = FakeSuperset()

        workspace = client.create_integration_workspace(
            repo_path=Path("/repo"),
            sprint_number=4,
            branch="alfred-code/sprint-4-integration",
        )

        self.assertEqual(workspace.branch, "alfred-code/sprint-4-integration")
        create = next(call for call in client.calls if call[:2] == ["workspaces", "create"])
        self.assertEqual(
            create[create.index("--name") + 1], "alfred-code-sprint-4-integration"
        )
        self.assertNotIn("--agent", create)
        self.assertNotIn("--prompt", create)
        self.assertNotIn("--command", create)

    def test_review_workspace_uses_a_distinct_exact_sha_branch(self):
        client = FakeSuperset()

        workspace, agent = client.create_review_workspace(
            "project-1",
            293,
            "alfred-code-review-293-dc011023",
            "review/293-dc0110232f22",
            "review",
            issue_number=292,
            controller_job="docs-292",
            verify_command="docker compose config -q",
            review_paths=["docs/contract.md", "packages/ctrl/CONTRACT.md"],
        )

        self.assertEqual(workspace.branch, "review/293-dc0110232f22")
        self.assertEqual(agent, "agent-1")
        create = next(call for call in client.calls if call[:2] == ["workspaces", "create"])
        self.assertEqual(create[create.index("--branch") + 1], "review/293-dc0110232f22")
        self.assertNotIn("--pr", create)
        command = create[create.index("--command") + 1]
        encoded = command.split()[2]
        lane = json.loads(base64.b64decode(encoded))
        self.assertEqual(lane["role"], "reviewer")
        self.assertEqual(
            lane["allowed"], ["docs/contract.md", "packages/ctrl/CONTRACT.md"]
        )
        self.assertEqual(lane["issue"], 292)
        self.assertEqual(lane["controller_job"], "docs-292")
        self.assertEqual(lane["verify"], "docker compose config -q")


if __name__ == "__main__":
    unittest.main()
