import json
import os
import tempfile
import unittest
from pathlib import Path

from VoiceCaptions.layout import DEFAULT_METRICS
from VoiceCaptions.settings import (
    DEFAULT_SPEAK_HOTKEY,
    MAX_CAPTION_FONT_SIZE,
    MAX_VISIBLE_SPEAKERS,
    MIN_CAPTION_FONT_SIZE,
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    canonicalize_hotkey,
    parse_hotkey,
    DEFAULT_SETTINGS,
    OverlaySettings,
    load_settings,
    position_from_settings,
    save_settings,
    settings_from_position,
)


class SettingsTests(unittest.TestCase):
    def test_missing_or_malformed_file_uses_safe_default(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            self.assertEqual(DEFAULT_SETTINGS, load_settings(path))
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(DEFAULT_SETTINGS, load_settings(path))

    def test_only_privacy_safe_display_preferences_are_stored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            expected = OverlaySettings(0.24, 0.63)
            self.assertTrue(save_settings(expected, path))
            self.assertEqual(expected, load_settings(path))
            payload = json.loads(path.read_text(encoding="utf-8"))
            # Placement and display preferences only - never speaker names,
            # caption text, or anything else from a session. Growing this set
            # is a deliberate act; each addition must be as inert as these.
            self.assertEqual(
                {
                    "schema",
                    "anchor_x",
                    "anchor_y",
                    "focus_mode",
                    "caption_hold_seconds",
                    "max_visible_speakers",
                    "caption_font_size",
                    "muted",
                    # Opaque local HMAC tokens selected by the member. The
                    # separate bounded directory holds only their visible
                    # labels; neither file stores caption text or Discord IDs.
                    "muted_speaker_ids",
                    # The anchor chain. Deliberate additions, and inert
                    # by the rule above: a mode word, a game's image
                    # basename the member chose on their own machine
                    # (validated separator-free, so never a path), and a
                    # monitor index. Local display preferences all three;
                    # nothing from a session, nothing that leaves the
                    # machine.
                    "anchor_mode",
                    "anchor_executable",
                    "anchor_monitor",
                    # Which way the caption stack grows: the word "up" or
                    # "down", validated to exactly those two. Deliberate
                    # addition, inert by the rule above.
                    "growth",
                },
                set(payload),
            )

    def test_caption_duration_and_visible_speakers_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            expected = OverlaySettings(
                anchor_x=0.4,
                anchor_y=0.6,
                focus_mode=True,
                caption_hold_seconds=10.0,
                max_visible_speakers=4,
            )
            self.assertTrue(save_settings(expected, path))
            self.assertEqual(expected, load_settings(path))

    def test_the_speaker_ceiling_matches_the_board_that_has_to_draw_them(self):
        """Two modules hold this number and they must not drift apart.

        settings keeps it as a literal so it need not import the caption
        model. If it ever exceeded the board's own ceiling, a member could
        save a row count the board rejects, and CaptionBoard would raise
        while the overlay was building - captions gone, over a number the
        settings screen had accepted as valid.
        """
        from VoiceCaptions.events import MAX_OVERLAY_ROWS

        self.assertEqual(MAX_OVERLAY_ROWS, MAX_VISIBLE_SPEAKERS)

    def test_font_size_round_trips_across_its_whole_range(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            for size in (
                MIN_CAPTION_FONT_SIZE,
                DEFAULT_SETTINGS.caption_font_size,
                18,
                MAX_CAPTION_FONT_SIZE,
            ):
                expected = OverlaySettings(caption_font_size=size)
                self.assertTrue(save_settings(expected, path))
                self.assertEqual(size, load_settings(path).caption_font_size)

    def test_an_out_of_range_font_size_falls_back_without_touching_the_rest(self):
        """A bad font size must not cost the member their other settings.

        Each display preference validates on its own, so a hand-edited or
        half-written file cannot turn one bad number into a full reset of
        placement, hold and row count as well.
        """
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            for size in (
                0,
                MIN_CAPTION_FONT_SIZE - 1,
                MAX_CAPTION_FONT_SIZE + 1,
                True,
                12.5,
                "18",
                None,
            ):
                path.write_text(
                    json.dumps(
                        {
                            "schema": 1,
                            "anchor_x": 0.25,
                            "anchor_y": 0.75,
                            "focus_mode": True,
                            "caption_hold_seconds": 6.0,
                            "max_visible_speakers": 3,
                            "caption_font_size": size,
                        }
                    ),
                    encoding="utf-8",
                )
                loaded = load_settings(path)
                self.assertEqual(
                    DEFAULT_SETTINGS.caption_font_size,
                    loaded.caption_font_size,
                    msg=f"font size {size!r} should have fallen back",
                )
                # Everything else survives the one bad field.
                self.assertEqual(6.0, loaded.caption_hold_seconds)
                self.assertEqual(3, loaded.max_visible_speakers)
                self.assertEqual(0.25, loaded.anchor_x)
                self.assertTrue(loaded.focus_mode)

    def test_legacy_file_gets_new_display_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "anchor_x": 0.2,
                        "anchor_y": 0.7,
                        "focus_mode": True,
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_settings(path)
            self.assertEqual(0.2, loaded.anchor_x)
            self.assertTrue(loaded.focus_mode)
            self.assertEqual(3.6, loaded.caption_hold_seconds)
            self.assertEqual(2, loaded.max_visible_speakers)
            # A file written before the font box existed reads as the size
            # every member already had, so nobody's overlay changes size
            # just by updating.
            self.assertEqual(
                DEFAULT_SETTINGS.caption_font_size, loaded.caption_font_size
            )

    def test_invalid_display_values_default_independently(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            invalid_pairs = (
                (True, 3),
                (float("nan"), 3),
                (0.25, 3),
                (99.0, 3),
                (6.0, True),
                (6.0, 0),
                (6.0, MAX_VISIBLE_SPEAKERS + 1),
            )
            for hold, speakers in invalid_pairs:
                path.write_text(
                    json.dumps(
                        {
                            "schema": 1,
                            "anchor_x": 0.25,
                            "anchor_y": 0.75,
                            "focus_mode": True,
                            "caption_hold_seconds": hold,
                            "max_visible_speakers": speakers,
                        }
                    ),
                    encoding="utf-8",
                )
                loaded = load_settings(path)
                expected_hold = 6.0 if hold == 6.0 else 3.6
                expected_speakers = speakers if speakers == 3 else 2
                self.assertEqual(expected_hold, loaded.caption_hold_seconds)
                self.assertEqual(expected_speakers, loaded.max_visible_speakers)
                self.assertTrue(loaded.focus_mode)

    def test_off_screen_placement_values_remain_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            saved = OverlaySettings(anchor_x=-0.25, anchor_y=1.4)
            self.assertTrue(save_settings(saved, path))
            loaded = load_settings(path)
            self.assertEqual(-0.25, loaded.anchor_x)
            self.assertEqual(1.4, loaded.anchor_y)

    def test_position_round_trip_uses_wow_relative_coordinates(self):
        client = (100, 200, 1920, 1080)
        size = (540, 90)
        settings = OverlaySettings(0.31, 0.44)
        position = position_from_settings(client, size, settings)
        restored = settings_from_position(client, size, position)
        self.assertAlmostEqual(settings.anchor_x, restored.anchor_x, places=3)
        self.assertAlmostEqual(settings.anchor_y, restored.anchor_y, places=3)

    def test_moving_the_overlay_keeps_focus_mode(self):
        """Saving a position must not switch a member's settings off.

        The Move window writes whatever this returns over the whole
        settings file. When it built a fresh OverlaySettings from the
        anchors alone, dragging the overlay one pixel and pressing Save
        turned focus mode off - and the companion's toggle went on
        claiming it was on, so the member had no way to see what had
        happened, only that everyone was suddenly audible again.
        """
        client = (100, 200, 1920, 1080)
        size = (540, 90)
        focused = OverlaySettings(
            0.31,
            0.44,
            focus_mode=True,
            caption_hold_seconds=6.0,
            max_visible_speakers=4,
        )
        position = position_from_settings(client, size, focused)
        moved = settings_from_position(client, size, position, current=focused)
        self.assertTrue(moved.focus_mode, "focus mode was silently cleared")
        self.assertEqual(6.0, moved.caption_hold_seconds)
        self.assertEqual(4, moved.max_visible_speakers)
        self.assertAlmostEqual(focused.anchor_x, moved.anchor_x, places=3)
        self.assertAlmostEqual(focused.anchor_y, moved.anchor_y, places=3)

    def test_an_unmeasurable_window_changes_nothing(self):
        """No client rectangle is not a reason to reset the member."""
        focused = OverlaySettings(0.31, 0.44, focus_mode=True)
        kept = settings_from_position(
            (0, 0, 0, 0), (540, 90), (10, 10), current=focused)
        self.assertEqual(focused, kept)

    def test_position_is_not_clamped_to_the_client(self):
        position = position_from_settings(
            (10, 20, 300, 100), (540, 90), OverlaySettings(1.0, 1.0)
        )
        self.assertEqual((40, 75), position)

    def test_position_above_the_client_round_trips_exactly(self):
        client = (0, 0, 1000, 1000)
        size = (300, 100)
        position = (350, -120)
        moved = settings_from_position(client, size, position)
        self.assertLess(moved.anchor_y, 0.0)
        self.assertEqual(
            position,
            position_from_settings(client, size, moved),
        )

    def test_more_speaker_rows_grow_upward_from_one_bottom_edge(self):
        client = (0, 0, 1920, 1080)
        settings = OverlaySettings(anchor_x=0.5, anchor_y=0.6)
        reference_height = DEFAULT_METRICS.tallest_stack(2)
        bottoms = []
        for rows in range(1, 5):
            height = DEFAULT_METRICS.tallest_stack(rows)
            _x, y = position_from_settings(
                client,
                (540, height),
                settings,
                bottom_reference_height=reference_height,
            )
            bottoms.append(y + height)
        self.assertEqual([bottoms[0]] * 4, bottoms)

    def test_bottom_anchored_position_round_trips(self):
        client = (100, 200, 1920, 1080)
        reference_height = DEFAULT_METRICS.tallest_stack(2)
        size = (540, DEFAULT_METRICS.tallest_stack(4))
        settings = OverlaySettings(
            anchor_x=0.31,
            anchor_y=0.54,
            caption_hold_seconds=6.0,
            max_visible_speakers=4,
        )
        position = position_from_settings(
            client,
            size,
            settings,
            bottom_reference_height=reference_height,
        )
        restored = settings_from_position(
            client,
            size,
            position,
            current=settings,
            bottom_reference_height=reference_height,
        )
        self.assertAlmostEqual(settings.anchor_x, restored.anchor_x, places=3)
        self.assertAlmostEqual(settings.anchor_y, restored.anchor_y, places=3)
        self.assertEqual(6.0, restored.caption_hold_seconds)
        self.assertEqual(4, restored.max_visible_speakers)


class CaptionAnchorSettingTests(unittest.TestCase):
    def test_moving_a_caption_keeps_its_selected_anchor(self):
        current = OverlaySettings(
            anchor_x=0.3,
            anchor_y=0.6,
            focus_mode=True,
            anchor_mode="monitor",
            anchor_monitor=2,
        )
        moved = settings_from_position(
            (0, 0, 1920, 1080),
            (540, 90),
            (300, 600),
            current=current,
        )
        self.assertEqual("monitor", moved.anchor_mode)
        self.assertEqual(2, moved.anchor_monitor)
        self.assertTrue(moved.focus_mode)


class HotkeyParsingTests(unittest.TestCase):
    """What the member is allowed to bind, and what Windows is handed."""

    def test_a_normal_combination_becomes_modifiers_and_a_key(self):
        modifiers, key = parse_hotkey("Ctrl+Alt+V")
        self.assertTrue(modifiers & MOD_CONTROL)
        self.assertTrue(modifiers & MOD_ALT)
        self.assertEqual(ord("V"), key)

    def test_repeat_is_always_suppressed(self):
        """A held key would otherwise fire dozens of times a second and
        reopen the box under the member's own typing."""
        for spec in ("Ctrl+Alt+V", "Shift+F9", "Alt+1"):
            modifiers, _key = parse_hotkey(spec)
            self.assertTrue(modifiers & MOD_NOREPEAT, spec)

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(parse_hotkey("Ctrl+Alt+V"), parse_hotkey(" ctrl + ALT + v "))

    def test_canonical_form_normalizes_aliases_order_case_and_spacing(self):
        self.assertEqual(
            "Ctrl+Alt+Shift+Win+V",
            canonicalize_hotkey(" win + SHIFT + alt + Control + v "),
        )
        self.assertEqual("Ctrl+F9", canonicalize_hotkey("control+f09"))

    def test_function_keys_map_to_their_virtual_codes(self):
        self.assertEqual(0x70, parse_hotkey("Ctrl+F1")[1])
        self.assertEqual(0x87, parse_hotkey("Ctrl+F24")[1])
        self.assertIsNone(parse_hotkey("Ctrl+F12"))
        self.assertIsNone(parse_hotkey("Ctrl+F25"))
        self.assertIsNone(parse_hotkey("Ctrl+F0"))

    def test_every_non_reserved_function_key_is_available(self):
        for number in range(1, 25):
            spec = f"Shift+F{number}"
            if number == 12:
                self.assertIsNone(parse_hotkey(spec), spec)
            else:
                self.assertIsNotNone(parse_hotkey(spec), spec)

    def test_shift_only_printable_keys_are_refused(self):
        for spec in ("Shift+V", "Shift+1"):
            self.assertIsNone(parse_hotkey(spec), spec)

    def test_printable_keys_accept_non_shift_global_modifiers(self):
        for spec in ("Ctrl+A", "Alt+Z", "Win+0", "Ctrl+Shift+9"):
            self.assertIsNotNone(parse_hotkey(spec), spec)

    def test_a_bare_key_is_refused(self):
        """Registering an unmodified key steals it from every application
        on the desktop, starting with the game being played."""
        for spec in ("V", "F9", "1"):
            self.assertIsNone(parse_hotkey(spec), spec)

    def test_nonsense_is_refused_rather_than_guessed(self):
        for spec in ("", "   ", "Ctrl", "Ctrl+", "Ctrl+A+B", "Ctrl+Space",
                     "Ctrl+Escape", None, 42, "+++"):
            self.assertIsNone(parse_hotkey(spec), repr(spec))

    def test_every_modifier_is_understood(self):
        self.assertTrue(parse_hotkey("Win+V")[0] & MOD_WIN)
        self.assertTrue(parse_hotkey("Shift+F9")[0] & MOD_SHIFT)
        self.assertTrue(parse_hotkey("Control+V")[0] & MOD_CONTROL)

    def test_invalid_shortcuts_have_no_canonical_form(self):
        for spec in ("Shift+V", "Ctrl+F12", "Ctrl+Space", None, 42):
            self.assertIsNone(canonicalize_hotkey(spec), repr(spec))

    def test_the_shipped_default_is_actually_registerable(self):
        self.assertIsNotNone(parse_hotkey(DEFAULT_SPEAK_HOTKEY))
        self.assertEqual(DEFAULT_SPEAK_HOTKEY, canonicalize_hotkey(DEFAULT_SPEAK_HOTKEY))





class MuteSettingTests(unittest.TestCase):
    """Mute hides caption text; it is a display preference like focus."""

    def test_mute_round_trips_and_defaults_off(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            self.assertFalse(load_settings(path).muted)
            saved = OverlaySettings(anchor_x=0.4, anchor_y=0.6, muted=True)
            self.assertTrue(save_settings(saved, path))
            self.assertTrue(load_settings(path).muted)

    def test_a_mangled_mute_value_reads_as_unmuted(self):
        """A broken settings file must never silently hide a deaf
        member's only channel."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            for value in ("yes", 1, "true", None, [True]):
                path.write_text(
                    json.dumps({
                        "schema": 1, "anchor_x": 0.5, "anchor_y": 0.5,
                        "muted": value,
                    }),
                    encoding="utf-8",
                )
                self.assertFalse(load_settings(path).muted, repr(value))

    def test_mute_is_independent_of_focus_and_placement(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            saved = OverlaySettings(
                anchor_x=0.3, anchor_y=0.7, focus_mode=True, muted=True,
                caption_hold_seconds=6.0, max_visible_speakers=3,
            )
            self.assertTrue(save_settings(saved, path))
            loaded = load_settings(path)
            self.assertTrue(loaded.muted)
            self.assertTrue(loaded.focus_mode)
            self.assertEqual(3, loaded.max_visible_speakers)

    def test_specific_voice_mutes_round_trip_as_opaque_tokens(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            saved = OverlaySettings(
                muted_speaker_ids=("a" * 24, "b" * 24),
            )
            self.assertTrue(save_settings(saved, path))
            self.assertEqual(saved.muted_speaker_ids, load_settings(path).muted_speaker_ids)

    def test_malformed_specific_voice_mutes_fail_open(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "anchor_x": 0.5,
                        "anchor_y": 0.5,
                        "muted_speaker_ids": [
                            "a" * 24,
                            "a" * 24,
                            "raw-discord-id",
                            "B" * 24,
                            None,
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(("a" * 24,), load_settings(path).muted_speaker_ids)


class FocusModeSettingTests(unittest.TestCase):
    def test_focus_round_trips_and_defaults_off(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            self.assertFalse(load_settings(path).focus_mode)
            saved = OverlaySettings(anchor_x=0.4, anchor_y=0.6, focus_mode=True)
            self.assertTrue(save_settings(saved, path))
            loaded = load_settings(path)
            self.assertTrue(loaded.focus_mode)
            self.assertEqual(0.4, loaded.anchor_x)

    def test_anything_but_literal_true_reads_as_off(self):
        """A mangled settings file must never silently mute people."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            for value in ("yes", 1, "true", None, [True]):
                path.write_text(
                    json.dumps(
                        {
                            "schema": 1,
                            "anchor_x": 0.5,
                            "anchor_y": 0.5,
                            "focus_mode": value,
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertFalse(
                    load_settings(path).focus_mode,
                    "focus_mode=%r must read as off" % (value,),
                )

    def test_bad_placement_resets_placement_but_keeps_focus(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            path.write_text(
                "{"
                '\"schema\": 1, '
                '\"anchor_x\": 1e309, '
                '\"anchor_y\": 0.5, '
                '\"focus_mode\": true, '
                '\"caption_hold_seconds\": 10.0, '
                '\"max_visible_speakers\": 3'
                "}",
                encoding="utf-8",
            )
            loaded = load_settings(path)
            self.assertEqual(0.5, loaded.anchor_x)
            self.assertTrue(
                loaded.focus_mode,
                "a corrupt anchor must not flip a deliberate focus choice",
            )
            self.assertEqual(10.0, loaded.caption_hold_seconds)
            self.assertEqual(3, loaded.max_visible_speakers)


class FocusProviderTests(unittest.TestCase):
    """The watch process learns of toggles by mtime, without a restart."""

    def test_provider_tracks_the_file_live(self):
        from VoiceCaptions.voice_captions import settings_focus_provider

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "overlay-settings.json"
            focused = settings_focus_provider(path)
            self.assertFalse(focused(), "missing file reads as off")

            save_settings(OverlaySettings(focus_mode=True), path)
            os.utime(path, ns=(1_000_000_000, 2_000_000_000))
            self.assertTrue(focused(), "a toggle on disk must be noticed")

            save_settings(OverlaySettings(focus_mode=False), path)
            os.utime(path, ns=(1_000_000_000, 3_000_000_000))
            self.assertFalse(focused(), "and noticed again when it flips back")

            path.unlink()
            self.assertFalse(focused(), "a deleted file reads as off")



class GrowthDirectionTests(unittest.TestCase):
    """Placed in the top half, the feed hangs and grows downward.

    The canvas is sized for the worst-case stack and cards were always
    bottom-anchored inside it, so even dragged fully up the visible feed
    stopped a whole empty canvas short of the top - and letting the
    window go higher would have clipped captions mid-raid. Growth
    direction is decided once, from where the member drops the feed, and
    stored, so the position round trip is exact in both modes.
    """

    CLIENT = (0, 0, 1000, 1000)
    SIZE = (300, 100)

    def test_a_drop_near_the_top_becomes_a_hanging_feed(self):
        moved = settings_from_position(self.CLIENT, self.SIZE, (350, 10))
        self.assertEqual("down", moved.growth)
        # And it lands back exactly where it was dropped.
        self.assertEqual(
            (350, 10), position_from_settings(self.CLIENT, self.SIZE, moved)
        )

    def test_a_drop_near_the_bottom_stays_a_standing_feed(self):
        moved = settings_from_position(self.CLIENT, self.SIZE, (350, 800))
        self.assertEqual("up", moved.growth)
        self.assertEqual(
            (350, 800), position_from_settings(self.CLIENT, self.SIZE, moved)
        )

    def test_a_hanging_feed_can_sit_at_the_very_top(self):
        """The point of the feature: captions as high as the screen allows."""
        moved = settings_from_position(self.CLIENT, self.SIZE, (350, 0))
        self.assertEqual("down", moved.growth)
        _x, y = position_from_settings(self.CLIENT, self.SIZE, moved)
        self.assertEqual(0, y)

    def test_a_hanging_feed_grows_downward_from_a_pinned_top_edge(self):
        """More rows must extend the feed toward the middle of the screen,
        never push its first caption off the top."""
        moved = settings_from_position(
            self.CLIENT, self.SIZE, (350, 40), bottom_reference_height=100
        )
        for taller in (100, 180, 260):
            _x, y = position_from_settings(
                self.CLIENT, (300, taller), moved, bottom_reference_height=100
            )
            self.assertEqual(40, y, "the top edge moved at height %d" % taller)

    def test_a_standing_feed_still_grows_upward_from_its_baseline(self):
        moved = settings_from_position(
            self.CLIENT, self.SIZE, (350, 800), bottom_reference_height=100
        )
        bottoms = []
        for taller in (100, 180, 260):
            _x, y = position_from_settings(
                self.CLIENT, (300, taller), moved, bottom_reference_height=100
            )
            bottoms.append(y + taller)
        self.assertEqual(1, len(set(bottoms)), bottoms)

    def test_placements_that_were_legal_before_keep_their_exact_meaning(self):
        """The standing interpretation wins whenever it is self-consistent,
        so no pre-feature settings file changes behaviour by one pixel."""
        for anchor_y in (0.5, 0.54, 0.63, 0.79, 1.0):
            with self.subTest(anchor_y=anchor_y):
                settings = OverlaySettings(0.5, anchor_y)
                position = position_from_settings(
                    self.CLIENT, self.SIZE, settings,
                    bottom_reference_height=400,
                )
                restored = settings_from_position(
                    self.CLIENT, self.SIZE, position, current=settings,
                    bottom_reference_height=400,
                )
                self.assertEqual("up", restored.growth)
                self.assertEqual(
                    position,
                    position_from_settings(
                        self.CLIENT, self.SIZE, restored,
                        bottom_reference_height=400,
                    ),
                )

    def test_growth_survives_the_file_and_junk_falls_back_to_up(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            hanging = OverlaySettings(0.5, 0.1, growth="down")
            self.assertTrue(save_settings(hanging, path))
            self.assertEqual("down", load_settings(path).growth)

            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["growth"] = "sideways"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual("up", load_settings(path).growth)

            # A file from before the field existed: today's behaviour.
            del payload["growth"]
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual("up", load_settings(path).growth)

if __name__ == "__main__":
    unittest.main(verbosity=2)
