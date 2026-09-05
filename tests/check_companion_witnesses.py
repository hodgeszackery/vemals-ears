# SPDX-License-Identifier: GPL-3.0-only
"""Optional regression witnesses for the companion behavior-test migration.

Run from the repository root with:
    python -m tests.check_companion_witnesses --json .ci-results/companion-witnesses.json

Each selected test must pass unchanged and assertion-fail for its specific
in-memory regression. This is not part of normal test discovery. The badge
witness needs native Tk; a skipped test never counts as a successful witness.
"""

import argparse
import ast
import hashlib
import inspect
import io
import json
from pathlib import Path
import sys
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.companion import load_companion


# Keep this list specific to the behaviors migrated in this batch.
WITNESSES = (
    ("hide_notice_removed", "hide",
     "tests.test_toast.NotifyTests.test_the_minimise_notice_goes_through_it"),
    ("hide_notice_bypasses_shared_channel", "hide",
     "tests.test_toast.NotifyTests.test_the_minimise_notice_goes_through_it"),
    ("tick_continues_after_quit", "tick",
     "tests.test_companion_quit.QuitSignalTests.test_tick_stops_after_a_fresh_quit_request"),
    ("quit_acknowledgement_removed", "consume_quit_request",
     "tests.test_companion_quit.QuitSignalTests.test_fresh_request_is_acknowledged_before_cleanup"),
    ("startup_retires_other_product", "main",
     ("tests.test_companion_startup.IndependentInstallTests.test_startup_has_no_retirement_step",
      "tests.test_companion_startup.IndependentInstallTests.test_second_launch_has_no_retirement_step")),
    ("result_badge_width_drift", "build_row",
     "tests.test_companion_status_badges.StatusBadgeSizeTests."
     "test_result_and_lifecycle_boxes_are_constructed_at_the_same_size"),
    ("lifecycle_badge_width_drift", "build_row",
     "tests.test_companion_status_badges.StatusBadgeSizeTests."
     "test_result_and_lifecycle_boxes_are_constructed_at_the_same_size"),
)


def is_call(node, owner, name):
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    if owner is None:
        return isinstance(function, ast.Name) and function.id == name
    return (isinstance(function, ast.Attribute) and function.attr == name
            and isinstance(function.value, ast.Name) and function.value.id == owner)


class Regression(ast.NodeTransformer):
    def __init__(self, name):
        self.name = name
        self.changes = 0

    def visit_Expr(self, node):
        if (self.name.startswith("hide_notice_")
                and is_call(node.value, "self", "notify")):
            self.changes += 1
            if self.name == "hide_notice_removed":
                return ast.copy_location(ast.Pass(), node)
            node.value.func.value = ast.Attribute(
                value=ast.Name(id="self", ctx=ast.Load()), attr="tray", ctx=ast.Load(),
            )
        elif (self.name == "quit_acknowledgement_removed"
                and is_call(node.value, "QUIT_SIGNAL", "unlink")):
            self.changes += 1
            return ast.copy_location(ast.Pass(), node)
        return self.generic_visit(node)

    def visit_If(self, node):
        if (self.name == "tick_continues_after_quit"
                and is_call(node.test, "self", "consume_quit_request")):
            for index, statement in enumerate(node.body):
                if isinstance(statement, ast.Return) and statement.value is None:
                    node.body[index] = ast.copy_location(ast.Pass(), statement)
                    self.changes += 1
        return self.generic_visit(node)

    def visit_FunctionDef(self, node):
        if self.name == "startup_retires_other_product" and node.name == "main":
            for index, statement in enumerate(node.body):
                if (isinstance(statement, ast.Assign)
                        and is_call(statement.value, None, "claim_instance_mutex")):
                    node.body.insert(index, ast.Expr(value=ast.Call(
                        func=ast.Name(id="retire_standalone_vemals_ears", ctx=ast.Load()),
                        args=[], keywords=[],
                    )))
                    self.changes += 1
                    break
        return self.generic_visit(node)

    def visit_Assign(self, node):
        badge = {"result_badge_width_drift": "result_badge",
                 "lifecycle_badge_width_drift": "lifecycle_badge"}.get(self.name)
        if (badge
                and any(isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "service"
                        and target.attr == badge for target in node.targets)
                and is_call(node.value, "tk", "Label")):
            for keyword in node.value.keywords:
                if keyword.arg == "width":
                    keyword.value = ast.Constant(value=12)
                    self.changes += 1
        return self.generic_visit(node)


def run_tests(names):
    output = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(stream=output).run(suite)
    return result, output.getvalue()


def check_witness(app, name, method_name, test_name):
    owner = app if method_name == "main" else app.CompanionWindow
    original = getattr(owner, method_name)
    test_names = [test_name] if isinstance(test_name, str) else list(test_name)
    baseline, baseline_output = run_tests(test_names)
    if not (baseline.wasSuccessful() and baseline.testsRun == len(test_names)
            and not baseline.skipped):
        raise RuntimeError("Clean control did not pass: " + name + "\n" + baseline_output)

    change = Regression(name)
    tree = change.visit(ast.parse(textwrap.dedent(inspect.getsource(original))))
    if change.changes != 1:
        raise RuntimeError(f"{name}: expected one mutation site, found {change.changes}")
    namespace = {}
    exec(compile(ast.fix_missing_locations(tree), "<companion-witness:" + name + ">", "exec"),
         app.__dict__, namespace)
    with mock.patch.object(owner, method_name, namespace[method_name]):
        mutant, mutant_output = run_tests(test_names)
    failure_ids = [test.id() for test, _traceback in mutant.failures]
    all_tests_caught = all(
        any(failed == selected or failed.startswith(selected + " (") for failed in failure_ids)
        for selected in test_names
    )
    if not (mutant.testsRun == len(test_names) and all_tests_caught and not mutant.errors
            and not mutant.skipped and not mutant.unexpectedSuccesses):
        raise RuntimeError(name + ": regression was not caught by assertions\n" + mutant_output)
    print(f"{name}: control passed; {len(mutant.failures)} assertion failure(s), 0 errors")
    return {
        "name": name, "tests": test_names, "control_passed": True,
        "assertion_failures": len(mutant.failures), "errors": 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="Optional JSON report path")
    options = parser.parse_args(argv)
    source_path = ROOT / "WoW-Companion.pyw"
    if options.json and options.json.resolve() == source_path.resolve():
        parser.error("The JSON report cannot replace the production source")
    source_before = source_path.read_bytes()
    report = {"witnesses": [], "source_unchanged": False, "passed": False}
    try:
        app = load_companion()
        for witness in WITNESSES:
            report["witnesses"].append(check_witness(app, *witness))
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        print(str(error), file=sys.stderr)
    finally:
        source_after = source_path.read_bytes()
        report["source_unchanged"] = source_after == source_before
        report["source_sha256_before"] = hashlib.sha256(source_before).hexdigest()
        report["source_sha256_after"] = hashlib.sha256(source_after).hexdigest()
        if not report["source_unchanged"]:
            report["passed"] = False
            print("Production source changed while witnesses ran", file=sys.stderr)
    if options.json:
        options.json.parent.mkdir(parents=True, exist_ok=True)
        options.json.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n",
                                encoding="ascii")
    print(f"{len(report['witnesses'])}/{len(WITNESSES)} witnesses caught; "
          f"production source unchanged: {report['source_unchanged']}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
