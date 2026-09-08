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

    def test_monitor_candidate_carries_full_bounds_beside_the_work_area(self):
        info = SimpleNamespace(
            szDevice=r"\\.\DISPLAY4",
            rcMonitor=SimpleNamespace(left=-1920, top=0, right=0, bottom=1080),
            rcWork=SimpleNamespace(left=-1920, top=0, right=0, bottom=1040),
            dwFlags=overlay.MONITORINFOF_PRIMARY,
        )
        candidate = overlay._monitor_candidate_from_info(info, 3)
        # The picker still shows the usable surface, unchanged.
        self.assertEqual(
            (-1920, 0, 1920, 1040),
            (candidate.left, candidate.top, candidate.width, candidate.height),
        )
        # And the whole display is now available beside it.
        self.assertEqual((-1920, 0, 1920, 1080), candidate.display_bounds)
        # A record built without bounds degrades to the work area rather
        # than reporting a zero-sized rect.
        legacy = overlay.CaptionMonitorCandidate(
            0, r"\\.\DISPLAY1", 0, 0, 1920, 1040, True
        )
        self.assertEqual((0, 0, 1920, 1040), legacy.display_bounds)

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

    def test_move_preview_falls_back_to_the_display_under_the_pointer(self):
        """Replaces a test that pinned the bug as a contract.

        Shino, 2026-09-08. With no anchor resolved - the ordinary case
        for somebody setting up before a raid - the preview used to be
        measured against the PRIMARY monitor whatever display the member
        was looking at. That rect sized it, positioned it, and was the
        frame Save measured a dragged position against.
        """
        two_displays = ((0, 0, 5120, 1440), (5120, 0, 1920, 1080))

        def root_at(pointer):
            return SimpleNamespace(
                winfo_screenwidth=lambda: 5120,
                winfo_screenheight=lambda: 1440,
                winfo_pointerxy=lambda: pointer,
            )

        # A resolved anchor still wins outright, unchanged.
        selected = OverlaySettings(anchor_mode="monitor", anchor_monitor=1)
        with mock.patch.object(
            placement, "resolve_anchor_rect", return_value=(-2560, 0, 2560, 1400)
        ):
            target = placement.preview_target(root_at((0, 0)), selected)
            self.assertEqual((-2560, 0, 2560, 1400), target.rect)
            self.assertTrue(target.resolved)

        # Nothing resolved, pointer on the second display: that display.
        with mock.patch.object(placement, "resolve_anchor_rect", return_value=None):
            target = placement.preview_target(
                root_at((6000, 500)), selected, displays=two_displays
            )
            self.assertEqual((5120, 0, 1920, 1080), target.rect)
            self.assertFalse(target.resolved)

            # A root that cannot report a pointer keeps the old rect
            # rather than raising - the resolver double below has no
            # winfo_pointerxy at all.
            bare = SimpleNamespace(
                winfo_screenwidth=lambda: 1920,
                winfo_screenheight=lambda: 1080,
            )
            self.assertEqual(
                (0, 0, 1920, 1080),
                placement.target_rect_for_settings(bare, selected),
            )

            # And with no monitor list - off win32, or the enumeration
            # failed - the last resort is the rect this returned before.
            root = root_at((6000, 500))
            self.assertEqual(
                (0, 0, root.winfo_screenwidth(), root.winfo_screenheight()),
                placement.target_rect_for_settings(
                    root, selected, displays=()
                ),
            )

    def test_a_resolved_anchor_outranks_the_display_under_the_pointer(self):
        """A windowed player whose game resolves is already pixel-exact."""
        root = SimpleNamespace(
            winfo_screenwidth=lambda: 5120,
            winfo_screenheight=lambda: 1440,
            winfo_pointerxy=lambda: (2000, 700),
        )
        displays = mock.Mock()
        with mock.patch.object(
            placement, "resolve_anchor_rect", return_value=(5120, 0, 1920, 1080)
        ), mock.patch.object(
            placement, "monitor_display_bounds", displays
        ):
            target = placement.preview_target(root, OverlaySettings())
        self.assertEqual((5120, 0, 1920, 1080), target.rect)
        self.assertTrue(target.resolved)
        displays.assert_not_called()

    def test_single_display_member_keeps_the_rect_move_already_used(self):
        """The primary's BOUNDS are the screen rect, bit for bit.

        Written as the expression rather than a literal so the intent is
        visible: nobody on one monitor moves by a pixel.
        """
        root = SimpleNamespace(
            winfo_screenwidth=lambda: 5120,
            winfo_screenheight=lambda: 1440,
            winfo_pointerxy=lambda: (2000, 700),
        )
        with mock.patch.object(placement, "resolve_anchor_rect", return_value=None):
            rect = placement.target_rect_for_settings(
                root, OverlaySettings(), displays=((0, 0, 5120, 1440),)
            )
        self.assertEqual(
            (0, 0, root.winfo_screenwidth(), root.winfo_screenheight()), rect
        )

    def test_display_bounds_for_rect_picks_the_display_the_probe_is_on(self):
        bounds = ((0, 0, 5120, 1440), (5120, 0, 1920, 1080))
        primary = (0, 0, 5120, 1440)
        self.assertEqual(
            (5120, 0, 1920, 1080),
            placement.display_bounds_for_rect(bounds, (6000, 500, 1, 1), primary),
        )
        self.assertEqual(
            primary,
            placement.display_bounds_for_rect(bounds, (2000, 700, 1, 1), primary),
        )
        # Inside the second display's bounds but below any work area -
        # over its taskbar. Still that display, which is the point of
        # using bounds rather than work areas here.
        self.assertEqual(
            (5120, 0, 1920, 1080),
            placement.display_bounds_for_rect(bounds, (6000, 1060, 1, 1), primary),
        )
        # A preview pushed off the desktop entirely by the old bug goes
        # to the NEAREST display, not to the primary.
        self.assertEqual(
            (5120, 0, 1920, 1080),
            placement.display_bounds_for_rect(bounds, (7201, 576, 540, 190), primary),
        )
        # No displays at all: the caller's fallback, by identity.
        fallback = (7, 7, 70, 70)
        self.assertIs(
            fallback,
            placement.display_bounds_for_rect((), (0, 0, 10, 10), fallback),
        )

    def test_only_an_unreachable_preview_is_rescued_onto_its_target(self):
        bounds = ((0, 0, 5120, 1440), (5120, 0, 1920, 1080))
        self.assertEqual(
            (6500, 576),
            placement.rescued_position(
                bounds, (5120, 0, 1920, 1080), (540, 190), (7201, 576)
            ),
        )
        # Already reachable: returned untouched, so a member who hung
        # their captions off a screen edge keeps them there. This
        # position is deliberately PAST the edge - 220 px of the card
        # still showing - so an unconditional clamp would move it.
        self.assertEqual(
            (4900, 1300),
            placement.rescued_position(
                bounds, (0, 0, 5120, 1440), (540, 190), (4900, 1300)
            ),
        )
        self.assertEqual(
            (1999, 899),
            placement.rescued_position(
                bounds, (0, 0, 5120, 1440), (540, 190), (1999, 899)
            ),
        )
        # The margin decides. Thirty pixels showing is reachable.
        self.assertTrue(
            placement.preview_is_reachable(
                bounds, (0, 0, 5120, 1440), (5090, 1410, 540, 190)
            )
        )
        # Ten is not.
        self.assertFalse(
            placement.preview_is_reachable(
                bounds, (5120, 0, 1920, 1080), (7030, 1070, 540, 190)
            )
        )
        # A preview larger than its target pins to the near edge rather
        # than inverting into a negative position.
        self.assertEqual(
            (5120, 0),
            placement.rescued_position(
                (), (5120, 0, 400, 100), (540, 190), (9000, 9000)
            ),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
