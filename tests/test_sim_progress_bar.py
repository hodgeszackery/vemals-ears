"""The sim progress bar, window side.

A running @ghstatus payload may carry stage/done/total/fraction/left.
The Gear Helper row draws a bar from it under the status line, shows the
bar on the first such line and retires it at the first settled state.
Shino, 2026-09-04: "instead of Preparing candidates I want to see the
status bar filling up".
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.support.companion import load_companion


class DescribeProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def running(self, **fields):
        payload = {"schema": 1, "phase": "running", "requestID": "r-1"}
        payload.update(fields)
        return payload

    def test_a_pass_reads_as_count_and_estimate(self):
        text, fraction, caption = self.companion.describe_progress(self.running(
            stage="precise", done=88, total=152, fraction=88 / 152, left=380.0,
        ))
        self.assertEqual(text, "Precise pass 88/152 - about 7 min left")
        self.assertAlmostEqual(fraction, 88 / 152)
        # The count is on the status line; the bar says its percent, so
        # the two lines do not repeat each other.
        self.assertIsNone(caption)

    def test_each_stage_has_its_own_name_and_unknown_still_fills(self):
        for stage, label in (
            ("screen", "Screening pass"),
            ("precise", "Precise pass"),
            ("vault", "Great Vault pass"),
            ("bis", "Best in slot pass"),
            ("something-newer", "Simulating"),
            (None, "Simulating"),
        ):
            with self.subTest(stage=stage):
                text, fraction, _caption = self.companion.describe_progress(self.running(
                    stage=stage, done=1, total=2, fraction=0.5,
                ))
                self.assertEqual(text, label + " 1/2")
                self.assertEqual(fraction, 0.5)

    def test_under_a_minute_is_said_not_rounded_to_zero(self):
        text, _fraction, _caption = self.companion.describe_progress(self.running(
            stage="screen", done=180, total=183, fraction=0.98, left=12.0,
        ))
        self.assertEqual(text, "Screening pass 180/183 - under a minute left")

    def test_the_baseline_has_no_count_and_an_empty_bar(self):
        text, fraction, caption = self.companion.describe_progress(self.running(
            stage="precise", done=0, total=0, fraction=0.0,
        ))
        self.assertEqual(text, "Precise pass - simulating the baseline")
        self.assertEqual(fraction, 0.0)
        # No honest percent for the baseline, so no caption at all.
        self.assertEqual(caption, "")

    def test_fraction_is_clamped_and_done_is_capped(self):
        text, fraction, _caption = self.companion.describe_progress(self.running(
            stage="precise", done=200, total=152, fraction=1.4,
        ))
        self.assertEqual(text, "Precise pass 152/152")
        self.assertEqual(fraction, 1.0)

    def test_lines_without_progress_leave_the_bar_alone(self):
        for payload in (
            self.running(),
            self.running(detail="SimulationCraft is running."),
            self.running(step=2, steps=4, detail="Screening the full gear candidate pool."),
            self.running(stage="precise", done=1),
            self.running(stage="precise", done=True, total=2, fraction=0.5),
            self.running(stage="precise", done=1, total=2, fraction="half"),
            self.running(stage="precise", done=1, total=2, fraction=float("nan")),
            {"schema": 1, "phase": "done", "done": 3, "total": 3, "fraction": 1.0},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(self.companion.describe_progress(payload))

    def test_nothing_in_a_payload_can_raise(self):
        # This runs inside the window's tick; an exception there stops
        # every row's clock for the life of the window. json.loads
        # accepts NaN and Infinity, so a payload can carry them.
        for payload in (
            self.running(stage="precise", done=1, total=float("nan"), fraction=0.5),
            self.running(stage="precise", done=float("inf"), total=3, fraction=0.5),
            self.running(stage="precise", done=1, total=float("inf"), fraction=0.5),
            self.running(stage=["precise"], done=1, total=3, fraction=0.5),
            self.running(stage={"a": 1}, done=1, total=3, fraction=0.5),
        ):
            with self.subTest(payload=str(payload)):
                result = self.companion.describe_progress(payload)
                self.assertTrue(result is None or isinstance(result, tuple))
        # An infinite or NaN estimate is dropped, not shown, not fatal.
        for left in (float("inf"), float("nan"), 1e300):
            with self.subTest(left=left):
                text, _fraction, _caption = self.companion.describe_progress(self.running(
                    stage="precise", done=1, total=3, fraction=0.33, left=left,
                ))
                self.assertTrue(text.startswith("Precise pass 1/3"), text)
        # A list for a stage is not a name, but the line still fills.
        text, _fraction, _caption = self.companion.describe_progress(self.running(
            stage=["precise"], done=1, total=3, fraction=0.5,
        ))
        self.assertEqual(text, "Simulating 1/3")

    def test_a_negative_estimate_is_not_shown(self):
        text, _fraction, _caption = self.companion.describe_progress(self.running(
            stage="precise", done=1, total=2, fraction=0.5, left=-3,
        ))
        self.assertEqual(text, "Precise pass 1/2")


class StatusLineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_a_progress_line_puts_the_count_on_the_badge_line(self):
        text, color, state = self.companion.describe_status({
            "schema": 1, "phase": "running", "stage": "screen",
            "done": 40, "total": 183, "fraction": 40 / 183, "left": 45.0,
        })
        self.assertEqual(text, "Screening pass 40/183 - under a minute left")
        self.assertEqual(color, self.companion.WARNING)
        self.assertEqual(state, "checking")

    def test_a_coarse_step_shows_its_sentence(self):
        text, _color, state = self.companion.describe_status({
            "schema": 1, "phase": "running", "step": 2, "steps": 4,
            "detail": "Screening the full gear candidate pool.",
        })
        self.assertEqual(text, "Screening the full gear candidate pool.")
        self.assertEqual(state, "checking")

    def test_a_plain_running_line_reads_as_it_always_did(self):
        text, _color, _state = self.companion.describe_status({
            "schema": 1, "phase": "running", "detail": "SimulationCraft is running.",
        })
        self.assertEqual(text, "Simulating - a few minutes")


class DrainTests(unittest.TestCase):
    """The service applies progress lines to its bar and nothing else."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def gear_service(self):
        window = SimpleNamespace(
            hosted_edition=False,
            result_carrier_available=True,
            append=mock.Mock(),
        )
        service = self.companion.Service(
            spec=next(spec for spec in self.companion.SERVICES if spec.key == "gear"),
            window=window,
        )
        process = object()
        service.process = process
        service.set_status = mock.Mock()
        service.set_progress = mock.Mock()
        service.clear_progress = mock.Mock()
        return service, process, window

    def status_line(self, **fields):
        payload = {"schema": 1, "phase": "running", "requestID": "r-1"}
        payload.update(fields)
        return self.companion.GHSTATUS_PREFIX + json.dumps(payload) + "\n"

    def test_a_progress_line_fills_the_bar_and_stays_out_of_activity(self):
        service, process, window = self.gear_service()
        service.lines.put((process, self.status_line(
            stage="precise", done=88, total=152, fraction=88 / 152, left=380.0,
        )))
        service.drain()
        service.set_status.assert_called_once_with(
            "Precise pass 88/152 - about 7 min left", self.companion.WARNING, "checking",
        )
        (fraction, caption), _ = service.set_progress.call_args
        self.assertAlmostEqual(fraction, 88 / 152)
        self.assertIsNone(caption)
        window.append.assert_not_called()

    def test_a_non_finite_field_does_not_stop_the_drain(self):
        service, process, _window = self.gear_service()
        service.lines.put((process, self.companion.GHSTATUS_PREFIX
            + '{"schema": 1, "phase": "running", "stage": "precise", '
            '"done": 1, "total": NaN, "fraction": 0.5, "left": Infinity}\n'))
        service.lines.put((process, self.status_line(
            stage="precise", done=2, total=4, fraction=0.5,
        )))
        service.drain()
        # The bad line fell through as a plain running status; the good
        # line behind it was still drained and applied.
        self.assertEqual(service.set_status.call_count, 2)
        (fraction, _caption), _ = service.set_progress.call_args
        self.assertEqual(fraction, 0.5)

    def test_a_coarse_step_labels_without_touching_the_bar(self):
        service, process, _window = self.gear_service()
        service.lines.put((process, self.status_line(
            step=3, steps=4, detail="Running the precise gear comparison.",
        )))
        service.drain()
        service.set_status.assert_called_once_with(
            "Running the precise gear comparison.", self.companion.WARNING, "checking",
        )
        service.set_progress.assert_not_called()

    def test_the_prose_count_fills_the_bar_too(self):
        service, process, _window = self.gear_service()
        service.lines.put((process, "[###......] 3/9 Nymrissa\n"))
        service.drain()
        service.set_status.assert_called_once_with(
            "[###......] 3/9 Nymrissa", self.companion.WARNING, "checking",
        )
        (fraction,), _ = service.set_progress.call_args
        self.assertAlmostEqual(fraction, 1 / 3)

    def test_a_complete_prose_count_clears_rather_than_fills(self):
        # The hosted client prints "[####] 1/1 Result ready for Load
        # Results." AFTER the result marker that set Done. A full bar
        # re-shown under Done would say the job was still running, and
        # nothing would retire it until the next job, hours later.
        service, process, _window = self.gear_service()
        service.lines.put((process, "[########################] 1/1 Result ready for Load Results.\n"))
        service.drain()
        service.set_progress.assert_not_called()
        service.clear_progress.assert_called_once_with()

    def test_a_zero_count_shows_an_empty_bar(self):
        service, process, _window = self.gear_service()
        service.lines.put((process, "[.........] 0/9 Starting\n"))
        service.drain()
        (fraction,), _ = service.set_progress.call_args
        self.assertEqual(fraction, 0.0)

    def test_a_child_that_exits_takes_its_bar_with_it(self):
        service, _process, _window = self.gear_service()
        process = mock.Mock()
        process.poll.return_value = 0
        service.process = process
        service.lines.put((process, None))
        service.drain()
        service.clear_progress.assert_called_once_with()


