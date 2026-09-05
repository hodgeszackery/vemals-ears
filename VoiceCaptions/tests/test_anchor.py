"""The caption anchor chain: precedence, fall-through, and fail-open.

These tests patch the two win32 primitives (window enumeration and
monitor enumeration) rather than faking user32, so they pin the DECISION
LOGIC - which step runs, in what order, and what a miss does - on any
platform the suite runs on.
"""

from __future__ import annotations

import unittest
from unittest import mock

from VoiceCaptions import overlay
from VoiceCaptions.settings import OverlaySettings

WOW_RECT = (10, 20, 1920, 1080)
CHOSEN_RECT = (30, 40, 2560, 1440)
MONITOR_RECT = (5120, 0, 2048, 1152)


def settings(**kwargs) -> OverlaySettings:
    return OverlaySettings(**kwargs)


class AnchorChainTests(unittest.TestCase):
    def test_auto_scans_the_profile_table(self):
        calls = []

        def fake_find(executables, title_fragment=None):
            calls.append((frozenset(executables), title_fragment))
            return WOW_RECT

        with mock.patch.object(overlay, "find_game_client_rect", fake_find):
            rect = overlay.resolve_anchor_rect(settings())

        self.assertEqual(WOW_RECT, rect)
        self.assertEqual(
            [(overlay.WOW_PROCESS_NAMES, "world of warcraft")],
            calls,
            "auto mode is the known-game table, WoW first",
        )

    def test_a_chosen_window_outranks_the_table(self):
        def fake_find(executables, title_fragment=None):
            if executables == frozenset({"ffxiv_dx11.exe"}):
                return CHOSEN_RECT
            return WOW_RECT

        with mock.patch.object(overlay, "find_game_client_rect", fake_find):
            rect = overlay.resolve_anchor_rect(
                settings(anchor_mode="window", anchor_executable="ffxiv_dx11.exe")
            )
        self.assertEqual(CHOSEN_RECT, rect)

    def test_a_chosen_window_matches_by_ownership_not_title(self):
        """The member already said which program they mean.

        Requiring a title fragment for a CHOSEN executable would make
        the choice silently fail for any game whose window title we did
        not predict - which is every game the profile table lacks, i.e.
        the entire audience of this mode.
        """
        seen = []

        def fake_find(executables, title_fragment=None):
            seen.append(title_fragment)
            return CHOSEN_RECT

        with mock.patch.object(overlay, "find_game_client_rect", fake_find):
            overlay.resolve_anchor_rect(
                settings(anchor_mode="window", anchor_executable="ffxiv_dx11.exe")
            )
        self.assertEqual([None], seen)

    def test_a_missing_chosen_window_falls_through_to_the_table(self):
        """The game is not running; captions must not vanish.

        A member who chose a game and then opens a different one still
        gets captions wherever auto-detect can put them. Fall-through,
        never a dead pin - the chain's whole reason to exist.
        """
        def fake_find(executables, title_fragment=None):
            if executables == frozenset({"gone.exe"}):
                return None
            return WOW_RECT

        with mock.patch.object(overlay, "find_game_client_rect", fake_find):
            rect = overlay.resolve_anchor_rect(
                settings(anchor_mode="window", anchor_executable="gone.exe")
            )
        self.assertEqual(WOW_RECT, rect)

    def test_a_pinned_monitor_is_used_when_present(self):
        with mock.patch.object(
            overlay, "monitor_work_area", return_value=MONITOR_RECT
        ) as monitors:
            with mock.patch.object(
                overlay, "find_game_client_rect", return_value=WOW_RECT
            ):
                rect = overlay.resolve_anchor_rect(
                    settings(anchor_mode="monitor", anchor_monitor=2)
                )
        self.assertEqual(MONITOR_RECT, rect)
        monitors.assert_called_once_with(2)

    def test_an_unplugged_monitor_falls_through_not_to_a_wrong_screen(self):
        """Index out of range is None, never a clamp.

        A pin to a monitor that was unplugged must fall through the
        chain - clamping to 'some other screen' would put captions on a
        display the member deliberately did not choose, with nothing on
        screen explaining why.
        """
        with mock.patch.object(overlay, "monitor_work_area", return_value=None):
            with mock.patch.object(
                overlay, "find_game_client_rect", return_value=WOW_RECT
            ):
                rect = overlay.resolve_anchor_rect(
                    settings(anchor_mode="monitor", anchor_monitor=7)
                )
        self.assertEqual(WOW_RECT, rect)

    def test_monitor_selection_never_clamps(self):
        monitors = [(0, 0, 100, 100), (100, 0, 100, 100)]
        self.assertEqual(monitors[1], overlay._select_monitor(monitors, 1))
        self.assertIsNone(overlay._select_monitor(monitors, 2))
        self.assertIsNone(overlay._select_monitor(monitors, -1))
        self.assertIsNone(overlay._select_monitor([], 0))

    def test_nothing_resolving_returns_none_for_the_callers_fallback(self):
        with mock.patch.object(overlay, "find_game_client_rect", return_value=None):
            with mock.patch.object(overlay, "monitor_work_area", return_value=None):
                self.assertIsNone(
                    overlay.resolve_anchor_rect(settings(anchor_mode="window",
                                                         anchor_executable="x.exe"))
                )


class OwnershipFailsOpenTests(unittest.TestCase):
    """The asymmetry the WoW-only code encoded, preserved by name.

    A stray window match puts captions in the wrong place; a strict miss
    loses them entirely. For an accessibility product wrong-place beats
    gone, so an unidentifiable owner is ACCEPTED.
    """

    def test_an_unidentifiable_owner_is_accepted(self):
        user32 = mock.Mock()
        with mock.patch.object(overlay, "process_image_name", return_value=None):
            self.assertTrue(
                overlay.window_belongs_to(1, user32, frozenset({"game.exe"}))
            )

    def test_a_known_foreign_owner_is_rejected(self):
        user32 = mock.Mock()
        with mock.patch.object(
            overlay, "process_image_name", return_value="discord.exe"
        ):
            self.assertFalse(
                overlay.window_belongs_to(1, user32, frozenset({"game.exe"}))
            )

    def test_the_wow_wrapper_is_the_general_rule_applied(self):
        user32 = mock.Mock()
        with mock.patch.object(
            overlay, "process_image_name", return_value="wowclassic.exe"
        ):
            self.assertTrue(overlay.window_belongs_to_wow(1, user32))


class AnchorSettingsContractTests(unittest.TestCase):
    """The keys Unit B writes against, pinned from the reading side."""

    def test_defaults_are_todays_behaviour(self):
        defaults = OverlaySettings()
        self.assertEqual("auto", defaults.anchor_mode)
        self.assertEqual("", defaults.anchor_executable)
        self.assertEqual(0, defaults.anchor_monitor)

    def test_a_mangled_file_cannot_smuggle_a_path_into_matching(self):
        from VoiceCaptions.settings import _valid_anchor_executable

        for hostile in (
            "C:/games/fake.exe",
            "..\\..\\system32\\cmd.exe".replace("\\\\", chr(92)),
            "a" * 65,
            "",
            42,
            "bad\x00name.exe",
        ):
            with self.subTest(value=hostile):
                self.assertEqual("", _valid_anchor_executable(hostile))

    def test_the_chosen_name_is_lowercased_like_the_process_images(self):
        from VoiceCaptions.settings import _valid_anchor_executable

        self.assertEqual("ffxiv_dx11.exe", _valid_anchor_executable("FFXIV_dx11.EXE"))


if __name__ == "__main__":
    unittest.main()
