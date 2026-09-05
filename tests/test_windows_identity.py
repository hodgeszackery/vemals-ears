import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_release  # noqa: E402


class WindowsIdentityTests(unittest.TestCase):
    def test_native_launcher_hosts_the_python_window_in_downtime(self):
        source = (ROOT / "Windows" / "DowntimeLauncher.c").read_text(encoding="utf-8")
        self.assertIn('L"Downtime.Companion"', source)
        self.assertIn('GetProcAddress(python_module, "Py_Main")', source)
        self.assertIn("exit_code = python_main(python_argc, python_argv)", source)
        self.assertNotIn("CreateProcessW(", source)
        self.assertIn('L"--vemals-ears-pair"', source)
        self.assertIn("pairing_arguments_are_safe(launcher_argc, launcher_argv)", source)
        self.assertIn("WoW-Companion.pyw", source)
        self.assertIn("WindowedExecutablePath", source)
        self.assertIn('L"Software\\\\Python"', source)
        self.assertIn("parsed_minor < 10", source)
        self.assertIn("KEY_WOW64_64KEY", source)
        self.assertIn("HKEY_CURRENT_USER", source)

    def test_native_launcher_does_not_capture_external_descendants(self):
        source = (ROOT / "Windows" / "DowntimeLauncher.c").read_text(encoding="utf-8")
        self.assertNotIn("CreateJobObjectW", source)
        self.assertNotIn("AssignProcessToJobObject", source)
        self.assertNotIn("JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", source)



    def test_each_product_has_its_own_native_entrypoint_and_identity(self):
        host = (ROOT / "Windows" / "DowntimeLauncher.c").read_text(encoding="ascii")
        member = host[host.index("#ifdef VEMALS_EARS"):host.index("#elif defined(DOWNTIME_LITE)")]
        self.assertIn('APP_MODEL_ID L"VemalsEars.Companion"', member)
        resource = (ROOT / "Installer" / "launcher.rc").read_text(encoding="ascii")
        self.assertIn('VALUE "FileDescription", "Vemal\'s Ears\\0"', resource)
        self.assertIn('APP_PAIRING_PREFIX L"vemalsears://pair?"', member)
        self.assertIn("APP_REQUIRES_BUNDLED_RUNTIME 1", member)
        self.assertIn('app\\\\Vemals-Ears.pyw', member)
        downtime = host[host.index("#elif defined(DOWNTIME_LITE)"):host.index("#endif")]
        self.assertIn('APP_PAIRING_PREFIX L"downtime://pair?"', downtime)
        self.assertIn('APP_PAIRING_ARGUMENT L"--downtime-pair"', downtime)
        self.assertIn("#ifndef VEMALS_EARS", host)
        self.assertIn('wcscmp(argv[1], L"--vemals-ears-pair")', host)
        smoke = (ROOT / "Windows" / "verify_native_products.ps1").read_text(encoding="ascii")
        self.assertIn("$result.host_pid -ne $process.Id", smoke)
        self.assertIn("$result.window_pid -ne $process.Id", smoke)
        self.assertIn('$modes += "legacy"', smoke)


    def test_vemals_ears_has_a_distinct_multisize_native_icon(self):
        icon = (ROOT / "Windows" / "VemalsEars.ico").read_bytes()
        self.assertEqual(b"\x00\x00\x01\x00", icon[:4])
        self.assertGreater(int.from_bytes(icon[4:6], "little"), 1)
        resource = (ROOT / "Installer" / "launcher.rc").read_text(encoding="ascii")
        self.assertIn('1 ICON "VemalsEars.ico"', resource)

    def test_native_smoke_checks_taskbar_relaunch_without_python_or_pairing_args(self):
        smoke = (ROOT / "Windows" / "verify_native_products.ps1").read_text(encoding="ascii")
        self.assertIn('$result.taskbar.RelaunchCommand -cne', smoke)
        self.assertIn('$result.taskbar.ID -cne $product.AppId', smoke)
        self.assertIn('$result.taskbar.RelaunchIconResource -cne', smoke)
        self.assertIn('$result.taskbar.PreventPinning', smoke)




    def test_an_over_long_registry_tag_cannot_kill_the_launcher(self):
        """wcscpy_s does NOT return an error when the destination is too
        small - the secure CRT calls the invalid parameter handler, and with
        none installed the UCRT terminates the process through __fastfail.
        Verified on the real toolchain: without a handler the probe died at
        0xC0000409 before its next statement; with one, wcscpy_s returned 34.

        So the launcher's `if (wcscpy_s(...) != 0)` was unreachable dead
        code. A registry key name may be 255 characters and HKCU needs no
        administrator rights, so any program on the machine could leave a key
        that made Downtime refuse to start with no window, no dialog and no
        exit code - the worst failure this app has, since the companion is
        the only channel some users have to read their raid.
        """
        source = (ROOT / "Windows" / "DowntimeLauncher.c").read_text(encoding="utf-8")
        # The length is checked BEFORE the copy, not by its return value.
        self.assertIn("if (wcslen(tag_name) >= ARRAY_LENGTH(version)) {", source)
        guard = source.index("if (wcslen(tag_name) >= ARRAY_LENGTH(version)) {")
        copy = source.index("wcscpy_s(version, ARRAY_LENGTH(version), tag_name)")
        self.assertLess(guard, copy, "the length guard must precede the copy")

    def test_a_bad_crt_argument_is_reported_rather_than_fatal(self):
        """Defence in depth for every other *_s call in the file: each one
        already checks its return value, and an installed handler is what
        makes those checks real instead of decorative."""
        source = (ROOT / "Windows" / "DowntimeLauncher.c").read_text(encoding="utf-8")
        self.assertIn("_set_invalid_parameter_handler(quiet_invalid_parameter)", source)
        self.assertIn("static void __cdecl quiet_invalid_parameter(", source)
        # uintptr_t is in the handler signature; without this include the
        # /W4 /WX build fails rather than silently misbehaving.
        self.assertIn("#include <stdint.h>", source)
        installed = source.index("_set_invalid_parameter_handler(quiet_invalid_parameter)")
        first_use = source.index("SetCurrentProcessExplicitAppUserModelID")
        self.assertLess(
            installed, first_use, "install the handler before any work runs"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
