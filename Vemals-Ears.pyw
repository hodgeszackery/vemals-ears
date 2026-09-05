"""Launch Vemal's Ears with its own voice UI and Windows identity."""

from pathlib import Path
import runpy


def main() -> int:
    application = runpy.run_path(str(Path(__file__).with_name("WoW-Companion.pyw")))
    return application["main"](edition=application["MEMBER_EDITION"])


if __name__ == "__main__":
    raise SystemExit(main())
