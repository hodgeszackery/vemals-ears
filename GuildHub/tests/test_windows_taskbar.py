import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from GuildHub import windows_taskbar as taskbar


class TaskbarPlanTests(unittest.TestCase):
    def test_native_products_have_independent_relaunch_targets(self):
        for member, executable, app_id, label in (
            (True, "VemalsEars.exe", "VemalsEars.Companion", "Vemal's Ears"),
            (False, "Downtime.exe", "Downtime.Companion", "Downtime"),
            (False, "DowntimeLite.exe", "Downtime.Companion", "Downtime Lite"),
        ):
            with self.subTest(executable=executable):
                path = "C:\\Program Files Test\\" + executable
                plan = dict(taskbar.taskbar_properties(member=member, launcher=path))
                self.assertEqual(app_id, plan["System.AppUserModel.ID"])
                self.assertEqual('"' + path + '"', plan["System.AppUserModel.RelaunchCommand"])
                self.assertEqual(label, plan["System.AppUserModel.RelaunchDisplayNameResource"])
                self.assertEqual(path + ",0", plan["System.AppUserModel.RelaunchIconResource"])
                self.assertIs(False, plan["System.AppUserModel.PreventPinning"])

    def test_private_python_and_cross_product_paths_are_never_pinnable(self):
        for member, path in (
            (True, "C:/VE/runtime/pythonw.exe"),
            (False, "C:/Downtime/runtime/python.exe"),
            (True, "C:/Downtime/Downtime.exe"),
            (False, "C:/VE/VemalsEars.exe"),
            (True, "VemalsEars.exe"),
            (True, 'C:/VE/"/VemalsEars.exe'),
            (True, "C:/VE/\n/VemalsEars.exe"),
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                taskbar.taskbar_properties(member=member, launcher=path)

    def test_source_sessions_prevent_pinning_before_setting_identity(self):
        plan = taskbar.taskbar_properties(member=True, launcher=None)
        self.assertEqual(("System.AppUserModel.PreventPinning", True), plan[0])
        self.assertEqual(("System.AppUserModel.ID", "VemalsEars.Companion"), plan[-1])
        self.assertIsNone(dict(plan)["System.AppUserModel.RelaunchCommand"])

    def test_taskbar_relaunch_never_carries_pairing_arguments(self):
        plan = dict(taskbar.taskbar_properties(
            member=True, launcher="C:/VE/VemalsEars.exe"))
        self.assertEqual('"C:\\VE\\VemalsEars.exe"', plan["System.AppUserModel.RelaunchCommand"])

    def test_missing_launcher_does_not_mutate_window_state(self):
        with mock.patch.object(taskbar, "_write_properties") as write:
            self.assertFalse(taskbar.apply_taskbar_identity(
                123, member=True, launcher="C:/missing/VemalsEars.exe"))
        write.assert_not_called()

    def test_property_failure_is_nonfatal(self):
        with mock.patch.object(taskbar, "_write_properties", side_effect=OSError):
            self.assertFalse(taskbar.apply_taskbar_identity(
                123, member=True, launcher=None))

    @unittest.skipUnless(os.name == "nt", "Windows property-store integration")
    def test_real_hidden_window_properties_and_cleanup(self):
        import ctypes
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        user32.GetAncestor.restype = ctypes.c_void_p
        hwnd = user32.GetAncestor(root.winfo_id(), 2)
        try:
            with tempfile.TemporaryDirectory() as folder:
                launcher = Path(folder) / "VemalsEars.exe"
                launcher.touch()
                self.assertTrue(taskbar.apply_taskbar_identity(
                    hwnd, member=True, launcher=launcher))
                observed = taskbar.read_taskbar_identity(hwnd)
                for name, expected in taskbar.taskbar_properties(member=True, launcher=launcher):
                    self.assertEqual(expected, observed[name.removeprefix(taskbar.PROPERTY_PREFIX)], name)
                # Optional independent cross-check on developer installs.
                # Neither CI nor the shipped runtime requires win32com.
                try:
                    from win32com.propsys import propsys
                except ImportError:
                    propsys = None
                if propsys is not None:
                    store = propsys.SHGetPropertyStoreForWindow(hwnd)
                    for name, expected in taskbar.taskbar_properties(member=True, launcher=launcher):
                        actual = store.GetValue(propsys.PSGetPropertyKeyFromName(name))
                        self.assertEqual(expected, actual.GetValue(), name)
                self.assertTrue(taskbar.clear_taskbar_identity(hwnd))
                self.assertEqual(dict.fromkeys(taskbar.PROPERTY_NAMES),
                                 taskbar.read_taskbar_identity(hwnd))
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
