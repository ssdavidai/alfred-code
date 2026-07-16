import json
import tempfile
import unittest
from pathlib import Path

from alfred_code.errors import PlanValidationError
from alfred_code.plans import LanePolicy, PlanValidator, path_matches


POLICY = {
    "forbidden_zone": ["db/schema.sql", "**/CONTRACT.md"],
    "lanes": {
        "I": {"allowed": ["api/**"], "verify": "pytest api"},
        "II": {"allowed": ["web/**"], "verify": "npm test"},
        "phase0": {"allowed": ["**"], "verify": "true"},
    },
}


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "lanes.json"
        path.write_text(json.dumps(POLICY))
        self.validator = PlanValidator(LanePolicy.load(path))
        self.base = "a" * 40

    def tearDown(self):
        self.temp.cleanup()

    def valid(self):
        return {
            "issue": 42,
            "base_sha": self.base,
            "summary": "Implement the API and UI",
            "risk": "medium",
            "jobs": [
                {
                    "id": "api-42",
                    "lane": "I",
                    "title": "API",
                    "branch": "lane-1/42-api",
                    "paths": ["api/routes.py"],
                    "verify": "pytest api",
                    "contracts_read": [],
                    "contracts_changed": [],
                    "depends_on": [],
                    "acceptance": ["API test passes"],
                },
                {
                    "id": "web-42",
                    "lane": "II",
                    "title": "UI",
                    "branch": "lane-2/42-web",
                    "paths": ["web/page.tsx"],
                    "verify": "npm test",
                    "contracts_read": [],
                    "contracts_changed": [],
                    "depends_on": [],
                    "acceptance": ["UI test passes"],
                },
            ],
        }

    def test_path_matching_matches_repository_hook_semantics(self):
        self.assertTrue(path_matches("api/routes.py", "api/**"))
        self.assertTrue(path_matches("a/b/CONTRACT.md", "**/CONTRACT.md"))
        self.assertFalse(path_matches("web/page.tsx", "api/**"))

    def test_valid_plan_is_normalized_and_hashed(self):
        plan, digest = self.validator.validate(
            self.valid(),
            issue_number=42,
            base_sha=self.base,
            issue_body_hash="body",
            decision_context_hash="context",
        )
        self.assertEqual(plan["schema"], 1)
        self.assertEqual(plan["issue_body_hash"], "body")
        self.assertEqual(plan["decision_context_hash"], "context")
        self.assertEqual(len(digest), 64)

    def test_legacy_plan_hash_does_not_gain_context_field_during_revalidation(self):
        plan, _ = self.validator.validate(
            self.valid(),
            issue_number=42,
            base_sha=self.base,
            issue_body_hash="body",
        )
        self.assertNotIn("decision_context_hash", plan)

    def test_job_id_is_globally_unique_by_issue_token(self):
        value = self.valid()
        value["jobs"][0]["id"] = "api"
        with self.assertRaisesRegex(PlanValidationError, "must include issue number 42"):
            self.validator.validate(value, issue_number=42, base_sha=self.base)

    def test_forbidden_zone_requires_phase0(self):
        value = self.valid()
        value["jobs"][0]["paths"] = ["api/CONTRACT.md"]
        with self.assertRaisesRegex(PlanValidationError, "Phase-0-owned"):
            self.validator.validate(value, issue_number=42, base_sha=self.base)

    def test_phase0_must_precede_every_lane(self):
        value = self.valid()
        value["jobs"].insert(
            0,
            {
                "id": "contract-42",
                "lane": "phase0",
                "title": "Contract",
                "branch": "phase0/42-contract",
                "paths": ["db/schema.sql"],
                "verify": "true",
                "contracts_read": [],
                "contracts_changed": ["db/schema.sql"],
                "depends_on": [],
            },
        )
        with self.assertRaisesRegex(PlanValidationError, "must depend directly"):
            self.validator.validate(value, issue_number=42, base_sha=self.base)

    def test_overlap_and_cycles_are_rejected(self):
        value = self.valid()
        value["jobs"][1]["paths"] = ["api/routes.py"]
        value["jobs"][1]["lane"] = "I"
        value["jobs"][1]["branch"] = "lane-1/42-web"
        value["jobs"][0]["depends_on"] = ["web-42"]
        value["jobs"][1]["depends_on"] = ["api-42"]
        with self.assertRaises(PlanValidationError) as raised:
            self.validator.validate(value, issue_number=42, base_sha=self.base)
        text = str(raised.exception)
        self.assertIn("appears more than once", text)
        self.assertIn("dependency cycle", text)


if __name__ == "__main__":
    unittest.main()
