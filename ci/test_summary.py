"""Surface slow units and skip counts without persisting full test logs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def render(report: dict) -> str:
    if "stages" in report:
        lines = [f"## {report['suite']} test timings", "",
                 f"Exit {report['returncode']}; {report['wall_seconds']:.1f}s wall time; "
                 f"{report['timeout_seconds']}s test timeout.", "", "Stages:"]
        for stage in report["stages"]:
            lines.append(f"- {stage['name']}: {stage['seconds']:.1f}s (exit {stage['returncode']})")
        lines.extend(["", "Stage timings only. Test counts, skips, and individual results remain in the job log."])
        return "\n".join(lines) + "\n"
    units = report["units"]
    lines = ["## Python/Lua test timings", "",
             f"{len(units)} units; {report['tests']} tests; {report['skipped']} skipped; "
             f"{report['wall_seconds']:.1f}s wall time.", ""]
    if "returncode" in report:
        lines.extend([f"Runner exit: {report['returncode']}.", ""])
    if report.get("failure_reasons"):
        lines.append("Runner failures:")
        lines.extend("- " + reason for reason in report["failure_reasons"])
        lines.append("")
    failed = [unit for unit in units if unit["returncode"] != 0]
    if failed:
        lines.append("Failed units:")
        for unit in failed:
            lines.append(f"- {unit['name']}: {unit['seconds']:.1f}s (exit {unit['returncode']})")
        lines.append("")
    lines.append("Slowest units:")
    for unit in sorted(units, key=lambda row: row["seconds"], reverse=True)[:10]:
        lines.append(f"- {unit['name']}: {unit['seconds']:.1f}s (exit {unit['returncode']})")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    path = parser.parse_args(argv).report
    code = 0
    if not path.is_file():
        report = "Test report is absent: see the failed setup/test step.\n"
        code = 1
    else:
        try:
            report = render(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report = "Test report could not be read: " + str(exc) + "\n"
            code = 1
    print(report, end="")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
