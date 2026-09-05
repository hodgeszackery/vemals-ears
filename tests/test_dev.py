"""Local VE checks operate entirely inside this checkout and stop on failure."""
import contextlib
import io
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import dev


class DevelopmentTasksTests(unittest.TestCase):
    def test_check_runs_python_and_adapter_with_reports(self):
        commands = dev.check_commands(4)
        text = " ".join(" ".join(command) for _cwd, command in commands)
        for required in ("ci.lint_workflows", "ci.check_dependencies",
                         "tests/run_all_tests.py --jobs 4", "--report", "--test"):
            self.assertIn(required, text)
        self.assertNotIn("warpsim", text.casefold())
        self.assertNotIn("run_lua", text)
        self.assertTrue(all(not directory.is_absolute() and ".." not in directory.parts
                            for directory, _command in commands))

    def test_dry_run_never_launches_and_failures_stop_later_commands(self):
        commands = [(Path("."), ["first"]), (Path("."), ["second"])]
        with mock.patch.object(dev.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dev.run_commands(commands, root=Path("."), dry_run=True), 0)
            run.assert_not_called()
            run.return_value = subprocess.CompletedProcess([], 7)
            self.assertEqual(dev.run_commands(commands, root=Path("."), dry_run=False), 7)
            self.assertEqual(run.call_count, 1)
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_spawn_failure_is_not_success(self):
        with mock.patch.object(dev.subprocess, "run", side_effect=OSError("missing")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, dev.run_commands([(Path("."), ["absent"])],
                                                root=Path("."), dry_run=False))


if __name__ == "__main__":
    unittest.main()
