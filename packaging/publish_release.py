"""Build, verify and publish a member release to the guild relay.

Deploying used to be eight steps in a required order, several of which
fail silently if skipped: a bundle built without the runtime still runs
here, an installer published without bumping the advertised version
offers nobody anything, and a half-copied download is served to whoever
asks during the copy.

Every check that can be made before activation runs first. One atomic release
pointer switches the matching installer and source pair; compatibility aliases
are refreshed afterward and can be safely retried.

    python packaging/publish_release.py --version 0.2.0
    python packaging/publish_release.py --version 0.2.0 --dry-run
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import secrets
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
PUBLISH_LOCK_NAME = ".vemals-ears-publish.lock"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from GuildHub.update_contract import (
    MAX_LEGACY_COMPATIBLE_UPDATE_BYTES,
    MAX_UPDATE_BYTES,
    RELEASE_POINTER_NAME,
    SOURCE_BUNDLE_NAME,
    is_canonical_release_version,
    release_pointer_document,
    release_version_order,
    validate_release_pointer,
    versioned_installer_name,
    versioned_source_bundle_name,
)


def file_identity(path: Path) -> tuple[int, str]:
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return size, digest.hexdigest()


def validate_published_update_size(size: int) -> None:
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise SystemExit("the installer size is invalid")
    if size > MAX_UPDATE_BYTES:
        raise SystemExit(
            "the installer exceeds the 512 MiB client acceptance ceiling"
        )
    if size > MAX_LEGACY_COMPATIBLE_UPDATE_BYTES:
        raise SystemExit(
            "the installer exceeds the 200 MiB legacy-client publication ceiling; "
            "publishing it would strand clients that skip the bridge release"
        )


def validate_source_bundle(path: Path) -> None:
    """Require the public member ZIP to contain its complete runtime source."""
    prefix = "VemalsEars/runtime/Source/"
    app_source = "VemalsEars/Source/vemals-ears-source.zip"
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("duplicate archive paths")
            name_set = set(names)
            manifest = json.loads(archive.read(prefix + "MANIFEST.json"))
            if not isinstance(manifest, dict) or set(manifest) != {
                "schema",
                "runtime_lock_sha256",
                "runtime_lock_hash_mode",
                "source_lock_sha256",
                "runtime_components",
                "artifacts",
                "build_notes",
                "coverage_notes",
            }:
                raise ValueError("invalid runtime source manifest")
            if (
                type(manifest.get("schema")) is not int
                or manifest.get("schema") != 1
                or manifest.get("runtime_lock_hash_mode") != "sha256-lf"
                or not isinstance(manifest.get("runtime_components"), dict)
                or not manifest["runtime_components"]
                or not isinstance(manifest.get("artifacts"), list)
                or not manifest["artifacts"]
            ):
                raise ValueError("invalid runtime source manifest")
            expected_sources = {prefix + "MANIFEST.json", prefix + "README.txt"}
            artifact_names = set()
            for item in manifest["artifacts"]:
                filename = item.get("filename") if isinstance(item, dict) else None
                digest = item.get("sha256") if isinstance(item, dict) else None
                if (
                    not isinstance(filename, str)
                    or not filename
                    or Path(filename).name != filename
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                ):
                    raise ValueError("invalid runtime source receipt")
                if filename in artifact_names:
                    raise ValueError("duplicate runtime source receipt")
                artifact_names.add(filename)
                member = prefix + filename
                expected_sources.add(member)
                actual = hashlib.sha256()
                with archive.open(member) as source:
                    while block := source.read(1024 * 1024):
                        actual.update(block)
                if actual.hexdigest() != digest:
                    raise ValueError("runtime source digest mismatch")
            actual_sources = {name for name in name_set if name.startswith(prefix)}
            if actual_sources != expected_sources:
                raise ValueError("runtime source inventory mismatch")
            with zipfile.ZipFile(io.BytesIO(archive.read(app_source))) as source:
                if "VemalsEars-source/SOURCE-MANIFEST.json" not in source.namelist():
                    raise ValueError("application source manifest is missing")
    except (KeyError, OSError, TypeError, ValueError, zipfile.BadZipFile) as error:
        raise SystemExit(
            "the public source bundle is incomplete or invalid: " + str(error)
        ) from error


def copy_atomic_verified(
    source: Path, destination: Path, expected: tuple[int, str]
) -> None:
    temporary = destination.with_name(
        destination.name + "." + str(os.getpid()) + ".publishing"
    )
    try:
        shutil.copy2(source, temporary)
        if file_identity(temporary) != expected:
            raise SystemExit("copied release artifact differs from " + str(source))
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    if file_identity(destination) != expected:
        raise SystemExit("published release artifact differs from " + str(source))


def retain_immutable_verified(
    source: Path, destination: Path, expected: tuple[int, str]
) -> None:
    """Atomically create one immutable versioned artifact name."""
    if immutable_target_exists(destination, expected):
        print("==> exact release artifact already retained at " + str(destination))
        return
    temporary = destination.with_name(
        destination.name + "." + str(os.getpid()) + ".publishing"
    )
    try:
        shutil.copy2(source, temporary)
        if file_identity(temporary) != expected:
            raise SystemExit("copied source bundle differs from " + str(source))
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or file_identity(destination) != expected:
                raise SystemExit(
                    "refusing to overwrite different immutable release artifact: "
                    + str(destination)
                )
    finally:
        if temporary.exists():
            temporary.unlink()
    if file_identity(destination) != expected:
        raise SystemExit("retained release artifact differs from " + str(source))
    print("==> retained exact release artifact at " + str(destination))


def immutable_target_exists(path: Path, expected: tuple[int, str]) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file() or file_identity(path) != expected:
        raise SystemExit(
            "refusing to overwrite different immutable release artifact: "
            + str(path)
        )
    return True


def read_release_pointer(path: Path) -> dict | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise SystemExit("release pointer is not a regular file: " + str(path))
    try:
        payload = path.read_bytes()
        if len(payload) > 16 * 1024:
            raise ValueError("document is oversized")
        def unique_object(pairs):
            document = {}
            for key, value in pairs:
                if key in document:
                    raise ValueError("duplicate JSON key")
                document[key] = value
            return document

        def reject_constant(value):
            raise ValueError("non-finite JSON constant: " + value)

        document = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise SystemExit("release pointer is invalid: " + str(error)) from error
    if not validate_release_pointer(document):
        raise SystemExit("release pointer has an invalid schema or artifact name")
    return document


def validate_pointer_transition(existing: dict | None, proposed: dict) -> None:
    if not validate_release_pointer(proposed):
        raise SystemExit("proposed release pointer is invalid")
    if existing is None:
        return
    if not validate_release_pointer(existing):
        raise SystemExit("existing release pointer is invalid")
    current_order = release_version_order(existing["version"])
    proposed_order = release_version_order(proposed["version"])
    if proposed_order < current_order:
        raise SystemExit("refusing to rewrite the release pointer to an older version")
    if proposed_order == current_order and proposed != existing:
        raise SystemExit(
            "refusing to rewrite one release version with different artifact bytes"
        )


def commit_release_pointer(path: Path, document: dict) -> None:
    existing = read_release_pointer(path)
    validate_pointer_transition(existing, document)
    if existing == document:
        print("==> release pointer already names these exact artifacts")
        return
    payload = (json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("ascii")
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".publishing")
    try:
        temporary.write_bytes(payload)
        if temporary.read_bytes() != payload:
            raise SystemExit("release pointer changed while being written")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    if read_release_pointer(path) != document:
        raise SystemExit("committed release pointer differs from the verified release")
    print("==> committed release pointer at " + str(path))


@contextlib.contextmanager
def publication_lock(destination: Path):
    """Serialize pointer transitions; only this invocation removes its lock."""
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / PUBLISH_LOCK_NAME
    token = secrets.token_hex(16)
    document = {
        "schema": 1,
        "pid": os.getpid(),
        "token": token,
    }
    payload = (json.dumps(document, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        owner = "unknown"
        try:
            existing = path.read_bytes()
            if len(existing) <= 1024:
                recorded = json.loads(existing.decode("ascii"))
                if (
                    isinstance(recorded, dict)
                    and recorded.get("schema") == 1
                    and isinstance(recorded.get("pid"), int)
                    and not isinstance(recorded.get("pid"), bool)
                    and recorded["pid"] > 0
                ):
                    owner = str(recorded["pid"])
        except (OSError, UnicodeError, ValueError):
            pass
        raise SystemExit(
            "release publication is already locked by process " + owner + ". "
            "If that publisher is no longer running, remove " + str(path)
        )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        yield
    finally:
        try:
            if path.read_bytes() == payload:
                path.unlink()
        except OSError:
            pass


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


def publish_artifacts(
    version: str,
    destination: Path,
    installer: Path,
    installer_identity: tuple[int, str],
    source_bundle: Path,
    source_identity: tuple[int, str],
    *,
    advertise: bool,
) -> None:
    versioned_source = destination / versioned_source_bundle_name(version)
    versioned_installer = destination / versioned_installer_name(version)
    pointer_path = destination / RELEASE_POINTER_NAME
    pointer = release_pointer_document(version, installer_identity, source_identity)

    # Fail known conflicts before even creating a destination directory. The
    # same checks run again under the lock to close the concurrent-publisher
    # gap between validation and the pointer replace.
    if advertise:
        existing_pointer = read_release_pointer(pointer_path)
        validate_pointer_transition(existing_pointer, pointer)
    immutable_target_exists(versioned_source, source_identity)
    immutable_target_exists(versioned_installer, installer_identity)

    with publication_lock(destination):
        if advertise:
            existing_pointer = read_release_pointer(pointer_path)
            validate_pointer_transition(existing_pointer, pointer)
        immutable_target_exists(versioned_source, source_identity)
        immutable_target_exists(versioned_installer, installer_identity)

        # Old versioned release artifacts are never removed or replaced.
        retain_immutable_verified(source_bundle, versioned_source, source_identity)
        retain_immutable_verified(installer, versioned_installer, installer_identity)

        if not advertise:
            print()
            print("retained immutable release artifacts WITHOUT activating them.")
            print("The release pointer and rolling download aliases are unchanged,")
            print("so /download, /source and automatic updates still serve the")
            print("previous release.")
            return

        # This one atomic replace activates the matching installer, source and
        # version together. Rolling names are compatibility aliases only and
        # are updated afterward.
        commit_release_pointer(pointer_path, pointer)
        rolling_source = destination / SOURCE_BUNDLE_NAME
        copy_atomic_verified(source_bundle, rolling_source, source_identity)
        print("==> published compatibility alias " + str(rolling_source))
        served_installer = destination / installer.name
        copy_atomic_verified(installer, served_installer, installer_identity)
        print("==> published compatibility alias " + str(served_installer))

        # The pointer is authoritative. Keep the legacy environment version in
        # sync after activation for older relay code and operator visibility.
        settings = console("/api/config", {"VEMALS_EARS_CLIENT_VERSION": version})
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
                "The release pointer is active, but the legacy advertised version "
                "could not be confirmed (" + str(error) + "). Check the console "
                "and relay status."
            )
        if applied != version:
            raise SystemExit(
                "The release pointer is active, but the relay did NOT accept the "
                "legacy version setting (it reports " + repr(applied) + ").\n"
                "Most likely the console service is running older code than "
                "this checkout: restart DowntimeConsole from an elevated "
                "prompt, then run this command again - republishing the same "
                "version is safe."
            )
        print("==> relay compatibility setting now says " + version + ". " +
              str(settings.get("restart", "")))

    print()
    print("Members are offered the update the next time their client")
    print("connects. Nothing is installed for them without a click.")


def publish(
    version: str,
    dry_run: bool,
    advertise: bool = True,
    sign_thumbprint: str = "",
    expected_publisher: str = "",
    timestamp_url: str = "",
    downloads: Path | None = None,
) -> int:
    if not is_canonical_release_version(version):
        raise SystemExit("version must look like 0.2.0")

    # Version changes travel through review before a release is built.
    if client_version() != version:
        raise SystemExit("GuildHub/version.py must already match the requested VE release version.")

    # The executable stamp must match this product's independent version.
    # Unsigned remains the default until a certificate exists. When one
    # does, it is a certificate in the current user's Windows store -
    # every token and cloud HSM presents that way - and signing is one
    # thumbprint away. No .pfx, no password, nothing typed into a log.
    build = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "Installer/build_installer.ps1", "-Version", version,
        "-SourceCompleteMember",
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
    run("building the installer and exact source bundle", build)
    run("running the source-complete bundle as a member would",
        [sys.executable, "packaging/verify_bundle.py"])

    source_bundle = DIST / SOURCE_BUNDLE_NAME
    for artifact in (INSTALLER, source_bundle):
        if not artifact.is_file():
            raise SystemExit("missing VE release artifact: " + str(artifact))
    installer_size = INSTALLER.stat().st_size
    validate_published_update_size(installer_size)
    with INSTALLER.open("rb") as installer:
        magic = installer.read(2)
    if magic != b"MZ":
        raise SystemExit("that is not a Windows executable")
    validate_source_bundle(source_bundle)
    installer_identity = file_identity(INSTALLER)
    source_identity = file_identity(source_bundle)
    if source_identity[0] > MAX_UPDATE_BYTES:
        raise SystemExit(
            "the source-complete member ZIP exceeds the 512 MiB release "
            "artifact ceiling"
        )
    try:
        release_pointer_document(version, installer_identity, source_identity)
    except ValueError as error:
        raise SystemExit(
            "the release artifact identities cannot form a valid release pointer: "
            + str(error)
        ) from error
    digest = installer_identity[1]
    print("==> installer is %.1f MB, sha256 %s"
          % (installer_size / (1024 * 1024), digest[:16] + "..."))
    print("==> exact source bundle is %.1f MB, sha256 %s"
          % (source_identity[0] / (1024 * 1024), source_identity[1][:16] + "..."))

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

    destination = downloads if downloads is not None else (
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "VemalsEars" / "downloads"
    )
    publish_artifacts(
        version,
        destination,
        INSTALLER,
        installer_identity,
        source_bundle,
        source_identity,
        advertise=advertise,
    )
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
        help="retain immutable versioned artifacts but do not switch the "
             "release pointer or rolling public aliases",
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
