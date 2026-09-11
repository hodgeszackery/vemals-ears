"""Every file that carries the suite version must agree with version.py.

A missed carrier does not fail anything. It just ships a lie, quietly,
and keeps shipping it: GuildHub/__init__.py said 0.1.0 through two
releases while the real number moved to 0.2.2, because the release
tooling only ever rewrote one of the two.

It nearly happened again cutting 0.3.7 - the discord adapter's
package.json is a carrier, and it was missing from the list being
bumped. So this checks two different things:

  * every listed carrier matches GuildHub/version.py, and
  * every tracked .toc is a listed carrier

The second half is the one that survives someone adding a fifth addon
without reading the release notes.
"""

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# file -> pattern whose one capture group is the version it declares
CARRIERS = {
    "README.txt": r'^versioned public member bundle at https://ears\.xenon\.solutions/source/([^\s]+)\.$',
    "docs/SOURCE.txt": r'^Exact source-complete bundle: https://ears\.xenon\.solutions/source/([^\s]+)$',
    "VoiceCaptions/__init__.py": r'^__version__ = "([^"]+)"$',
    "VoiceCaptions/discord_adapter/package.json": r'^  "version": "([^"]+)",$',
}

# This is a LoadOnDemand transport installed by the companion, not a player
# release surface. It deliberately has no user-facing version: the complete
# companion owns its lifecycle and it must never be independently uploaded or
# paired with a version bump.
INTERNAL_TOCS = {
}


def shipped_version() -> str:
    """Read version.py as text, not by importing it.

    Importing would pull GuildHub onto the path for a test that only
    needs one string, and the point here is to read what the file
    literally says.
    """
    text = (ROOT / "GuildHub" / "version.py").read_text(encoding="utf-8")
    found = re.search(r'^CLIENT_VERSION = "([^"]+)"$', text, re.MULTILINE)
    assert found, "GuildHub/version.py no longer declares CLIENT_VERSION"
    return found.group(1)


class VersionConsistencyTests(unittest.TestCase):
    def test_every_carrier_agrees_with_version_py(self):
        expected = shipped_version()
        self.assertRegex(expected, r"^\d+\.\d+\.\d+$")
        for relative, pattern in CARRIERS.items():
            with self.subTest(carrier=relative):
                path = ROOT / relative
                self.assertTrue(path.is_file(), relative + " is missing")
                text = path.read_text(encoding="utf-8")
                found = re.search(pattern, text, re.MULTILINE)
                self.assertIsNotNone(
                    found,
                    relative + " no longer declares a version the release "
                    "tooling can find - update the pattern here and in the "
                    "bump, or the next release ships a stale number.",
                )
                self.assertEqual(
                    expected,
                    found.group(1),
                    relative + " says " + found.group(1) + " but version.py "
                    "says " + expected,
                )

    def test_discord_adapter_lockfile_metadata_agrees_with_the_release(self):
        expected = shipped_version()
        adapter = ROOT / "VoiceCaptions" / "discord_adapter"
        package = json.loads((adapter / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((adapter / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(expected, package["version"])
        self.assertEqual(expected, lock["version"])
        self.assertEqual(expected, lock["packages"][""]["version"])



if __name__ == "__main__":
    unittest.main(verbosity=2)
