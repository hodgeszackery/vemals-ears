"""Exercise only synthetic, owned Tk windows; never host state or services."""

import ctypes
from ctypes import wintypes
import os
import time
import tkinter as tk
import unittest
from unittest import mock

from Hosting import window_chrome


HTCLIENT = 1
HTCAPTION = 2
HTTRANSPARENT = -1
WM_NCHITTEST = 0x0084
WM_NCLBUTTONDBLCLK = 0x00A3
WM_SYSCOMMAND = 0x0112
SC_CLOSE = 0xF060
SW_RESTORE = 9
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_EX_TOOLWINDOW = 0x00000080


def packed_point(x, y):
    return (x & 0xFFFF) | ((y & 0xFFFF) << 16)


class ChromeHitTestTests(unittest.TestCase):
    def test_screen_coordinates_preserve_negative_monitor_positions(self):
        for point in ((-1200, -500), (200, -50), (-30, 600), (32767, -32768)):
            with self.subTest(point=point):
                self.assertEqual(point, window_chrome.signed_point(packed_point(*point)))

    def test_all_resize_edges_and_corners_take_priority_over_header_dragging(self):
        rectangle = (-1200, -500, -400, 100)
        cases = (
            ((-1200, -500), 13), ((-401, -500), 14),
            ((-1200, 99), 16), ((-401, 99), 17),
            ((-1200, -200), 10), ((-401, -200), 11),
            ((-800, -500), 12), ((-800, 99), 15),
        )
        for point, expected in cases:
            with self.subTest(point=point):
                self.assertEqual(expected, window_chrome.hit_test(
                    *point, rectangle, 8, 8, draggable=True))

    def test_header_dragging_is_explicit_and_maximized_edges_do_not_resize(self):
        rectangle = (100, 100, 900, 700)
        self.assertEqual(HTCLIENT, window_chrome.hit_test(300, 140, rectangle, 8, 8))
        self.assertEqual(HTCAPTION, window_chrome.hit_test(300, 140, rectangle, 8, 8, draggable=True))
        self.assertEqual(HTCLIENT, window_chrome.hit_test(100, 100, rectangle, 8, 8, maximized=True))
        self.assertEqual(HTCAPTION, window_chrome.hit_test(
            100, 100, rectangle, 8, 8, maximized=True, draggable=True))


class ChromeSubclassSafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = mock.Mock()
        self.root.overrideredirect.return_value = False
        self.root.winfo_id.return_value = 123
        self.native = mock.Mock()
        self.native.window.return_value = 456
        self.native.suitable.return_value = True
        self.native.install.return_value = True
        self.native.remove.return_value = True
        self.native.exists.return_value = True
        self.native.is_maximized.return_value = False
        self.native.children.return_value = []
        self.native.callback.side_effect = lambda function: function
        self.native.frame_changed.return_value = True
        self.native.forward.return_value = 937
        self.native.client_point.return_value = (100, 100)
        self.native.rectangle.return_value = (0, 0, 800, 600)
        self.native.borders.return_value = (8, 8)
        self.chrome = window_chrome.NativeWindowChrome(self.root, native=self.native)
        self.addCleanup(self.clear_mock_callbacks)

    def clear_mock_callbacks(self):
        for key, owner in list(window_chrome._ACTIVE.items()):
            if owner is self.chrome:
                window_chrome._ACTIVE.pop(key)

    def test_failed_subclass_install_keeps_native_frame_untouched(self):
        self.native.install.return_value = False
        self.assertFalse(self.chrome.attach())
        self.native.frame_changed.assert_not_called()
        self.assertNotIn(self.chrome, window_chrome._ACTIVE.values())
        self.root.overrideredirect.assert_called_once_with()

    def test_failed_child_install_rolls_back_partial_hooks_to_native_frame(self):
        self.native.children.return_value = [123, 789]
        self.native.install.side_effect = [True, True, False]
        self.assertFalse(self.chrome.attach())
        self.assertFalse(self.chrome._active)
        self.assertFalse(self.chrome._installed)
        self.assertNotIn(self.chrome, window_chrome._ACTIVE.values())
        self.assertEqual([123, 456], [call.args[0] for call in self.native.remove.call_args_list])
        self.native.frame_changed.assert_called_once_with(456)

    def test_failed_removal_retains_callback_owner_until_native_destruction(self):
        self.assertTrue(self.chrome.attach())
        callback = self.chrome._callback
        self.native.remove.return_value = False
        self.chrome.detach()
        self.assertIn(self.chrome, window_chrome._ACTIVE.values())
        self.assertIs(callback, self.chrome._callback)
        self.assertEqual(937, callback(456, WM_NCHITTEST, 0, packed_point(1, 1), id(self.chrome), 0))
        self.assertFalse(self.chrome.attach(), "Do not install another callback while removal is unresolved")
        self.assertEqual(937, callback(456, 0x0082, 0, 0, id(self.chrome), 0))
        self.assertNotIn(self.chrome, window_chrome._ACTIVE.values())

    def test_native_callbacks_do_not_reenter_tk_and_forward_unhandled_messages(self):
        self.assertTrue(self.chrome.attach())
        self.root.reset_mock()
        self.assertEqual(0, self.chrome._callback(456, 0x0083, 1, 1234, id(self.chrome), 0))
        self.assertEqual(HTCLIENT, self.chrome._callback(456, WM_NCHITTEST, 0, packed_point(100, 100), id(self.chrome), 0))
        self.assertEqual(937, self.chrome._callback(456, WM_SYSCOMMAND, SC_CLOSE, 0, id(self.chrome), 0))
        self.assertEqual([], self.root.mock_calls)
        self.chrome.detach()

    def test_hit_test_error_falls_through_to_existing_window_procedure(self):
        self.assertTrue(self.chrome.attach())
        self.native.rectangle.side_effect = OSError("Synthetic invalid window bounds")
        self.assertEqual(937, self.chrome._callback(456, WM_NCHITTEST, 0, packed_point(100, 100), id(self.chrome), 0))
        self.chrome.detach()

    def test_child_destruction_does_not_disable_remaining_native_window(self):
        self.native.children.return_value = [123, 789]
        self.assertTrue(self.chrome.attach())
        self.chrome._drag_rects = ((0, 0, 800, 70),)
        self.native.client_point.return_value = (100, 35)
        callback = self.chrome._callback
        self.assertEqual(HTTRANSPARENT, callback(789, WM_NCHITTEST, 0, packed_point(100, 35), id(self.chrome), 0))
        self.assertEqual(937, callback(789, 0x0082, 0, 0, id(self.chrome), 0))
        self.assertTrue(self.chrome._active)
        self.assertNotIn((789, id(self.chrome)), window_chrome._ACTIVE)
        self.assertIs(self.chrome, window_chrome._ACTIVE[(456, id(self.chrome))])
        self.assertEqual(HTCAPTION, callback(456, WM_NCHITTEST, 0, packed_point(100, 35), id(self.chrome), 0))
        self.chrome.detach()
        self.assertNotIn(self.chrome, window_chrome._ACTIVE.values())

    def test_failed_child_removal_keeps_callback_alive_until_child_destruction(self):
        self.native.children.return_value = [123]
        self.assertTrue(self.chrome.attach())
        self.native.remove.side_effect = lambda hwnd, *_args: hwnd != 123
        self.chrome.detach()
        self.assertIs(self.chrome, window_chrome._ACTIVE[(123, id(self.chrome))])
        self.assertNotIn((456, id(self.chrome)), window_chrome._ACTIVE)
        self.assertFalse(self.chrome.attach(), "Do not replace a callback while a child still uses it")
        callback = self.chrome._callback
        self.assertEqual(937, callback(123, WM_NCHITTEST, 0, packed_point(100, 35), id(self.chrome), 0))
        self.assertEqual(937, callback(123, 0x0082, 0, 0, id(self.chrome), 0))
        self.assertNotIn(self.chrome, window_chrome._ACTIVE.values())


