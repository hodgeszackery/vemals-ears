"""Bounded local labels for the per-speaker caption mute picker.

Only an opaque speaker token and the already-visible display name are kept.
Caption text, Discord account IDs, and audio never enter this file.
"""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .settings import SPEAKER_TOKEN_PATTERN, STATE_FOLDER

SCHEMA = 1
DIRECTORY_NAME = "speaker-directory.json"
MAX_KNOWN_SPEAKERS = 128
MAX_SPEAKER_NAME = 48


@dataclass(frozen=True, slots=True)
class KnownSpeaker:
    speaker_id: str
    display_name: str


def default_speaker_directory_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return root / STATE_FOLDER / DIRECTORY_NAME


def _clean_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = unicodedata.normalize("NFC", value).strip()
    if not name or len(name) > MAX_SPEAKER_NAME:
        return None
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in name):
        return None
    return name


def load_known_speakers(
    path: Path | None = None,
) -> tuple[KnownSpeaker, ...]:
    target = path or default_speaker_directory_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return ()
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return ()
    rows = payload.get("speakers")
    if not isinstance(rows, list):
        return ()
    result: list[KnownSpeaker] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "name"}:
            continue
        speaker_id = row.get("id")
        name = _clean_name(row.get("name"))
        if (
            not isinstance(speaker_id, str)
            or not SPEAKER_TOKEN_PATTERN.fullmatch(speaker_id)
            or speaker_id in seen
            or name is None
        ):
            continue
        result.append(KnownSpeaker(speaker_id, name))
        seen.add(speaker_id)
        if len(result) >= MAX_KNOWN_SPEAKERS:
            break
    return tuple(result)


def save_known_speakers(
    speakers: tuple[KnownSpeaker, ...] | list[KnownSpeaker],
    path: Path | None = None,
) -> bool:
    target = path or default_speaker_directory_path()
    clean: list[KnownSpeaker] = []
    seen: set[str] = set()
    for speaker in speakers:
        speaker_id = getattr(speaker, "speaker_id", None)
        name = _clean_name(getattr(speaker, "display_name", None))
        if (
            not isinstance(speaker_id, str)
            or not SPEAKER_TOKEN_PATTERN.fullmatch(speaker_id)
            or speaker_id in seen
            or name is None
        ):
            continue
        clean.append(KnownSpeaker(speaker_id, name))
        seen.add(speaker_id)
        if len(clean) >= MAX_KNOWN_SPEAKERS:
            break
    payload = {
        "schema": SCHEMA,
        "speakers": [
            {"id": speaker.speaker_id, "name": speaker.display_name}
            for speaker in clean
        ],
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return True
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def remember_speaker(
    speaker_id: str,
    display_name: str,
    path: Path | None = None,
) -> bool:
    if not SPEAKER_TOKEN_PATTERN.fullmatch(str(speaker_id)):
        return False
    name = _clean_name(display_name)
    if name is None:
        return False
    target = path or default_speaker_directory_path()
    current = list(load_known_speakers(target))
    for speaker in current:
        if speaker.speaker_id == speaker_id and speaker.display_name == name:
            return True
    current = [speaker for speaker in current if speaker.speaker_id != speaker_id]
    current.append(KnownSpeaker(speaker_id, name))
    if len(current) > MAX_KNOWN_SPEAKERS:
        current = current[-MAX_KNOWN_SPEAKERS:]
    return save_known_speakers(current, target)
