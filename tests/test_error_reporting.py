"""An error message must name the thing that actually failed.

This exists because of a real incident. GuildHub/host.py caught a generic
PublisherError and printed "Vemal's Ears caption server is unavailable".
That exception type is raised in a dozen places with eleven different
messages, and the one that fired said the QUICK-CHAT relay was
unavailable - a different subsystem, failing for a different reason.

The caption server was fine and proved it three ways. Meanwhile the
worker crash-looped 357 times and three separate investigations went at
the component the log named, because it named it confidently.

The guard below is deliberately repo-wide rather than a test of that one
handler: the next handler to do this will be somewhere else.
"""

from __future__ import annotations

import ast
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_PARTS = {".git", "__pycache__", "dist", "node_modules", "runtime"}

# Reporting calls whose literal text reaches a person.
REPORTING = {"print", "info", "warning", "error", "exception", "critical"}

# Long enough to be a sentence rather than a label or a format fragment.
MEANINGFUL = 12

# The defect is a handler asserting a specific FAILURE it cannot know, so
# the message has to read like one before the count of causes matters.
#
# Without this the guard also fires on control-flow exceptions driving
# ordinary status lines - boss_talents_auto catches NoPendingRequest, a
# signal meaning "everything readable is already handled", and announces
# "result loaded by WoW. Ready for another request." That claims no
# cause, so there is nothing for it to get wrong.
FAILURE_WORDS = (
    "error",
    "failed",
    "failure",
    "unavailable",
    "could not",
    "cannot",
    "can not",
    "unable",
    "invalid",
    "missing",
    "not installed",
    "denied",
    "refused",
    "timed out",
)


def asserts_a_failure(message: str) -> bool:
    lowered = message.casefold()
    return any(word in lowered for word in FAILURE_WORDS)


def source_files() -> list[Path]:
    found = []
    for pattern in ("*.py", "*.pyw"):
        for path in ROOT.rglob(pattern):
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            if "tests" in path.parts:
                continue
            found.append(path)
    return sorted(found)


def parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None


def raised_messages() -> dict[str, set[str]]:
    """Exception type -> the distinct literal messages it is raised with."""
    messages: dict[str, set[str]] = defaultdict(set)
    for path in source_files():
        tree = parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            call = node.exc
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            if call.args and isinstance(call.args[0], ast.Constant):
                value = call.args[0].value
                if isinstance(value, str) and value:
                    messages[call.func.id].add(value)
    return messages


def reported_literal(handler: ast.ExceptHandler) -> str | None:
    for node in ast.walk(handler):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name not in REPORTING:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if len(arg.value) >= MEANINGFUL:
                    return arg.value
            if isinstance(arg, ast.JoinedStr):
                literal = "".join(
                    piece.value for piece in arg.values
                    if isinstance(piece, ast.Constant)
                    and isinstance(piece.value, str)
                )
                if len(literal) >= MEANINGFUL:
                    return literal
    return None


def mentions(handler: ast.ExceptHandler) -> bool:
    """Does the handler use the exception it caught?"""
    if handler.name is None:
        return False
    return any(
        isinstance(node, ast.Name) and node.id == handler.name
        for node in ast.walk(handler)
    )


class ErrorsNameWhatFailedTests(unittest.TestCase):
    def test_no_handler_substitutes_a_message_it_cannot_know(self):
        """A handler must not assert a cause it has no way to know.

        Flagged when ALL of these hold:
          - it catches an exception type raised with several different
            messages, so the handler cannot tell which one it has;
          - it reports a sentence to a person;
          - it never mentions the exception it caught.

        The fix is almost always one line: report str(error), or catch a
        narrower type that only means the thing being claimed.
        """
        messages = raised_messages()
        offenders = []
        for path in source_files():
            tree = parse(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                if isinstance(node.type, ast.Name):
                    caught = [node.type.id]
                elif isinstance(node.type, ast.Tuple):
                    caught = [
                        element.id for element in node.type.elts
                        if isinstance(element, ast.Name)
                    ]
                else:
                    continue
                if not caught:
                    continue
                literal = reported_literal(node)
                if literal is None or mentions(node):
                    continue
                if not asserts_a_failure(literal):
                    continue
                distinct: set[str] = set()
                for name in caught:
                    distinct |= messages.get(name, set())
                if len(distinct) > 1:
                    offenders.append(
                        "%s:%d catches %s and prints %r, but that type "
                        "carries %d different messages"
                        % (
                            path.relative_to(ROOT).as_posix(),
                            node.lineno,
                            "/".join(caught),
                            literal[:60],
                            len(distinct),
                        )
                    )
        self.assertEqual(
            [], offenders,
            "an error message names a cause the handler cannot know:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_guard_can_actually_see_the_pattern(self):
        """A guard that cannot fire is worse than none.

        Rebuild the exact shape from the incident and confirm the
        detector flags it, so this suite cannot pass by looking at
        nothing.
        """
        sample = (
            "class Boom(RuntimeError):\n"
            "    pass\n"
            "def a():\n"
            "    raise Boom('the caption server is unavailable')\n"
            "def b():\n"
            "    raise Boom('the quick-chat relay is unavailable')\n"
            "def c():\n"
            "    try:\n"
            "        a()\n"
            "    except Boom:\n"
            "        print('Error: the caption server is unavailable')\n"
        )
        tree = ast.parse(sample)
        handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
        self.assertEqual(1, len(handlers))
        self.assertIsNotNone(
            reported_literal(handlers[0]),
            "the detector no longer sees a reported message",
        )
        self.assertFalse(
            mentions(handlers[0]),
            "the detector no longer notices the exception is unused",
        )

    def test_a_success_message_on_a_control_flow_exception_is_accepted(self):
        """Not every caught exception is an error being reported.

        boss_talents_auto catches NoPendingRequest - a signal meaning
        everything readable is already handled - and announces a result
        was loaded. That asserts no cause, so the guard must ignore it.
        Without this the check fires on ordinary control flow and gets
        turned off, which is the usual death of a useful guard.
        """
        self.assertFalse(
            asserts_a_failure(
                "BossTalents result loaded by WoW. Ready for another request."
            )
        )
        self.assertTrue(
            asserts_a_failure("Error: the caption server is unavailable")
        )
        self.assertTrue(
            asserts_a_failure("Gear Helper could not write the result")
        )

    def test_a_handler_that_reports_the_real_error_is_accepted(self):
        """The guard must not punish the correct shape."""
        sample = (
            "def c():\n"
            "    try:\n"
            "        pass\n"
            "    except ValueError as error:\n"
            "        print('Error: ' + str(error))\n"
        )
        tree = ast.parse(sample)
        handler = next(
            n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)
        )
        self.assertTrue(
            mentions(handler),
            "a handler that does report the real error would be flagged",
        )


if __name__ == "__main__":
    unittest.main()
