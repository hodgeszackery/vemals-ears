"""VE's applying notice and product declarations must agree.

Third-party notices describe other licenses and must not be mistaken for
a conflicting declaration about VE itself. Artifact tests separately verify
that the license and matching source actually reach recipients.
"""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LICENSE = "GPL-3.0-only"


class LicenseDeclarationTests(unittest.TestCase):
    def test_the_repository_carries_the_license_text(self):
        """The standalone product keeps the original GPLv3 applying grant."""
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", text)
        self.assertIn("Version 3, 29 June 2007", text)
        self.assertIn("Zackery Hodges", text)
        # The applying notice, which is what makes this a licence GRANT
        # rather than a copy of the GPL sitting in a folder.
        self.assertIn("under the terms of the GNU General Public License", text)
        self.assertIn("version 3", text)
        self.assertIn("WITHOUT ANY WARRANTY", text)
        # The two clauses that decided this choice, kept under test because
        # they are the ones a future reader will want to check: a recipient
        # may charge, and nobody may narrow the grant afterwards.
        self.assertIn("You may charge any price or no price", text)
        self.assertIn("may not impose any further restrictions", text)

    def test_product_declarations_agree_with_the_applying_notice(self):
        readme = (ROOT / "README.txt").read_text(encoding="ascii")
        self.assertIn("License: GPL-3.0-only; see LICENSE.", readme)
        notice = (ROOT / "Installer/THIRD-PARTY-NOTICES.txt").read_text(encoding="ascii")
        self.assertIn(
            "Vemal's Ears itself is licensed under the GNU General Public License,\n"
            "version 3 only.", notice,
        )
        grant = (ROOT / "LICENSE").read_text(encoding="ascii").split("------", 1)[0]
        self.assertIn("Vemal's Ears", grant)
        self.assertIn("under the terms of the GNU General Public License", grant)
        self.assertNotIn("MIT License", grant)

    def test_embedded_aom_and_libyuv_notices_point_to_bundled_terms(self):
        lock = json.loads(
            (ROOT / "Installer/runtime-sources.lock.json").read_text(encoding="ascii")
        )
        artifacts = {item["name"]: item for item in lock["artifacts"]}
        notice = (ROOT / "Installer/THIRD-PARTY-NOTICES.txt").read_text(encoding="ascii")
        expected = {
            "pillow-aom": "AOMedia libaom 3.14.1",
            "pillow-libyuv": "libyuv commit 644251f252a84bf8ce91ff0aca86a9b16b069ab8",
        }
        for name, label in expected.items():
            with self.subTest(component=name):
                item = artifacts[name]
                self.assertIn(label, notice)
                self.assertIn("runtime/Source/" + item["filename"], notice)
                self.assertEqual(2, len(item["license_files"]))
                for path in item["license_files"]:
                    self.assertIn(path, notice)


    def test_the_discord_adapter_manifest_declares_it(self):
        manifest = json.loads(
            (ROOT / "VoiceCaptions" / "discord_adapter" / "package.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(LICENSE, manifest.get("license"))



if __name__ == "__main__":
    unittest.main(verbosity=2)
