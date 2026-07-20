import json
import unittest
from pathlib import Path

from alfred_code.errors import PlanValidationError
from alfred_code.planner import (
    extract_json,
    extract_planner_result,
    structured_planner_command,
)


class PlannerOutputTests(unittest.TestCase):
    def test_direct_json(self):
        self.assertEqual(extract_json('{"issue": 1}'), {"issue": 1})

    def test_fenced_json(self):
        self.assertEqual(extract_json('result\n```json\n{"issue": 2}\n```'), {"issue": 2})

    def test_missing_json_is_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "output length: 28"):
            extract_json("I think this requires lane I")

    def test_claude_planner_uses_structured_output_schema(self):
        sha = "a" * 40
        command = structured_planner_command(("/Users/test/.local/bin/claude", "-p"), 292, sha)
        self.assertEqual(command[-2], "--json-schema")
        schema = json.loads(command[-1])
        self.assertEqual(schema["properties"]["issue"]["const"], 292)
        self.assertEqual(schema["properties"]["base_sha"]["const"], sha)
        self.assertEqual(schema["properties"]["jobs"]["minItems"], 1)
        self.assertIn("--output-format", command)
        self.assertEqual(command[command.index("--output-format") + 1], "json")

    def test_codex_planner_uses_schema_file_jsonl_and_stdin(self):
        sha = "c" * 40
        schema_path = Path("/tmp/plan.schema.json")
        command = structured_planner_command(
            ("/Users/test/.local/bin/codex", "exec", "--model", "gpt-5.6-sol"),
            333,
            sha,
            schema_path=schema_path,
        )

        self.assertEqual(command[command.index("--output-schema") + 1], str(schema_path))
        self.assertIn("--json", command)
        self.assertEqual(command[-1], "-")

    def test_claude_json_envelope_preserves_usage_and_returns_structured_output(self):
        plan = {"issue": 292, "jobs": []}
        envelope = {
            "type": "result",
            "session_id": "session-1",
            "structured_output": plan,
            "usage": {"output_tokens": 12},
            "modelUsage": {"claude-test": {"outputTokens": 12}},
        }
        result, metadata = extract_planner_result(json.dumps(envelope))
        self.assertEqual(result, plan)
        self.assertEqual(metadata, envelope)

    def test_codex_jsonl_preserves_usage_and_returns_final_agent_json(self):
        plan = {"issue": 333, "jobs": []}
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(plan)},
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 80,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                },
            },
        ]

        result, metadata = extract_planner_result(
            "\n".join(json.dumps(event) for event in events)
        )

        self.assertEqual(result, plan)
        self.assertEqual(metadata["provider"], "codex")
        self.assertEqual(metadata["session_id"], "thread-1")
        self.assertEqual(metadata["usage"]["output_tokens"], 20)

    def test_non_claude_planner_command_is_unchanged(self):
        command = ("custom-planner", "--json")
        self.assertEqual(structured_planner_command(command, 1, "b" * 40), list(command))


if __name__ == "__main__":
    unittest.main()
