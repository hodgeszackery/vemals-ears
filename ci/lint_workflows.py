"""Run a hash-pinned official actionlint binary without a Go build or cache."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import platform
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.7.12"
ASSETS = {
    "Linux": ("linux_amd64.tar.gz", "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"),
    "Windows": ("windows_amd64.zip", "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9"),
}
MAX_DOWNLOAD = 32 * 1024 * 1024


def verified_binary(payload: bytes, *, system: str) -> bytes:
    if len(payload) > MAX_DOWNLOAD:
        raise ValueError("Oversized actionlint archive")
    _asset, digest = ASSETS[system]
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("actionlint archive SHA256 mismatch")
    # Extract only a named regular executable into our temporary directory.
    # Never extract archive-supplied paths or reuse an unverified executable.
    if system == "Windows":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            info = archive.getinfo("actionlint.exe")
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.is_dir() or info.external_attr & 0x10 or kind not in (0, stat.S_IFREG):
                raise ValueError("Unexpected actionlint executable")
            if not 0 < info.file_size <= MAX_DOWNLOAD:
                raise ValueError("Oversized or empty actionlint executable")
            with archive.open(info) as stream:
                binary = stream.read(MAX_DOWNLOAD + 1)
            if len(binary) != info.file_size or len(binary) > MAX_DOWNLOAD:
                raise ValueError("Unexpected actionlint executable size")
            return binary
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        member = archive.getmember("actionlint")
        if not member.isfile() or not 0 < member.size <= MAX_DOWNLOAD:
            raise ValueError("Unexpected actionlint executable")
        with archive.extractfile(member) as stream:
            binary = stream.read(MAX_DOWNLOAD + 1)
        if len(binary) != member.size or len(binary) > MAX_DOWNLOAD:
            raise ValueError("Unexpected actionlint executable size")
        return binary


def main() -> int:
    system = platform.system()
    if system not in ASSETS or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SystemExit("actionlint is pinned for x64 Linux and Windows only")
    asset, _digest = ASSETS[system]
    url = "https://github.com/rhysd/actionlint/releases/download/v" + VERSION + "/actionlint_" + VERSION + "_" + asset
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read(MAX_DOWNLOAD + 1)
    if len(payload) > MAX_DOWNLOAD:
        raise SystemExit("Oversized actionlint download")
    binary = verified_binary(payload, system=system)
    workflows = sorted(
        path for path in (ROOT / ".github/workflows").iterdir()
        if path.is_file() and path.suffix in (".yml", ".yaml")
    )
    if not workflows:
        raise SystemExit("No workflows found")
    with tempfile.TemporaryDirectory(prefix="downtime-actionlint-") as directory:
        executable = Path(directory) / ("actionlint.exe" if system == "Windows" else "actionlint")
        executable.write_bytes(binary)
        executable.chmod(0o700)
        result = subprocess.run([str(executable), "-color", "-shellcheck=", "-pyflakes=",
                                 *(str(path) for path in workflows)], cwd=ROOT,
                                timeout=60, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
