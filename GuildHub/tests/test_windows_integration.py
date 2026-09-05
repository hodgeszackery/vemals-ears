import unittest
from unittest import mock

from GuildHub.windows_integration import (
    CLASSES_KEY,
    DOWNTIME_CLASSES_KEY,
    DOWNTIME_DISPLAY_NAME,
    DISPLAY_NAME,
    PAIR_ARGUMENT,
    URL_PREFIX,
    CurrentUserRegistry,
    WindowsIntegrationError,
    build_default_icon,
    build_open_command,
    register_current_user_protocol,
    registration_plan,
)


class FakeRegistry:
    def __init__(self, error=None):
        self.error = error
        self.writes = []

    def set_string(self, key_path, value_name, value):
        if self.error is not None:
            raise self.error
        self.writes.append((key_path, value_name, value))


class FakeRegistryKey:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, _error_type, _error, _traceback):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_WRITE = 0x20006
    REG_SZ = 1

    def __init__(self):
        self.created = []
        self.values = []

    def CreateKeyEx(self, root, path, reserved, access):
        self.created.append((root, path, reserved, access))
        return FakeRegistryKey(path)

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values.append((key.path, name, reserved, kind, value))


class WindowsIntegrationTests(unittest.TestCase):
    def test_apps_register_disjoint_handlers_with_their_own_arguments(self):
        registry = FakeRegistry()
        register_current_user_protocol(r"C:\Apps\VemalsEars.exe", registry=registry)
        ears_writes = list(registry.writes)
        downtime_plan = register_current_user_protocol(
            r"C:\Apps\Downtime.exe", registry=registry, app="downtime",
        )
        self.assertTrue(all(entry.key_path.startswith(DOWNTIME_CLASSES_KEY)
                            for entry in downtime_plan))
        self.assertTrue(all(path.startswith(CLASSES_KEY) for path, _key, _value in ears_writes))
        values = {(entry.key_path, entry.value_name): entry.value for entry in downtime_plan}
        self.assertEqual(DOWNTIME_DISPLAY_NAME, values[(DOWNTIME_CLASSES_KEY, "")])
        self.assertEqual(
            '"C:\\Apps\\Downtime.exe" --downtime-pair "%1"',
            values[(DOWNTIME_CLASSES_KEY + "\\shell\\open\\command", "")],
        )

    def test_unknown_product_cannot_register_arbitrary_scheme(self):
        registry = FakeRegistry()
        with self.assertRaises(WindowsIntegrationError):
            register_current_user_protocol(r"C:\Apps\Downtime.exe", app="https", registry=registry)
        self.assertEqual([], registry.writes)

    def test_downtime_source_handler_quotes_app_and_selects_downtime_argument(self):
        self.assertEqual(
            '"C:\\Python 3\\pythonw.exe" -E -s -B '
            '"C:\\Apps\\Downtime App\\WoW-Companion.pyw" --downtime-pair "%1"',
            build_open_command(
                r"C:\Apps\Downtime App\WoW-Companion.pyw",
                pythonw_path=r"C:\Python 3\pythonw.exe", app="downtime",
            ),
        )

    def test_source_command_uses_pythonw_and_quotes_every_dynamic_argument(self):
        command = build_open_command(
            r"C:\Program Files\Vemals Ears\WoW-Companion.pyw",
            pythonw_path=r"C:\Python 3\pythonw.exe",
        )
        self.assertEqual(
            '"C:\\Python 3\\pythonw.exe" -E -s -B '
            '"C:\\Program Files\\Vemals Ears\\WoW-Companion.pyw" '
            '--vemals-ears-pair "%1"',
            command,
        )

    def test_frozen_command_launches_executable_directly(self):
        command = build_open_command(r"C:\Apps\VemalsEars.exe")
        self.assertEqual(
            '"C:\\Apps\\VemalsEars.exe" --vemals-ears-pair "%1"',
            command,
        )
        self.assertEqual("--vemals-ears-pair", PAIR_ARGUMENT)

    def test_source_command_derives_pythonw_from_current_interpreter(self):
        with mock.patch(
            "GuildHub.windows_integration.sys.executable",
            r"C:\Portable Python\python.exe",
        ):
            command = build_open_command(r"C:\Apps\WoW-Companion.pyw")
        self.assertTrue(command.startswith('"C:\\Portable Python\\pythonw.exe" -E -s -B '))

    def test_command_rejects_relative_or_unsupported_application(self):
        for value in (
            r"WoW-Companion.pyw",
            r"C:\Apps\companion.py",
            'C:\\Apps\\bad"name.exe',
        ):
            with self.subTest(value=value):
                with self.assertRaises(WindowsIntegrationError):
                    build_open_command(value)

    def test_plan_contains_url_protocol_icon_and_open_command(self):
        plan = registration_plan(
            r"C:\Apps\WoW-Companion.pyw",
            pythonw_path=r"C:\Python\pythonw.exe",
            icon_path=r"C:\Apps\companion.ico",
        )
        values = {
            (entry.key_path, entry.value_name): entry.value for entry in plan
        }
        self.assertEqual("vemalsears://", URL_PREFIX)
        self.assertEqual(DISPLAY_NAME, values[(CLASSES_KEY, "")])
        self.assertEqual("", values[(CLASSES_KEY, "URL Protocol")])
        self.assertEqual(
            '"C:\\Apps\\companion.ico",0',
            values[(CLASSES_KEY + "\\DefaultIcon", "")],
        )
        self.assertIn(
            (CLASSES_KEY + "\\shell\\open\\command", ""),
            values,
        )

    def test_registration_uses_injected_current_user_boundary(self):
        registry = FakeRegistry()
        plan = register_current_user_protocol(
            r"C:\Apps\VemalsEars.exe",
            registry=registry,
        )
        self.assertEqual(
            [(entry.key_path, entry.value_name, entry.value) for entry in plan],
            registry.writes,
        )
        self.assertEqual("URL Protocol", registry.writes[-1][1])

    def test_production_adapter_writes_only_under_current_user(self):
        winreg = FakeWinreg()
        registry = CurrentUserRegistry(winreg)
        registry.set_string(CLASSES_KEY, "URL Protocol", "")
        self.assertEqual(
            [(winreg.HKEY_CURRENT_USER, CLASSES_KEY, 0, winreg.KEY_WRITE)],
            winreg.created,
        )
        self.assertEqual(
            [(CLASSES_KEY, "URL Protocol", 0, winreg.REG_SZ, "")],
            winreg.values,
        )

    def test_registry_failure_is_user_friendly_and_redacted(self):
        private = r"C:\Users\Private Person\token-ghe_secret"
        registry = FakeRegistry(OSError(private))
        with self.assertRaises(WindowsIntegrationError) as captured:
            register_current_user_protocol(
                r"C:\Apps\VemalsEars.exe",
                registry=registry,
            )
        rendered = str(captured.exception)
        self.assertEqual(
            "Windows could not register Vemal's Ears pairing links for this account.",
            rendered,
        )
        self.assertNotIn(private, rendered)
        self.assertIsNone(captured.exception.__cause__)

    def test_default_icon_quotes_the_selected_path(self):
        self.assertEqual(
            '"C:\\Apps\\Vemals Ears\\VemalsEars.exe",0',
            build_default_icon(r"C:\Apps\Vemals Ears\VemalsEars.exe"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
