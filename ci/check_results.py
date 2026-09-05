"""Every standalone VE job and the adapter test step must pass."""
from __future__ import annotations

import json
import os
from pathlib import Path


def validate(needs: object) -> list[str]:
    if not isinstance(needs, dict):
        return ["Missing workflow results"]
    errors = []
    for name in ("validate", "python-and-node"):
        job = needs.get(name)
        if not isinstance(job, dict) or job.get("result") != "success":
            errors.append(name + " was required to pass")
    windows = needs.get("python-and-node")
    outputs = windows.get("outputs") if isinstance(windows, dict) else None
    if not isinstance(outputs, dict) or outputs.get("discord") != "success":
        errors.append("Required Discord adapter step did not pass")
    return errors


def main() -> int:
    try:
        needs = json.loads(os.environ.get("CI_NEEDS", ""))
    except (TypeError, ValueError):
        needs = None
    errors = validate(needs)
    report = "## CI result\n\n" + ("FAIL" if errors else "PASS") + "\n\n"
    report += "\n".join("- " + error for error in errors) + "\n"
    print(report, end="")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="ascii") as stream:
            stream.write(report)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
