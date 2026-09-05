# SPDX-License-Identifier: GPL-3.0-only
"""Validate the standalone Vemal's Ears checkout without publishing."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys

from ci.plan_tests import verify_test_roots

ROOT = Path(__file__).resolve().parent
ADAPTER = Path("VoiceCaptions/discord_adapter")


def check_commands(jobs: int):
    return [
        (Path("."), [sys.executable, "-m", "ci.lint_workflows"]),
        (Path("."), [sys.executable, "-m", "ci.check_dependencies"]),
        (Path("."), [sys.executable, "tests/run_all_tests.py", "--jobs", str(jobs),
                     "--report", ".ci-results/python.json"]),
        (ADAPTER, [shutil.which("node") or "node", "--test"]),
    ]


def run_commands(commands, *, root: Path, dry_run: bool) -> int:
    for directory, command in commands:
        print("[" + directory.as_posix() + "] " + subprocess.list2cmdline(command), flush=True)
        if dry_run:
            continue
        try:
            result = subprocess.run(command, cwd=root / directory, check=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            print("Could not launch task: " + str(error), file=sys.stderr)
            return 2
        if result.returncode:
            return result.returncode
    return 0


def positive_integer(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def main(argv=None, *, root=ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="task", required=True)
    check = sub.add_parser("check", help="run the complete Python and Node suites")
    check.add_argument("--jobs", type=positive_integer, default=8)
    check.add_argument("--dry-run", action="store_true")
    sub.add_parser("doctor", help="verify local test prerequisites")
    args = parser.parse_args(argv)
    verify_test_roots(root)
    if args.task == "doctor":
        print("Python: " + sys.executable, flush=True)
        return run_commands([(Path("."), [sys.executable, "-m", "ci.check_dependencies"])],
                            root=root, dry_run=False)
    return run_commands(check_commands(args.jobs), root=root, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
