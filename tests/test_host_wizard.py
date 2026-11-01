"""Host setup retries preserve credentials and keep Tk work on its UI thread."""

import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest import mock

from Hosting import wizard


class FakeController:
    def __init__(self, configured=False, fail_prepare=False):
        self.calls = []
        self.threads = []
        self.fail_prepare = fail_prepare
        self.settings = {
            "configured": configured, "hostname": "ears.example.org",
            "guild_name": "Fixture guild", "guild_id": "123456789012345678",
            "application_id": "234567890123456789", "port": 8770,
            "client_download_url": "https://example.org/member.exe",
            "client_installer": "", "tunnel_enabled": True,
            "bot_install_url": "https://discord.com/oauth2/authorize?client_id=234567890123456789",
            "join_url": "https://ears.example.org/join",
        }

    def record(self, name):
        self.calls.append(name)
        self.threads.append(threading.get_ident())

    def read_settings(self):
        self.record("read_settings")
        return dict(self.settings)

    def configure(self, settings):
        self.record("configure")
        self.settings.update({key: value for key, value in settings.items()
                              if key not in {"bot_token", "tunnel_token"}})
        self.settings["configured"] = True

    def prepare(self, progress):
        self.record("prepare")
        progress("Downloading the speech model...")
        if self.fail_prepare:
            raise RuntimeError("A download failed for XENO_private_bot_token and XENO_private_tunnel_token.")

    def install_services(self):
        self.record("install_services")

    def status(self):
        self.record("status")
        return {"configured": True, "health": True, "relay": "Running",
                "worker": "Running", "tunnel": "Running", "detail": "Local relay is ready."}

    def start_services(self):
        self.record("start_services")

    def stop_services(self):
        self.record("stop_services")


class WizardWorkflowTests(unittest.TestCase):
    def test_retry_reuses_existing_configuration_after_a_download_failure(self):
        backend = FakeController(fail_prepare=True)
        configured = []
        with self.assertRaises(RuntimeError):
            wizard.prepare_host(backend, {"bot_token": "XENO_private_bot_token"},
                                lambda text: None, configured.append)
        self.assertEqual(1, backend.calls.count("configure"))
        self.assertTrue(configured[0]["configured"])
        self.assertNotIn("bot_token", configured[0])
        backend.fail_prepare = False
        result = wizard.prepare_host(backend, {}, lambda text: None, configured.append)
        self.assertEqual(1, backend.calls.count("configure"))
        self.assertEqual(1, backend.calls.count("install_services"))
        self.assertTrue(result["status"]["health"])

    def test_explicit_edit_saves_settings_but_plain_retry_does_not(self):
        backend = FakeController(configured=True)
        wizard.prepare_host(backend, {"hostname": "new.example.org", "bot_token": ""},
                            lambda text: None, lambda settings: None, configure_existing=True)
        self.assertEqual("new.example.org", backend.settings["hostname"])
        self.assertEqual(1, backend.calls.count("configure"))
        wizard.prepare_host(backend, {}, lambda text: None, lambda settings: None)
        self.assertEqual(1, backend.calls.count("configure"))

    def test_forms_require_private_tokens_only_when_not_already_saved(self):
        settings = FakeController().settings
        self.assertIsNotNone(wizard.validate_server(settings))
        self.assertIsNone(wizard.validate_server(settings, require_token=False))
        self.assertIsNotNone(wizard.validate_connection(settings))
        self.assertIsNone(wizard.validate_connection(settings, require_tunnel_token=False))
        settings["tunnel_enabled"] = False
        self.assertIsNone(wizard.validate_connection(settings))
        settings["client_download_url"] = "http://example.org/member.exe"
        self.assertIsNotNone(wizard.validate_connection(settings))

    def test_member_file_must_exist_and_browser_links_cannot_contain_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            installer = Path(folder) / "Member Setup.exe"
            installer.write_bytes(b"fixture")
            self.assertIsNone(wizard.validate_connection({"client_installer": str(installer)}))
            self.assertIsNotNone(wizard.validate_connection({"client_installer": str(installer) + ".missing"}))
        for url in ("http://example.org", "https://user:secret@example.org", "file:///C:/private", "https://example.org\nprivate"):
            self.assertFalse(wizard.public_url(url))
        self.assertTrue(wizard.public_url("https://example.org/join"))

    def test_preview_controller_never_keeps_secret_values(self):
        backend = wizard.PreviewController()
        backend.configure({"bot_token": "XENO_private_bot_token", "tunnel_token": "XENO_private_tunnel_token"})
        self.assertNotIn("token", repr(backend.read_settings()))

    def test_local_port_is_validated_before_setup(self):
        settings = FakeController().settings
        for port in ("0", "65536", "not a port", ""):
            with self.subTest(port=port):
                self.assertIn("local relay port", wizard.validate_server(settings | {"port": port}, require_token=False))
        for port in ("1", "18770", "65535"):
            self.assertIsNone(wizard.validate_server(settings | {"port": port}, require_token=False))


class WizardUiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest("A Tk display is required: " + str(error))
        self.root.withdraw()
        self.addCleanup(self.close_root)

    def close_root(self):
        try:
            if hasattr(self, "app"):
                self.app.closed = True
                self.root.after_cancel(self.app.poll_id)
            self.root.destroy()
        except tk.TclError:
            pass

    def ready(self):
        deadline = time.monotonic() + 5
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.app.busy, "The mocked backend did not finish")

    def create(self, backend):
        self.backend = backend
        self.opened = []
        self.app = wizard.HostWizard(self.root, backend, open_url=self.opened.append)
        self.ready()

    def test_configured_reopen_shows_status_and_never_prefills_tokens(self):
        self.create(FakeController(configured=True))
        self.assertEqual("dashboard", self.app.page)
        self.assertEqual("", self.app.values["bot_token"].get())
        self.assertEqual("", self.app.values["tunnel_token"].get())
        self.assertNotIn(threading.get_ident(), self.backend.threads)
        self.assertNotIn("configure", self.backend.calls)
        self.app.edit_settings()
        self.assertTrue(self.app.editing)
        self.assertEqual("*", self.app.bot_entry.cget("show"))
        self.app.next_page()
        self.assertEqual("connection", self.app.page)
        self.app.cancel_edit()
        self.assertEqual("dashboard", self.app.page)
        self.assertFalse(self.app.editing)

    def test_fresh_setup_failure_redacts_tokens_and_retry_preserves_configuration(self):
        self.create(FakeController(fail_prepare=True))
        self.app.values["bot_token"].set("XENO_private_bot_token")
        self.app.values["tunnel_token"].set("XENO_private_tunnel_token")
        self.app.values["installer_mode"].set("url")
        self.app.next_page()
        self.assertEqual("connection", self.app.page)
        main_thread = threading.get_ident()
        original_set = tk.StringVar.set

        def ui_only(variable, value):
            self.assertEqual(main_thread, threading.get_ident(), "Tk was called from a backend thread")
            return original_set(variable, value)

        with mock.patch.object(tk.StringVar, "set", ui_only):
            self.app.begin_setup()
            self.ready()
            self.assertTrue(self.app.configured)
            self.assertNotIn("XENO_private", self.app.notice.get())
            self.assertIn("[private value]", self.app.notice.get())
            self.assertEqual("", self.app.values["bot_token"].get())
            self.backend.fail_prepare = False
            self.app.begin_setup()
            self.ready()
        self.assertEqual("dashboard", self.app.page)
        self.assertEqual(1, self.backend.calls.count("configure"))
        self.assertNotIn(main_thread, self.backend.threads)

    def test_dashboard_controls_are_background_only_and_browser_actions_are_explicit(self):
        self.create(FakeController(configured=True))
        self.app.control_services("stop")
        self.ready()
        self.app.control_services("start")
        self.ready()
        self.assertIn("stop_services", self.backend.calls)
        self.assertIn("start_services", self.backend.calls)
        self.assertNotIn(threading.get_ident(), self.backend.threads)
        self.assertEqual([], self.opened)
        self.app._open("https://example.org/join")
        self.assertEqual(["https://example.org/join"], self.opened)

    def test_editing_unused_identity_requires_a_new_token_and_port_reaches_backend(self):
        backend = FakeController(configured=True)
        backend.settings["identity_locked"] = False
        self.create(backend)
        self.app.edit_settings()
        self.app.values["guild_id"].set("345678901234567890")
        self.app.values["port"].set("18770")
        self.app.next_page()
        self.assertEqual("server", self.app.page)
        self.assertIn("bot token", self.app.notice.get())
        self.app.values["bot_token"].set("XENO_new_bot_token")
        self.app.next_page()
        self.assertEqual("connection", self.app.page)
        self.app.begin_setup()
        self.ready()
        self.assertEqual("18770", backend.settings["port"])
        self.assertEqual("345678901234567890", backend.settings["guild_id"])
        self.assertEqual("dashboard", self.app.page)

    def test_compact_window_keeps_navigation_visible_and_scrolls_form(self):
        self.create(FakeController(configured=True))
        self.root.deiconify()
        self.root.geometry("680x560")
        self.app.edit_settings()
        self.app.show_connection()
        self.root.update()
        self.assertLessEqual(self.app.navigation.winfo_y() + self.app.navigation.winfo_height(),
                             self.root.winfo_height())
        self.assertGreater(self.app.body.winfo_reqheight() + 40, self.app.canvas.winfo_height())
        self.app.canvas.yview_moveto(1)
        self.root.update_idletasks()
        self.assertGreater(self.app.canvas.yview()[0], 0)
        for child in self.app.navigation.winfo_children():
            self.assertGreater(child.winfo_width(), 1)
            self.assertLessEqual(child.winfo_x() + child.winfo_width(), self.app.navigation.winfo_width())
        self.app.cancel_edit()
        self.root.update()
        for child in self.app.navigation.winfo_children():
            self.assertGreater(child.winfo_width(), 1)
            self.assertLessEqual(child.winfo_x() + child.winfo_width(), self.app.navigation.winfo_width())


if __name__ == "__main__":
    unittest.main()
