"""Adversarial aggregate-gate and prerequisite checks, without dependencies."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ci import check_dependencies, check_results, test_summary


def needs():
    return {
        "validate": {"result": "success"},
        "python-and-node": {"result": "success", "outputs": {"discord": "success"}},
    }


class ResultGateTests(unittest.TestCase):
    def test_all_owned_checks_must_succeed(self):
        self.assertEqual([], check_results.validate(needs()))

    def test_malformed_or_missing_needs_fails_closed(self):
        for value in (None, [], "success", True, 1, {}):
            with self.subTest(value=value):
                self.assertTrue(check_results.validate(value))

    def test_each_job_cannot_skip_fail_cancel_or_disappear(self):
        for name in ("validate", "python-and-node"):
            for value in (None, {}, [], "success", {"result": "skipped"},
                          {"result": "failure"}, {"result": "cancelled"},
                          {"result": "neutral"}, {"result": True}):
                with self.subTest(job=name, value=value):
                    data = needs()
                    data[name] = value
                    self.assertTrue(check_results.validate(data))
            data = needs()
            del data[name]
            self.assertTrue(check_results.validate(data))

    def test_discord_step_cannot_skip_fail_or_omit_its_result(self):
        for value in (None, "", "skipped", "failure", "cancelled", "neutral", True, [], {}):
            with self.subTest(value=value):
                data = needs()
                data["python-and-node"]["outputs"]["discord"] = value
                self.assertTrue(check_results.validate(data))

    def test_discord_outputs_must_be_present_and_well_formed(self):
        for value in (None, [], {}, "success"):
            data = needs()
            data["python-and-node"]["outputs"] = value
            self.assertTrue(check_results.validate(data))

    def test_validation_does_not_mutate_workflow_data(self):
        data = needs()
        before = copy.deepcopy(data)
        check_results.validate(data)
        self.assertEqual(before, data)

    def test_cli_malformed_or_absent_json_fails(self):
        for raw in ("", "broken", "{", "[]", "null"):
            with self.subTest(raw=raw), mock.patch.dict("os.environ", {"CI_NEEDS": raw}, clear=True):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(1, check_results.main())

    def test_cli_appends_summary_only_after_all_results_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "summary.txt"
            target.write_text("existing summary\n", encoding="ascii")
            with mock.patch.dict("os.environ", {
                "CI_NEEDS": json.dumps(needs()), "GITHUB_STEP_SUMMARY": str(target),
            }, clear=True), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, check_results.main())
            self.assertTrue(target.read_text().startswith("existing summary\n"))
            self.assertIn("PASS", target.read_text())


class PrerequisiteTests(unittest.TestCase):
    def test_python_dependency_failure_cannot_become_skip(self):
        with mock.patch.object(check_dependencies.importlib, "import_module", side_effect=ImportError):
            with self.assertRaises(ImportError):
                check_dependencies.main()

    def test_node_is_mandatory(self):
        with mock.patch.object(check_dependencies.importlib, "import_module"):
            with mock.patch.object(check_dependencies.shutil, "which", return_value=None):
                with self.assertRaisesRegex(SystemExit, "Node is required"):
                    check_dependencies.main()

    def test_wrong_opus_version_cannot_become_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "VoiceCaptions/discord_adapter/node_modules/opusscript/package.json"
            package.parent.mkdir(parents=True)
            package.write_text('{"version":"9.9.9"}', encoding="ascii")
            with mock.patch.object(check_dependencies, "ROOT", root):
                with mock.patch.object(check_dependencies.importlib, "import_module"):
                    with mock.patch.object(check_dependencies.shutil, "which", return_value="node"):
                        with self.assertRaisesRegex(SystemExit, "Locked opusscript"):
                            check_dependencies.main()

    def test_success_imports_all_python_dependencies_and_executes_node_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "VoiceCaptions/discord_adapter"
            package = adapter / "node_modules/opusscript/package.json"
            package.parent.mkdir(parents=True)
            package.write_text('{"version":"0.0.8"}', encoding="ascii")
            with mock.patch.object(check_dependencies, "ROOT", root):
                with mock.patch.object(check_dependencies.importlib, "import_module") as imports:
                    with mock.patch.object(check_dependencies.shutil, "which", return_value="node.exe"):
                        with mock.patch.object(check_dependencies.subprocess, "run") as run:
                            with contextlib.redirect_stdout(io.StringIO()):
                                self.assertEqual(check_dependencies.main(), 0)
            self.assertEqual(imports.call_args_list, [
                mock.call("PIL.Image"), mock.call("websockets"), mock.call("yaml"),
            ])
            self.assertEqual(run.call_args.args[0], [
                "node.exe", "--input-type=module", "--eval", "import 'opusscript';",
            ])
            self.assertTrue(run.call_args.kwargs["check"])
            self.assertEqual(run.call_args.kwargs["timeout"], 30)
            self.assertEqual(run.call_args.kwargs["cwd"], adapter)


class TimingSummaryTests(unittest.TestCase):
    def test_summary_counts_skips_and_limits_sorted_slow_units(self):
        report = {
            "tests": 123, "skipped": 4, "wall_seconds": 12.34,
            "units": [{"name": "unit-" + str(i), "seconds": i, "returncode": 0}
                      for i in range(12)],
        }
        rendered = test_summary.render(report)
        self.assertIn("12 units; 123 tests; 4 skipped; 12.3s wall time", rendered)
        self.assertEqual(rendered.count("(exit 0)"), 10)
        self.assertLess(rendered.index("unit-11:"), rendered.index("unit-10:"))
        self.assertNotIn("unit-0:", rendered)


if __name__ == "__main__":
    unittest.main()
