"""Preserve and check the upstream notices in the private Python runtime.

The documentation tree contains incorporated-software notices that are not
all present in CPython's root LICENSE.txt. Keep their original bytes before
either runtime builder removes documentation to reduce the download size.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCORPORATED_SOURCE = "Doc/html/_sources/license.rst.txt"
INCORPORATED_NOTICE = "PYTHON-INCORPORATED-LICENSES.txt"

# These are the actual license artifacts in the locked wheels, including
# pywin32's component licenses. A new dependency needs an explicit audit.
WHEEL_LICENSE_FILES = {
    "pillow": ("licenses/LICENSE",),
    "websockets": ("LICENSE",),
    "pywin32": (
        "licenses/adodbapi/license.txt",
        "licenses/com/License.txt",
        "licenses/com/win32comext/mapi/src/MAPIStubLibrary/LICENSE",
        "licenses/isapi/README.txt",
        "licenses/pythonwin/License.txt",
        "licenses/pythonwin/Scintilla/License.txt",
        "licenses/pythonwin/pywin/idle/LICENSE.txt",
        "licenses/win32/License.txt",
    ),
}


def required_license_paths(lock: dict) -> tuple[str, ...]:
    paths = ["LICENSE.txt", INCORPORATED_NOTICE]
    for wheel in lock.get("wheels", ()):
        name = wheel["name"].lower().replace("-", "_")
        if name not in WHEEL_LICENSE_FILES:
            raise SystemExit("runtime license audit is missing for wheel: " + name)
        prefix = "Lib/site-packages/" + name + "-" + wheel["version"] + ".dist-info/"
        paths.extend(prefix + relative for relative in WHEEL_LICENSE_FILES[name])
    return tuple(paths)


def require_nonempty(path: Path) -> None:
    if not path.is_file() or not path.read_bytes().strip():
        raise SystemExit("required upstream runtime license is missing or empty: " + str(path))


def preserve_incorporated_notice(runtime: Path) -> None:
    source = runtime / INCORPORATED_SOURCE
    require_nonempty(source)
    shutil.copyfile(source, runtime / INCORPORATED_NOTICE)


def validate_runtime_licenses(runtime: Path, lock: dict) -> None:
    for relative in required_license_paths(lock):
        require_nonempty(runtime / relative)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--lock", type=Path, default=ROOT / "Installer" / "runtime-lock.json")
    parser.add_argument("--preserve", action="store_true")
    args = parser.parse_args(argv)
    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    if args.preserve:
        preserve_incorporated_notice(args.runtime)
    validate_runtime_licenses(args.runtime, lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
