"""Tests for the parallel runner itself.

The runner had no tests at all, which is how it shipped able to drop a
whole suite and still print OK.
"""

from __future__ import annotations

import importlib.util
import sys
import contextlib
import io
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "run_all_tests", ROOT / "tests" / "run_all_tests.py"
)
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules["run_all_tests"] = RUNNER
SPEC.loader.exec_module(RUNNER)


class SelectUnitsTests(unittest.TestCase):
    """The documented filter has to match what a person would type."""

    UNITS = [
        ("GuildHub.tests.test_store", ["cmd"]),
        ("VoiceCaptions.tests.test_supervisor", ["cmd"]),
        ("tests.test_packaging", ["cmd"]),
        ("extra", ["cmd"]),
    ]

    def test_a_lowercase_filter_matches_a_capitalised_module(self):
        """`guildhub` matched nothing, and the runner said so as though
        the suite did not exist."""
        chosen = RUNNER.select_units(self.UNITS, ["guildhub"])
        self.assertEqual(["GuildHub.tests.test_store"], [u[0] for u in chosen])

    def test_an_uppercase_filter_works_too(self):
        chosen = RUNNER.select_units(self.UNITS, ["VOICECAPTIONS"])
        self.assertEqual(
            ["VoiceCaptions.tests.test_supervisor"], [u[0] for u in chosen]
        )

    def test_several_filters_union(self):
        chosen = RUNNER.select_units(self.UNITS, ["guildhub", "extra"])
        self.assertEqual(
            ["GuildHub.tests.test_store", "extra"], [u[0] for u in chosen]
        )

    def test_a_filter_matching_nothing_still_returns_nothing(self):
        self.assertEqual([], RUNNER.select_units(self.UNITS, ["nope"]))


class DiscoveryRefusesToBeSilentlyEmptyTests(unittest.TestCase):
    """A missing directory must not read as a passing one."""

    def test_a_missing_directory_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            RUNNER.discover_modules("no/such/place", "nope")
        self.assertIn("missing", str(caught.exception))

    def test_a_directory_with_no_modules_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            RUNNER.discover_modules("packaging", "packaging")
        self.assertIn("no test_*.py", str(caught.exception))

    def test_a_real_directory_yields_units(self):
        units = RUNNER.discover_modules("GuildHub/tests", "GuildHub.tests")
        self.assertTrue(units)
        self.assertTrue(
            all(name.startswith("GuildHub.tests.") for name, _cmd in units)
        )

    def test_new_companion_modules_are_discovered_without_a_runner_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, _prefix in RUNNER.PYTHON_TEST_ROOTS:
                target = root / relative
                target.mkdir(parents=True, exist_ok=True)
                (target / "test_existing.py").touch()
            replay = root / "VoiceCaptions/tests/test_added_later.py"
            replay.touch()
            with mock.patch.object(RUNNER, "ROOT", root):
                units = dict(RUNNER.build_units())
                self.assertEqual(
                    [sys.executable, "-m", "unittest", "VoiceCaptions.tests.test_added_later"],
                    units["VoiceCaptions.tests.test_added_later"],
                )
                (root / "GuildHub/tests/test_added_later.py").touch()
                self.assertIn("GuildHub.tests.test_added_later", dict(RUNNER.build_units()))

    def test_a_test_named_directory_is_not_a_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_fake.py").mkdir()
            with mock.patch.object(RUNNER, "ROOT", root), self.assertRaises(SystemExit):
                RUNNER.discover_modules(".", "tests")

    def test_every_configured_root_is_required(self):
        for missing, _prefix in RUNNER.PYTHON_TEST_ROOTS:
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for relative, _prefix in RUNNER.PYTHON_TEST_ROOTS:
                    if relative == missing:
                        continue
                    target = root / relative
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "test_present.py").touch()
                with mock.patch.object(RUNNER, "ROOT", root), self.assertRaises(SystemExit):
                    RUNNER.build_units()


