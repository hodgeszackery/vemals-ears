import unittest

from VoiceCaptions.stt.contracts import AudioSegment, SAMPLE_RATE
from VoiceCaptions.stt.vocabulary import build_wow_prompt, normalize_terms


def pcm(milliseconds: int = 20, fill: int = 7) -> bytearray:
    return bytearray([fill] * (SAMPLE_RATE * 2 * milliseconds // 1000))


class AudioSegmentTests(unittest.TestCase):
    def test_requires_owned_mutable_pcm(self):
        with self.assertRaises(TypeError):
            AudioSegment("mira", 1, bytes(640))  # type: ignore[arg-type]

    def test_wipe_overwrites_and_releases_owned_buffer(self):
        segment = AudioSegment("mira", 1, pcm())
        self.assertGreater(len(segment.pcm), 0)
        segment.wipe()
        self.assertTrue(segment.wiped)
        self.assertEqual(bytearray(), segment.pcm)
        segment.wipe()

    def test_audio_format_and_identity_are_validated(self):
        with self.assertRaises(ValueError):
            AudioSegment("bad id", 1, pcm())
        with self.assertRaises(ValueError):
            AudioSegment("mira", 1, pcm(), sample_rate=48_000)
        with self.assertRaises(ValueError):
            AudioSegment("mira", 1, bytearray(3))


class VocabularyTests(unittest.TestCase):
    def test_prompt_contains_wow_terms_and_sanitized_player_hints(self):
        prompt = build_wow_prompt(("Mira\r\nDROP", "Bloodlust", "Drex, Tank"))
        self.assertIn("World of Warcraft", prompt)
        self.assertIn("Mira DROP", prompt)
        self.assertIn("Drex Tank", prompt)
        self.assertEqual(1, prompt.count("Bloodlust"))
        self.assertNotIn("\n", prompt)

    def test_terms_are_bounded_and_deduplicated(self):
        terms = normalize_terms(["Soak", "soak", "x" * 100] * 100)
        self.assertEqual(("Soak", "x" * 40), terms)


if __name__ == "__main__":
    unittest.main(verbosity=2)
