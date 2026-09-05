"""The worker cutover preflight must validate source without changing state."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'GuildHub' / 'Install-VemalsEars-Worker.ps1'
POWERSHELL = shutil.which('powershell') or shutil.which('pwsh')


@unittest.skipUnless(os.name == 'nt' and POWERSHELL, 'Windows PowerShell required')
class WorkerInstallationPreflightTests(unittest.TestCase):
    def run_preflight(self, script, state, profile):
        return subprocess.run(
            [POWERSHELL, '-NoProfile', '-File', str(script), '-CheckOnly',
             '-PythonPath', sys.executable, '-NssmPath', str(state / 'no-nssm.exe'),
             '-StateDirectory', str(state), '-UserLocalAppData', str(profile)],
            cwd=script.parent, capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )

    def test_complete_source_does_not_create_operator_state_or_require_nssm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, profile = root / 'state', root / 'profile'
            result = self.run_preflight(SCRIPT, state, profile)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn('source and dependencies verified', result.stdout)
            self.assertFalse(state.exists())
            self.assertFalse(profile.exists())

    def test_incomplete_checkout_fails_before_creating_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'GuildHub' / SCRIPT.name
            script.parent.mkdir()
            shutil.copyfile(SCRIPT, script)
            state, profile = root / 'state', root / 'profile'
            result = self.run_preflight(script, state, profile)
            self.assertNotEqual(0, result.returncode)
            self.assertIn('Incomplete VE checkout', result.stdout + result.stderr)
            self.assertFalse(state.exists())
            self.assertFalse(profile.exists())


if __name__ == '__main__':
    unittest.main()