class SettledStateTests(unittest.TestCase):
    """set_status retires the bar for every state except checking."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def service_with_bar(self):
        service = self.companion.Service(
            spec=next(spec for spec in self.companion.SERVICES if spec.key == "gear"),
            window=SimpleNamespace(),
        )
        service.progress_row = mock.Mock()
        service.progress_bar = mock.Mock()
        service.progress_caption = mock.Mock()
        return service

    def test_progress_shows_the_row_once_and_a_settled_state_hides_it(self):
        service = self.service_with_bar()
        service.set_progress(0.25)
        service.set_progress(0.5)
        service.progress_row.pack.assert_called_once_with(fill="x", pady=(8, 0))
        service.progress_bar.set_progress.assert_called_with(0.5)
        service.progress_caption.config.assert_called_with(text="50%")
        self.assertTrue(service.progress_visible)
        # An explicit caption, empty included, is shown as given.
        service.set_progress(0.0, "")
        service.progress_caption.config.assert_called_with(text="")
        service.set_progress(0.75, "three of four")
        service.progress_caption.config.assert_called_with(text="three of four")

        service.set_status("Simulating - a few minutes", self.companion.WARNING, "checking")
        service.progress_row.pack_forget.assert_not_called()

        for state in ("done", "ready", "attention", "stopped", "queued"):
            with self.subTest(state=state):
                service.progress_visible = True
                service.progress_row.pack_forget.reset_mock()
                service.set_status("settled", self.companion.SUCCESS, state)
                service.progress_row.pack_forget.assert_called_once_with()
                self.assertFalse(service.progress_visible)

    def test_a_status_with_no_job_state_leaves_the_bar_alone(self):
        service = self.service_with_bar()
        service.set_progress(0.5)
        service.set_status("Still going", self.companion.WARNING)
        service.progress_row.pack_forget.assert_not_called()
        service.progress_caption.config.assert_called_with(text="50%")

    def test_the_row_is_packed_after_its_anchor(self):
        service = self.service_with_bar()
        service.progress_anchor = mock.Mock(name="top")
        service.set_progress(0.1)
        service.progress_row.pack.assert_called_once_with(
            fill="x", pady=(8, 0), after=service.progress_anchor,
        )

    def test_unusable_fractions_are_ignored(self):
        service = self.service_with_bar()
        for value in ("half", None, float("nan")):
            with self.subTest(value=value):
                service.set_progress(value)
        service.progress_row.pack.assert_not_called()
        self.assertFalse(service.progress_visible)

    def test_a_row_without_a_bar_accepts_progress_quietly(self):
        service = self.companion.Service(
            spec=next(spec for spec in self.companion.SERVICES if spec.key == "voice"),
            window=SimpleNamespace(),
        )
        service.set_progress(0.5, "x")
        service.clear_progress()
        self.assertFalse(service.progress_visible)


class RowTests(unittest.TestCase):
    """The real row: the bar exists, hidden, on the rows that sim."""

    @classmethod
    def setUpClass(cls):
        cls.companion = load_companion()

    def test_the_gear_row_carries_a_hidden_bar_and_shows_it_on_progress(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(
                root, edition=self.companion.FULL_EDITION, preview=True,
            )
            root.update()
            by_key = {service.spec.key: service for service in window.services}
            gear = by_key["gear"]
            self.assertIsNotNone(gear.progress_bar)
            self.assertIsNotNone(gear.progress_row)
            self.assertEqual(gear.progress_row.winfo_manager(), "")
            # A child of the card content beside the top block, not of
            # the copy column inside it: inside, showing it would raise
            # that block and re-centre the buttons on its right.
            top = gear.progress_anchor
            self.assertIsNotNone(top)
            self.assertIs(gear.progress_row.master, top.master)
            self.assertIs(gear.status.master.master, top)
            root.update_idletasks()
            button_y_before = gear.button.winfo_rooty()
            top_height_before = top.winfo_height()

            gear.set_progress(0.4)
            root.update_idletasks()
            self.assertEqual(gear.progress_row.winfo_manager(), "pack")
            self.assertEqual(gear.progress_caption.cget("text"), "40%")
            self.assertAlmostEqual(gear.progress_bar.progress, 0.4)
            # The bar sits directly under the top block, which did not
            # grow, so the Stop button did not move.
            self.assertEqual(top.winfo_height(), top_height_before)
            self.assertEqual(gear.button.winfo_rooty(), button_y_before)
            self.assertGreaterEqual(
                gear.progress_row.winfo_y(), top.winfo_y() + top.winfo_height(),
            )
            packed = top.master.pack_slaves()
            self.assertEqual(packed.index(gear.progress_row), packed.index(top) + 1)

            gear.set_status("Results ready - reload WoW", self.companion.SUCCESS, "done")
            root.update_idletasks()
            self.assertEqual(gear.progress_row.winfo_manager(), "")
            self.assertEqual(gear.button.winfo_rooty(), button_y_before)

            self.assertIsNotNone(by_key["talents"].progress_bar)
            for key in ("voice", "voice_discord"):
                self.assertNotIn(key, by_key)
        finally:
            root.destroy()

    def test_voice_app_has_no_simulation_progress_controls(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            window = self.companion.CompanionWindow(
                root, edition=self.companion.MEMBER_EDITION, preview=True,
            )
            self.assertEqual(["voice"], [service.spec.key for service in window.services])
            self.assertIsNone(window.services[0].progress_bar)
            self.assertIsNone(window.services[0].progress_row)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
