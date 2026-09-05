"""The licence must be declared everywhere a consumer looks for it.

A LICENSE file alone is easy to contradict. CurseForge reads the toc,
npm reads package.json, and players read the listing copy - and before
this existed the GearHelper listing promised "free, open source" while
the repository declared nothing at all, which is the mismatch that
matters: a public claim the project did not actually make.

The sweep over tracked .toc files is the part with teeth. A fifth addon
cannot join the suite without declaring its licence.
"""

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LICENSE = "GPL-3.0-only"


class LicenseDeclarationTests(unittest.TestCase):
    def test_the_repository_carries_the_license_text(self):
        """Shino, 2026-09-02, choosing GPL so Warpsim can be a headline
        feature of 0.5.0 rather than a thing held at arm's length."""
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

    def test_nothing_still_claims_the_old_licence(self):
        """A half-finished relicence is worse than either licence: it makes
        two contradictory public claims about the same code."""
        listed = subprocess.run(
            ["git", "ls-files", "*.toc", "*.json", "*.md", "*.txt", "LICENSE"],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        stale = []
        for relative in [line.strip() for line in listed.stdout.splitlines() if line.strip()]:
            if relative.startswith("Warpsim/") or "package-lock" in relative:
                continue  # upstream text and dependency metadata, not our claim
            path = ROOT / relative
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line in text.splitlines():
                lowered = line.casefold()
                if "mit license" in lowered or "mit licensed" in lowered:
                    if "whisper" in lowered or "silero" in lowered:
                        continue  # third parties keep their own licences
                    stale.append(f"{relative}: {line.strip()}")
        self.assertEqual([], stale, "these still claim MIT after the relicence")


    def test_the_discord_adapter_manifest_declares_it(self):
        manifest = json.loads(
            (ROOT / "VoiceCaptions" / "discord_adapter" / "package.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(LICENSE, manifest.get("license"))



if __name__ == "__main__":
    unittest.main(verbosity=2)
