"""Service entry point for the caption worker: the Discord bot plus STT.

Loads relay.env, derives the worker settings the host contract expects, and
runs the watch loop. The publisher connects to the relay over loopback -
same box, no reason to round-trip the public edge - and the Discord
credentials arrive as secret FILES the console writes, because a LocalSystem
service cannot read a user's Windows credential vault.
"""

from __future__ import annotations

import os
import sys

if __package__ in {None, ""}:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from GuildHub import host, relay_env


def main() -> int:
    values = relay_env.apply()
    # The worker publishes to the relay on this same machine. Loopback plain
    # ws is the case the relay's bind rules deliberately allow.
    os.environ.setdefault(
        "VEMALS_EARS_SERVER_URL",
        "ws://127.0.0.1:" + values.get("VEMALS_EARS_PORT", "8770"),
    )
    return host.main(["--watch"])


if __name__ == "__main__":
    sys.exit(main())
