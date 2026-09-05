# SPDX-License-Identifier: GPL-3.0-only
"""Startup, second-launch handoff and independent product installation."""

from contextlib import contextmanager, ExitStack
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.companion import isolated_companion, load_companion


class SecondLaunchHandoffTests(unittest.TestCase):
    """A second launch raises the running companion; the box is the fallback."""

    def setUp(self):
        self.companion = self.enterContext(isolated_companion())

    def run_main(self, *, raise_result, edition="full-local"):
        import tkinter.messagebox

        shown = []
        with (
            mock.patch.object(self.companion.tk, "Tk"),
            mock.patch.object(self.companion, "claim_instance_mutex", return_value=True),
            mock.patch.object(self.companion, "running_instance_pid", return_value=4242),
            mock.patch.object(
                self.companion, "raise_running_companion", return_value=raise_result,
            ) as raiser,
            mock.patch.object(
                tkinter.messagebox, "showinfo",
                side_effect=lambda *a, **k: shown.append(a),
            ),
            mock.patch.object(self.companion, "consume_pairing_argument", return_value=None),
        ):
            code = self.companion.main(edition=edition)
        return code, shown, raiser

    def test_a_successful_raise_shows_no_dialog(self):
        code, shown, raiser = self.run_main(raise_result=True)
        self.assertEqual(0, code)
        self.assertEqual([], shown, "the window came forward; nothing to say")
        raiser.assert_called_once_with(4242)

    def test_a_failed_raise_falls_back_to_the_honest_message(self):
        code, shown, _raiser = self.run_main(raise_result=False)
        self.assertEqual(0, code)
        self.assertEqual(1, len(shown))
        message = shown[0][1]
        self.assertIn("did not respond", message)
        # A withdrawn window has no taskbar button to navigate back to.
        self.assertNotIn("taskbar button", message)


class RaiseObservesTheWindowTests(unittest.TestCase):
    """The raise succeeds only when the window observably came forward."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def fake_user32(self, *, visible):
        user32 = mock.Mock()
        user32.FindWindowW.return_value = 7001
        user32.GetWindowThreadProcessId.side_effect = lambda hwnd, pid_pointer: None
        user32.IsWindowVisible.return_value = 1 if visible else 0
        user32.IsIconic.return_value = 0
        user32.GetForegroundWindow.return_value = 9999
        return user32

    def test_a_window_that_never_appears_is_a_reported_failure(self):
        user32 = self.fake_user32(visible=False)
        now = 0.0
        clock_reads = 0

        def monotonic():
            nonlocal clock_reads
            clock_reads += 1
            self.assertLess(clock_reads, 100, "window polling did not reach its timeout")
            return now

        def sleep(seconds):
            nonlocal now
            self.assertGreater(seconds, 0, "window polling must wait between attempts")
            now += seconds

        with (
            mock.patch.object(self.companion.ctypes, "WinDLL", return_value=user32),
            mock.patch.object(self.companion, "window_process_id", return_value=4242),
            mock.patch.object(self.companion, "main_window_for_pid", return_value=8001),
            mock.patch.object(self.companion.time, "monotonic", monotonic),
            mock.patch.object(self.companion.time, "sleep", side_effect=sleep) as wait,
        ):
            result = self.companion.raise_running_companion(4242)
        wait.assert_called()
        self.assertFalse(
            result,
            "an invisible window must be reported as failure, not assumed up",
        )

    def test_a_window_that_appears_is_success_and_the_show_was_asked_for(self):
        user32 = self.fake_user32(visible=True)
        with (
            mock.patch.object(self.companion.ctypes, "WinDLL", return_value=user32),
            mock.patch.object(self.companion, "window_process_id", return_value=4242),
            mock.patch.object(self.companion, "main_window_for_pid", return_value=8001),
        ):
            result = self.companion.raise_running_companion(4242)
        self.assertTrue(result)
        user32.AllowSetForegroundWindow.assert_called_once()
        user32.PostMessageW.assert_called_once()
        args = user32.PostMessageW.call_args.args
        self.assertEqual(self.companion.WM_COMMAND, args[1])
        self.assertEqual(self.companion.TRAY_COMMAND_SHOW, args[2])


class IndependentInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_launch_never_uninstalls_the_other_product(self):
        with (
            mock.patch.object(self.companion.subprocess, "run") as runner,
            mock.patch.object(self.companion.winreg, "OpenKey") as registry,
        ):
            self.assertFalse(self.companion.retire_standalone_vemals_ears())
        runner.assert_not_called()
        registry.assert_not_called()

    @contextmanager
    def startup(self, edition, *, other_pid=None):
        """Run real startup configuration with native and UI boundaries replaced."""
        with isolated_companion(edition or "member") as app, ExitStack() as stack:
            patches = {
                "consume_pairing_argument": None,
                "claim_instance_mutex": other_pid is not None,
                "running_instance_pid": other_pid,
                "raise_running_companion": True,
                "provision_downtime_results_carrier": True,
                "ensure_icon": app.ICON_FILE,
                "register_pairing_links": True,
                "migrate_autostart": None,
                "CompanionWindow": None,
                "retire_standalone_vemals_ears": False,
            }
            calls = {
                name: stack.enter_context(mock.patch.object(app, name, return_value=value))
                for name, value in patches.items()
            }
            root = stack.enter_context(mock.patch.object(app.tk, "Tk")).return_value
            code = app.main() if edition is None else app.main(edition=edition)
            self.assertEqual(0, code)
            root.withdraw.assert_called_once_with()
            calls["retire_standalone_vemals_ears"].assert_not_called()
            yield app, root, calls

    def test_startup_has_no_retirement_step(self):
        for edition in ("full-local", "member", "downtime-hosted"):
            with self.subTest(edition=edition), self.startup(edition) as (app, root, calls):
                calls["CompanionWindow"].assert_called_once_with(
                    root, edition=edition, result_carrier_available=True,
                )
                root.mainloop.assert_called_once_with()
                calls["raise_running_companion"].assert_not_called()
                self.assertEqual(str(app.os.getpid()), app.LOCK.read_text(encoding="ascii"))

    def test_source_startup_defaults_to_the_member_application(self):
        with self.startup(None) as (app, root, calls):
            calls["CompanionWindow"].assert_called_once_with(
                root, edition=app.MEMBER_EDITION, result_carrier_available=True,
            )
            self.assertEqual("member", app.resolve_edition())
            self.assertEqual("VemalsEars", app.STATE_DIR.name)

    def test_second_launch_has_no_retirement_step(self):
        for edition in ("full-local", "member", "downtime-hosted"):
            with self.subTest(edition=edition), self.startup(edition, other_pid=4242) as (
                app, root, calls,
            ):
                calls["raise_running_companion"].assert_called_once_with(4242)
                calls["CompanionWindow"].assert_not_called()
                root.mainloop.assert_not_called()
                calls["migrate_autostart"].assert_not_called()
                self.assertFalse(app.LOCK.exists())


if __name__ == "__main__":
    unittest.main()
