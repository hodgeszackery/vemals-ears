"""Hidden, isolated window-owner probe for each native application host."""

import ctypes
import json
import os
import sys
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

from GuildHub.windows_taskbar import (
    apply_taskbar_identity, clear_taskbar_identity, read_taskbar_identity,
)

root = tk.Tk()
root.withdraw()
root.title(os.environ["NATIVE_PRODUCT_NAME"])
root.update_idletasks()

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
kernel32.GetModuleFileNameW.restype = wintypes.DWORD
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.GetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.POINTER(ctypes.c_wchar_p)]
shell32.GetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
ole32 = ctypes.WinDLL("ole32")
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None

window = user32.GetAncestor(root.winfo_id(), 2)
window_pid = wintypes.DWORD()
user32.GetWindowThreadProcessId(window, ctypes.byref(window_pid))
image = ctypes.create_unicode_buffer(32768)
kernel32.GetModuleFileNameW(None, image, len(image))
if not apply_taskbar_identity(
    int(window), member=Path(image.value).name == "VemalsEars.exe",
    launcher=Path(image.value), product_name=root.title(),
):
    raise RuntimeError("native window taskbar properties could not be set")
taskbar = read_taskbar_identity(int(window))
app_id = ctypes.c_wchar_p()
if shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(app_id)) != 0:
    raise RuntimeError("native host did not set its application identity")
try:
    Path(os.environ["NATIVE_PRODUCT_RESULT"]).write_text(
        json.dumps({
            "host_pid": os.getpid(),
            "window_pid": window_pid.value,
            "window_title": root.title(),
            "image": image.value,
            "app_id": app_id.value,
            "sys_executable": sys.executable,
            "ignore_environment": sys.flags.ignore_environment,
            "no_user_site": sys.flags.no_user_site,
            "dont_write_bytecode": sys.dont_write_bytecode,
            "argv": sys.argv[1:],
            "taskbar": taskbar,
        }),
        encoding="ascii",
    )
finally:
    ole32.CoTaskMemFree(app_id)
    clear_taskbar_identity(int(window))
    root.destroy()
