"""Every change validates the complete standalone VE test inventory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ci.test_inventory import REQUIRED_TEST_ROOTS

ROOT = Path(__file__).resolve().parents[1]


class MissingTestSuite(RuntimeError):
    """An owned suite disappeared from the checkout."""


def verify_test_roots(root: Path) -> None:
    for relative, pattern in REQUIRED_TEST_ROOTS:
        directory = root / relative
        if not directory.is_dir():
            raise MissingTestSuite("Required test directory is missing: " + relative)
        if not any(path.is_file() for path in directory.glob(pattern)):
            raise MissingTestSuite("Required test directory has no " + pattern + " files: " + relative)


def main(argv=None, *, root=ROOT) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    verify_test_roots(root)
    print(json.dumps({"python": True, "discord": True, "reason": "complete_standalone_inventory"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
