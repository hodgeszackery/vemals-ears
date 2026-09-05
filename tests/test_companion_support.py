# SPDX-License-Identifier: GPL-3.0-only
"""Test loader identity and application state across failing/nested scopes."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests.support import companion as support


class LoaderTests(unittest.TestCase):
    def test_real_loader_is_cached_without_importing_a_test_module(self):
        # A fresh process proves the support import is independent of test
        # discovery and cannot allocate the companion's native job twice.
        source = """import sys
from tests.support.companion import load_companion, MODULE_NAME
app = load_companion()
assert load_companion() is app
assert sys.modules[MODULE_NAME] is app
assert 'tests.test_companion_window' not in sys.modules
assert app.Service.__module__ == MODULE_NAME
"""
        done = subprocess.run([sys.executable, "-c", source], cwd=support.ROOT,
                              capture_output=True, text=True, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_failed_import_restores_registry_and_can_retry(self):
        for existing in ("absent", "blocked", "module"):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                entry = root / "WoW-Companion.pyw"
                entry.write_text("raise RuntimeError('incomplete import')\n", encoding="ascii")
                previous = types.ModuleType(support.MODULE_NAME)
                with mock.patch.object(support, "ROOT", root), \
                     mock.patch.object(support, "_COMPANION_MODULE", None), \
                     mock.patch.dict(sys.modules), mock.patch.object(sys, "path", list(sys.path)):
                    if existing == "module":
                        sys.modules[support.MODULE_NAME] = previous
                    elif existing == "blocked":
                        sys.modules[support.MODULE_NAME] = None
                    else:
                        sys.modules.pop(support.MODULE_NAME, None)
                    with self.assertRaisesRegex(RuntimeError, "incomplete import"):
                        support.load_companion()
                    self.assertIsNone(support._COMPANION_MODULE)
                    if existing == "module":
                        self.assertIs(sys.modules[support.MODULE_NAME], previous)
                    elif existing == "blocked":
                        self.assertIn(support.MODULE_NAME, sys.modules)
                        self.assertIsNone(sys.modules[support.MODULE_NAME])
                    else:
                        self.assertNotIn(support.MODULE_NAME, sys.modules)
                    entry.write_text("from dataclasses import dataclass\n@dataclass\nclass Ready:\n    value: int = 42\n", encoding="ascii")
                    loaded = support.load_companion()
                    self.assertEqual(loaded.Ready().value, 42)
                    self.assertIs(support.load_companion(), loaded)


class ApplicationIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.load_companion()

    def test_all_editions_restore_entire_module_and_environment(self):
        for edition in (self.app.FULL_EDITION, self.app.MEMBER_EDITION, self.app.HOSTED_EDITION):
            with self.subTest(edition=edition):
                # Inspect the actual module, not just the fixture's field
                # list: another configuration global must not escape unnoticed.
                before = vars(self.app).copy()
                environment = dict(os.environ)
                with support.isolated_companion(edition) as app:
                    self.assertIs(app, self.app)
                    self.assertEqual(app.ACTIVE_EDITION, edition)
                    self.assertIsNot(app.set_windows_app_identity, before["set_windows_app_identity"])
                    self.assertIsInstance(app.set_windows_app_identity, mock.Mock)
                    app.set_windows_app_identity.assert_called_once_with(edition == app.MEMBER_EDITION)
                    local = Path(os.environ["LOCALAPPDATA"])
                    program = Path(os.environ["PROGRAMDATA"])
                    self.assertTrue(app.STATE_DIR.is_relative_to(local))
                    self.assertTrue(program.is_dir())
                    self.assertNotEqual(local, program)
                    self.assertTrue(app.save_settings({"minimize_to_taskbar": False}))
                    saved = app.SETTINGS_FILE
                    self.assertTrue(saved.is_file())
                self.assertEqual(vars(self.app), before)
                self.assertEqual(dict(os.environ), environment)
                self.assertFalse(local.exists())
                self.assertFalse(program.exists())
                self.assertFalse(saved.exists())

    def test_nested_failure_restores_outer_settings_then_original_state(self):
        original = vars(self.app).copy()
        environment = dict(os.environ)
        with support.isolated_companion("member") as outer:
            outer.save_settings({"minimize_to_taskbar": True})
            saved = outer.SETTINGS_FILE
            outer_state = vars(outer).copy()
            outer_environment = dict(os.environ)
            with self.assertRaisesRegex(RuntimeError, "test failed"):
                with support.isolated_companion("downtime-hosted") as inner:
                    self.assertNotEqual(inner.SETTINGS_FILE, saved)
                    self.assertFalse(inner.load_settings()["minimize_to_taskbar"])
                    inner.configure_application("member")
                    raise RuntimeError("test failed")
            self.assertEqual(vars(outer), outer_state)
            self.assertEqual(dict(os.environ), outer_environment)
            self.assertTrue(saved.is_file())
            self.assertTrue(outer.load_settings()["minimize_to_taskbar"])
        self.assertEqual(vars(self.app), original)
        self.assertEqual(dict(os.environ), environment)

    def test_failed_configuration_restores_before_yield_and_removes_data(self):
        configured = self.app.configure_application
        directories = []

        def fail(edition):
            configured(edition)
            directories.append(Path(os.environ["LOCALAPPDATA"]))
            raise RuntimeError("setup failed")

        with mock.patch.object(self.app, "configure_application", side_effect=fail):
            before = vars(self.app).copy()
            environment = dict(os.environ)
            with self.assertRaisesRegex(RuntimeError, "setup failed"):
                with support.isolated_companion("member"):
                    self.fail("failed configuration must not yield")
            self.assertEqual(vars(self.app), before)
            self.assertEqual(dict(os.environ), environment)
        self.assertEqual(len(directories), 1)
        self.assertFalse(directories[0].exists())

    def test_member_setup_never_migrates_outer_user_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            outer = Path(folder)
            legacy = outer / "GearHelper/companion-settings.json"
            legacy.parent.mkdir()
            payload = b'{"minimize_to_taskbar":false,"speak_hotkey":false}\n'
            legacy.write_bytes(payload)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": folder, "PROGRAMDATA": folder}):
                with support.isolated_companion("member") as app:
                    self.assertFalse(app.SETTINGS_FILE.exists())
                    self.assertTrue(app.load_settings()["minimize_to_taskbar"])
                self.assertEqual(legacy.read_bytes(), payload)
                self.assertFalse((outer / "VemalsEars").exists())


if __name__ == "__main__":
    unittest.main()
