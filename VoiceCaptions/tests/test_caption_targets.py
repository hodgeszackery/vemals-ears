from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from VoiceCaptions import overlay, placement
from VoiceCaptions.settings import OverlaySettings


def snapshot(
    executable: str,
    title: str,
    rect: tuple[int, int, int, int],
) -> overlay._WindowSnapshot:
    return overlay._WindowSnapshot(
        overlay.CaptionWindowCandidate(executable, title), rect
    )


class WindowCandidateTests(unittest.TestCase):
    def test_picker_deduplicates_windows_by_executable(self):
        smaller = snapshot("game.exe", "Old title", (0, 0, 800, 600))
        larger = snapshot("game.exe", "Current title", (0, 0, 1600, 900))
        with mock.patch.object(
            overlay,
            "_visible_window_snapshots",
            return_value=(smaller, larger),
        ):
            choices = overlay.enumerate_caption_windows()
        self.assertEqual(1, len(choices))
        self.assertEqual("game.exe", choices[0].target.executable)
        self.assertEqual("window", choices[0].target.mode)
        self.assertEqual("Current title (game.exe)", choices[0].label)

    def test_blank_title_has_an_executable_label_and_a_usable_choice(self):
        choice = overlay.CaptionWindowCandidate("game.exe", "")
        self.assertEqual("game.exe", choice.label)
        self.assertEqual(
            overlay.CaptionTargetChoice("window", "game.exe"), choice.target
        )

    def test_choice_does_not_carry_a_live_window_identity(self):
        choice = overlay.CaptionWindowCandidate("game.exe", "Private character")
        self.assertEqual(
            ("window", "game.exe", 0),
            (
                choice.target.mode,
                choice.target.executable,
                choice.target.monitor_index,
            ),
        )
        self.assertNotIn("Private character", repr(choice))


class MonitorCandidateTests(unittest.TestCase):
    def test_monitor_candidate_uses_work_area_and_resolver_index(self):
        info = SimpleNamespace(
            szDevice=r"\\.\DISPLAY4",
            rcMonitor=SimpleNamespace(left=-1920, top=0, right=0, bottom=1080),
            rcWork=SimpleNamespace(left=-1920, top=0, right=0, bottom=1040),
            dwFlags=overlay.MONITORINFOF_PRIMARY,
        )
        candidate = overlay._monitor_candidate_from_info(info, 3)
        self.assertIsNotNone(candidate)
        self.assertEqual(3, candidate.index)
        self.assertEqual((-1920, 0, 1920, 1040), (
            candidate.left,
            candidate.top,
            candidate.width,
            candidate.height,
        ))
        self.assertTrue(candidate.primary)
        self.assertEqual(
            overlay.CaptionTargetChoice("monitor", monitor_index=3),
            candidate.target,
        )

    def test_monitor_work_area_reads_the_same_index_the_picker_writes(self):
        first = overlay.CaptionMonitorCandidate(
            0, r"\\.\DISPLAY1", 0, 0, 1920, 1040, True
        )
        second = overlay.CaptionMonitorCandidate(
            1, r"\\.\DISPLAY2", -2560, 0, 2560, 1400
        )
        with mock.patch.object(
            overlay, "_monitor_records", return_value=(first, second)
        ):
            self.assertEqual(
                (-2560, 0, 2560, 1400), overlay.monitor_work_area(1)
            )


class AnchorRefreshTests(unittest.TestCase):
    def test_next_overlay_tick_reads_settings_before_captions_and_target(self):
        selected = OverlaySettings(anchor_mode="monitor", anchor_monitor=2)
        calls: list[str] = []
        observed = []
        subject = SimpleNamespace()

        def refresh_settings():
            calls.append("settings")
            subject.settings = selected

        def refresh_target(_now):
            calls.append("target")
            observed.append(subject.settings.anchor_monitor)

        subject.__dict__.update(
            drain_events=lambda _now: calls.append("events"),
            refresh_settings=refresh_settings,
            refresh_target=refresh_target,
            render=lambda _now: calls.append("render"),
            root=SimpleNamespace(after=lambda _delay, _callback: calls.append("after")),
            settings=OverlaySettings(),
            tick=lambda: None,
        )
        with mock.patch.object(overlay.time, "monotonic", return_value=1.0):
            overlay.CaptionOverlay.tick(subject)
        self.assertEqual(["settings", "events", "target", "render", "after"], calls)
        self.assertEqual([2], observed)

    def test_settings_change_invalidates_the_target_refresh_interval(self):
        current = OverlaySettings()
        selected = OverlaySettings(anchor_mode="window", anchor_executable="game.exe")
        subject = SimpleNamespace(
            settings_path=SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime_ns=9)),
            settings_mtime_ns=-1,
            settings=current,
            board=SimpleNamespace(configure_display=lambda **_kwargs: None),
            font_size=current.caption_font_size,
            canvas_height=100,
            last_target_check=25.0,
            last_signature=("old",),
            _canvas_height_for=lambda _rows, _size: 100,
        )
        with mock.patch.object(overlay, "load_settings", return_value=selected):
            overlay.CaptionOverlay.refresh_settings(subject)
        self.assertEqual(-overlay.TARGET_REFRESH_SECONDS, subject.last_target_check)

    def test_move_preview_uses_the_anchor_resolver_then_a_safe_fallback(self):
        root = SimpleNamespace(
            winfo_screenwidth=lambda: 1920,
            winfo_screenheight=lambda: 1080,
        )
        selected = OverlaySettings(anchor_mode="monitor", anchor_monitor=1)
        with mock.patch.object(
            placement, "resolve_anchor_rect", return_value=(-2560, 0, 2560, 1400)
        ):
            self.assertEqual(
                (-2560, 0, 2560, 1400),
                placement.target_rect_for_settings(root, selected),
            )
        with mock.patch.object(placement, "resolve_anchor_rect", return_value=None):
            self.assertEqual(
                (0, 0, 1920, 1080),
                placement.target_rect_for_settings(root, selected),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
