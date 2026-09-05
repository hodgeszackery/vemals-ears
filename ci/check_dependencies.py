"""CI prerequisites must fail, not silently turn integration tests into skips."""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    for module in ("PIL.Image", "websockets", "yaml"):
        importlib.import_module(module)
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node is required for the Python/adapter integration test")
    adapter = ROOT / "VoiceCaptions/discord_adapter"
    package = adapter / "node_modules/opusscript/package.json"
    if not package.is_file() or json.loads(package.read_text(encoding="utf-8")).get("version") != "0.0.8":
        raise SystemExit("Locked opusscript must be installed; the PCM contract must not skip")
    subprocess.run([node, "--input-type=module", "--eval", "import 'opusscript';"],
                   cwd=adapter, check=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print("Python, image, websocket, and Node PCM dependencies are present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
