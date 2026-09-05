"""Build, verify and publish a member release to the guild relay.

Deploying used to be eight steps in a required order, several of which
fail silently if skipped: a bundle built without the runtime still runs
here, an installer published without bumping the advertised version
offers nobody anything, and a half-copied download is served to whoever
asks during the copy.

Refuses rather than half-publishes. Every check that can be made before
anything member-facing changes is made first.

    python packaging/publish_release.py --version 0.2.0
    python packaging/publish_release.py --version 0.2.0 --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
INSTALLER = DIST / "Vemals-Ears-Setup.exe"
CONSOLE = "http://127.0.0.1:8771"
CSRF_HEADER = "X-Vemals-Ears-Console"


def run(label: str, command: list[str], **kwargs) -> None:
    print("==> " + label)
    result = subprocess.run(command, cwd=str(ROOT), **kwargs)
    if result.returncode != 0:
        raise SystemExit(label + " failed; nothing was published.")


def console(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        CONSOLE + path,
        data=json.dumps(payload).encode("ascii"),
        headers={"Content-Type": "application/json", CSRF_HEADER: "1"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


# The number lives in GuildHub/version.py, which exists so there is one
# line to rewrite. client.py only re-exports it now, so reading or
# writing client.py here would find an import statement and silently do
# nothing - which is how the copy in GuildHub/__init__.py sat at 0.1.0
# through two releases.
VERSION_SOURCE = "GuildHub/version.py"


def client_version() -> str:
    """The version the CLIENT reports, which is what members compare."""
    source = (ROOT / VERSION_SOURCE).read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith("CLIENT_VERSION"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("could not read CLIENT_VERSION from " + VERSION_SOURCE)


def stamped_version(path: Path) -> str:
    """The FileVersion Windows shows in the executable's properties.

    Read through PowerShell rather than a parser: this is the same field
    a member sees in right-click Properties, and on an unsigned installer
    it is the only reassurance they get. Returns "" when it cannot be
    read, which the caller treats as a failure rather than a pass.
    """
    finished = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "(Get-Item -LiteralPath '" + str(path) + "').VersionInfo.FileVersion",
        ],
        capture_output=True,
        text=True,
    )
    if finished.returncode != 0:
        return ""
    return (finished.stdout or "").strip()


def publish(
    version: str,
    dry_run: bool,
    advertise: bool = True,
    sign_thumbprint: str = "",
    expected_publisher: str = "",
    timestamp_url: str = "",
    downloads: Path | None = None,
) -> int:
    if not version.replace(".", "").isdigit() or version.count(".") != 2:
        raise SystemExit("version must look like 0.2.0")

    # Version changes travel through review before a release is built.
    if client_version() != version:
        raise SystemExit("GuildHub/version.py must already match the requested VE release version.")

    run("staging the runtime", [sys.executable, "packaging/build_runtime.py"])
    run("building the bundles", [
        sys.executable, "build_release.py",
        "--member", "--require-runtime",
    ])
    run("running the bundles as a member would",
        [sys.executable, "packaging/verify_bundle.py"])
    # The executable stamp must match this product's independent version.
    # Unsigned remains the default until a certificate exists. When one
    # does, it is a certificate in the current user's Windows store -
    # every token and cloud HSM presents that way - and signing is one
    # thumbprint away. No .pfx, no password, nothing typed into a log.
    build = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "Installer/build_installer.ps1", "-Version", version,
    ]
    if sign_thumbprint:
        if not expected_publisher:
            raise SystemExit(
                "--sign-thumbprint needs --expected-publisher: the exact "
                "subject on the certificate, so a wrong one is refused"
            )
        build += [
            "-Signed", "-SigningProvider", "store",
            "-SigningThumbprint", sign_thumbprint,
            "-ExpectedPublisher", expected_publisher,
        ]
        if timestamp_url:
            build += ["-SigningTimestampUrl", timestamp_url]
    else:
        build.append("-AllowUnsigned")
    run("building the installer", build)

    if not INSTALLER.is_file():
        raise SystemExit("no installer at " + str(INSTALLER))
    payload = INSTALLER.read_bytes()
    if payload[:2] != b"MZ":
        raise SystemExit("that is not a Windows executable")
    digest = hashlib.sha256(payload).hexdigest()
    print("==> installer is %.1f MB, sha256 %s"
          % (len(payload) / (1024 * 1024), digest[:16] + "..."))

    # Check the BINARY, not the label. Verifying the advertised version
    # against relay.env compares the label with the file that stores the
    # label - it cannot notice a build that says something else. That is
    # exactly how a 0.2.0-stamped installer got advertised as 0.2.1: a
    # member who took the offer would install 0.2.0, report 0.2.0, and be
    # re-offered "0.2.1" forever, because the client compares versions by
    # string equality and no download could ever satisfy it.
    stamped = stamped_version(INSTALLER)
    if not stamped:
        raise SystemExit(
            "could not read the installer's FileVersion, so there is no "
            "proof it is " + version + ". Refusing to publish a build "
            "whose stamped version is unknown."
        )
    if stamped.strip() != version:
        raise SystemExit(
            "the installer is stamped " + repr(stamped.strip()) + " but "
            "this release claims " + repr(version) + ".\n"
            "Publishing would advertise a version the download can never "
            "satisfy, and every member who updated would be offered it "
            "again forever. Rebuild with the right version.")
    print("==> installer's stamped FileVersion is " + stamped + ", matching")

    if dry_run:
        print()
        print("dry run: nothing was published and no member was told.")
        return 0

    # Atomic, because /download is public and unauthenticated: a plain
    # copy serves a truncated executable to anyone who asks mid-write.
    destination = downloads if downloads is not None else (
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "VemalsEars" / "downloads"
    )
    sources = (INSTALLER, DIST / "vemals-ears-member.zip")
    for source in sources:
        if not source.is_file():
            raise SystemExit("Missing VE release artifact: " + str(source))
    destination.mkdir(parents=True, exist_ok=True)
    for source in sources:
        served = destination / source.name
        temporary = served.with_name(served.name + ".publishing")
        shutil.copy2(source, temporary)
        os.replace(temporary, served)
        print("==> published to " + str(served))

    if not advertise:
        print()
        print("published the download WITHOUT advertising it.")
        print("The relay still offers nothing, so no client is told to")
        print("update and no client older than 0.2.0 is disturbed. Members")
        print("can install it by hand from the download link. Advertise it")
        print("only once every active device reports 0.2.0 or newer:")
        print("    python GuildHub/server.py list")
        return 0

    # Told last: until this is set the relay advertises nothing, so a
    # failure above leaves members on the build that still works.
    settings = console("/api/config", {"VEMALS_EARS_CLIENT_VERSION": version})

    # Read back the file the relay actually loads, not the console's
    # answer. The console only writes keys it RECOGNISES, so one running
    # older code drops an unknown key without error and still returns
    # 200 - which is exactly what happened on the first real publish: the
    # installer went out, the version did not, and every member stayed on
    # the old build while this script reported success. Checking the end
    # state rather than the acknowledgement is the whole point.
    applied = ""
    env_file = (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
                / "VemalsEars" / "relay.env")
    try:
        for line in env_file.read_text(encoding="ascii").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "VEMALS_EARS_CLIENT_VERSION":
                applied = value.strip()
    except OSError as error:
        raise SystemExit(
            "The installer is published but the advertised version could "
            "not be confirmed (" + str(error) + "). Members may not be "
            "offered the update; check the console before assuming they were."
        )
    if applied != version:
        raise SystemExit(
            "The installer is published, but the relay did NOT accept the "
            "version (it reports " + repr(applied) + ").\n"
            "Nobody will be offered the update until this is fixed.\n"
            "Most likely the console service is running older code than "
            "this checkout: restart DowntimeConsole from an elevated "
            "prompt, then run this command again - republishing the same "
            "version is safe."
        )
    print("==> relay now advertises " + version + ". " +
          str(settings.get("restart", "")))
    print()
    print("Members are offered the update the next time their client")
    print("connects. Nothing is installed for them without a click.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True,
                        help="the version this release IS, like 0.2.0")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and verify, publish nothing")
    parser.add_argument("--downloads", type=Path,
                        help="Destination downloads directory; defaults to PROGRAMDATA/VemalsEars/downloads.")
    parser.add_argument(
        "--no-advertise",
        action="store_true",
        help="publish the download but do NOT tell clients about it. The "
             "update advert adds a key to the authenticated frame that "
             "clients older than 0.2.0 reject outright, so while any are "
             "still enrolled this ships the file without breaking them.",
    )
    parser.add_argument(
        "--sign-thumbprint", default="",
        help="sign the installer with this certificate from the current "
             "user's Windows certificate store (40 hex digits). Requires "
             "--expected-publisher. Omitted: the build ships unsigned.",
    )
    parser.add_argument(
        "--expected-publisher", default="",
        help="the exact certificate subject, e.g. \"CN=Your Name\"; the "
             "build refuses to ship a signature by anyone else",
    )
    parser.add_argument(
        "--timestamp-url", default="",
        help="RFC3161 timestamp authority; the provider default when omitted",
    )
    arguments = parser.parse_args(argv)
    return publish(
        arguments.version,
        arguments.dry_run,
        advertise=not arguments.no_advertise,
        sign_thumbprint=arguments.sign_thumbprint,
        expected_publisher=arguments.expected_publisher,
        timestamp_url=arguments.timestamp_url,
        downloads=arguments.downloads,
    )


if __name__ == "__main__":
    sys.exit(main())
