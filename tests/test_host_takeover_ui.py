"""Handoff UI separates inspection, explicit cutover and replacement startup."""

from pathlib import Path
import tempfile
import threading
import unittest

from Hosting import wizard
from tests.test_host_wizard import FakeController, WizardUiTests


class FakeLegacy:
    def __init__(self):
        self.calls = []

    def inspect(self):
        self.calls.append(("inspect", threading.get_ident()))
        return {"available": True, "guild_name": "Fixture guild",
                "public_http_url": "https://ears.example.org", "handoff_pending": False}

    def export(self, path, password, progress):
        self.calls.append(("export", threading.get_ident()))
        return {"bundle": path, "guild_name": "Fixture guild",
                "public_http_url": "https://ears.example.org"}

    def cancel_unused(self, **options):
        if options != {"replacement_never_started": True}:
            raise AssertionError("Missing explicit unused confirmation")
        self.calls.append(("cancel", threading.get_ident()))
        return self.inspect()


class FakeTakeover:
    def __init__(self, host):
        self.host = host
        self.legacy = FakeLegacy()
        self.calls = []
        self.pending = None

    def preview(self, path, password):
        self.calls.append("preview")
        self.pending = True
        return {"guild_name": "Fixture guild", "guild_id": "123456789012345678",
                "application_id": "234567890123456789", "public_http_url": "https://ears.example.org"}

    def import_pending(self, *, original_stopped):
        assert original_stopped is True and self.pending is True
        self.calls.append("import")
        self.pending = None
        self.host.settings.update({"configured": True, "mode": "remote-worker",
                                   "takeover_confirmed": True, "port": None,
                                   "tunnel_enabled": False, "identity_locked": True})
        return self.host.read_settings()

    def confirm_start(self, *, original_stopped):
        assert original_stopped is True
        self.calls.append("confirm")
        self.host.settings["takeover_confirmed"] = True


class TakeoverUiTests(unittest.TestCase):
    setUp = WizardUiTests.setUp
    close_root = WizardUiTests.close_root
    ready = WizardUiTests.ready

    def create_takeover(self, *, configured=False, fail_prepare=False):
        self.backend = FakeController(configured=configured, fail_prepare=fail_prepare)
        self.transfer = FakeTakeover(self.backend)
        self.opened = []
        self.app = wizard.HostWizard(self.root, self.backend, open_url=self.opened.append,
                                    takeover_backend=self.transfer)
        self.ready()
        return self.app.takeover_ui

    def test_initial_choices_do_not_inspect_or_stop_the_existing_worker(self):
        ui = self.create_takeover()
        self.assertEqual("choose-host", self.app.page)
        self.assertEqual([], self.transfer.legacy.calls)
        ui.show_source()
        self.ready()
        self.assertEqual("takeover-export", self.app.page)
        self.assertEqual(["inspect"], [call[0] for call in self.transfer.legacy.calls])
        self.assertNotEqual(threading.get_ident(), self.transfer.legacy.calls[0][1])

    def test_export_requires_explicit_stop_and_does_not_reveal_passphrase(self):
        ui = self.create_takeover()
        with tempfile.TemporaryDirectory() as directory:
            self.app.values["handoff_file"].set(str(Path(directory).resolve() / "handoff.vehost"))
            self.app.values["handoff_password"].set("XENO_private_handoff_passphrase")
            self.app.values["handoff_password_confirm"].set("XENO_private_handoff_passphrase")
            ui.export()
            self.assertEqual([], self.transfer.legacy.calls)
            ui.stop_for_export.set(True)
            ui.export()
            self.ready()
        self.assertEqual(["export"], [call[0] for call in self.transfer.legacy.calls])
        self.assertEqual("", self.app.values["handoff_password"].get())
        self.assertEqual("", self.app.values["handoff_password_confirm"].get())
        self.assertNotIn("XENO_private", self.app.notice.get())
        self.assertEqual("takeover-export", self.app.page)

    def test_open_only_previews_and_start_requires_original_stopped_confirmation(self):
        ui = self.create_takeover()
        ui.show_import()
        self.app.values["handoff_file"].set("XENO_fixture.vehost")
        self.app.values["handoff_password"].set("XENO_private_passphrase")
        ui.open_import()
        self.ready()
        self.assertEqual("takeover-confirm", self.app.page)
        self.assertEqual(["preview"], self.transfer.calls)
        self.assertNotIn("install_services", self.backend.calls)
        self.assertEqual("", self.app.values["handoff_password"].get())
        ui.begin_import()
        self.assertEqual(["preview"], self.transfer.calls)
        ui.original_stopped.set(True)
        ui.begin_import()
        self.ready()
        self.assertEqual(["preview", "import"], self.transfer.calls)
        self.assertEqual(1, self.backend.calls.count("install_services"))
        self.assertEqual("dashboard", self.app.page)

    def test_download_retry_uses_imported_identity_without_importing_again(self):
        ui = self.create_takeover(fail_prepare=True)
        self.transfer.pending = True
        ui.original_stopped.set(True)
        ui.begin_import()
        self.ready()
        self.assertTrue(self.app.configured)
        self.assertNotIn("install_services", self.backend.calls)
        self.backend.fail_prepare = False
        self.app.begin_setup()
        self.ready()
        self.assertEqual(["import"], self.transfer.calls)
        self.assertNotIn("configure", self.backend.calls)
        self.assertEqual(1, self.backend.calls.count("install_services"))

    def test_remote_identity_edit_and_unconfirmed_start_are_blocked(self):
        ui = self.create_takeover(configured=True)
        self.app.settings.update({"mode": "remote-worker", "takeover_confirmed": False})
        self.app.edit_settings()
        self.assertEqual("dashboard", self.app.page)
        self.app.control_services("start")
        self.assertEqual("takeover-activate", self.app.page)
        ui.activate()
        self.assertNotIn("start_services", self.backend.calls)
        self.assertNotIn("install_services", self.backend.calls)

    def test_cancel_unused_confirmation_is_reset_each_time_screen_is_shown(self):
        ui = self.create_takeover()
        ui.source_ready({"available": True, "handoff_pending": True})
        ui.cancel_unused()
        self.assertEqual([], self.transfer.legacy.calls)
        ui.never_started.set(True)
        ui.source_ready({"available": True, "handoff_pending": True})
        self.assertFalse(ui.never_started.get())
        ui.never_started.set(True)
        ui.cancel_unused()
        self.ready()
        self.assertEqual("cancel", self.transfer.legacy.calls[0][0])

    def test_handoff_passphrase_never_enters_fresh_host_settings(self):
        self.create_takeover()
        self.app.values["handoff_password"].set("XENO_private_passphrase")
        self.assertFalse(any(key.startswith("handoff_") for key in self.app.collect_settings()))


del WizardUiTests


if __name__ == "__main__":
    unittest.main()
