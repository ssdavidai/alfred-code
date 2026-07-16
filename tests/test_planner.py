import unittest

from alfred_code.errors import PlanValidationError
from alfred_code.planner import extract_json


class PlannerOutputTests(unittest.TestCase):
    def test_direct_json(self):
        self.assertEqual(extract_json('{"issue": 1}'), {"issue": 1})

    def test_fenced_json(self):
        self.assertEqual(extract_json('result\n```json\n{"issue": 2}\n```'), {"issue": 2})

    def test_missing_json_is_rejected(self):
        with self.assertRaises(PlanValidationError):
            extract_json("I think this requires lane I")


if __name__ == "__main__":
    unittest.main()
