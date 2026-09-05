"""Remove only Vemal's Ears member credentials and caption-local state."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))


def _remove_regular_file(path: Path) -> bool:
    try:
        if path.is_file() and not path.is_symlink():
            path.unlink()
    except OSError:
        return False
    return True


def main() -> int:
    clean = True
    try:
        from GuildHub.credentials import WindowsDeviceCredentialStore

        WindowsDeviceCredentialStore().delete()
    except Exception:
        clean = False

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data)
        clean = _remove_regular_file(
            root / "VemalsEars" / "vemals-ears-paired.signal"
        ) and clean
        # quit_now() is the only place the lock is unlinked, so a
        # force-killed companion always leaves a stale one - and a stale
        # lock naming a recycled pid refuses future launches.
        clean = _remove_regular_file(
            root / "VemalsEars" / "companion-window.lock"
        ) and clean
        clean = _remove_regular_file(
            root / "VemalsEars" / "companion-quit.request"
        ) and clean
        clean = _remove_regular_file(
            root / "WoWVoiceCaptions" / "overlay-settings.json"
        ) and clean
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
