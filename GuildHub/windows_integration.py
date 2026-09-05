"""Current-user Windows registration for independent companion pairing links.

The public URL scheme is ``vemalsears://``. Registration is deliberately
limited to HKCU so installing the companion never requires administrator
rights. The incoming URL is passed as one quoted argument and is never read or
stored by this module.
"""

from __future__ import annotations

import ntpath
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


URL_SCHEME = "vemalsears"
URL_PREFIX = URL_SCHEME + "://"
PAIR_ARGUMENT = "--vemals-ears-pair"
CLASSES_KEY = "Software\\Classes\\" + URL_SCHEME
DISPLAY_NAME = "URL:Vemal's Ears Pairing Link"
DOWNTIME_URL_SCHEME = "downtime"
DOWNTIME_URL_PREFIX = DOWNTIME_URL_SCHEME + "://"
DOWNTIME_PAIR_ARGUMENT = "--downtime-pair"
DOWNTIME_CLASSES_KEY = "Software\\Classes\\" + DOWNTIME_URL_SCHEME
DOWNTIME_DISPLAY_NAME = "URL:Downtime Pairing Link"


class WindowsIntegrationError(RuntimeError):
    """Vemal's Ears could not safely install its Windows link handler."""


class RegistryApi(Protocol):
    """Small injectable registry boundary used by the registration routine."""

    def set_string(self, key_path: str, value_name: str, value: str) -> None: ...


@dataclass(frozen=True)
class RegistryValue:
    key_path: str
    value_name: str
    value: str


def _product(app: str) -> tuple[str, str, str, str]:
    if app == URL_SCHEME:
        return CLASSES_KEY, DISPLAY_NAME, PAIR_ARGUMENT, "Vemal's Ears"
    if app == DOWNTIME_URL_SCHEME:
        return (
            DOWNTIME_CLASSES_KEY, DOWNTIME_DISPLAY_NAME,
            DOWNTIME_PAIR_ARGUMENT, "Downtime",
        )
    raise WindowsIntegrationError("The companion application identity is invalid.")


def _application_path(value: str | os.PathLike[str], field: str) -> str:
    try:
        raw = os.fspath(value)
    except TypeError:
        raise WindowsIntegrationError(
            "Vemal's Ears could not register links because its application path is invalid."
        ) from None
    if not isinstance(raw, str):
        raise WindowsIntegrationError(
            "Vemal's Ears could not register links because its application path is invalid."
        )
    clean = raw.strip()
    if (
        not clean
        or not ntpath.isabs(clean)
        or '"' in clean
        or any(ord(character) < 32 for character in clean)
    ):
        raise WindowsIntegrationError(
            "Vemal's Ears could not register links because its " + field + " path is invalid."
        )
    return ntpath.normpath(clean)


def _quote_windows_argument(value: str) -> str:
    """Quote one argument using Windows CommandLineToArgvW escaping rules."""

    rendered = ['"']
    backslashes = 0
    for character in value:
        if character == "\\":
            backslashes += 1
            continue
        if character == '"':
            rendered.append("\\" * (backslashes * 2 + 1))
            rendered.append('"')
            backslashes = 0
            continue
        rendered.append("\\" * backslashes)
        rendered.append(character)
        backslashes = 0
    rendered.append("\\" * (backslashes * 2))
    rendered.append('"')
    return "".join(rendered)


def _default_pythonw_path() -> str:
    executable = Path(sys.executable)
    return str(executable.with_name("pythonw.exe"))


