"""Contract and hand-calculated scoring checks for the independent example."""

import copy
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tests" / "fixtures" / "weighted-tardiness"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVALUATOR = load_module("weighted_tardiness_evaluator", TASK / "benchmark/evaluate.py")
BASELINE = load_module("weighted_tardiness_baseline", TASK / "benchmark/baseline.py")
FIXTURES = json.loads((TASK / "benchmark/fixtures.json").read_text(encoding="utf-8"))


class WeightedTardinessTests(unittest.TestCase):
    def setUp(self):
        self.case = copy.deepcopy(FIXTURES["valid"][0]["case"])

    def test_task_manifest_and_referenced_files(self):
        manifest = json.loads((TASK / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["task_id"], "weighted-tardiness")
        self.assertEqual(manifest["version"], "1.0.1")
        self.assertEqual(
            manifest["objective"],
            {"name": "weighted_tardiness", "direction": "minimize", "aggregation": "mean_if_all_valid"},
        )
        self.assertEqual(
            set(manifest),
            {"schema_version", "task_id", "version", "description_file", "objective"},
        )
        self.assertTrue((TASK / manifest["description_file"]).is_file())
        benchmark = json.loads((TASK / "benchmark/benchmark.json").read_text())
        self.assertEqual(benchmark["protocol"], {"kind": "function"})
        self.assertEqual(benchmark["task_id"], manifest["task_id"])
        for key in ("evaluator_file", "baseline_file", "tests_file"):
            self.assertTrue((TASK / "benchmark" / Path(benchmark[key]).name).is_file(), key)

    def test_all_hand_calculated_fixtures(self):
        self.assertEqual(FIXTURES["schema_version"], 1)
        self.assertEqual({len(item["case"]["jobs"]) for item in FIXTURES["valid"]}, {6, 8, 10})
        for fixture in FIXTURES["valid"]:
            with self.subTest(fixture=fixture["name"]):
                before = copy.deepcopy(fixture)
                EVALUATOR.validate_input(fixture["case"])
                actual = EVALUATOR.score(fixture["solution"], fixture["case"])
                self.assertEqual(actual, fixture["expected"])
                self.assertEqual(fixture, before)

    def test_invalid_input_fixtures(self):
        for fixture in FIXTURES["invalid_inputs"]:
            with self.subTest(fixture=fixture["name"]):
                with self.assertRaises(ValueError) as error:
                    EVALUATOR.validate_input(fixture["case"])
                self.assertIn(fixture["error_contains"], str(error.exception))

    def test_invalid_solution_fixtures(self):
        for fixture in FIXTURES["invalid_solutions"]:
            with self.subTest(fixture=fixture["name"]):
                with self.assertRaises(ValueError) as error:
                    EVALUATOR.score(fixture["solution"], fixture["case"])
                self.assertIn(fixture["error_contains"], str(error.exception))

    def test_job_count_boundaries(self):
        for count in (2, 12):
            case = {"jobs": [{"id": str(index), "p": 1, "w": 1, "d": 0} for index in range(count)]}
            with self.subTest(valid_count=count):
                EVALUATOR.validate_input(case)
        for count in (0, 1, 13):
            case = {"jobs": [{"id": str(index), "p": 1, "w": 1, "d": 0} for index in range(count)]}
            with self.subTest(invalid_count=count):
                with self.assertRaises(ValueError):
                    EVALUATOR.validate_input(case)

    def test_numeric_boundaries_and_strict_types(self):
        for field, minimum, maximum in (("p", 1, 20), ("w", 1, 10), ("d", 0, 200)):
            for value in (minimum, maximum):
                case = copy.deepcopy(self.case)
                case["jobs"][0][field] = value
                with self.subTest(field=field, valid=value):
                    EVALUATOR.validate_input(case)
            for value in (minimum - 1, maximum + 1, True, False, float(minimum), str(minimum), None):
                case = copy.deepcopy(self.case)
                case["jobs"][0][field] = value
                with self.subTest(field=field, invalid=value):
                    with self.assertRaises(ValueError):
                        EVALUATOR.validate_input(case)

    def test_exact_object_structure(self):
        wrong_inputs = [None, [], {"jobs": self.case["jobs"], "metadata": {}}, {"jobs": tuple(self.case["jobs"]) }]
        for job in (None, [], {}, {"id": "A", "p": 1, "w": 1}, {"id": "A", "p": 1, "w": 1, "d": 0, "x": 0}):
            case = copy.deepcopy(self.case)
            case["jobs"][0] = job
            wrong_inputs.append(case)
        for case in wrong_inputs:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    EVALUATOR.validate_input(case)

    def test_job_ids(self):
        for job_id in (None, True, 1, [], "", " \t\n", "B"):
            case = copy.deepcopy(self.case)
            case["jobs"][0]["id"] = job_id
            with self.subTest(invalid_id=job_id):
                with self.assertRaises(ValueError):
                    EVALUATOR.validate_input(case)
        case = copy.deepcopy(self.case)
        case["jobs"][0]["id"] = "job 甲"
        case["jobs"][1]["id"] = " job 甲 "
        EVALUATOR.validate_input(case)
        solution = {"order": ["C", " job 甲 ", "E", "job 甲", "F", "D"]}
        self.assertEqual(EVALUATOR.score(solution, case)["score"], 18)

    def test_solution_structure_and_permutation(self):
        order = [job["id"] for job in self.case["jobs"]]
        wrong_solutions = [None, [], {}, order, {"order": tuple(order)}, {"order": order + ["G"]},
                           {"order": order, "trace": []}, {"order": [[], *order[1:]]}]
        for solution in wrong_solutions:
            with self.subTest(solution=solution):
                with self.assertRaises(ValueError):
                    EVALUATOR.score(solution, self.case)

    def test_score_revalidates_case(self):
        self.case["jobs"][0]["w"] = -1
        with self.assertRaises(ValueError):
            EVALUATOR.score({"order": ["A", "B", "C", "D", "E", "F"]}, self.case)

    def test_baseline_reference_and_deterministic_ties(self):
        before = copy.deepcopy(self.case)
        expected = {"order": ["C", "B", "E", "A", "F", "D"]}
        self.assertEqual(BASELINE.solve(self.case), expected)
        self.assertEqual(BASELINE.solve(self.case), expected)
        self.assertEqual(EVALUATOR.score(expected, self.case)["score"], 18)
        self.assertEqual(self.case, before)
        reversed_case = {"jobs": list(reversed(self.case["jobs"]))}
        self.assertEqual(BASELINE.solve(reversed_case), expected)

    def test_baseline_is_valid_for_all_valid_fixture_cases(self):
        for fixture in FIXTURES["valid"]:
            with self.subTest(fixture=fixture["name"]):
                result = EVALUATOR.score(BASELINE.solve(fixture["case"]), fixture["case"])
                self.assertIs(type(result["score"]), int)
                self.assertGreaterEqual(result["score"], 0)


if __name__ == "__main__":
    unittest.main()