@unittest.skipUnless(os.name == "nt", "Native Windows window messages")
class NativeChromeWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest("A Tk display is required: " + str(error))
        self.addCleanup(self.destroy_root)
        self.root.withdraw()
        self.root.title("Vemal's Ears isolated window chrome test")
        self.root.geometry("680x460+120+120")
        self.root.minsize(320, 240)
        self.header = tk.Frame(self.root, height=70)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        self.title = tk.Label(self.header, text="Synthetic host header")
        self.title.pack(side="left", padx=20)
        self.action = tk.Button(self.header, text="Synthetic action")
        self.action.pack(side="right", padx=12)
        self.entry = tk.Entry(self.root)
        self.entry.pack(fill="x", padx=24, pady=20)
        self.close_requests = []
        self.states = []
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self.chrome = window_chrome.NativeWindowChrome(
            self.root, drag_widgets=(self.header, self.title),
            on_close=self.request_close, on_state_change=self.states.append)
        self.root.deiconify()
        self.root.update()
        self.assertTrue(self.chrome.attach(), "Native chrome must attach on the Windows test host")
        self.root.update()
        self.native = ctypes.WinDLL("user32", use_last_error=True)
        signatures = {
            "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "GetParent": ([wintypes.HWND], wintypes.HWND),
            "ChildWindowFromPointEx": ([wintypes.HWND, wintypes.POINT, wintypes.UINT], wintypes.HWND),
            "GetWindow": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "GetWindowLongPtrW": ([wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "GetClientRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "ClientToScreen": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "ScreenToClient": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "IsZoomed": ([wintypes.HWND], wintypes.BOOL),
            "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "SendMessageW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.native, name)
            function.argtypes = arguments
            function.restype = result
        self.hwnd = self.native.GetAncestor(self.root.winfo_id(), 2)
        self.assertTrue(self.hwnd)

    def request_close(self):
        # A real wizard may refuse close while setup is working. Keep this
        # test window alive to prove the native request honors that callback.
        self.close_requests.append("requested")

    def destroy_root(self):
        if hasattr(self, "chrome"):
            self.chrome.detach()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("The owned preview window did not reach the requested state")

    def rectangle(self):
        result = wintypes.RECT()
        self.assertTrue(self.native.GetWindowRect(self.hwnd, ctypes.byref(result)))
        return result.left, result.top, result.right, result.bottom

    def hit(self, x, y):
        return self.native_hit_route(x, y)[1]

    def native_hit_route(self, x, y):
        """Begin at the actual owned child under a point, without global input."""
        target = self.hwnd
        while True:
            point = wintypes.POINT(x, y)
            self.assertTrue(self.native.ScreenToClient(target, ctypes.byref(point)))
            child = self.native.ChildWindowFromPointEx(target, point, 3)
            if not child or child == target:
                break
            target = child
        visited = set()
        while target and target not in visited:
            visited.add(target)
            result = self.native.SendMessageW(target, WM_NCHITTEST, 0, packed_point(x, y))
            if result != HTTRANSPARENT:
                return target, result
            target = self.native.GetParent(target)
        self.fail("Native hit testing did not reach an owned input target")

    def widget_center(self, widget):
        return widget.winfo_rootx() + widget.winfo_width() // 2, widget.winfo_rooty() + widget.winfo_height() // 2

    def test_native_control_styles_and_taskbar_eligibility_survive_without_override(self):
        style = self.native.GetWindowLongPtrW(self.hwnd, GWL_STYLE)
        for flag in (WS_SYSMENU, WS_THICKFRAME, WS_MINIMIZEBOX, WS_MAXIMIZEBOX):
            self.assertTrue(style & flag)
        self.assertFalse(self.root.overrideredirect())
        self.assertTrue(self.native.IsWindowVisible(self.hwnd))
        self.assertFalse(self.native.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW)
        self.assertFalse(self.native.GetWindow(self.hwnd, 4), "The window must not become an owned popup")
        # Eligibility is testable here; the shell's actual taskbar entry is
        # separately checked during the visible installer preview.

    def test_minimize_and_native_restore_keep_same_usable_window(self):
        self.entry.focus_force()
        self.root.update()
        self.chrome.minimize()
        self.wait_for(lambda: self.native.IsIconic(self.hwnd))
        self.native.ShowWindow(self.hwnd, SW_RESTORE)
        self.wait_for(lambda: not self.native.IsIconic(self.hwnd) and self.native.IsWindowVisible(self.hwnd))
        self.assertEqual(self.hwnd, self.native.GetAncestor(self.root.winfo_id(), 2))
        self.assertFalse(self.root.overrideredirect())
        self.entry.focus_force()
        self.root.update()
        self.assertIs(self.entry, self.root.focus_get())
        self.entry.insert(0, "Restored input works")
        self.assertEqual("Restored input works", self.entry.get())

    def test_maximize_restore_and_header_double_click_use_native_window_state(self):
        original = self.rectangle()
        self.chrome.toggle_maximize()
        self.wait_for(lambda: self.native.IsZoomed(self.hwnd))
        self.assertTrue(self.chrome.is_maximized())
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

        self.native.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.native.MonitorFromWindow.restype = wintypes.HANDLE
        self.native.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        self.native.GetMonitorInfoW.restype = wintypes.BOOL
        monitor = MonitorInfo(size=ctypes.sizeof(MonitorInfo))
        self.assertTrue(self.native.GetMonitorInfoW(self.native.MonitorFromWindow(self.hwnd, 2), ctypes.byref(monitor)))
        client = wintypes.RECT()
        self.assertTrue(self.native.GetClientRect(self.hwnd, ctypes.byref(client)))
        top_left, bottom_right = wintypes.POINT(client.left, client.top), wintypes.POINT(client.right, client.bottom)
        self.assertTrue(self.native.ClientToScreen(self.hwnd, ctypes.byref(top_left)))
        self.assertTrue(self.native.ClientToScreen(self.hwnd, ctypes.byref(bottom_right)))
        self.assertGreaterEqual(top_left.x, monitor.work.left)
        self.assertGreaterEqual(top_left.y, monitor.work.top)
        self.assertLessEqual(bottom_right.x, monitor.work.right)
        self.assertLessEqual(bottom_right.y, monitor.work.bottom)
        self.chrome.toggle_maximize()
        self.wait_for(lambda: not self.native.IsZoomed(self.hwnd))
        self.assertEqual(original, self.rectangle())
        self.assertEqual([True, False], self.states[-2:])
        for expected in (True, False):
            x, y = self.widget_center(self.title)
            target, hit = self.native_hit_route(x, y)
            self.assertEqual((self.hwnd, HTCAPTION), (target, hit))
            self.native.SendMessageW(target, WM_NCLBUTTONDBLCLK, hit, packed_point(x, y))
            self.wait_for(lambda: bool(self.native.IsZoomed(self.hwnd)) == expected)
        self.assertEqual(original, self.rectangle())
        self.assertFalse(self.root.overrideredirect())

    def test_native_edges_resize_while_buttons_and_entry_remain_client_controls(self):
        left, top, right, bottom = self.rectangle()
        cases = ((left + 1, top + 1, 13), (right - 2, top + 1, 14),
                 (left + 1, bottom - 2, 16), (right - 2, bottom - 2, 17),
                 (left + 1, (top + bottom) // 2, 10), (right - 2, (top + bottom) // 2, 11),
                 ((left + right) // 2, top + 1, 12), ((left + right) // 2, bottom - 2, 15))
        for x, y, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, self.hit(x, y))
        self.assertEqual(HTCAPTION, self.hit(*self.widget_center(self.title)))
        self.assertEqual(HTCLIENT, self.hit(*self.widget_center(self.action)))
        self.assertEqual(HTCLIENT, self.hit(*self.widget_center(self.entry)))

    def test_header_child_windows_route_caption_input_to_wrapper_not_tk_client(self):
        point = self.widget_center(self.title)
        # The label, its frame and Tk's inner root all sit above the wrapper.
        # A direct wrapper hit test misses any one of these swallowing input.
        for widget in (self.title, self.header, self.root):
            child = widget.winfo_id()
            self.assertNotEqual(self.hwnd, child)
            self.assertEqual(self.hwnd, self.native.GetAncestor(child, 2))
            with self.subTest(widget=widget.winfo_class()):
                self.assertEqual(HTTRANSPARENT, self.native.SendMessageW(
                    child, WM_NCHITTEST, 0, packed_point(*point)))
        self.assertEqual((self.hwnd, HTCAPTION), self.native_hit_route(*point))
        blank = (self.header.winfo_rootx() + self.header.winfo_width() // 2,
                 self.header.winfo_rooty() + self.header.winfo_height() // 2)
        self.assertEqual((self.hwnd, HTCAPTION), self.native_hit_route(*blank))
        for widget in (self.action, self.entry):
            with self.subTest(widget=widget.winfo_class()):
                target, hit = self.native_hit_route(*self.widget_center(widget))
                self.assertEqual(HTCLIENT, hit)
                self.assertEqual(widget.winfo_id(), target)

    def test_detach_restores_child_input_and_destruction_releases_all_callback_owners(self):
        self.chrome.detach()
        self.root.update()
        point = self.widget_center(self.title)
        self.assertEqual((self.title.winfo_id(), HTCLIENT), self.native_hit_route(*point))
        self.assertNotIn(self.chrome, window_chrome._ACTIVE.values())
        self.assertTrue(self.chrome.attach())
        self.root.update()
        self.assertEqual((self.hwnd, HTCAPTION), self.native_hit_route(*self.widget_center(self.title)))
        self.title.destroy()
        self.root.update()
        self.assertTrue(self.chrome._active)
        self.root.destroy()
        self.assertNotIn(self.chrome, window_chrome._ACTIVE.values())

    def test_system_close_uses_close_callback_without_forced_destruction(self):
        self.native.SendMessageW(self.hwnd, WM_SYSCOMMAND, SC_CLOSE, 0)
        self.wait_for(lambda: len(self.close_requests) == 1)
        self.assertTrue(self.native.IsWindow(self.hwnd))
        # Directly posting an Alt+F4 key message does not reproduce Windows'
        # keyboard translation, even on plain Tk. Real Alt+F4 is checked in
        # the visible preview; its resulting native close path is tested here.

    def test_detach_restores_native_frame_and_can_attach_again(self):
        self.chrome.detach()
        self.root.update()
        self.assertFalse(self.root.overrideredirect())
        self.assertTrue(self.native.IsWindow(self.hwnd))
        self.assertTrue(self.chrome.attach())
        self.root.update()
        self.assertEqual(HTCAPTION, self.hit(*self.widget_center(self.title)))

    def test_attach_while_hidden_refreshes_header_hit_testing_when_shown(self):
        self.chrome.detach()
        self.root.withdraw()
        self.root.update()
        self.assertTrue(self.chrome.attach())
        self.root.deiconify()
        self.root.update()
        self.assertEqual(HTCAPTION, self.hit(*self.widget_center(self.title)))
        self.assertEqual(HTCLIENT, self.hit(*self.widget_center(self.action)))

    def test_destroy_without_explicit_detach_releases_native_callback_owner(self):
        self.assertIn(self.chrome, window_chrome._ACTIVE.values())
        self.root.destroy()
        self.assertNotIn(self.chrome, window_chrome._ACTIVE.values())
        self.assertFalse(self.native.IsWindow(self.hwnd))


class ChromeWizardIntegrationTests(unittest.TestCase):
    def setUp(self):
        from Hosting import wizard
        from tests.test_host_wizard import FakeController

        self.wizard = wizard
        self.fake_controller = FakeController
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest("A Tk display is required: " + str(error))
        self.root.withdraw()
        self.addCleanup(self.destroy_root)

    def destroy_root(self):
        if hasattr(self, "app"):
            self.app.busy = False
            if not self.app.closed:
                self.app.close()
        else:
            self.root.destroy()

    def create(self, *, attached):
        self.native = mock.Mock()
        self.native.attach.return_value = attached
        self.factory = self.enterContext(mock.patch.object(window_chrome, "NativeWindowChrome", return_value=self.native))
        self.backend = self.fake_controller(configured=False)
        self.app = self.wizard.HostWizard(self.root, self.backend)
        deadline = time.monotonic() + 3
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.app.busy)

    def test_window_controls_use_native_actions_and_same_busy_close_guard(self):
        self.create(attached=True)
        self.assertEqual("pack", self.app.header_controls.winfo_manager())
        self.app.minimize_button.invoke()
        self.app.maximize_button.invoke()
        self.native.minimize.assert_called_once_with()
        self.native.toggle_maximize.assert_called_once_with()
        self.factory.call_args.kwargs["on_state_change"](True)
        self.assertEqual("Restore", self.app.maximize_button.cget("text"))
        self.factory.call_args.kwargs["on_state_change"](False)
        self.assertEqual("Maximize", self.app.maximize_button.cget("text"))
        self.app.busy = True
        with mock.patch.object(self.wizard.messagebox, "askyesno", return_value=False) as confirm:
            self.app.close_button.invoke()
            self.factory.call_args.kwargs["on_close"]()
        self.assertEqual(2, confirm.call_count)
        self.assertFalse(self.app.closed)
        self.assertTrue(self.root.winfo_exists())
        self.native.detach.assert_not_called()
        self.assertNotIn("configure", self.backend.calls)
        self.assertNotIn("install_services", self.backend.calls)

    def test_native_attach_failure_keeps_branding_and_hides_duplicate_controls(self):
        self.create(attached=False)
        self.assertEqual("", self.app.header_controls.winfo_manager())
        self.assertEqual("pack", self.app.header.winfo_manager())
        self.assertFalse(self.root.overrideredirect())
        self.app.close()
        self.native.detach.assert_called_once_with()
        self.assertTrue(self.app.closed)


if __name__ == "__main__":
    unittest.main()
