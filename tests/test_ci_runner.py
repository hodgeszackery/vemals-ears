"""CI optimizations must preserve subprocess isolation and honest failures."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ci import check_dependencies, test_summary
from tests import run_all_tests as runner


class RunnerContracts(unittest.TestCase):
    def test_timeout_is_a_failed_unit_with_output(self):
        error = subprocess.TimeoutExpired(["python"], 7, output=b"partial", stderr=b"error")
        with mock.patch.object(runner.subprocess, "run", side_effect=error) as launch:
            name, code, elapsed, output = runner.run_unit(("unit", ["python"]), 7)
        self.assertEqual(("unit", 124), (name, code))
        self.assertGreaterEqual(elapsed, 0)
        self.assertIn("partial", output)
        self.assertIn("TIMEOUT after 7s", output)
        self.assertEqual(7, launch.call_args.kwargs["timeout"])

    def test_launch_failure_cannot_pass(self):
        with mock.patch.object(runner.subprocess, "run", side_effect=OSError("missing")):
            self.assertEqual(125, runner.run_unit(("unit", ["missing"]))[1])

    def test_child_failure_and_streams_are_preserved(self):
        done = subprocess.CompletedProcess(["python"], 1, "out", "err")
        with mock.patch.object(runner.subprocess, "run", return_value=done) as launch:
            result = runner.run_unit(("unit", ["python"]))
        self.assertEqual(1, result[1])
        self.assertEqual("errout", result[3])
        self.assertEqual(["python"], launch.call_args.args[0])
        self.assertNotIn("shell", launch.call_args.kwargs)

    def test_report_retains_counts_failures_and_skips(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new/report.json"
            runner.write_report(str(target), [
                ("b", 0, 2.25, "Ran 5 tests in 1s\nOK (skipped=2)\n"),
                ("a", 1, 1, "Ran 1 test in 1s\nFAILED\n"),
            ], wall=3, workers=2)
            report = json.loads(target.read_text())
        self.assertEqual((6, 2), (report["tests"], report["skipped"]))
        self.assertEqual(["a", "b"], [row["name"] for row in report["units"]])
        self.assertEqual(1, report["units"][0]["returncode"])
        self.assertEqual(1, report["returncode"])
        self.assertIn("b: 2.2s", test_summary.render(report))

    def test_report_records_whole_run_failures_even_when_children_pass(self):
        for drifted, floor, expected in (
            (False, 2, "tests: expected at least 2"),
            (True, 1, "Python registration changed"),
        ):
            with self.subTest(drifted=drifted), tempfile.TemporaryDirectory() as directory:
                report_path = Path(directory) / "report.json"
                before = {"": "C:/Real/Python"}
                after = {"": "C:/Temp/Python"} if drifted else dict(before)
                with mock.patch.object(runner, "build_units", return_value=[("passing", [])]), \
                     mock.patch.object(runner, "run_unit", return_value=("passing", 0, 0.1, "Ran 1 test in 0s\nOK\n")), \
                     mock.patch.object(runner, "python_registration", side_effect=[before, after]), \
                     mock.patch.multiple(runner, EXPECTED_UNITS=1, EXPECTED_TESTS=floor), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(1, runner.main(["--sequential", "--quiet", "--report", str(report_path)]))
                report = json.loads(report_path.read_text(encoding="ascii"))
                self.assertEqual(1, report["returncode"])
                self.assertEqual(0, report["units"][0]["returncode"])
                self.assertTrue(any(expected in reason for reason in report["failure_reasons"]))

    def test_report_write_failure_preserves_test_diagnostics_and_registry_check(self):
        with mock.patch.object(runner, "build_units", return_value=[("broken", [])]), \
             mock.patch.object(runner, "run_unit", return_value=("broken", 1, 0.1, "actual failure detail\n")), \
             mock.patch.object(runner, "python_registration", return_value=None) as registration, \
             mock.patch.object(runner, "write_report", side_effect=OSError("disk unavailable")), \
             contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(1, runner.main(["broken", "--sequential", "--quiet", "--report", "ignored.json"]))
        self.assertEqual(2, registration.call_count)
        self.assertIn("actual failure detail", output.getvalue())
        self.assertIn("disk unavailable", errors.getvalue())

    def test_unwritable_requested_report_makes_passing_run_fail(self):
        with mock.patch.object(runner, "build_units", return_value=[("passing", [])]), \
             mock.patch.object(runner, "run_unit", return_value=("passing", 0, 0.1, "Ran 1 test in 0s\nOK\n")), \
             mock.patch.object(runner, "python_registration", return_value=None), \
             mock.patch.object(runner, "write_report", side_effect=OSError("disk unavailable")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, runner.main(["passing", "--sequential", "--quiet", "--report", "ignored.json"]))

    def test_nonpositive_worker_and_timeout_values_reject(self):
        for value in ("0", "-1"):
            with self.assertRaises(argparse.ArgumentTypeError):
                runner.positive_integer(value)
        self.assertEqual(8, runner.positive_integer("8"))

    def test_actual_child_is_still_a_separate_process(self):
        import os
        import sys
        result = runner.run_unit(("isolated", [sys.executable, "-c", "import os; print(os.getpid())"]), 10)
        self.assertEqual(0, result[1])
        self.assertNotEqual(os.getpid(), int(result[3].strip()))

    def test_filters_still_fail_when_nothing_selected(self):
        with mock.patch.object(runner, "build_units", return_value=[("real", [])]), \
             mock.patch.object(runner, "python_registration", return_value=None), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, runner.main(["no-match"]))


class DependencyContracts(unittest.TestCase):
    def test_missing_node_is_an_error_not_a_skip(self):
        with mock.patch.object(check_dependencies.importlib, "import_module"), \
             mock.patch.object(check_dependencies.shutil, "which", return_value=None):
            with self.assertRaisesRegex(SystemExit, "Node is required"):
                check_dependencies.main()

    def test_missing_package_is_an_error(self):
        with mock.patch.object(check_dependencies.importlib, "import_module", side_effect=ImportError):
            with self.assertRaises(ImportError):
                check_dependencies.main()


if __name__ == "__main__":
    unittest.main()
