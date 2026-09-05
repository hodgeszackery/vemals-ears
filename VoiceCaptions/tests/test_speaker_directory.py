import json
import tempfile
import unittest
from pathlib import Path

from VoiceCaptions.speaker_directory import (
    KnownSpeaker,
    load_known_speakers,
    remember_speaker,
    save_known_speakers,
)


class SpeakerDirectoryTests(unittest.TestCase):
    def test_only_opaque_id_and_visible_name_are_stored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "speakers.json"
            speaker = KnownSpeaker("a" * 24, "Mira")
            self.assertTrue(save_known_speakers([speaker], path))
            self.assertEqual((speaker,), load_known_speakers(path))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({"schema", "speakers"}, set(payload))
            self.assertEqual({"id", "name"}, set(payload["speakers"][0]))
            self.assertNotIn("caption", path.read_text(encoding="utf-8").lower())

    def test_a_known_name_is_not_rewritten_for_every_caption(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "speakers.json"
            self.assertTrue(remember_speaker("b" * 24, "Drex", path))
            first = path.stat().st_mtime_ns
            self.assertTrue(remember_speaker("b" * 24, "Drex", path))
            self.assertEqual(first, path.stat().st_mtime_ns)

    def test_changed_name_replaces_the_old_local_label(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "speakers.json"
            self.assertTrue(remember_speaker("c" * 24, "Old name", path))
            self.assertTrue(remember_speaker("c" * 24, "New name", path))
            self.assertEqual(
                (KnownSpeaker("c" * 24, "New name"),),
                load_known_speakers(path),
            )

    def test_raw_ids_and_control_characters_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "speakers.json"
            self.assertFalse(remember_speaker("123456789012345678", "Mira", path))
            self.assertFalse(remember_speaker("d" * 24, "Bad\nName", path))
            self.assertEqual((), load_known_speakers(path))


if __name__ == "__main__":
    unittest.main()
