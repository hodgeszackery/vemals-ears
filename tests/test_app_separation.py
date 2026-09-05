"""Regression coverage for two products sharing a source UI implementation."""
import unittest
from unittest import mock

from tests.support.companion import isolated_companion


class SeparateApplicationTests(unittest.TestCase):
    def setUp(self):
        self.app = self.enterContext(isolated_companion())

    def test_both_products_keep_independent_settings_and_ipc(self):
        app = self.app
        app.configure_application(app.FULL_EDITION)
        downtime = (app.LOCK, app.QUIT_SIGNAL, app.PAIR_SIGNAL, app.INSTANCE_MUTEX_NAME)
        self.assertTrue(app.save_settings({"minimize_to_taskbar": False}))
        original = app.SETTINGS_FILE.read_bytes()
        app.configure_application(app.MEMBER_EDITION)
        voice = (app.LOCK, app.QUIT_SIGNAL, app.PAIR_SIGNAL, app.INSTANCE_MUTEX_NAME)
        self.assertTrue(all(left != right for left, right in zip(downtime, voice)))
        self.assertEqual(app.resolve_edition(), app.MEMBER_EDITION)
        self.assertFalse(app.load_settings()["minimize_to_taskbar"])
        self.assertTrue(app.save_settings({"minimize_to_taskbar": True}))
        app.configure_application(app.HOSTED_EDITION)
        self.assertEqual(app.SETTINGS_FILE.read_bytes(), original)
        self.assertEqual(app.INSTANCE_MUTEX_NAME, downtime[-1])
        app.configure_application(app.MEMBER_EDITION)
        self.assertTrue(app.load_settings()["minimize_to_taskbar"])

    def test_each_product_claims_its_own_windows_mutex(self):
        create = mock.Mock(return_value=123)
        kernel = mock.Mock(CreateMutexW=create)
        with mock.patch.object(self.app.ctypes, "WinDLL", return_value=kernel), \
                mock.patch.object(self.app.ctypes, "get_last_error", return_value=0), \
                mock.patch.object(self.app, "INSTANCE_MUTEX_HANDLE", None):
            for edition in (self.app.FULL_EDITION, self.app.MEMBER_EDITION):
                self.app.configure_application(edition)
                self.assertFalse(self.app.claim_instance_mutex())
                self.app.release_instance_mutex()
        self.assertEqual([call.args[2] for call in create.call_args_list],
                         [r"Local\Downtime.Companion", r"Local\VemalsEars.Companion"])

    def test_configure_selects_process_identity_before_window_creation(self):
        for edition, member in ((self.app.FULL_EDITION, False),
                                (self.app.MEMBER_EDITION, True),
                                (self.app.HOSTED_EDITION, False)):
            self.app.configure_application(edition)
            self.app.set_windows_app_identity.assert_called_with(member)

    def test_second_launch_finds_only_its_own_products_tray(self):
        app = self.app
        finder = mock.Mock(return_value=123)
        api = mock.Mock(FindWindowW=finder)
        with mock.patch.object(app.ctypes, "WinDLL", return_value=api):
            for edition in (app.FULL_EDITION, app.MEMBER_EDITION, app.HOSTED_EDITION):
                app.configure_application(edition)
                self.assertEqual(app.companion_tray_window(), 123)
        self.assertEqual([call.args[0] for call in finder.call_args_list],
                         ["DowntimeCompanionTray", "VemalsEarsCompanionTray",
                          "DowntimeCompanionTray"])

    def test_lock_file_recognizes_only_this_products_native_images(self):
        app = self.app
        with mock.patch.object(app.sys, "executable", "C:/runtime/pythonw.exe"), \
                mock.patch.object(app.subprocess, "run") as run:
            for edition, own_image, other_image in (
                (app.MEMBER_EDITION, "VemalsEars.exe", "Downtime.exe"),
                (app.HOSTED_EDITION, "DowntimeLite.exe", "VemalsEars.exe"),
                (app.FULL_EDITION, "Downtime.exe", "VemalsEars.exe"),
            ):
                with self.subTest(edition=edition):
                    app.configure_application(edition)
                    app.STATE_DIR.mkdir(parents=True, exist_ok=True)
                    app.LOCK.write_text("4242", encoding="ascii")
                    run.return_value.stdout = f'"{own_image}","4242","Console","1","123 K"\n'
                    self.assertEqual(app.running_instance_pid(), 4242)
                    run.return_value.stdout = f'"{other_image}","4242","Console","1","123 K"\n'
                    self.assertIsNone(app.running_instance_pid())

    def test_guild_connection_and_protocol_follow_the_app(self):
        with mock.patch("GuildHub.credentials.WindowsDeviceCredentialStore") as voice, \
                mock.patch("GuildHub.credentials.WindowsDowntimeCredentialStore") as jobs, \
                mock.patch("GuildHub.windows_integration.register_current_user_protocol") as register, \
                mock.patch.object(self.app, "installed_launcher", return_value=None), \
                mock.patch.object(self.app, "full_launcher", return_value=None):
            self.app.configure_application(self.app.MEMBER_EDITION)
            self.assertIs(self.app.guild_credential_store(), voice.return_value)
            self.assertTrue(self.app.register_pairing_links(None))
            self.assertEqual(register.call_args.kwargs["app"], "vemalsears")
            self.assertEqual(register.call_args.args[0].name, "Vemals-Ears.pyw")
            self.app.configure_application(self.app.FULL_EDITION)
            self.assertIs(self.app.guild_credential_store(), jobs.return_value)
            self.assertTrue(self.app.register_pairing_links(None))
            self.assertEqual(register.call_args.kwargs["app"], "downtime")
            self.assertEqual(register.call_args.args[0].name, "WoW-Companion.pyw")

    def test_autostart_migration_leaves_the_other_product_alone(self):
        app = self.app
        values = {"Downtime": '"D:/Downtime.exe"', "Vemal\'s Ears": '"V:/VemalsEars.exe"'}
        initial = dict(values)
        with mock.patch.object(app, "read_autostart_value", side_effect=values.get), \
                mock.patch.object(app, "autostart_command", side_effect=lambda member: initial["Vemal\'s Ears" if member else "Downtime"]), \
                mock.patch.object(app, "delete_owned_autostart_value") as delete, \
                mock.patch.object(app, "write_autostart_value") as write:
            self.assertTrue(app.migrate_autostart(False))
            self.assertTrue(app.migrate_autostart(True))
        delete.assert_not_called()
        write.assert_not_called()
        self.assertEqual(values, initial)


if __name__ == "__main__":
    unittest.main()
