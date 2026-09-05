# SPDX-License-Identifier: GPL-3.0-only
"""Companion test loading and temporary product configuration.

These fixtures change process globals and environment variables. Use them
serially within a test process; parallel test modules use separate processes.
The process identity setter is mocked. Tk windows, services, credentials,
registry access and mutex handles remain
the caller's responsibility to mock or clean up.
"""

from contextlib import contextmanager, ExitStack
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MODULE_NAME = "wow_companion_test_module"
_COMPANION_MODULE = None

# configure_application owns these process-local identity and filesystem
# values. Restore the exact snapshot, including the initial ACTIVE_EDITION=None.
APPLICATION_GLOBALS = (
    "ACTIVE_EDITION", "STATE_DIR", "LOCK", "SETTINGS_FILE", "ICON_FILE",
    "MEMBER_ICON_FILE", "HOSTED_ICON_FILE", "PAIR_SIGNAL", "QUIT_SIGNAL",
    "INSTANCE_MUTEX_NAME", "TRAY_WINDOW_CLASS",
)


def load_companion():
    """Load the .pyw once without importing any test cases.

    Import creates a native watcher job. Keeping one module avoids repeatedly
    allocating that resource or resetting state between test classes. Register
    it before execution so dataclasses can resolve their module. A failed
    import must leave neither a cached success nor a partial registry entry.
    """
    global _COMPANION_MODULE
    if _COMPANION_MODULE is not None:
        return _COMPANION_MODULE
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    loader = importlib.machinery.SourceFileLoader(MODULE_NAME, str(ROOT / "WoW-Companion.pyw"))
    spec = importlib.util.spec_from_loader(MODULE_NAME, loader)
    module = importlib.util.module_from_spec(spec)
    missing = object()
    previous = sys.modules.get(MODULE_NAME, missing)
    sys.modules[MODULE_NAME] = module
    try:
        loader.exec_module(module)
    except BaseException:
        if previous is missing:
            sys.modules.pop(MODULE_NAME, None)
        else:
            sys.modules[MODULE_NAME] = previous
        raise
    _COMPANION_MODULE = module
    return module


@contextmanager
def isolated_companion(edition="member"):
    """Yield the shared module configured in fresh, temporary data directories.

    Member setup can migrate preferences immediately, so patch both data
    roots and register restoration before configuring. PROGRAMDATA also
    controls whether previews expose the locally installed operator console.
    Nested contexts get independent directories and restore their outer state.
    The process-wide Windows identity setter is mocked before configuration;
    restoring Python globals cannot undo that native process side effect.
    """
    module = load_companion()
    with ExitStack() as stack:
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="companion-test-")))
        local = folder / "local"
        program = folder / "program"
        local.mkdir()
        program.mkdir()
        stack.enter_context(mock.patch.dict(os.environ, {
            "LOCALAPPDATA": str(local), "PROGRAMDATA": str(program),
        }))
        for name in APPLICATION_GLOBALS:
            stack.enter_context(mock.patch.object(module, name, getattr(module, name)))
        stack.enter_context(mock.patch.object(module, "set_windows_app_identity", return_value=True))
        module.configure_application(edition)
        yield module
