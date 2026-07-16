import json
import unittest

from alfred_code.errors import PlanValidationError
from alfred_code.planner import extract_json, structured_planner_command


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

    def test_non_claude_planner_command_is_unchanged(self):
        command = ("custom-planner", "--json")
        self.assertEqual(structured_planner_command(command, 1, "b" * 40), list(command))


if __name__ == "__main__":
    unittest.main()
