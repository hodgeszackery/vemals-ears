"""Run standalone VE Python modules in separate processes.

Use --list to inspect discovery, module-name filters for focused work, and
--report to retain timings. Empty roots, zero-test child successes, worker
failures, and a smaller-than-recorded full suite all fail the command.
The adapter has its own Node runner; python dev.py check runs both.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ci.test_inventory import PYTHON_TEST_ROOTS  # noqa: E402

# The floor. A suite is only worth its word if it notices its own
# absence: before this, moving or renaming a tests directory made
# discover_modules return [], and the run printed a smaller count,
# said OK, and exited 0 with roughly 470 tests never executed.
#
# Raise these deliberately when the suite genuinely grows. Lowering
# one should be a conscious decision with a reason in the commit.
# Standalone VE inventory after the repository split. The original combined
# floor and transferred coverage are recorded in docs/TEST-MIGRATION.txt.
# Each owned root remains mandatory; the Node suite is a separate CI step.
# GPL readiness adds three source/notice modules and exercises archived
# modified builds, legal UI, and retained-runtime refresh contracts.
EXPECTED_UNITS = 62
EXPECTED_TESTS = 1221


def discover_modules(relative: str, prefix: str) -> list[tuple[str, list[str]]]:
    """One unit per test module, so a slow module cannot hide behind a fast one."""
    directory = ROOT / relative
    if not directory.is_dir():
        raise SystemExit(
            "test directory is missing: " + relative + "\n"
            "Nothing would run from it and the suite would still "
            "report OK, which is how a whole package disappears "
            "from a green run."
        )
    units = []
    for path in sorted(directory.glob("test_*.py")):
        if not path.is_file():
            continue
        dotted = prefix + "." + path.stem
        units.append((dotted, [sys.executable, "-m", "unittest", dotted]))
    if not units:
        raise SystemExit(
            "no test_*.py modules under " + relative + "\n"
            "An empty directory reads exactly like a passing one."
        )
    return units


def build_units() -> list[tuple[str, list[str]]]:
    units: list[tuple[str, list[str]]] = []
    for relative, prefix in PYTHON_TEST_ROOTS:
        # Namespace packages work with explicit unittest module names, even
        # without __init__.py. This also gives companion tests a root import
        # path, so the replay suite can import GearHelper without sys.path hacks.
        units += discover_modules(relative, prefix)
    return units


def run_unit(unit: tuple[str, list[str]], timeout: int = 180) -> tuple[str, int, float, str]:
    name, command = unit
    start = time.perf_counter()
    try:
        done = subprocess.run(
            command, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        def as_text(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")
        output = as_text(error.stderr) + as_text(error.stdout)
        return name, 124, time.perf_counter() - start, output + f"\nTIMEOUT after {timeout}s\n"
    except OSError as error:
        return name, 125, time.perf_counter() - start, "Could not launch test unit: " + str(error)
    elapsed = time.perf_counter() - start
    return name, done.returncode, elapsed, (done.stderr or "") + (done.stdout or "")


def count_of(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("Ran ") or "suites passed" in line:
            return line.strip()
    return ""


def reported_tests(output: str) -> int:
    return sum(int(value) for value in re.findall(r"(?m)^Ran (\d+) tests?\b", output))


def validate_result(result):
    """A child exiting zero without running tests cannot pass, even filtered."""
    name, code, seconds, output = result
    if code or reported_tests(output) > 0:
        return result
    return name, 1, seconds, output + "\nRunner refused success without a nonzero unittest test count.\n"


def select_units(units, filters):
    """Units matching any filter, compared case-insensitively.

    Unit names are dotted module paths - "GuildHub.tests.test_store" -
    so the obvious thing to type, `guildhub`, matched nothing at all and
    the runner reported "no test units matched" as though the suite did
    not exist.
    """
    return [
        unit for unit in units
        if any(f.casefold() in unit[0].casefold() for f in filters)
    ]


def write_report(path: str, results, *, wall: float, workers: int,
                 returncode: int | None = None, failure_reasons=()) -> None:
    units = []
    for name, code, seconds, output in results:
        skips = [int(value) for value in re.findall(r"\bskipped=(\d+)\b", output)]
        units.append({"name": name, "returncode": code, "seconds": round(seconds, 4),
                      "tests": reported_tests(output), "skipped": sum(skips)})
    report = {"schema": 1, "wall_seconds": round(wall, 4), "workers": workers,
              "returncode": returncode if returncode is not None else int(any(unit["returncode"] for unit in units)),
              "failure_reasons": list(failure_reasons),
              "tests": sum(unit["tests"] for unit in units),
              "skipped": sum(unit["skipped"] for unit in units),
              "units": sorted(units, key=lambda unit: unit["name"])}
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="ascii")


def positive_integer(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


PYTHON_REGISTRATION = r"Software\Python\PythonCore\3.14\InstallPath"


def python_registration() -> dict[str, str] | None:
    """Every value under the user's registered Python, or None.

    None on any platform without the key, which includes CI and every
    non-Windows machine - there is nothing to protect there.
    """
    try:
        import winreg
    except ImportError:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, PYTHON_REGISTRATION)
    except OSError:
        return None
    values: dict[str, str] = {}
    try:
        index = 0
        while True:
            try:
                name, value, _kind = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            if isinstance(value, str):
                values[name] = value
    finally:
        key.Close()
    return values


def report_python_registration_drift(before, after) -> bool:
    """True when the run moved the machine's registered Python.

    Reports only. This guard damaged the machine twice while being
    built, both times through a restore path: once when a unit test
    handed it synthetic paths, once when a sabotage bypassed an injected
    writer. Detection was always the valuable half - the original
    failure was a suite printing OK while Downtime.exe had been quietly
    repointed - so the writer is gone and nothing here can write to that
    key.
    """
    if before is None or after is None or before == after:
        return False
    print()
    print("THE REGISTERED PYTHON MOVED DURING THIS RUN")
    for name in sorted(set(before) | set(after)):
        was = before.get(name, "(absent)")
        now = after.get(name, "(absent)")
        if was != now:
            label = name or "(default)"
            print("  " + label)
            print("    was: " + was)
            print("    now: " + now)
    print()
    print("  Something reached the Python Install Manager shim while a test")
    print("  had LOCALAPPDATA pointed somewhere temporary, so it installed")
    print("  and registered a fresh interpreter for the whole user.")
    print("  Downtime.exe resolves its interpreter from this registration,")
    print("  so leaving it would break the app long after this run.")
    print("  Put it back before running Downtime, with:")
    for name in sorted(before):
        label = name if name else "(default)"
        print("    %-24s %s" % (label, before[name]))
    print("  Then find what reached the shim - it will happen again.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("filters", nargs="*", help="only units containing these substrings")
    parser.add_argument("--sequential", action="store_true", help="run one at a time")
    parser.add_argument("--jobs", type=positive_integer, default=0, help="worker count (default: twice the CPU count, minimum 8)")
    parser.add_argument("--timeout", type=positive_integer, default=180, help="maximum seconds per unit (default: 180)")
    parser.add_argument("--report", help="write a compact JSON timing and skip report")
    parser.add_argument("--quiet", action="store_true", help="only print the summary and failures")
    parser.add_argument("--list", action="store_true", help="list matching unit names without running tests")
    args = parser.parse_args(argv)

    units = build_units()
    if args.filters:
        units = select_units(units, args.filters)
    if not units:
        print("no test units matched", file=sys.stderr)
        return 2
    if args.list:
        for name, _command in units:
            print(name)
        return 0

    registration_before = python_registration()

    # Modules keep separate process state. Several wait on subprocesses or
    # Tk events, so retain a bounded configurable pool for that overlap.
    workers = 1 if args.sequential else (
        args.jobs or min(len(units), max((os.cpu_count() or 4) * 2, 8))
    )
    started = time.perf_counter()
    results: list[tuple[str, int, float, str]] = []

    if workers == 1:
        for unit in units:
            result = validate_result(run_unit(unit, args.timeout))
            results.append(result)
            if not args.quiet:
                mark = "ok  " if result[1] == 0 else "FAIL"
                print("  %s %-42s %6.1fs  %s" % (mark, result[0], result[2], count_of(result[3])), flush=True)
    else:
        # Test modules already run in isolated child processes. Threads only
        # wait for those children; spawning another layer of Python workers
        # adds Windows startup time and memory without adding isolation.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(run_unit, unit, args.timeout): unit[0] for unit in units}
            for future in as_completed(futures):
                result = validate_result(future.result())
                results.append(result)
                if not args.quiet:
                    mark = "ok  " if result[1] == 0 else "FAIL"
                    print("  %s %-42s %6.1fs  %s" % (mark, result[0], result[2], count_of(result[3])), flush=True)

    wall = time.perf_counter() - started
    serial = sum(r[2] for r in results)
    failures = [r for r in results if r[1] != 0]
    total_tests = sum(reported_tests(output) for _n, _c, _s, output in results)

    # Check the whole run before writing its report: green child exits do not
    # imply success when a suite disappeared or a child moved the registry.
    registration_drifted = report_python_registration_drift(
        registration_before, python_registration()
    )
    shortfall = []
    if not args.filters:
        if len(results) < EXPECTED_UNITS:
            shortfall.append("units: expected at least %d, ran %d" % (EXPECTED_UNITS, len(results)))
        if total_tests < EXPECTED_TESTS:
            shortfall.append("tests: expected at least %d, ran %d" % (EXPECTED_TESTS, total_tests))
    failure_reasons = list(shortfall)
    if failures:
        failure_reasons.append("Failed test units: " + ", ".join(sorted(row[0] for row in failures)))
    if registration_drifted:
        failure_reasons.append("Python registration changed during the run")
    returncode = int(bool(failure_reasons))
    if args.report:
        try:
            write_report(args.report, results, wall=wall, workers=workers,
                         returncode=returncode, failure_reasons=failure_reasons)
        except OSError as error:
            print("Could not write test report: " + str(error), file=sys.stderr)
            returncode = 1

    # Every failing unit prints in full. Knowing that something broke without
    # knowing what is worse than a slow sequential run.
    for name, _code, _elapsed, output in sorted(failures):
        print("\n" + "=" * 72)
        print("FAILED: " + name)
        print("=" * 72)
        print(output.rstrip())

    print("\n" + "-" * 72)
    print("%d units, %d tests, %d workers" % (len(results), total_tests, workers))
    # Concurrent child timings include contention; their sum is not a measured
    # sequential baseline or evidence for a speedup claim.
    print("wall clock %.1fs  (summed unit time %.1fs)" % (wall, serial))
    if failures:
        print("FAILED: " + ", ".join(sorted(name for name, _c, _e, _o in failures)))

    # A filtered run is meant to be smaller, so the floor only applies
    # to a full one.
    if shortfall:
        print("THE SUITE SHRANK:")
        for line in shortfall:
            print("  " + line)
        print("  Passing tests cannot account for missing coverage.")
        print("  If the reduction is deliberate, update EXPECTED_UNITS / EXPECTED_TESTS in this file.")

    if returncode:
        return returncode

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
