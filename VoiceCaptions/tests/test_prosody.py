import array
import math
import unittest

from VoiceCaptions.prosody import (
    NORMAL_STYLE,
    SHOUT_STYLE,
    WHISPER_STYLE,
    SpeechStyleDetector,
    speech_level_dbfs,
)


def tone(amplitude: int, *, frames: int = 8) -> bytearray:
    samples = array.array(
        "h",
        (
            int(amplitude * math.sin(2.0 * math.pi * index / 40.0))
            for index in range(320 * frames)
        ),
    )
    return bytearray(samples.tobytes())


class SpeechLevelTests(unittest.TestCase):
    def test_level_tracks_signal_amplitude(self):
        quiet = speech_level_dbfs(tone(1_000))
        normal = speech_level_dbfs(tone(5_000))
        loud = speech_level_dbfs(tone(20_000))

        self.assertIsNotNone(quiet)
        self.assertIsNotNone(normal)
        self.assertIsNotNone(loud)
        self.assertLess(quiet, normal)
        self.assertLess(normal, loud)

    def test_trailing_silence_does_not_dominate_speech_level(self):
        spoken = tone(5_000, frames=8)
        with_silence = spoken + bytearray(320 * 2 * 6)

        self.assertAlmostEqual(
            speech_level_dbfs(spoken),
            speech_level_dbfs(with_silence),
            delta=0.1,
        )

    def test_missing_or_malformed_pcm_has_no_level(self):
        self.assertIsNone(speech_level_dbfs(bytearray()))
        self.assertIsNone(speech_level_dbfs(b"\x00"))
        self.assertIsNone(speech_level_dbfs(bytearray(640)))
        self.assertIsNone(speech_level_dbfs(memoryview(bytearray())[::2]))


class SpeechStyleDetectorTests(unittest.TestCase):
    def setUp(self):
        self.detector = SpeechStyleDetector()

    def _calibrate(self, speaker: str, level: float = -20.0) -> None:
        for variation in (-1.0, 0.5, 0.0):
            self.assertEqual(
                NORMAL_STYLE,
                self.detector.classify_and_observe(speaker, level + variation),
            )

    def test_relative_whisper_and_shout_follow_each_speakers_baseline(self):
        self._calibrate("mira", -20.0)

        self.assertEqual(
            WHISPER_STYLE,
            self.detector.classify_and_observe("mira", -31.0),
        )
        self.assertEqual(
            SHOUT_STYLE,
            self.detector.classify_and_observe("mira", -11.0),
        )
        self.assertEqual(
            NORMAL_STYLE,
            self.detector.classify_and_observe("mira", -20.5),
        )

    def test_speaker_baselines_do_not_leak_into_each_other(self):
        self._calibrate("quiet-mic", -34.0)
        self._calibrate("loud-mic", -12.0)

        self.assertEqual(
            SHOUT_STYLE,
            self.detector.classify_and_observe("quiet-mic", -24.0),
        )
        self.assertEqual(
            WHISPER_STYLE,
            self.detector.classify_and_observe("loud-mic", -23.0),
        )

    def test_uncertain_bootstrap_and_invalid_levels_stay_normal(self):
        self.assertEqual(
            NORMAL_STYLE,
            self.detector.classify_and_observe("new", -22.0),
        )
        self.assertEqual(
            NORMAL_STYLE,
            self.detector.classify_and_observe("new", None),
        )
        self.assertEqual(
            NORMAL_STYLE,
            self.detector.classify_and_observe("new", float("nan")),
        )

    def test_extreme_bootstrap_levels_are_styled_without_poisoning_baseline(self):
        self.assertEqual(
            WHISPER_STYLE,
            self.detector.classify_and_observe("new", -50.0),
        )
        self.assertEqual(
            SHOUT_STYLE,
            self.detector.classify_and_observe("new", -4.0),
        )
        self._calibrate("new", -20.0)
        self.assertEqual(
            WHISPER_STYLE,
            self.detector.classify_and_observe("new", -31.0),
        )

    def test_a_stable_gain_shift_eventually_becomes_the_new_normal(self):
        for style, shifted in (
            (SHOUT_STYLE, (-11.0, -10.5, -11.5, -10.0, -11.0)),
            (WHISPER_STYLE, (-31.0, -30.5, -31.5, -30.0, -31.0)),
        ):
            with self.subTest(style=style):
                detector = SpeechStyleDetector()
                for variation in (-1.0, 0.5, 0.0):
                    detector.classify_and_observe("mira", -20.0 + variation)

                observed = [
                    detector.classify_and_observe("mira", level)
                    for level in shifted
                ]

                self.assertEqual([style] * 4 + [NORMAL_STYLE], observed)
                self.assertEqual(
                    NORMAL_STYLE,
                    detector.classify_and_observe("mira", shifted[-1]),
                )

    def test_one_normal_utterance_cancels_a_possible_gain_shift(self):
        self._calibrate("mira")
        for level in (-11.0, -10.5, -11.5, -10.0):
            self.assertEqual(
                SHOUT_STYLE,
                self.detector.classify_and_observe("mira", level),
            )

        self.assertEqual(
            NORMAL_STYLE,
            self.detector.classify_and_observe("mira", -20.0),
        )
        for level in (-11.0, -10.5, -11.5, -10.0):
            self.assertEqual(
                SHOUT_STYLE,
                self.detector.classify_and_observe("mira", level),
            )

    def test_retain_forgets_speakers_outside_the_current_roster(self):
        self._calibrate("mira")
        self._calibrate("drex")

        self.detector.retain(("drex",))

        self.assertNotIn("mira", self.detector._speakers)
        self.assertIn("drex", self.detector._speakers)

    def test_forget_restarts_calibration(self):
        self._calibrate("mira")
        self.assertEqual(
            SHOUT_STYLE,
            self.detector.classify_and_observe("mira", -10.0),
        )
        self.detector.forget("mira")
        self.assertEqual(
            NORMAL_STYLE,
            self.detector.classify_and_observe("mira", -10.0),
        )


if __name__ == "__main__":
    unittest.main()