class RunnerOutcomeTests(unittest.TestCase):
    def run_main(self, units, results, args, **floor):
        output = io.StringIO()
        floor = {"EXPECTED_UNITS": RUNNER.EXPECTED_UNITS,
                 "EXPECTED_TESTS": RUNNER.EXPECTED_TESTS, **floor}
        with mock.patch.object(RUNNER, "build_units", return_value=units), \
             mock.patch.object(RUNNER, "run_unit", side_effect=results), \
             mock.patch.object(RUNNER, "python_registration", return_value=None), \
             mock.patch.multiple(RUNNER, **floor), \
             contextlib.redirect_stdout(output):
            code = RUNNER.main(args)
        return code, output.getvalue()

    def test_list_only_discovers_and_filters_without_launching_or_reading_registry(self):
        output = io.StringIO()
        with mock.patch.object(RUNNER, "build_units", return_value=SelectUnitsTests.UNITS), \
             mock.patch.object(RUNNER, "run_unit") as run, \
             mock.patch.object(RUNNER, "python_registration") as registration, \
             contextlib.redirect_stdout(output):
            self.assertEqual(0, RUNNER.main(["--list", "guildhub"]))
        self.assertEqual("GuildHub.tests.test_store\n", output.getvalue())
        run.assert_not_called()
        registration.assert_not_called()

    def test_filtered_empty_unittest_cannot_pass(self):
        for output in ("", "OK\n", "Ran 0 tests in 0s\nOK\n"):
            with self.subTest(output=output):
                code, log = self.run_main(
                    [("empty", [])], [("empty", 0, 0.1, output)],
                    ["empty", "--sequential", "--quiet"],
                )
                self.assertEqual(1, code)
                self.assertIn("nonzero unittest test count", log)

    def test_parallel_run_also_rejects_missing_test_counts(self):
        code, log = self.run_main(
            [("empty", []), ("full", [])],
            [("empty", 0, 0.1, "OK\n"), ("full", 0, 0.1, "Ran 1 test in 0s\nOK\n")],
            ["empty", "full", "--jobs", "2", "--quiet"],
        )
        self.assertEqual(1, code)
        self.assertIn("FAILED: empty", log)



    def test_full_run_retains_both_unit_and_case_floors(self):
        for floor in ({"EXPECTED_UNITS": 2, "EXPECTED_TESTS": 1},
                      {"EXPECTED_UNITS": 1, "EXPECTED_TESTS": 2}):
            with self.subTest(floor=floor):
                code, log = self.run_main(
                    [("real", [])], [("real", 0, 0.1, "Ran 1 test in 0s\nOK\n")],
                    ["--sequential", "--quiet"], **floor,
                )
                self.assertEqual(1, code)
                self.assertIn("THE SUITE SHRANK", log)

    def test_failed_child_still_triggers_registration_drift_check(self):
        before = {"": "C:/Real/Python"}
        after = {"": "C:/Temp/Python"}
        with mock.patch.object(RUNNER, "build_units", return_value=[("failed", [])]), \
             mock.patch.object(RUNNER, "run_unit", return_value=("failed", 1, 0.1, "broken\n")), \
             mock.patch.object(RUNNER, "python_registration", side_effect=[before, after]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(1, RUNNER.main(["failed", "--sequential", "--quiet"]))
        self.assertIn("THE REGISTERED PYTHON MOVED", output.getvalue())


class PythonRegistrationGuardTests(unittest.TestCase):
    """The guard reports and never writes, so no test can move the key."""

    def test_the_suite_has_no_way_to_write_that_key(self):
        # The guard damaged this machine twice through a restore path.
        # There is no longer one, and this fails if anybody adds it back.
        source = (ROOT / "tests" / "run_all_tests.py").read_text(encoding="utf-8")
        for forbidden in ("SetValueEx", "CreateKeyEx", "KEY_SET_VALUE"):
            self.assertNotIn(
                forbidden,
                source,
                "the runner must not be able to write the registry",
            )

    """A test run must not repoint the machine's Python and say OK.

    On 2026-09-02 one did. Nothing failed, the suite printed OK, and
    Downtime.exe broke hours later because its launcher resolves an
    interpreter from that registration. The trigger was never found - the
    evidence lived in the throwaway install's cache and was deleted
    during cleanup - so this guards the outcome, not a suspect.
    """

    def test_unchanged_registration_is_not_drift(self):
        values = {"": r"C:\Python", "ExecutablePath": r"C:\Python\python.exe"}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            drifted = RUNNER.report_python_registration_drift(values, dict(values))
        self.assertFalse(drifted)
        self.assertEqual("", output.getvalue())

    def test_a_missing_key_is_not_drift(self):
        # Non-Windows and CI have nothing to protect; the guard must stay
        # silent rather than failing every run on those machines.
        for before, after in ((None, None), (None, {}), ({}, None)):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(
                    RUNNER.report_python_registration_drift(before, after)
                )

    def test_a_moved_registration_is_reported_and_restored(self):
        before = {"": r"C:\Real\Python", "ExecutablePath": r"C:\Real\python.exe"}
        after = {"": r"C:\Temp\throwaway", "ExecutablePath": r"C:\Temp\python.exe"}

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            drifted = RUNNER.report_python_registration_drift(before, after)

        self.assertTrue(drifted)
        printed = output.getvalue()
        # The old values have to be printed, because a person is now the
        # one who puts them back.
        self.assertIn(r"C:\Real\python.exe", printed)
        # The message has to name both paths: the whole failure was that
        # nobody could see which interpreter had been substituted.
        self.assertIn(r"C:\Real\Python", printed)
        self.assertIn(r"C:\Temp\throwaway", printed)
        self.assertIn("Downtime.exe", printed)

    def test_it_never_claims_to_have_fixed_anything(self):
        before = {"": r"C:\Real\Python"}
        after = {"": r"C:\Temp\throwaway"}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertTrue(
                RUNNER.report_python_registration_drift(before, after)
            )
        printed = output.getvalue()
        self.assertNotIn("Restored", printed)
        self.assertIn("Put it back", printed)

    def test_reading_the_real_registration_never_raises(self):
        # Whatever this machine is, the guard must not be the thing that
        # breaks the suite.
        values = RUNNER.python_registration()
        self.assertTrue(values is None or isinstance(values, dict))

if __name__ == "__main__":
    unittest.main()
