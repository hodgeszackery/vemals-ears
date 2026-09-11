"""Release size and corresponding-source publication contract.

The client acceptance ceiling is deliberately larger than today's package so
one unusually large future build does not disable update discovery. Published
installers remain within the older clients' 200 MiB ceiling until a
minimum-client-aware migration can safely retire that compatibility promise.
"""

MAX_UPDATE_BYTES = 512 * 1024 * 1024
MAX_LEGACY_COMPATIBLE_UPDATE_BYTES = 200 * 1024 * 1024
SOURCE_BUNDLE_NAME = "vemals-ears-member.zip"
INSTALLER_NAME = "Vemals-Ears-Setup.exe"
RELEASE_POINTER_NAME = "vemals-ears-release.json"


def is_canonical_release_version(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    return len(parts) == 3 and all(
        1 <= len(part) <= 6
        and all(character in "0123456789" for character in part)
        and int(part) <= 65535
        and str(int(part)) == part
        for part in parts
    )


def release_version_order(value: object) -> tuple[int, int, int] | None:
    if not is_canonical_release_version(value):
        return None
    return tuple(int(part) for part in value.split("."))


def versioned_source_bundle_name(version: str) -> str:
    return "vemals-ears-member-" + version + ".zip"


def versioned_installer_name(version: str) -> str:
    return "Vemals-Ears-Setup-" + version + ".exe"


def release_pointer_document(
    version: str,
    installer_identity: tuple[int, str],
    source_identity: tuple[int, str],
) -> dict:
    if not is_canonical_release_version(version):
        raise ValueError("release version is not canonical")
    document = {
        "schema": 1,
        "version": version,
        "installer": {
            "filename": versioned_installer_name(version),
            "bytes": installer_identity[0],
            "sha256": installer_identity[1],
        },
        "source": {
            "filename": versioned_source_bundle_name(version),
            "bytes": source_identity[0],
            "sha256": source_identity[1],
        },
    }
    if not validate_release_pointer(document):
        raise ValueError("release artifact identity is invalid")
    return document


def validate_release_pointer(document: object) -> bool:
    if not isinstance(document, dict) or set(document) != {
        "schema", "version", "installer", "source"
    }:
        return False
    version = document.get("version")
    if (
        type(document.get("schema")) is not int
        or document.get("schema") != 1
        or not is_canonical_release_version(version)
    ):
        return False
    expected_names = {
        "installer": versioned_installer_name(version),
        "source": versioned_source_bundle_name(version),
    }
    for key, expected_name in expected_names.items():
        artifact = document.get(key)
        if not isinstance(artifact, dict) or set(artifact) != {
            "filename", "bytes", "sha256"
        }:
            return False
        size = artifact.get("bytes")
        digest = artifact.get("sha256")
        if (
            artifact.get("filename") != expected_name
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or size > MAX_UPDATE_BYTES
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            return False
    return True
