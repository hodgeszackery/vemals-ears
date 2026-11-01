"""A Tk header inside a normal Windows window, with native move/resize behavior.

Only the non-client calculation and hit test are replaced. The existing Tk
window procedure still handles system commands, focus, close and sizing. No
window styles or Tk decoration modes are changed. Unsupported hosts retain
their ordinary caption.
"""

import ctypes
from ctypes import wintypes
import os


WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCDESTROY = 0x0082
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE = 0xF020
SC_MAXIMIZE = 0xF030
SC_RESTORE = 0xF120
SC_CLOSE = 0xF060
HTCLIENT = 1
HTCAPTION = 2
WS_OVERLAPPEDWINDOW = 0x00CF0000

# Windows does not retain a Python reference to a subclass callback. Keep the
# owner alive until removal succeeds or the window finishes destruction.
_ACTIVE = {}


def signed_point(lparam):
    """Decode GET_X/Y_LPARAM, including monitors left of or above the origin."""
    return (ctypes.c_short(lparam & 0xFFFF).value,
            ctypes.c_short((lparam >> 16) & 0xFFFF).value)


def hit_test(x, y, rect, border_x, border_y, maximized=False, draggable=False):
    """Return the native hit code for a screen point and (L, T, R, B) bounds."""
    left, top, right, bottom = rect
    if not maximized and left <= x < right and top <= y < bottom:
        at_left, at_right = x < left + border_x, x >= right - border_x
        at_top, at_bottom = y < top + border_y, y >= bottom - border_y
        if at_top:
            return 13 if at_left else 14 if at_right else 12
        if at_bottom:
            return 16 if at_left else 17 if at_right else 15
        if at_left:
            return 10
        if at_right:
            return 11
    return HTCAPTION if draggable else HTCLIENT


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                ("work", wintypes.RECT), ("flags", wintypes.DWORD)]