def build_open_command(
    application_path: str | os.PathLike[str],
    *,
    pythonw_path: str | os.PathLike[str] | None = None,
    app: str = URL_SCHEME,
) -> str:
    """Build the registry command for a source .pyw or frozen .exe companion."""

    _key, _display, pair_argument, _name = _product(app)
    application = _application_path(application_path, "application")
    suffix = ntpath.splitext(application)[1].lower()
    if suffix == ".exe":
        command = _quote_windows_argument(application)
    elif suffix == ".pyw":
        pythonw = _application_path(
            _default_pythonw_path() if pythonw_path is None else pythonw_path,
            "pythonw",
        )
        if ntpath.basename(pythonw).lower() != "pythonw.exe":
            raise WindowsIntegrationError(
                "Vemal's Ears could not register links because pythonw.exe is unavailable."
            )
        command = (
            _quote_windows_argument(pythonw)
            + " -E -s -B "
            + _quote_windows_argument(application)
        )
    else:
        raise WindowsIntegrationError(
            "Vemal's Ears can register links only for its companion application."
        )
    return command + " " + pair_argument + " " + _quote_windows_argument("%1")


def build_default_icon(
    application_path: str | os.PathLike[str],
    *,
    icon_path: str | os.PathLike[str] | None = None,
) -> str:
    """Build the DefaultIcon value, using icon resource zero by default."""

    selected = _application_path(
        application_path if icon_path is None else icon_path,
        "icon",
    )
    return _quote_windows_argument(selected) + ",0"


def registration_plan(
    application_path: str | os.PathLike[str],
    *,
    pythonw_path: str | os.PathLike[str] | None = None,
    icon_path: str | os.PathLike[str] | None = None,
    app: str = URL_SCHEME,
) -> tuple[RegistryValue, ...]:
    """Return every HKCU value needed for the Vemal's Ears URL protocol."""

    classes_key, display_name, _argument, _name = _product(app)
    command = build_open_command(application_path, pythonw_path=pythonw_path, app=app)
    icon = build_default_icon(application_path, icon_path=icon_path)
    # Install the command first and URL Protocol last. A first-time failed
    # registration therefore doesn't advertise a handler with no command.
    return (
        RegistryValue(classes_key + "\\shell\\open\\command", "", command),
        RegistryValue(classes_key + "\\DefaultIcon", "", icon),
        RegistryValue(classes_key, "", display_name),
        RegistryValue(classes_key, "URL Protocol", ""),
    )


class CurrentUserRegistry:
    """Production registry adapter rooted at HKEY_CURRENT_USER."""

    def __init__(self, winreg_module: object | None = None) -> None:
        if winreg_module is None:
            if os.name != "nt":
                raise WindowsIntegrationError(
                    "Vemal's Ears link setup is available only on Windows."
                )
            try:
                import winreg as winreg_module
            except ImportError:
                raise WindowsIntegrationError(
                    "Windows could not open link settings for Vemal's Ears."
                ) from None
        self._winreg = winreg_module

    def set_string(self, key_path: str, value_name: str, value: str) -> None:
        registry = self._winreg
        with registry.CreateKeyEx(
            registry.HKEY_CURRENT_USER,
            key_path,
            0,
            registry.KEY_WRITE,
        ) as key:
            registry.SetValueEx(key, value_name, 0, registry.REG_SZ, value)


def register_current_user_protocol(
    application_path: str | os.PathLike[str],
    *,
    pythonw_path: str | os.PathLike[str] | None = None,
    icon_path: str | os.PathLike[str] | None = None,
    registry: RegistryApi | None = None,
    app: str = URL_SCHEME,
) -> tuple[RegistryValue, ...]:
    """Install or refresh the current user's ``vemalsears://`` handler."""

    _key, _display, _argument, product_name = _product(app)
    try:
        plan = registration_plan(
            application_path,
            pythonw_path=pythonw_path,
            icon_path=icon_path,
            app=app,
        )
        target = CurrentUserRegistry() if registry is None else registry
        for entry in plan:
            target.set_string(entry.key_path, entry.value_name, entry.value)
        return plan
    except WindowsIntegrationError:
        raise
    except Exception:
        # Registry exceptions can include the local username or path. The UI
        # needs an actionable message, not those machine details.
        raise WindowsIntegrationError(
            "Windows could not register " + product_name
            + " pairing links for this account."
        ) from None
