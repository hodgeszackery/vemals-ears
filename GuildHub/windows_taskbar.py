"""Window-only taskbar identity; never edit the user's shortcuts or pins.

Python remains sys.executable for child workers, so it must not become a
taskbar relaunch target. Only a product-native EXE may be pinned. Source-only
sessions keep their application grouping but deliberately disable pinning.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import uuid
from contextlib import contextmanager
from pathlib import Path


PROPERTY_PREFIX = "System.AppUserModel."
PROPERTY_NAMES = (
    "PreventPinning", "RelaunchCommand", "RelaunchDisplayNameResource",
    "RelaunchIconResource", "ID",
)
PROPERTY_IDS = dict(zip(PROPERTY_NAMES, (9, 2, 4, 3, 5)))


class _Guid(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _Guid), ("pid", ctypes.c_uint32)]


class _CountedPointer(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("pointer", ctypes.c_void_p)]


class _VariantValue(ctypes.Union):
    _fields_ = [("text", ctypes.c_wchar_p), ("boolean", ctypes.c_int16),
                ("vector", _CountedPointer)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_uint16), ("reserved1", ctypes.c_uint16),
                ("reserved2", ctypes.c_uint16), ("reserved3", ctypes.c_uint16),
                ("value", _VariantValue)]


def _guid(value: str) -> _Guid:
    return _Guid.from_buffer_copy(uuid.UUID(value).bytes_le)


_PROPERTY_FORMAT = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"
_PROPERTY_STORE_IID = "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"


def _property_key(name: str) -> _PropertyKey:
    return _PropertyKey(_guid(_PROPERTY_FORMAT), PROPERTY_IDS[name.removeprefix(PROPERTY_PREFIX)])


def _check_hresult(result: int) -> None:
    if result < 0:
        raise OSError("Windows taskbar property operation failed: " + hex(result & 0xffffffff))


@contextmanager
def _property_store(hwnd: int):
    """Use the OS interface directly; trimmed bundles do not ship win32com.

    IPropertyStore vtable ordering is the documented IUnknown methods,
    GetCount, GetAt, GetValue, SetValue, Commit. This acquires only the given
    HWND's store and never opens Explorer's persisted shortcuts or pins.
    """
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_int32
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.PropVariantClear.argtypes = [ctypes.POINTER(_PropVariant)]
    ole32.PropVariantClear.restype = ctypes.c_int32
    initialized = int(ole32.CoInitializeEx(None, 2))
    # A UI already initialized as MTA can still use the existing apartment.
    if initialized != -2147417850:  # RPC_E_CHANGED_MODE
        _check_hresult(initialized)
    pointer = ctypes.c_void_p()
    release = None
    try:
        getter = shell32.SHGetPropertyStoreForWindow
        getter.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Guid),
                           ctypes.POINTER(ctypes.c_void_p)]
        getter.restype = ctypes.c_int32
        iid = _guid(_PROPERTY_STORE_IID)
        _check_hresult(int(getter(hwnd, ctypes.byref(iid), ctypes.byref(pointer))))
        if not pointer.value:
            raise OSError("Windows returned no taskbar property store.")
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        release = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(table[2])
        method = ctypes.WINFUNCTYPE(
            ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(_PropertyKey),
            ctypes.POINTER(_PropVariant),
        )
        get_value, set_value = method(table[5]), method(table[6])
        commit = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p)(table[7])
        yield pointer, get_value, set_value, commit, ole32.PropVariantClear
    finally:
        if release is not None:
            release(pointer)
        if initialized >= 0:
            ole32.CoUninitialize()


def _safe_path(value: str | os.PathLike[str]) -> str:
    path = os.fspath(value)
    if (not isinstance(path, str) or not ntpath.isabs(path)
            or '"' in path or any(ord(char) < 32 for char in path)):
        raise ValueError("The taskbar target must be an absolute application path.")
    return ntpath.normpath(path)


def taskbar_properties(
    *,
    member: bool,
    launcher: str | os.PathLike[str] | None,
    product_name: str | None = None,
    icon_path: str | os.PathLike[str] | None = None,
) -> tuple[tuple[str, str | bool | None], ...]:
    """Build an ordered, testable property plan without reading user state."""
    app_id = "VemalsEars.Companion" if member else "Downtime.Companion"
    if launcher is None:
        # Microsoft requires PreventPinning before ID. Clear any relaunch
        # metadata left on an existing HWND before making it source-only.
        values: tuple[tuple[str, str | bool | None], ...] = (
            ("PreventPinning", True), ("RelaunchCommand", None),
            ("RelaunchDisplayNameResource", None),
            ("RelaunchIconResource", None), ("ID", app_id),
        )
    else:
        application = _safe_path(launcher)
        filename = ntpath.basename(application).casefold()
        allowed = {"vemalsears.exe"} if member else {"downtime.exe", "downtimelite.exe"}
        if filename not in allowed:
            raise ValueError("The taskbar target is not this product's native launcher.")
        name = product_name or (
            "Vemal's Ears" if member else
            "Downtime Lite" if filename == "downtimelite.exe" else "Downtime"
        )
        if not name.strip() or any(ord(char) < 32 for char in name):
            raise ValueError("The taskbar application name is invalid.")
        icon = application if icon_path is None else _safe_path(icon_path)
        if ntpath.splitext(icon)[1].casefold() not in {".ico", ".exe"}:
            raise ValueError("The taskbar icon must be a Windows icon resource.")
        values = (
            ("PreventPinning", False),
            ("RelaunchCommand", '"' + application + '"'),
            ("RelaunchDisplayNameResource", name),
            ("RelaunchIconResource", icon + ",0"), ("ID", app_id),
        )
    return tuple((PROPERTY_PREFIX + key, value) for key, value in values)


def _write_properties(hwnd: int, values: tuple) -> None:
    with _property_store(hwnd) as (pointer, _get, set_value, commit, _clear):
        for name, value in values:
            key = _property_key(name)
            variant = _PropVariant()
            buffer = None
            if isinstance(value, str):
                buffer = ctypes.create_unicode_buffer(value)
                variant.vt = 31  # VT_LPWSTR; SetValue copies the string.
                variant.value.text = ctypes.cast(buffer, ctypes.c_wchar_p)
            elif isinstance(value, bool):
                variant.vt = 11  # VT_BOOL
                variant.value.boolean = -1 if value else 0
            elif value is not None:
                raise ValueError("Unsupported taskbar property value.")
            _check_hresult(int(set_value(pointer, ctypes.byref(key), ctypes.byref(variant))))
        _check_hresult(int(commit(pointer)))


def read_taskbar_identity(hwnd: int) -> dict[str, str | bool | None]:
    """Read only this HWND's properties for local native smoke verification."""
    values: dict[str, str | bool | None] = {}
    with _property_store(hwnd) as (pointer, get_value, _set, _commit, clear):
        for name in PROPERTY_NAMES:
            key = _property_key(name)
            variant = _PropVariant()
            try:
                _check_hresult(int(get_value(pointer, ctypes.byref(key), ctypes.byref(variant))))
                if variant.vt == 0:
                    values[name] = None
                elif variant.vt == 31:
                    values[name] = variant.value.text
                elif variant.vt == 11:
                    values[name] = bool(variant.value.boolean)
                else:
                    raise ValueError("Unexpected taskbar property type.")
            finally:
                clear(ctypes.byref(variant))
    return values


def apply_taskbar_identity(
    hwnd: int,
    *,
    member: bool,
    launcher: str | os.PathLike[str] | None,
    product_name: str | None = None,
    icon_path: str | os.PathLike[str] | None = None,
) -> bool:
    """Apply before showing each new/recreated Tk top-level HWND.

    The caller selects the native launcher from its own installation, not
    sys.executable. A missing native launcher deliberately disables pinning.
    An unavailable Windows property API must not stop the caption window.
    """
    try:
        if not hwnd:
            return False
        if launcher is not None and not Path(launcher).is_file():
            return False
        values = taskbar_properties(
            member=member, launcher=launcher, product_name=product_name,
            icon_path=icon_path,
        )
        _write_properties(hwnd, values)
        return True
    except Exception:
        return False


def clear_taskbar_identity(hwnd: int) -> bool:
    """Release this window's property values before destroying its HWND."""
    try:
        if not hwnd:
            return False
        _write_properties(hwnd, tuple(
            (PROPERTY_PREFIX + name, None) for name in PROPERTY_NAMES
        ))
        return True
    except Exception:
        return False