class _Win32:
    """Small native boundary; created lazily so imports also work off Windows."""

    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.common = ctypes.WinDLL("comctl32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
            ctypes.c_size_t, ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_size_t)
        self._declare(self.user, "GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND)
        self._declare(self.user, "IsWindow", [wintypes.HWND], wintypes.BOOL)
        self._declare(self.user, "IsZoomed", [wintypes.HWND], wintypes.BOOL)
        self._declare(self.user, "GetWindowThreadProcessId",
                      [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
        self._declare(self.kernel, "GetCurrentThreadId", [], wintypes.DWORD)
        self.get_style = self._declare(
            self.user, "GetWindowLongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "GetWindowLongW",
            [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t)
        self._declare(self.user, "GetWindowRect",
                      [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL)
        self._declare(self.user, "ScreenToClient",
                      [wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL)
        self._declare(self.user, "GetSystemMetrics", [ctypes.c_int], ctypes.c_int)
        self._declare(self.user, "SetWindowPos",
                      [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL)
        self._declare(self.user, "PostMessageW",
                      [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t], wintypes.BOOL)
        self._declare(self.user, "MonitorFromRect",
                      [ctypes.POINTER(wintypes.RECT), wintypes.DWORD], wintypes.HANDLE)
        self._declare(self.user, "GetMonitorInfoW",
                      [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)], wintypes.BOOL)
        self._declare(self.common, "SetWindowSubclass",
                      [wintypes.HWND, self.callback_type, ctypes.c_size_t, ctypes.c_size_t], wintypes.BOOL)
        self._declare(self.common, "RemoveWindowSubclass",
                      [wintypes.HWND, self.callback_type, ctypes.c_size_t], wintypes.BOOL)
        self._declare(self.common, "DefSubclassProc",
                      [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_ssize_t)
        self.dpi = getattr(self.user, "GetDpiForWindow", None)
        self.dpi_metric = getattr(self.user, "GetSystemMetricsForDpi", None)
        if self.dpi and self.dpi_metric:
            self.dpi.argtypes, self.dpi.restype = [wintypes.HWND], wintypes.UINT
            self.dpi_metric.argtypes = [ctypes.c_int, wintypes.UINT]
            self.dpi_metric.restype = ctypes.c_int

    @staticmethod
    def _declare(library, name, args, result):
        function = getattr(library, name)
        function.argtypes, function.restype = args, result
        return function

    def window(self, inner):
        return self.user.GetAncestor(inner, 2)  # GA_ROOT: the Tk wrapper HWND.

    def suitable(self, hwnd):
        style = self.get_style(hwnd, -16)
        return bool(self.exists(hwnd)
                    and self.user.GetWindowThreadProcessId(hwnd, None) == self.kernel.GetCurrentThreadId()
                    and style & WS_OVERLAPPEDWINDOW == WS_OVERLAPPEDWINDOW
                    and not style & 0x40000000  # WS_CHILD
                    and not self.get_style(hwnd, -20) & 0x80)  # WS_EX_TOOLWINDOW

    def exists(self, hwnd):
        return bool(self.user.IsWindow(hwnd))

    def callback(self, function):
        return self.callback_type(function)

    def install(self, hwnd, callback, identity):
        return bool(self.common.SetWindowSubclass(hwnd, callback, identity, 0))

    def remove(self, hwnd, callback, identity):
        return bool(self.common.RemoveWindowSubclass(hwnd, callback, identity))

    def forward(self, hwnd, message, wparam, lparam):
        return self.common.DefSubclassProc(hwnd, message, wparam, lparam)

    def frame_changed(self, hwnd):
        # NOMOVE | NOSIZE | NOZORDER | NOACTIVATE | FRAMECHANGED.
        return bool(self.user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0037))

    def is_maximized(self, hwnd):
        return bool(self.user.IsZoomed(hwnd))

    def command(self, hwnd, command):
        return bool(self.user.PostMessageW(hwnd, WM_SYSCOMMAND, command, 0))

    def rectangle(self, hwnd):
        rect = wintypes.RECT()
        if not self.user.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise OSError("Cannot read window bounds")
        return rect.left, rect.top, rect.right, rect.bottom

    def client_point(self, hwnd, x, y):
        point = wintypes.POINT(x, y)
        if not self.user.ScreenToClient(hwnd, ctypes.byref(point)):
            raise OSError("Cannot read window coordinates")
        return point.x, point.y

    def borders(self, hwnd):
        if self.dpi and self.dpi_metric:
            dpi = self.dpi(hwnd) or 96
            metric = lambda index: self.dpi_metric(index, dpi)
        else:
            metric = self.user.GetSystemMetrics
        padding = metric(92)  # SM_CXPADDEDBORDER.
        return max(1, metric(32) + padding), max(1, metric(33) + padding)

    def constrain_maximized_client(self, hwnd, lparam):
        if not lparam or not self.is_maximized(hwnd):
            return
        # RECT is also the first member of NCCALCSIZE_PARAMS. Use the proposed
        # bounds to select the correct monitor, including negative origins.
        rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
        monitor = self.user.MonitorFromRect(ctypes.byref(rect), 2)
        info = _MonitorInfo(size=ctypes.sizeof(_MonitorInfo))
        if monitor and self.user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect.left, rect.top = max(rect.left, info.work.left), max(rect.top, info.work.top)
            rect.right, rect.bottom = min(rect.right, info.work.right), min(rect.bottom, info.work.bottom)

    def dark_frame(self, hwnd):
        # Optional on older Windows versions. No style change is needed for
        # the DWM shadow; keep the normal thick frame and request dark trim.
        try:
            dwm = ctypes.WinDLL("dwmapi")
            setter = self._declare(dwm, "DwmSetWindowAttribute",
                                   [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD], ctypes.c_long)
            enabled = wintypes.BOOL(1)
            setter(hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
        except (AttributeError, OSError):
            pass


class NativeWindowChrome:
    """Attach to a Tk root on its UI thread, preserving its close protocol.

    ``drag_widgets`` must explicitly list passive header widgets. Descendants
    not on that list are excluded so controls inside a header stay interactive.
    Native callbacks use cached geometry and never invoke Tk reentrantly.
    """

    def __init__(self, root, drag_widgets=(), on_close=None, on_state_change=None, *, native=None):
        self.root = root
        self.drag_widgets = tuple(drag_widgets)
        self.on_close = on_close
        self.on_state_change = on_state_change
        self.native = native
        self.hwnd = None
        self._callback = None
        self._identity = id(self)
        self._active = False
        self._installed = False
        self._bindings = []
        self._refresh_id = None
        self._drag_rects = ()
        self._excluded_rects = ()
        self._last_state = None

    def attach(self):
        if self._active:
            return True
        if self._installed:
            return False
        if self.native is None:
            if os.name != "nt":
                return False
            try:
                self.native = _Win32()
            except (AttributeError, OSError):
                return False
        try:
            self.root.update_idletasks()
            if self.root.overrideredirect():
                return False
            self.hwnd = self.native.window(self.root.winfo_id())
            if not self.hwnd or not self.native.suitable(self.hwnd):
                return False
            self._callback = self.native.callback(self._dispatch)
            if not self.native.install(self.hwnd, self._callback, self._identity):
                return False
            self._installed = self._active = True
            _ACTIVE[(self.hwnd, self._identity)] = self
            self._bindings = [
                ("<Configure>", self.root.bind("<Configure>", self._queue_refresh, add="+")),
                ("<Destroy>", self.root.bind("<Destroy>", self._destroy, add="+")),
            ]
            self.native.dark_frame(self.hwnd)
            if not self.native.frame_changed(self.hwnd):
                raise OSError("Cannot recalculate window frame")
            self._refresh()
            return True
        except Exception:
            self.detach()
            return False

    def _dispatch(self, hwnd, message, wparam, lparam, _identity, _data):
        try:
            if message == WM_NCDESTROY:
                self._active = False
                self.native.remove(hwnd, self._callback, self._identity)
                self._installed = False
                _ACTIVE.pop((hwnd, self._identity), None)
                # Keep self._callback intact while its native invocation exits.
            elif self._active and message == WM_NCCALCSIZE:
                self.native.constrain_maximized_client(hwnd, lparam)
                return 0
            elif self._active and message == WM_NCHITTEST:
                x, y = signed_point(lparam)
                cx, cy = self.native.client_point(hwnd, x, y)
                contains = lambda rect: rect[0] <= cx < rect[2] and rect[1] <= cy < rect[3]
                draggable = (any(map(contains, self._drag_rects))
                             and not any(map(contains, self._excluded_rects)))
                return hit_test(x, y, self.native.rectangle(hwnd), *self.native.borders(hwnd),
                                maximized=self.native.is_maximized(hwnd), draggable=draggable)
        except Exception:
            # Never allow an exception to escape a ctypes callback into Windows.
            pass
        return self.native.forward(hwnd, message, wparam, lparam)

    def _queue_refresh(self, _event=None):
        if self._active and self._refresh_id is None:
            self._refresh_id = self.root.after_idle(self._refresh)

    def _refresh(self):
        self._refresh_id = None
        if not self._active:
            return
        try:
            # Convert Tk screen positions through the wrapper HWND, avoiding
            # assumptions about native caption/frame offsets and DPI scaling.
            def rectangle(widget):
                x, y = self.native.client_point(self.hwnd, widget.winfo_rootx(), widget.winfo_rooty())
                return x, y, x + widget.winfo_width(), y + widget.winfo_height()

            included = set(self.drag_widgets)
            self._drag_rects = tuple(rectangle(widget) for widget in included if widget.winfo_ismapped())
            excluded = []
            for widget in included:
                for child in widget.winfo_children():
                    if child not in included and child.winfo_ismapped():
                        excluded.append(rectangle(child))
            self._excluded_rects = tuple(excluded)
            state = self.is_maximized()
            if state != self._last_state:
                self._last_state = state
                if self.on_state_change is not None:
                    self.on_state_change(state)
        except Exception:
            # Tk may already be destroying its children.
            pass

    def is_maximized(self):
        return bool(self.hwnd and self.native and self.native.exists(self.hwnd)
                    and self.native.is_maximized(self.hwnd))

    def minimize(self):
        if self._active:
            self.native.command(self.hwnd, SC_MINIMIZE)

    def toggle_maximize(self):
        if self._active:
            self.native.command(self.hwnd, SC_RESTORE if self.is_maximized() else SC_MAXIMIZE)

    def close(self):
        if self.on_close is not None:
            self.on_close()
        elif self._active:
            self.native.command(self.hwnd, SC_CLOSE)

    def _destroy(self, event):
        if event.widget is self.root:
            self.detach()

    def detach(self):
        self._active = False
        if self._refresh_id is not None:
            try:
                self.root.after_cancel(self._refresh_id)
            except Exception:
                pass
            self._refresh_id = None
        for event, binding in self._bindings:
            try:
                self.root.unbind(event, binding)
            except Exception:
                pass
        self._bindings = []
        if self._installed:
            try:
                alive = self.native.exists(self.hwnd)
                removed = not alive or self.native.remove(self.hwnd, self._callback, self._identity)
                if removed:
                    self._installed = False
                    _ACTIVE.pop((self.hwnd, self._identity), None)
                # If removal fails, retain the callback but pass every message
                # through until WM_NCDESTROY. Native caption restoration is safe.
                if alive:
                    self.native.frame_changed(self.hwnd)
            except Exception:
                pass
