"""Hosting/RELAY-IMPORT.json has to describe the files it names.

The record says which upstream revision the relay came from and what each
imported file hashes to here. Nothing used to check it, so an edit to the
relay could leave the record describing a file that no longer exists in
that form. Changing a listed file now means updating its imported_sha256
(LF line endings) and, for a behaviour change, the adaptations list.
"""

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "Hosting" / "RELAY-IMPORT.json"


class RelayImportRecordTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads(RECORD.read_text(encoding="utf-8"))

    def test_imported_hashes_match_the_lf_normalized_files(self):
        self.assertEqual("LF line endings", self.record["hash_normalization"])
        self.assertGreaterEqual(len(self.record["files"]), 6)
        for entry in self.record["files"]:
            with self.subTest(path=entry["path"]):
                content = (ROOT / entry["path"]).read_bytes()
                normalized = content.replace(b"\r\n", b"\n")
                self.assertEqual(
                    entry["imported_sha256"],
                    hashlib.sha256(normalized).hexdigest(),
                )
                self.assertRegex(entry["upstream_sha256"], r"^[0-9a-f]{64}$")

    def test_the_host_transfer_adaptation_is_recorded(self):
        self.assertTrue(
            any(
                "/v1/transfer-offer" in adaptation
                for adaptation in self.record["adaptations"]
            )
        )


if __name__ == "__main__":
    unittest.main()
