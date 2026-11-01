"""Vemal's Ears guild server.

The service intentionally speaks plain WebSocket. Keep it on loopback, or put
it behind a TLS reverse proxy before exposing it outside a private container
network.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from http import HTTPStatus
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from GuildHub.protocol import (
    CAPTION_SUBPROTOCOLS,
    CAPTION_PATH,
    MAX_STREAM_ID,
    MAX_TOKEN,
    MAX_WIRE_BYTES,
    PAIR_PATH,
    PUBLISH_PATH,
    QUICK_CHAT_PATH,
    QUICK_CHAT_PROFILE_PATH,
    QUICK_CHAT_WORKER_PATH,
    SUBPROTOCOL,
    SUBPROTOCOL_V2,
    VIEWER_LINK_PATH,
    AudienceUpdate,
    DeviceAuthentication,
    GuildHubProtocolError,
    PublishRequest,
    QuickChatRequest,
    ViewerEnrollment,
    ViewerLink,
    caption_envelope,
    cursor_advance,
    decode_message,
    encode_message,
    error_message,
    parse_device_authentication,
    parse_pair_request,
    parse_publisher_command,
    parse_quick_chat,
    parse_quick_chat_delivery,
    parse_quick_chat_profile_request,
    parse_service_authentication,
    parse_viewer_link_request,
    quick_chat_acknowledgement,
    quick_chat_profile,
    viewer_link_response,
)
from GuildHub.release_pointer import (
    ReleasePointerError,
    ReleaseSnapshot,
    canonical_version,
    immutable_artifact,
    load_release_pointer,
)
from GuildHub.store import (
    CredentialStore,
    DeviceRecord,
    PairingError,
)
from VoiceCaptions.events import (
    CaptionEvent,
    ProtocolEvent,
    SessionEvent,
    event_to_json,
)


LOGGER = logging.getLogger("GuildHub.server")
WEBSOCKET_LOGGER = logging.getLogger("GuildHub.websocket")
WEBSOCKET_LOGGER.setLevel(logging.CRITICAL + 1)
WEBSOCKET_LOGGER.propagate = False
HEALTH_PATH = "/healthz"
JOIN_PATH = "/join"
# Machine-readable twin of the join page's data-server attribute, so a
# desktop client handed the https join link can resolve the websocket
# URL itself instead of making the member relay it by hand. Everything
# here is already public in the join page HTML.
PAIRING_CONFIG_PATH = "/pairing-config"
UPDATE_PATH = "/v1/update"
UPDATE_DEVICE_ID_HEADER = "X-Vemals-Ears-Device-ID"
UPDATE_CLIENT_VERSION_HEADER = "X-Vemals-Ears-Client-Version"
DOWNLOAD_PATH = "/download"
DOWNLOAD_PREFIX = DOWNLOAD_PATH + "/"
# The source-complete portable bundle is published beside the installer.
# Keeping this a fixed sibling name means request input never selects a
# filesystem path, while members can obtain corresponding source from the
# same server and release as the executable.
SOURCE_DOWNLOAD_PATH = "/source"
SOURCE_DOWNLOAD_PREFIX = SOURCE_DOWNLOAD_PATH + "/"
PRIVACY_PATH = "/privacy"
MAX_UPDATE_VERSION = 32
QUICK_CHAT_CACHE_LIMIT = 512
QUICK_CHAT_RATE_DEVICE_LIMIT = 512
QUICK_CHAT_RATE_LIMIT = 5
QUICK_CHAT_RATE_WINDOW_SECONDS = 10.0
VIEWER_LINK_TTL_SECONDS = 5 * 60
VIEWER_LINK_CODE_LENGTH = 8
VIEWER_LINK_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
AUDIENCE_SNAPSHOT_LIMIT = 256
ARTIFACT_ADMISSION_TIMEOUT_SECONDS = 0.1


# What the companion can actually run. install_update() saves the offered
# bytes under an .exe name and hands them to Windows, so a published file
# that is not an installer is not an update - it is a zip about to be
# executed. The checksum cannot catch that: the bytes match the offer
# perfectly. What is wrong is the promise, not the delivery.
INSTALLER_SUFFIXES = (".exe", ".msi")


@dataclass(frozen=True)
class PublishedRelease:
    pointer_present: bool
    snapshot: ReleaseSnapshot | None


def published_release(download_file: str) -> PublishedRelease:
    """Resolve one pointer snapshot, distinguishing absence from invalidity."""
    if not download_file:
        return PublishedRelease(False, None)
    try:
        snapshot = load_release_pointer(Path(download_file).parent)
    except ReleasePointerError as error:
        LOGGER.warning("not serving the VE release pointer: %s", error)
        return PublishedRelease(True, None)
    return PublishedRelease(snapshot is not None, snapshot)


def _versioned_download_url(download_url: str, version: str) -> str:
    if not download_url or canonical_version(version) is None:
        return ""
    parts = urlsplit(download_url)
    if not parts.scheme or not parts.netloc:
        return ""
    return urlunsplit(
        (parts.scheme, parts.netloc, DOWNLOAD_PREFIX + version, "", "")
    )


def update_offer(
    client_version: str,
    download_file: str,
    download_url: str,
    *,
    release: PublishedRelease | None = None,
    public_server_url: str = "",
) -> dict[str, Any] | None:
    """What to tell an authenticated member about the current build.

    None when the operator has not said which version the published file
    is, when there is no published file, or when the published file is
    not something the member's companion can install: an offer nobody can
    check, fetch or run is worse than no offer, because the member would
    be told to update and then given nothing to update to.
    """
    selected = release if release is not None else published_release(download_file)
    if selected.pointer_present:
        snapshot = selected.snapshot
        url = (
            _versioned_download_url(
                _public_http_from_server(public_server_url),
                snapshot.version,
            )
            if snapshot is not None
            and public_server_url
            else ""
        )
        if snapshot is None or not url:
            return None
        return {
            "version": snapshot.version,
            "sha256": snapshot.installer.sha256,
            "bytes": snapshot.installer.bytes,
            "url": url,
        }

    version = (client_version or "").strip()
    if not version or len(version) > MAX_UPDATE_VERSION:
        return None
    if not download_file or not download_url:
        return None
    if Path(download_file).suffix.lower() not in INSTALLER_SUFFIXES:
        # Said out loud, because the symptom on the member's side is
        # silence - no banner - and the operator would have no way to
        # connect that to a path they set weeks earlier.
        LOGGER.warning(
            "not advertising %s: an update must be an installer (%s)",
            download_file,
            " or ".join(INSTALLER_SUFFIXES),
        )
        return None
    fingerprint = download_fingerprint(Path(download_file))
    if fingerprint is None:
        return None
    digest, size = fingerprint
    return {
        "version": version,
        "sha256": digest,
        "bytes": size,
        "url": download_url,
    }


def download_fingerprint(path: Path) -> tuple[str, int] | None:
    """The published download's sha256 and size, or None if there is none.

    Cached on (mtime, size) so a member connecting does not re-hash 21 MB,
    but a republished file is still noticed - the operator swaps that file
    by hand and must not have to restart the relay for members to be
    offered the new build.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _DOWNLOAD_FINGERPRINTS.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    fingerprint = (digest.hexdigest(), stat.st_size)
    # One entry per file; the key changes when the file does.
    _DOWNLOAD_FINGERPRINTS.clear()
    _DOWNLOAD_FINGERPRINTS[key] = fingerprint
    return fingerprint


_DOWNLOAD_FINGERPRINTS: dict[tuple[str, int, int], tuple[str, int]] = {}

# The member download can be either the installer or the source archive,
# so the type follows the file rather than being assumed. Serving an .exe
# as application/zip is the kind of thing a browser or a corporate proxy
# quietly does something unhelpful with.
DOWNLOAD_CONTENT_TYPES = {
    ".zip": "application/zip",
    ".exe": "application/octet-stream",
    ".msi": "application/octet-stream",
}
DEFAULT_PORT = 8765
DEFAULT_GUILD_NAME = "Vemal's Ears"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_JOIN_SCRIPT = """(function () {
  "use strict";
  var code = window.location.hash.slice(1);
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  var status = document.getElementById("status");
  var box = document.getElementById("link");
  var copy = document.getElementById("copy");
  if (!code || /^(main|tools|connect)$/.test(code)) {
    status.textContent = "Open the invitation from your guild operator to copy your link here.";
    return;
  }
  if (!/^ghe_[A-Za-z0-9_-]{1,188}$/.test(code)) {
    status.textContent = "This invitation link is invalid. Ask your guild operator for a fresh link.";
    return;
  }
  // Shino, 2026-09-02: "make them copy paste the link in the application.
  // Don't try to pair or consume the key in the link because they need to
  // download the app first."
  //
  // That is the whole answer, and it retires three problems at once. A
  // browser will not launch an app without a user gesture, so the
  // automatic handoff never fired. The confirmation it eventually raised
  // opened behind the game and went unanswered. And the invitation is
  // one-use, so a handoff attempted before the app is even installed
  // spends it for nothing. The page now hands over the link and stops.
  var link = window.location.origin + window.location.pathname + "#" + code;
  box.textContent = link;
  status.textContent = "Install above if you have not yet, then open the app, "
    + "choose Connect guild in Vemal's Ears, and paste this link.";
  copy.hidden = false;
  copy.addEventListener("click", function () {
    function selected() {
      var range = document.createRange();
      range.selectNodeContents(box);
      var chosen = window.getSelection();
      chosen.removeAllRanges();
      chosen.addRange(range);
    }
    // Clipboard access can be refused, and a button that silently does
    // nothing is worse than one that selects the text for you.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(link).then(function () {
        copy.textContent = "Copied";
      }, function () {
        selected();
        copy.textContent = "Press Ctrl+C";
      });
      return;
    }
    selected();
    copy.textContent = "Press Ctrl+C";
  });
}());"""
_JOIN_SCRIPT_HASH = base64.b64encode(
    hashlib.sha256(_JOIN_SCRIPT.encode("ascii")).digest()
).decode("ascii")


@dataclass(frozen=True)
class ServerConfig:
    guild_id: str = "guild"
    guild_name: str = DEFAULT_GUILD_NAME
    publisher_token: str = field(default="", repr=False)
    public_server_url: str = ""
    client_download_url: str = ""
    auth_timeout: float = 5.0
    replay_limit: int = 256
    client_queue_limit: int = 64
    connection_limit: int = 256
    revalidation_seconds: float = 15.0
    audience_lease_seconds: float = 15.0
    # Credential housekeeping. Shino, 2026-09-02: "our approval tokens are
    # cluttered again - can we auto revoke or delete unused keys". Every
    # window is in seconds and any of them at 0 disables that half, so the
    # operator can turn the whole thing off without editing code.
    housekeeping_seconds: float = 3600.0
    unused_device_seconds: float = 48 * 3600.0
    idle_device_seconds: float = 30 * 24 * 3600.0
    revoked_retention_seconds: float = 30 * 24 * 3600.0
    # A typed line depends only on the relay. This deadline protects the
    # member request from a starved event loop while its shared task finishes.
    quick_chat_response_timeout: float = 8.0
    trust_proxy: bool = False
    # Optional: a local file served at /download, for guilds hosting
    # the member client themselves instead of pointing at a public
    # release. Operator configuration only - no request input ever
    # selects a path, so there is no traversal surface.
    download_file: str = ""
    # The version that download IS. Members are told an update exists by
    # comparing this against what they report at enrollment; leaving it
    # empty simply means nobody is ever told.
    client_version: str = ""

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.guild_id) or len(self.guild_id) > 64:
            raise ValueError("guild id must be a short safe identifier")
        clean_guild_name = unicodedata.normalize("NFKC", self.guild_name).strip()
        if not clean_guild_name or len(clean_guild_name) > 80:
            raise ValueError("guild name is missing or too long")
        if any(
            unicodedata.category(character).startswith("C")
            for character in clean_guild_name
        ):
            raise ValueError("guild name contains control characters")
        object.__setattr__(self, "guild_name", clean_guild_name)
        public_server_url = _pair_server_url(
            self.public_server_url, "public server URL"
        )
        client_download_url = _download_url(self.client_download_url)
        object.__setattr__(self, "public_server_url", public_server_url)
        object.__setattr__(self, "client_download_url", client_download_url)
        if (
            len(self.publisher_token) < 32
            or len(self.publisher_token) > MAX_TOKEN
            or not _SAFE_ID.fullmatch(self.publisher_token)
        ):
            raise ValueError(
                "publisher token must be 32-192 letters, digits, dashes, or underscores"
            )
        if self.auth_timeout <= 0 or self.auth_timeout > 60:
            raise ValueError("authentication timeout must be between 0 and 60 seconds")
        if self.replay_limit < 1 or self.replay_limit > 4096:
            raise ValueError("replay limit must be between 1 and 4096")
        if self.client_queue_limit < 1 or self.client_queue_limit > 1024:
            raise ValueError("client queue limit must be between 1 and 1024")
        if self.connection_limit < 1 or self.connection_limit > 4096:
            raise ValueError("connection limit must be between 1 and 4096")
        if (
            isinstance(self.revalidation_seconds, bool)
            or not isinstance(self.revalidation_seconds, (int, float))
            or self.revalidation_seconds <= 0
            or self.revalidation_seconds > 60
        ):
            raise ValueError("device revalidation must be between 0 and 60 seconds")
        if (
            isinstance(self.audience_lease_seconds, bool)
            or not isinstance(self.audience_lease_seconds, (int, float))
            or self.audience_lease_seconds <= 0
            or self.audience_lease_seconds > 60
        ):
            raise ValueError("audience lease must be between 0 and 60 seconds")
        if (
            isinstance(self.quick_chat_response_timeout, bool)
            or not isinstance(self.quick_chat_response_timeout, (int, float))
            or self.quick_chat_response_timeout <= 0
            or self.quick_chat_response_timeout > 30
        ):
            raise ValueError(
                "quick-chat response timeout must be between 0 and 30 seconds"
            )
        if not isinstance(self.trust_proxy, bool):
            raise ValueError("trust proxy acknowledgement must be true or false")


def _http_response(
    body: bytes,
    content_type: str,
    status_code: int = 200,
    *,
    script_hash: str | None = None,
) -> Response:
    reason = HTTPStatus(status_code).phrase
    content_security_policy = (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    )
    if script_hash is not None:
        content_security_policy += "; script-src 'sha256-" + script_hash + "'"
    return Response(
        status_code,
        reason,
        Headers(
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("Pragma", "no-cache"),
                ("Referrer-Policy", "no-referrer"),
                (
                    "Content-Security-Policy",
                    content_security_policy,
                ),
                ("X-Content-Type-Options", "nosniff"),
                ("X-Frame-Options", "DENY"),
            ]
        ),
        body,
    )


def _attachment_response(
    target: Path,
    content_type: str,
    *,
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> Response | None:
    try:
        payload = target.read_bytes()
    except OSError:
        return None
    if expected_bytes is not None and len(payload) != expected_bytes:
        return None
    if (
        expected_sha256 is not None
        and not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected_sha256)
    ):
        return None
    response = _http_response(payload, content_type)
    response.headers["Content-Disposition"] = (
        'attachment; filename="' + target.name + '"'
    )
    return response


def _update_authentication(request: Request) -> DeviceAuthentication | None:
    """Parse one update check without putting its token in a URL or log."""
    authorization = request.headers.get_all("Authorization")
    device_ids = request.headers.get_all(UPDATE_DEVICE_ID_HEADER)
    client_versions = request.headers.get_all(UPDATE_CLIENT_VERSION_HEADER)
    if not (
        len(authorization) == len(device_ids) == len(client_versions) == 1
        and authorization[0].startswith("Bearer ")
    ):
        return None
    token = authorization[0][7:]
    if authorization[0] != "Bearer " + token:
        return None
    try:
        return parse_device_authentication(
            {
                "type": "authenticate",
                "device_id": device_ids[0],
                "device_token": token,
                "last_stream_id": "",
                "last_cursor": 0,
                "client_version": client_versions[0],
            }
        )
    except GuildHubProtocolError:
        return None





def _source_version(value: str) -> str | None:
    return canonical_version(value)


def _member_download_file(
    download_file: str,
    release: PublishedRelease | None = None,
) -> Path | None:
    selected = release if release is not None else published_release(download_file)
    if selected.pointer_present:
        return selected.snapshot.installer.path if selected.snapshot is not None else None
    if not download_file:
        return None
    candidate = Path(download_file)
    return candidate if candidate.is_file() else None


def _versioned_installer_file(
    download_file: str,
    version: str,
    release: PublishedRelease | None = None,
) -> Path | None:
    selected = release if release is not None else published_release(download_file)
    if not selected.pointer_present or selected.snapshot is None:
        return None
    if canonical_version(version) is None:
        return None
    if version == selected.snapshot.version:
        return selected.snapshot.installer.path
    return immutable_artifact(
        Path(download_file).parent,
        version,
        installer=True,
    )


def _source_download_file(
    download_file: str,
    version: str | None = None,
    release: PublishedRelease | None = None,
) -> Path | None:
    """A rolling or immutable source-complete VE bundle beside the installer."""
    if not download_file:
        return None
    selected = release if release is not None else published_release(download_file)
    if selected.pointer_present:
        if selected.snapshot is None:
            return None
        if version is None or version == selected.snapshot.version:
            return selected.snapshot.source.path
        return immutable_artifact(
            Path(download_file).parent,
            version,
            installer=False,
        )
    published = Path(download_file)
    if version is None:
        filename = "vemals-ears-member.zip"
    else:
        normalized = _source_version(version)
        if normalized is None or normalized != version:
            return None
        filename = "vemals-ears-member-" + normalized + ".zip"
    candidate = published.with_name(filename)
    if candidate == published:
        return None
    return candidate if candidate.is_file() else None











def _privacy_page(guild_name: str, replay_limit: int) -> bytes:
    """The Guild & Privacy document: install in brief, data handling in full.

    Every data-handling sentence here is asserted by a test against what
    the code actually does. A privacy page that overstates is worse than
    no privacy page, because it is the one the guild will be held to.
    """
    safe_guild = escape(guild_name)
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Guild &amp; Privacy</title>
<style>
:root{{color-scheme:dark;font-family:"Segoe UI",Arial,sans-serif;background:#050908;color:#ecf4ef}}
*{{box-sizing:border-box}}body{{margin:0;padding:40px 24px;background:radial-gradient(ellipse at 90% 0%,#16382980,transparent 55%),#050908}}
main{{width:min(760px,100%);margin:auto;padding:40px 44px;background:#0d1915;border:1px solid #293e34;border-radius:8px}}
h1,h2{{font-family:Bahnschrift,"Arial Narrow","Segoe UI",sans-serif;line-height:1.12;letter-spacing:-.04em}}
h1{{margin:28px 0 18px;font-size:clamp(2.5rem,6vw,3.7rem);font-weight:600}}h2{{margin:36px 0 14px;color:#e8c681;font-size:1.4rem;font-weight:500}}
p,li{{line-height:1.8;color:#bbcdc3;font-size:.95rem}}ol{{padding-left:22px;margin:14px 0}}ol li{{padding-left:8px;margin-bottom:14px}}
a{{color:#69e6b6;text-decoration:underline;text-underline-offset:4px}}a:hover{{color:#e8c681}}a:focus-visible{{outline:3px solid #e8c681;outline-offset:5px}}
.back{{font-size:.85rem;text-decoration:none;color:#e8c681}}@media(max-width:600px){{body{{padding:20px 16px}}main{{padding:24px 20px}}}}
</style>
</head>
<body>
<main>
<a class="back" href="/join">Back to Vemal's Ears</a>
<h1>Guild &amp; Privacy</h1>
<p>{safe_guild} runs Vemal's Ears so raid voice chat is readable live.</p>
<h2>Installing</h2>
<ol>
<li>Download Vemal's Ears (By Downtime) from your
<a href="/join">guild download page</a> for live captions and Message Group.</li>
<li>Run the Windows installer only if you trust the download's source.
It installs for your user only and never asks for administrator access.</li>
<li>Run /vem setup in your Discord guild, copy your personal invitation,
then open Vemal's Ears, choose Connect guild, and paste the link.
Downloading does not use the invitation. Each invitation can be used once;
request a fresh invitation if it has expired or was already used.</li>
</ol>
<h2>Your voice, your text</h2>
<p>Nothing is transcribed without consent: only speakers who have turned
consent on are processed, and consent can be withdrawn at any time, even
mid-session.</p>
<p>No voice, caption, or Message Group text is persisted. Audio is transcribed
live and never recorded or saved. Caption and Message Group text is delivered
live; the relay keeps up to {int(replay_limit)} feed events in memory for an
authorized reconnect. Voice-channel authorization applies to every replay and
excludes text from before you joined or while you were absent. Ending a caption
session erases that session's caption and Message Group text from replay.
Stopping or restarting the relay clears all replay.</p>
<p>What the guild server does keep: your consent choice, your device
pairing, an opaque viewer link used to verify that your device is in the
active Discord voice channel, and the character name you map for class
colours - erase the mapping any time with /vem unmap, and your consent
memory with /vem forget. It also keeps the Message Group display name and
optional class colour saved for your paired device until the guild's device
retention process removes that record. The relay never stores your Discord
user ID.</p>
</main>
</html>
"""
    return page.encode("utf-8")


def _landing_page(
    public_server_url: str,
    client_download_url: str,
    source_download_url: str = "",
) -> bytes:
    """A VE-only download and invitation page; viewing never claims a code."""
    safe_server = escape(public_server_url, quote=True)
    safe_download = escape(client_download_url, quote=True)
    safe_source = escape(source_download_url, quote=True)
    download = (
        '<a class="download" href="' + safe_download + '">Download Vemal\'s Ears</a>'
        '<p class="note">Windows installer. Installs for your user.</p>'
        if safe_download
        else '<p class="notice">The client installer is not published on this server yet. '
             'Ask your guild host for the download.</p>'
    )
    source = (
        '<p class="note"><a href="' + safe_source + '">Portable ZIP and corresponding source</a>'
        ' for this release.</p>'
        if safe_source else ''
    )
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vemal's Ears - Guild captions</title>
<style>
:root{{color-scheme:dark;font-family:"Segoe UI",Arial,sans-serif;background:#080e0c;color:#edf5ef}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(ellipse at 90% 0%,#173b2980,transparent 60%),#080e0c}}
a{{color:#91e4b7;text-underline-offset:4px}}a:hover{{color:#e8c681}}a:focus-visible,button:focus-visible{{outline:3px solid #e8c681;outline-offset:5px}}
main{{max-width:980px;margin:auto;padding:36px 36px 64px}}header,footer{{display:flex;justify-content:space-between;gap:24px;align-items:center}}
header{{padding-bottom:28px;border-bottom:1px solid #2a4034}}.brand{{font-weight:650;letter-spacing:-.02em;font-size:1.1rem}}.byline{{color:#a8bcaf;font-size:.82rem}}
.hero{{padding:72px 0 60px;max-width:760px}}.eyebrow{{color:#e8c681;letter-spacing:.12em;text-transform:uppercase;font-size:.78rem}}
h1,h2{{font-family:Bahnschrift,"Segoe UI",sans-serif;font-weight:600;letter-spacing:-.04em;line-height:1.1}}
h1{{font-size:clamp(2.8rem,7vw,5rem);margin:20px 0 24px}}h2{{font-size:2rem;margin:0 0 20px}}p,li{{line-height:1.75;color:#bdd0c3}}.lead{{font-size:1.15rem;max-width:55ch}}
.download,button{{display:inline-block;padding:14px 22px;border:1px solid #e8c681;border-radius:5px;background:#e8c681;color:#152019;font:inherit;font-weight:650;cursor:pointer;text-decoration:none}}
.download{{margin-top:18px}}.download:hover,button:hover{{background:#f5dca8;color:#152019}}.note{{font-size:.85rem;margin:12px 0}}.notice{{border-left:3px solid #e8c681;padding-left:16px}}
.connect{{padding:34px;background:#102019;border:1px solid #314b3b;border-radius:8px}}ol{{padding-left:24px;margin:0}}li{{padding-left:8px;margin:14px 0}}strong{{color:#edf5ef}}
code#link{{display:block;overflow-wrap:anywhere;white-space:pre-wrap;background:#07120c;padding:16px;border:1px solid #345740;border-radius:4px;color:#d2f2db;font-size:.84rem;line-height:1.6}}
code#link:empty{{display:none}}button{{margin-top:12px}}#status{{margin-bottom:0}}footer{{margin-top:40px;padding-top:24px;border-top:1px solid #2a4034;font-size:.85rem;color:#a8bcaf}}
@media(max-width:600px){{main{{padding:24px 20px 40px}}.hero{{padding:44px 0}}.connect{{padding:24px 20px}}header,footer{{align-items:flex-start;flex-direction:column;gap:10px}}}}
</style>
</head>
<body data-server="{safe_server}">
<main id="main">
<header><span class="brand">Vemal's Ears <span class="byline">(By Downtime)</span></span><a href="/privacy">Guild &amp; Privacy</a></header>
<section class="hero"><p class="eyebrow">Your guild. Every voice, readable.</p>
<h1>Stay in the conversation.</h1>
<p class="lead">Live Discord captions and typed replies in a small, movable overlay.
Your guild hosts the speech processing. Each speaker chooses whether to be captioned.</p>
{download}{source}</section>
<section class="connect" id="connect" aria-labelledby="connect-title">
<h2 id="connect-title">Connect to your guild</h2>
<ol><li><strong>Install Vemal's Ears.</strong> Open the app after installation.</li>
<li><strong>Get your invitation.</strong> Run /vem setup in your guild's Discord server.
A guild manager can also invite you with /vem invite.</li>
<li><strong>Copy your personal link.</strong> In Vemal's Ears, choose Connect guild
and paste it. Downloading does not use the invitation; each link can be used once.</li></ol>
<code id="link"></code><button id="copy" type="button" hidden>Copy link</button>
<p id="status" role="status" aria-live="polite">Open your personal invitation to copy the link here.</p>
</section>
<p class="note">Join the active Discord voice channel to receive captions and Message Group text.
Only consenting speakers are transcribed. Audio and caption text are kept in memory.</p>
<footer><span>Vemal's Ears - hosted by your guild</span><a href="/privacy">How your data is handled</a></footer>
</main><script>{_JOIN_SCRIPT}</script>
</body>
</html>
"""
    return page.encode("ascii")


# A planned restart is not an authentication problem and not a fault, so
# it travels under its own code. 1001 "going away" is the close code the
# WebSocket spec reserves for exactly this, and a client that never
# learns the difference can only guess - which is what produced the
# reconnect churn during a deploy.
MAINTENANCE_CODE = "server_maintenance"
MAINTENANCE_MESSAGE = "Paused for updates - the guild server is restarting."
MAINTENANCE_CLOSE = 1001


# A viewer whose captions stop because the WORKER stopped is told that,
# rather than that they left the voice channel. The code is new, and the
# client's error frame is strict about the three keys it carries, so an
# older client that met an unknown code would fall through to "pair this
# device again". Only clients from this release on are sent it.
CAPTIONS_STOPPED_CODE = "captions_stopped"
CAPTIONS_STOPPED_SINCE = (0, 5, 11)
CAPTION_ACTIVE_STATE = "active"
CAPTION_PAUSED_STATE = "paused"


def understands_captions_stopped(client_version: str) -> bool:
    """Whether this client shipped with the captions_stopped code.

    Anything unparseable is treated as OLD. A viewer briefly reading the
    older sentence is a much smaller harm than one being told to pair
    their device again in the middle of a raid.
    """
    parts = (client_version or "").strip().split(".")
    if len(parts) != 3:
        return False
    try:
        numbers = tuple(int(part) for part in parts)
    except ValueError:
        return False
    return numbers >= CAPTIONS_STOPPED_SINCE


@dataclass(eq=False)
class _Subscriber:
    queue: asyncio.Queue[str]
    close_slow: Callable[[], Awaitable[None]]
    subprotocol: str = SUBPROTOCOL
    viewer_token: str | None = None
    replay_generation: int = 0
    active: bool = True
    # Whether this viewer's client understands CAPTIONS_STOPPED_CODE.
    # Read from the version it reported when it authenticated, the same
    # way subprotocol records what shape it can read.
    understands_stop: bool = False


@dataclass(eq=False)
class _CaptionSession:
    connection: ServerConnection
    subscriber: _Subscriber
    viewer_token: str | None = None


@dataclass(frozen=True)
class _PendingViewerLink:
    device_id: str
    expires_at: int


@dataclass(eq=False)
class _QuickChatWorkerSession:
    connection: ServerConnection
    active: bool = True


@dataclass(frozen=True)
class _QuickChatOutcome:
    cursor: int | None = None
    code: str = ""
    message: str = ""
    close_code: int = 1008


@dataclass(frozen=True)
class _QuickChatCacheEntry:
    fingerprint: bytes
    task: asyncio.Task[_QuickChatOutcome]


class CaptionBroker:
    """Bounded in-memory replay and fan-out for validated caption events."""

    def __init__(
        self,
        replay_limit: int = 256,
        queue_limit: int = 64,
        *,
        stream_id: str | None = None,
    ) -> None:
        if replay_limit < 1 or queue_limit < 1:
            raise ValueError("broker limits must be positive")
        selected_stream_id = stream_id or ("ghs_" + secrets.token_urlsafe(18))
        if (
            len(selected_stream_id) > MAX_STREAM_ID
            or not _SAFE_ID.fullmatch(selected_stream_id)
        ):
            raise ValueError("stream id is invalid")
        self._stream_id = selected_stream_id
        self._cursor = 0
        self._replay: deque[
            tuple[int, str, str, frozenset[str] | None]
        ] = deque(
            maxlen=replay_limit
        )
        self._queue_limit = queue_limit
        self._subscribers: set[_Subscriber] = set()
        # A copied replay must stop carrying text as soon as its source buffer
        # is cleared. Subscribers retain this generation with their snapshot.
        self._replay_generation = 0
        self._session_open = False
        self._session_accepts_captions = False
        # This is an authentication snapshot, not another stream event.
        # A member joining after /vem start still needs the privacy state,
        # but publishing a duplicate would advance the shared cursor and
        # break publisher acknowledgements.
        self._latest_session: SessionEvent | None = None

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def latest_session(self) -> SessionEvent | None:
        return self._latest_session

    @property
    def replay_generation(self) -> int:
        return self._replay_generation

    def subscribe(
        self,
        last_stream_id: str,
        last_cursor: int,
        close_slow: Callable[[], Awaitable[None]],
        *,
        subprotocol: str = SUBPROTOCOL,
        viewer_token: str | None = None,
        minimum_cursor: int = 0,
        understands_stop: bool = False,
    ) -> tuple[_Subscriber, int, list[str]]:
        if subprotocol not in CAPTION_SUBPROTOCOLS:
            raise ValueError("unsupported caption subprotocol")
        baseline_cursor = self._cursor
        replay: list[str] = []
        if last_stream_id == self._stream_id and last_cursor < self._cursor:
            if self._replay:
                oldest_cursor = self._replay[0][0]
                baseline_cursor = max(
                    last_cursor, minimum_cursor, oldest_cursor - 1
                )
                for cursor, encoded_v1, encoded_v2, audience in self._replay:
                    if cursor <= baseline_cursor:
                        continue
                    if audience is not None and viewer_token not in audience:
                        replay.append(
                            encode_message(
                                cursor_advance(self._stream_id, cursor)
                            )
                        )
                        continue
                    encoded = (
                        encoded_v2
                        if subprotocol == SUBPROTOCOL_V2
                        else encoded_v1
                    )
                    replay.append(encoded)
        subscriber = _Subscriber(
            queue=asyncio.Queue(maxsize=self._queue_limit),
            close_slow=close_slow,
            subprotocol=subprotocol,
            viewer_token=viewer_token,
            replay_generation=self._replay_generation,
            understands_stop=understands_stop,
        )
        self._subscribers.add(subscriber)
        return subscriber, baseline_cursor, replay

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        # A close handshake can remain pending after fan-out removal. In-flight
        # sends cannot be recalled, but this revokes every subsequent send.
        subscriber.active = False
        self._subscribers.discard(subscriber)

    def clear_replay(self) -> None:
        """Erase retained event frames without resetting stream continuity."""
        self._replay.clear()
        self._replay_generation += 1

    def reset_session(self) -> None:
        """Erase replay and the session snapshot owned by a departed worker."""
        self.clear_replay()
        self._latest_session = None
        self._session_open = False
        self._session_accepts_captions = False

    def publish(
        self,
        event: ProtocolEvent,
        audience: frozenset[str] | None = None,
        *,
        session_bound: bool = True,
    ) -> int:
        self._cursor += 1
        private_advance = encode_message(
            cursor_advance(self._stream_id, self._cursor)
        )
        discard_delayed_caption = (
            session_bound
            and not self._session_accepts_captions
            and isinstance(event, CaptionEvent)
        )
        if discard_delayed_caption:
            # The publisher still receives a continuous acknowledgement, but
            # a speech result that lost the race with /vem stop carries no text
            # into live delivery or replay.
            encoded_v1 = private_advance
            encoded_v2 = private_advance
        else:
            encoded_v1 = encode_message(
                caption_envelope(self._stream_id, self._cursor, event)
            )
            encoded_v2 = encode_message(
                caption_envelope(
                    self._stream_id,
                    self._cursor,
                    event,
                    subprotocol=SUBPROTOCOL_V2,
                )
            )
            if encoded_v2 == encoded_v1:
                encoded_v2 = encoded_v1
        if isinstance(event, SessionEvent):
            was_open = self._session_open
            session_open = event.state in {
                CAPTION_ACTIVE_STATE,
                CAPTION_PAUSED_STATE,
            }
            accepts_captions = event.state == CAPTION_ACTIVE_STATE
            if not session_open or not was_open:
                # Ready, error, stopped, and unknown close the replay lifetime.
                # Active or paused after a closed state opens a fresh lifetime.
                # Pausing an existing session preserves its visible history,
                # but captions remain blocked until an explicit active state.
                self.clear_replay()
            self._latest_session = event
            self._session_open = session_open
            self._session_accepts_captions = accepts_captions
        self._replay.append((self._cursor, encoded_v1, encoded_v2, audience))
        for subscriber in tuple(self._subscribers):
            delivered = (
                encoded_v2
                if subscriber.subprotocol == SUBPROTOCOL_V2
                else encoded_v1
            )
            if audience is not None and subscriber.viewer_token not in audience:
                delivered = private_advance
            try:
                subscriber.queue.put_nowait(delivered)
            except asyncio.QueueFull:
                self.unsubscribe(subscriber)
                asyncio.create_task(subscriber.close_slow())
        return self._cursor


# PRIVACY - fixed behaviour, not a setting.
#
# Captions and typed chat reach only paired devices whose owner is inside
# the active Discord voice channel. New speech captions require active capture.
# Typed chat can continue after capture stops while the publisher still supplies
# a fresh voice roster. Losing that roster closes every viewer with the stopped
# or voice-channel error understood by that client version.
#
# Shino, 2026-09-06: "This is a privacy concern and this switch cannot be
# flipped lightly." So it is not a switch any more: the
# VEMALS_EARS_REQUIRE_VOICE_MEMBERSHIP variable and --require-voice-membership
# flag that used to open viewing to every paired device are gone, and
# nothing in relay.env, the console or the command line brings that back.
# Opening viewing beyond the voice channel again is a code change to the
# audience routing below - and, in his words, one that "any system challenges
# me on" before it is made.


class GuildHubServer:
    def __init__(
        self,
        store: CredentialStore,
        config: ServerConfig,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.config = config
        self._clock = clock
        self._monotonic = monotonic
        self.broker = CaptionBroker(
            replay_limit=config.replay_limit,
            queue_limit=config.client_queue_limit,
        )
        self._active_handlers: set[ServerConnection] = set()
        self._caption_sessions: dict[str, _CaptionSession] = {}
        self._caption_session_lock = asyncio.Lock()
        self._audience_key: tuple[int, int] | None = None
        self._audience_tokens: frozenset[str] = frozenset()
        self._audience_updated_at = float("-inf")
        self._audience_snapshots: OrderedDict[
            tuple[int, int], frozenset[str]
        ] = OrderedDict()
        self._viewer_join_cursor: dict[str, int] = {}
        self._pending_viewer_links: dict[str, _PendingViewerLink] = {}
        self._viewer_link_by_device: dict[str, str] = {}
        self._publisher_connection: ServerConnection | None = None
        self._publisher_lock = asyncio.Lock()
        self._quick_chat_worker_session: _QuickChatWorkerSession | None = None
        self._quick_chat_worker_lock = asyncio.Lock()
        self._quick_chat_state_lock = asyncio.Lock()
        self._quick_chat_cache: OrderedDict[
            tuple[str, str], _QuickChatCacheEntry
        ] = OrderedDict()
        self._quick_chat_rate_windows: OrderedDict[str, deque[float]] = (
            OrderedDict()
        )
        # websockets' process_request API requires a complete bytes body.
        # Admit one artifact response until its HTTP connection closes so
        # slow clients cannot retain several release-sized bodies at once.
        self._artifact_slot = asyncio.Semaphore(1)
        self._artifact_release_tasks: set[asyncio.Task[None]] = set()
        self._release_validation_lock = asyncio.Lock()

    async def _published_release(self) -> PublishedRelease:
        async with self._release_validation_lock:
            return await asyncio.to_thread(
                published_release,
                self.config.download_file,
            )

    async def _release_artifact_slot(self, connection: ServerConnection) -> None:
        try:
            await connection.wait_closed()
        finally:
            self._artifact_slot.release()

    async def _release_artifact_after_read(
        self,
        read: asyncio.Task[Response | None],
    ) -> None:
        try:
            await read
        except BaseException:
            # The canceled request cannot observe the result. Consume any
            # worker exception, then release only after its thread is done.
            pass
        finally:
            self._artifact_slot.release()

    def _track_artifact_release(self, task: asyncio.Task[None]) -> None:
        self._artifact_release_tasks.add(task)
        task.add_done_callback(self._artifact_release_tasks.discard)

    async def _serve_attachment(
        self,
        connection: ServerConnection,
        target: Path,
        content_type: str,
        *,
        expected_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> Response | None:
        try:
            await asyncio.wait_for(
                self._artifact_slot.acquire(),
                timeout=ARTIFACT_ADMISSION_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return _http_response(
                b"A release download is already in progress. Try again shortly.\n",
                "text/plain; charset=utf-8",
                status_code=503,
            )
        read = asyncio.create_task(
            asyncio.to_thread(
                _attachment_response,
                target,
                content_type,
                expected_bytes=expected_bytes,
                expected_sha256=expected_sha256,
            )
        )
        try:
            response = await asyncio.shield(read)
        except asyncio.CancelledError:
            release = asyncio.create_task(self._release_artifact_after_read(read))
            self._track_artifact_release(release)
            raise
        except BaseException:
            self._artifact_slot.release()
            raise
        if response is None:
            self._artifact_slot.release()
            return None
        release = asyncio.create_task(self._release_artifact_slot(connection))
        self._track_artifact_release(release)
        return response

    @property
    def active_handler_count(self) -> int:
        return len(self._active_handlers)

    @property
    def active_caption_count(self) -> int:
        return len(self._caption_sessions)

    @property
    def active_publisher_count(self) -> int:
        return int(self._publisher_connection is not None)

    @property
    def active_quick_chat_worker_count(self) -> int:
        session = self._quick_chat_worker_session
        return int(session is not None and session.active)

    @property
    def quick_chat_cache_count(self) -> int:
        return len(self._quick_chat_cache)

    @property
    def quick_chat_rate_device_count(self) -> int:
        return len(self._quick_chat_rate_windows)

    def _ineligible_close(self, understands_stop: bool) -> tuple[str, str]:
        """The reason a viewer is being closed - never WHETHER it is.

        Shino, 2026-09-07: during any worker outage every viewer read
        "you left the voice channel or have not linked your Vemal's Ears
        application", because the audience roster is published BY the
        worker. Fifteen seconds after it stops the lease goes stale, the
        roster reads empty, and a member sitting in the channel the whole
        time is told they left it.

        The publisher connection is the discriminator: no publisher means
        the worker is gone, and that is a different sentence from "you
        are not in the channel". The set of viewers closed is decided
        before this is ever called, so nothing here can widen access.

        Older clients keep the exact code they already understand. Their
        error frame is strict - three keys, and an unrecognised code
        falls through to "pair this device again", which would be worse
        than the wrong sentence - so the new code goes only to clients
        that have shipped with it.
        """
        if understands_stop and self._publisher_connection is None:
            return (
                CAPTIONS_STOPPED_CODE,
                "Vemal's Ears is not running. Captions return when it starts.",
            )
        return (
            "voice_channel_required",
            "Join the active Discord voice channel to view captions.",
        )

    def _audience_is_fresh(self) -> bool:
        return (
            self._audience_key is not None
            and self._monotonic() - self._audience_updated_at
            <= self.config.audience_lease_seconds
        )

    def _current_audience(self) -> frozenset[str]:
        return self._audience_tokens if self._audience_is_fresh() else frozenset()

    def _viewer_is_current(self, token: str | None) -> bool:
        return token is not None and token in self._current_audience()

    def _prune_viewer_links(self) -> None:
        now = int(self._clock())
        for code, pending in tuple(self._pending_viewer_links.items()):
            if pending.expires_at > now:
                continue
            self._pending_viewer_links.pop(code, None)
            if self._viewer_link_by_device.get(pending.device_id) == code:
                self._viewer_link_by_device.pop(pending.device_id, None)

    def _new_viewer_link(self, device_id: str) -> tuple[str, int]:
        self._prune_viewer_links()
        previous = self._viewer_link_by_device.pop(device_id, None)
        if previous is not None:
            self._pending_viewer_links.pop(previous, None)
        expires_at = int(self._clock()) + VIEWER_LINK_TTL_SECONDS
        for _attempt in range(32):
            code = "".join(
                secrets.choice(VIEWER_LINK_CODE_ALPHABET)
                for _index in range(VIEWER_LINK_CODE_LENGTH)
            )
            if code in self._pending_viewer_links:
                continue
            self._pending_viewer_links[code] = _PendingViewerLink(
                device_id=device_id,
                expires_at=expires_at,
            )
            self._viewer_link_by_device[device_id] = code
            return code, expires_at
        raise RuntimeError("unable to allocate a viewer link code")

    async def _close_ineligible_caption_sessions(
        self, allowed: frozenset[str]
    ) -> None:
        closing: list[tuple[ServerConnection, bool]] = []
        async with self._caption_session_lock:
            for device_id, session in tuple(self._caption_sessions.items()):
                if session.viewer_token is not None and session.viewer_token in allowed:
                    continue
                self._caption_sessions.pop(device_id, None)
                self.broker.unsubscribe(session.subscriber)
                closing.append(
                    (session.connection, session.subscriber.understands_stop)
                )
        if closing:
            await asyncio.gather(
                *(
                    self._error_and_close(
                        connection, *self._ineligible_close(understands_stop), 1008
                    )
                    for connection, understands_stop in closing
                ),
                return_exceptions=True,
            )

    async def announce_maintenance(self) -> None:
        """Tell every viewer this restart is planned, then let go."""
        # run_server calls this before leaving its serving context. Explicitly
        # erase the process-owned replay here as well as relying on process exit.
        self.broker.reset_session()
        closing: list[ServerConnection] = []
        async with self._caption_session_lock:
            for device_id, session in tuple(self._caption_sessions.items()):
                self._caption_sessions.pop(device_id, None)
                self.broker.unsubscribe(session.subscriber)
                closing.append(session.connection)
        if not closing:
            return
        LOGGER.info(
            "announcing planned maintenance to %d viewer(s)", len(closing)
        )
        # The close frame is the whole announcement. An error frame sent
        # first does not survive the close - measured on 2026-09-02, with
        # and without a drain between them - so sending one would be a
        # message that reliably arrives nowhere.
        await asyncio.gather(
            *(
                connection.close(
                    code=MAINTENANCE_CLOSE, reason=MAINTENANCE_MESSAGE
                )
                for connection in closing
            ),
            return_exceptions=True,
        )

    async def _close_caption_device(self, device_id: str) -> None:
        connection: ServerConnection | None = None
        async with self._caption_session_lock:
            session = self._caption_sessions.pop(device_id, None)
            if session is not None:
                self.broker.unsubscribe(session.subscriber)
                connection = session.connection
        if connection is not None:
            try:
                await connection.close(code=1008, reason="Connection closed")
            except ConnectionClosed:
                pass

    async def _clear_audience(self) -> None:
        # The publisher owns the capture lease. Losing or replacing that lease
        # ends its replay lifetime even if the worker could not publish a final
        # stopped state before its socket disappeared.
        self.broker.reset_session()
        self._audience_key = None
        self._audience_tokens = frozenset()
        self._audience_updated_at = float("-inf")
        self._audience_snapshots.clear()
        self._viewer_join_cursor.clear()
        await self._close_ineligible_caption_sessions(frozenset())

    async def _apply_audience_update(self, update: AudienceUpdate) -> None:
        key = (update.generation, update.revision)
        current = frozenset(update.viewer_tokens)
        if self._audience_key is not None and key == self._audience_key:
            if current != self._audience_tokens:
                raise GuildHubProtocolError(
                    "audience refresh changed the current revision"
                )
            was_fresh = self._audience_is_fresh()
            self._audience_updated_at = self._monotonic()
            if not was_fresh:
                for token in current:
                    self._viewer_join_cursor[token] = self.broker.cursor
            return
        if self._audience_key is not None and key < self._audience_key:
            raise GuildHubProtocolError("audience update is not strictly ordered")
        previous = self._current_audience()
        if self._audience_key is None or update.generation != self._audience_key[0]:
            previous = frozenset()
        self._audience_key = key
        self._audience_tokens = current
        self._audience_updated_at = self._monotonic()
        self._audience_snapshots[key] = current
        self._audience_snapshots.move_to_end(key)
        while len(self._audience_snapshots) > AUDIENCE_SNAPSHOT_LIMIT:
            self._audience_snapshots.popitem(last=False)
        for token in current - previous:
            self._viewer_join_cursor[token] = self.broker.cursor
        for token in previous - current:
            self._viewer_join_cursor.pop(token, None)
        await self._close_ineligible_caption_sessions(current)

    def _publish_audience(self, request: PublishRequest) -> frozenset[str]:
        # See the PRIVACY note at the top of this module: every publish
        # is routed through the live voice audience, without exception.
        current = self._current_audience()
        if isinstance(request.event, CaptionEvent):
            if (
                request.audience_generation is None
                or request.audience_revision is None
            ):
                raise GuildHubProtocolError("caption audience routing is required")
            captured = self._audience_snapshots.get(
                (request.audience_generation, request.audience_revision)
            )
            if captured is None:
                raise GuildHubProtocolError("caption audience snapshot is unavailable")
            return frozenset(captured.intersection(current))
        return current

    async def _invite_viewer(self, request: ViewerEnrollment) -> bool:
        """Create the Discord-issued pairing invitation, identity attached.

        The device that claims this code inherits the viewer token, so
        presence in the voice channel is the only thing that decides
        whether captions reach it afterwards - no linking step, and
        nothing for the person to copy.
        """
        operation = asyncio.create_task(
            asyncio.to_thread(
                self.store.create_enrollment,
                float(request.ttl_seconds),
                code=request.code,
                viewer_token=request.viewer_token,
            )
        )
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            await asyncio.gather(operation, return_exceptions=True)
            raise
        except Exception as error:
            # The publisher only learns created=false. The operator needs
            # the reason, and the reason is never the code or the token.
            LOGGER.warning(
                "pairing invitation refused: %s", type(error).__name__
            )
            return False
        LOGGER.info("pairing invitation stored")
        return True

    async def _bind_viewer(self, request: ViewerLink) -> bool:
        self._prune_viewer_links()
        pending = self._pending_viewer_links.pop(request.code, None)
        if pending is None:
            return False
        if self._viewer_link_by_device.get(pending.device_id) == request.code:
            self._viewer_link_by_device.pop(pending.device_id, None)
        if pending.expires_at <= int(self._clock()):
            return False
        operation = asyncio.create_task(
            asyncio.to_thread(
                self.store.bind_viewer_token,
                pending.device_id,
                request.viewer_token,
            )
        )
        try:
            linked = bool(await asyncio.shield(operation))
        except asyncio.CancelledError:
            await asyncio.gather(operation, return_exceptions=True)
            raise
        except Exception:
            linked = False
        if linked:
            # Re-authentication picks up the new binding.
            await self._close_caption_device(pending.device_id)
        return linked

    async def process_request(
        self,
        _connection: ServerConnection,
        request: Request,
    ) -> Response | None:
        if request.path == HEALTH_PATH:
            body = b'{"status":"ok","service":"vemals-ears"}\n'
            return _http_response(body, "application/json; charset=utf-8")
        if request.path == UPDATE_PATH:
            authentication = _update_authentication(request)
            device = None
            if authentication is not None:
                device = await self._device_record(
                    authentication.device_id,
                    authentication.device_token,
                    authentication.client_version,
                )
            if device is None:
                body = b'{"type":"error","code":"authentication_failed"}\n'
                return _http_response(
                    body,
                    "application/json; charset=utf-8",
                    status_code=401,
                )
            release = await self._published_release()
            manifest = {
                "type": "update_manifest",
                "update": update_offer(
                    self.config.client_version,
                    self.config.download_file,
                    self.config.client_download_url,
                    release=release,
                    public_server_url=self.config.public_server_url,
                ),
            }
            body = (
                json.dumps(
                    manifest,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("ascii")
            return _http_response(body, "application/json; charset=utf-8")
        if request.path == DOWNLOAD_PATH:
            release = await self._published_release()
            target = _member_download_file(
                self.config.download_file,
                release,
            )
            expected_bytes = (
                release.snapshot.installer.bytes
                if release.snapshot is not None
                else None
            )
            response = (
                await self._serve_attachment(
                    _connection,
                    target,
                    DOWNLOAD_CONTENT_TYPES.get(
                        target.suffix.lower(), "application/octet-stream"
                    ),
                    expected_bytes=expected_bytes,
                    expected_sha256=(
                        release.snapshot.installer.sha256
                        if release.snapshot is not None
                        else None
                    ),
                )
                if target is not None
                else None
            )
            if response is None:
                return _http_response(
                    b"No download is published on this server." + b"\n",
                    "text/plain; charset=utf-8",
                    status_code=404,
                )
            return response
        if request.path == SOURCE_DOWNLOAD_PATH or request.path.startswith(
            SOURCE_DOWNLOAD_PREFIX
        ):
            version = None
            if request.path != SOURCE_DOWNLOAD_PATH:
                version = request.path[len(SOURCE_DOWNLOAD_PREFIX):]
            release = await self._published_release()
            target = _source_download_file(
                self.config.download_file,
                version,
                release,
            )
            expected_bytes = (
                release.snapshot.source.bytes
                if release.snapshot is not None
                and target == release.snapshot.source.path
                else None
            )
            response = (
                await self._serve_attachment(
                    _connection,
                    target,
                    "application/zip",
                    expected_bytes=expected_bytes,
                    expected_sha256=(
                        release.snapshot.source.sha256
                        if release.snapshot is not None
                        and target == release.snapshot.source.path
                        else None
                    ),
                )
                if target is not None
                else None
            )
            if response is None:
                return _http_response(
                    b"No Vemal's Ears source bundle is published on this server.\n",
                    "text/plain; charset=utf-8",
                    status_code=404,
                )
            return response
        if request.path.startswith(DOWNLOAD_PREFIX):
            version = request.path[len(DOWNLOAD_PREFIX):]
            release = await self._published_release()
            target = _versioned_installer_file(
                self.config.download_file,
                version,
                release,
            )
            expected_bytes = (
                release.snapshot.installer.bytes
                if release.snapshot is not None
                and target == release.snapshot.installer.path
                else None
            )
            response = (
                await self._serve_attachment(
                    _connection,
                    target,
                    "application/octet-stream",
                    expected_bytes=expected_bytes,
                    expected_sha256=(
                        release.snapshot.installer.sha256
                        if release.snapshot is not None
                        and target == release.snapshot.installer.path
                        else None
                    ),
                )
                if target is not None
                else None
            )
            if response is None:
                return _http_response(
                    b"No matching Vemal's Ears installer is published on this server.\n",
                    "text/plain; charset=utf-8",
                    status_code=404,
                )
            return response
        if request.path == PRIVACY_PATH:
            return _http_response(
                _privacy_page(self.config.guild_name, self.config.replay_limit),
                "text/html; charset=utf-8",
            )
        if request.path == PAIRING_CONFIG_PATH:
            body = json.dumps(
                {
                    "service": "vemals-ears",
                    "server_url": self.config.public_server_url,
                    "guild_name": self.config.guild_name,
                }
            ).encode("utf-8")
            return _http_response(body, "application/json; charset=utf-8")
        if request.path in {JOIN_PATH, JOIN_PATH + "/"}:
            release = await self._published_release()
            if release.pointer_present:
                snapshot = release.snapshot
                client_download_url = (
                    _versioned_download_url(
                        _public_http_from_server(self.config.public_server_url),
                        snapshot.version,
                    )
                    if snapshot is not None
                    else ""
                )
                source_download_url = (
                    SOURCE_DOWNLOAD_PREFIX + snapshot.version
                    if snapshot is not None and client_download_url
                    else ""
                )
            else:
                client_download_url = self.config.client_download_url
                source_download_url = (
                    SOURCE_DOWNLOAD_PREFIX + self.config.client_version
                    if _source_download_file(
                        self.config.download_file,
                        self.config.client_version,
                        release,
                    ) is not None
                    else ""
                )
            body = _landing_page(
                self.config.public_server_url,
                client_download_url,
                source_download_url=source_download_url,
            )
            return _http_response(
                body,
                "text/html; charset=utf-8",
                script_hash=_JOIN_SCRIPT_HASH,
            )
        if request.path in {
            PAIR_PATH,
            CAPTION_PATH,
            PUBLISH_PATH,
            QUICK_CHAT_PATH,
            QUICK_CHAT_PROFILE_PATH,
            QUICK_CHAT_WORKER_PATH,
            VIEWER_LINK_PATH,
        }:
            return None
        return _http_response(b"Not found.\n", "text/plain; charset=utf-8", 404)

    async def handle(self, connection: ServerConnection) -> None:
        if len(self._active_handlers) >= self.config.connection_limit:
            await connection.close(code=1013, reason="Service unavailable")
            return
        self._active_handlers.add(connection)
        try:
            if connection.subprotocol not in CAPTION_SUBPROTOCOLS:
                await connection.close(code=1002, reason="Subprotocol required")
                return
            path = connection.request.path if connection.request is not None else ""
            try:
                if path == PAIR_PATH:
                    await self._pair(connection)
                elif path == CAPTION_PATH:
                    await self._captions(connection)
                elif path == PUBLISH_PATH:
                    await self._publish(connection)
                elif path == QUICK_CHAT_PATH:
                    await self._quick_chat(connection)
                elif path == QUICK_CHAT_PROFILE_PATH:
                    await self._quick_chat_profile(connection)
                elif path == QUICK_CHAT_WORKER_PATH:
                    await self._quick_chat_worker(connection)
                elif path == VIEWER_LINK_PATH:
                    await self._viewer_link(connection)
                else:
                    await connection.close(code=1008, reason="Unknown endpoint")
            except ConnectionClosed:
                return
            except Exception as error:
                # Do not include exception messages: protocol and database
                # errors can turn attacker-controlled content into log output.
                LOGGER.error("Connection handler failed (%s)", type(error).__name__)
                try:
                    await self._error_and_close(
                        connection,
                        "server_error",
                        "The service could not complete the request.",
                        1011,
                    )
                except ConnectionClosed:
                    pass
        finally:
            self._active_handlers.discard(connection)

    async def _pair(self, connection: ServerConnection) -> None:
        try:
            payload = await self._first_message(connection)
            request = parse_pair_request(payload)
        except (GuildHubProtocolError, TimeoutError):
            await self._error_and_close(
                connection, "invalid_request", "Invalid request.", 1008
            )
            return
        try:
            credentials = self.store.claim_enrollment(
                request.enrollment_code,
                request.device_name,
                request.client_version,
            )
        except PairingError:
            await self._error_and_close(
                connection, "enrollment_failed", "Enrollment failed.", 1008
            )
            return
        await connection.send(
            encode_message(
                {
                    "type": "paired",
                    "device_id": credentials.device_id,
                    "device_token": credentials.device_token,
                    "guild_id": self.config.guild_id,
                    "guild_name": self.config.guild_name,
                }
            )
        )
        await connection.close(code=1000, reason="Enrollment complete")

    async def _viewer_link(self, connection: ServerConnection) -> None:
        try:
            payload = await self._first_message(connection)
            request = parse_viewer_link_request(payload)
            device = await self._device_record(
                request.device_id,
                request.device_token,
            )
        except (GuildHubProtocolError, TimeoutError):
            device = None
        if device is None:
            await self._authentication_failed(connection)
            return
        code, expires_at = self._new_viewer_link(device.device_id)
        await connection.send(
            encode_message(viewer_link_response(code, expires_at))
        )
        await connection.close(code=1000, reason="Viewer link created")

    async def _captions(self, connection: ServerConnection) -> None:
        authentication = None
        device = None
        try:
            payload = await self._first_message(connection)
            authentication = parse_device_authentication(payload)
            # Only the opening handshake records the version. The 15 second
            # revalidation loop reuses this same call and would otherwise
            # rewrite an unchanged string onto every device, every 15
            # seconds, forever.
            device = await self._device_record(
                authentication.device_id,
                authentication.device_token,
                authentication.client_version,
            )
        except (GuildHubProtocolError, TimeoutError):
            device = None
        if device is None or authentication is None:
            await self._authentication_failed(connection)
            return
        viewer = device.viewer_token
        understands_stop = understands_captions_stopped(
            authentication.client_version
        )
        if not self._viewer_is_current(viewer):
            await self._error_and_close(
                connection, *self._ineligible_close(understands_stop), 1008
            )
            return

        async def close_slow() -> None:
            try:
                await connection.close(code=1013, reason="Service unavailable")
            except ConnectionClosed:
                pass

        background_tasks: set[asyncio.Task[object]] = set()
        previous: _CaptionSession | None = None
        subscriber: _Subscriber | None = None
        session: _CaptionSession | None = None
        access_denied = False
        # The lock covers only the map mutation. Closing the previous
        # connection happens OUTSIDE it, in a background task, because
        # websockets bounds that close only by close_timeout - five
        # seconds here - against a peer that never answers, and one
        # member's sleeping laptop must not park a server-wide lock that
        # every connect and every disconnect cleanup has to take.
        async with self._caption_session_lock:
            if not self._viewer_is_current(viewer):
                access_denied = True
            else:
                previous = self._caption_sessions.pop(
                    authentication.device_id, None
                )
                if previous is not None:
                    self.broker.unsubscribe(previous.subscriber)
                minimum_cursor = self._viewer_join_cursor.get(
                    str(viewer), self.broker.cursor
                )
                subscriber, baseline_cursor, replay = self.broker.subscribe(
                    authentication.last_stream_id,
                    authentication.last_cursor,
                    close_slow,
                    subprotocol=connection.subprotocol,
                    viewer_token=viewer,
                    minimum_cursor=minimum_cursor,
                    understands_stop=understands_stop,
                )
                # Captured under the same no-await critical section as the replay
                # baseline. A newer session event is already queued normally.
                session_snapshot = self.broker.latest_session
                session = _CaptionSession(connection, subscriber, viewer)
                self._caption_sessions[authentication.device_id] = session
        if access_denied:
            await self._error_and_close(
                connection, *self._ineligible_close(understands_stop), 1008
            )
            return
        assert subscriber is not None and session is not None
        if previous is not None:
            # One caption socket per device is deliberate, and a single
            # replacement is ordinary - a laptop waking, a client restart.
            # A CONTINUOUS stream of them is the shape of two companion
            # installs sharing one pairing: each connect kicks the other
            # with an unplanned 1008, the kicked one reconnects, and they
            # trade the socket forever with nothing in the log. The
            # device id is the public identifier; the token never appears.
            LOGGER.info(
                "caption connection for device %s replaced by a newer "
                "connection from the same device; a steady run of these "
                "means two clients share one pairing",
                authentication.device_id,
            )

            async def close_previous(target=previous):
                try:
                    await target.connection.close(
                        code=1008, reason="Connection closed"
                    )
                except ConnectionClosed:
                    pass

            replaced = asyncio.create_task(close_previous())
            background_tasks.add(replaced)
            replaced.add_done_callback(background_tasks.discard)
        try:
            authenticated_message = {
                "type": "authenticated",
                "stream_id": self.broker.stream_id,
                "cursor": baseline_cursor,
            }
            # Presence of client_version is the capability bit. The relay
            # learned that optional request field before clients began
            # sending it, while old strict clients keep receiving the exact
            # three-field response they already understand.
            if (
                authentication.client_version
                and session_snapshot is not None
                and subscriber.replay_generation
                == self.broker.replay_generation
            ):
                authenticated_message["session"] = json.loads(
                    event_to_json(session_snapshot)
                )
            # Update advertisement rides the AUTHENTICATED channel on
            # purpose. The download itself is public, so its checksum has
            # to arrive somewhere an attacker who can rewrite the download
            # cannot also rewrite the number it is checked against.
            release = await self._published_release()
            update = update_offer(
                self.config.client_version,
                self.config.download_file,
                self.config.client_download_url,
                release=release,
                public_server_url=self.config.public_server_url,
            )
            if update is not None:
                authenticated_message["update"] = update
            await connection.send(encode_message(authenticated_message))
            revalidator = asyncio.create_task(
                self._revalidate_caption_access(
                    connection,
                    authentication.device_id,
                    authentication.device_token,
                    subscriber,
                )
            )
            background_tasks.add(revalidator)
            for encoded in replay:
                if not await self._send_caption_frame(
                    connection,
                    subscriber,
                    encoded,
                    replay_generation=subscriber.replay_generation,
                ):
                    return

            sender = asyncio.create_task(
                self._send_captions(connection, subscriber)
            )
            closed = asyncio.create_task(connection.wait_closed())
            background_tasks.update({sender, closed})
            done, pending = await asyncio.wait(
                {sender, revalidator, closed}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled():
                    task.exception()
        finally:
            for task in background_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*background_tasks, return_exceptions=True)
            self.broker.unsubscribe(subscriber)
            async with self._caption_session_lock:
                if self._caption_sessions.get(authentication.device_id) is session:
                    self._caption_sessions.pop(authentication.device_id, None)

    async def _publish(self, connection: ServerConnection) -> None:
        try:
            payload = await self._first_message(connection)
            provided = parse_service_authentication(payload, "publisher_token")
            authenticated = _constant_token_equal(
                provided, self.config.publisher_token
            )
        except (GuildHubProtocolError, TimeoutError):
            authenticated = False
        if not authenticated:
            await self._authentication_failed(connection)
            return
        async with self._publisher_lock:
            previous = self._publisher_connection
            if previous is not None and previous is not connection:
                try:
                    await previous.close(code=1008, reason="Connection closed")
                except ConnectionClosed:
                    pass
            self._publisher_connection = connection
        # A publisher connection is the lease owner. Never inherit a roster
        # or capture snapshot from the worker it replaced.
        await self._clear_audience()
        try:
            await connection.send(
                encode_message({"type": "authenticated", "cursor": self.broker.cursor})
            )
            async for raw in connection:
                try:
                    command = parse_publisher_command(
                        decode_message(raw),
                        subprotocol=connection.subprotocol,
                    )
                    if isinstance(command, AudienceUpdate):
                        await self._apply_audience_update(command)
                        response = {
                            "type": "audience_updated",
                            "generation": command.generation,
                            "revision": command.revision,
                        }
                    elif isinstance(command, ViewerLink):
                        linked = await self._bind_viewer(command)
                        response = {
                            "type": "viewer_linked",
                            "code": command.code,
                            "linked": linked,
                        }
                    elif isinstance(command, ViewerEnrollment):
                        created = await self._invite_viewer(command)
                        response = {
                            "type": "viewer_enrolled",
                            "code": command.code,
                            "created": created,
                        }
                    else:
                        audience = self._publish_audience(command)
                        cursor = self.broker.publish(command.event, audience)
                        response = {"type": "published", "cursor": cursor}
                except GuildHubProtocolError:
                    await self._error_and_close(
                        connection, "invalid_request", "Invalid request.", 1008
                    )
                    return
                await connection.send(encode_message(response))
        finally:
            lost_lease = False
            async with self._publisher_lock:
                if self._publisher_connection is connection:
                    self._publisher_connection = None
                    lost_lease = True
            if lost_lease:
                await self._clear_audience()

    async def _quick_chat(self, connection: ServerConnection) -> None:
        """Publish one authenticated Message Group line to the authorized feed."""
        try:
            request = parse_quick_chat(await self._first_message(connection))
            device = await self._device_record(
                request.device_id,
                request.device_token,
            )
        except (GuildHubProtocolError, TimeoutError):
            device = None
            request = None
        if device is None or request is None:
            await self._authentication_failed(connection)
            return
        if not device.quick_chat_name:
            await self._error_and_close(
                connection,
                "quick_chat_profile_required",
                "Set your typed-chat display name in Vemal's Ears Preferences.",
                1008,
            )
            return

        entry, immediate = await self._quick_chat_submission(request, device)
        if immediate is not None:
            outcome = immediate
        elif entry is not None:
            try:
                outcome = await self._await_quick_chat_response(entry)
            except TimeoutError:
                # Practically unreachable now that nothing in the shared task
                # waits on Discord - but a starved relay can still miss the
                # deadline. Keep the task alive: it will still publish, and a
                # retry with this same request ID returns that cursor.
                outcome = _QuickChatOutcome(
                    code="delivery_pending",
                    message=(
                        "The relay is still processing this message. "
                        "Retry the same draft in a moment."
                    ),
                    close_code=1013,
                )
        else:
            outcome = _QuickChatOutcome(
                code="service_busy",
                message="Quick chat is busy. Try again with a new message.",
                close_code=1013,
            )
        if outcome.cursor is None:
            await self._error_and_close(
                connection,
                outcome.code,
                outcome.message,
                outcome.close_code,
            )
            return
        await connection.send(
            encode_message(
                quick_chat_acknowledgement(request.request_id, outcome.cursor)
            )
        )
        await connection.close(code=1000, reason="Quick chat sent")

    async def _quick_chat_profile(self, connection: ServerConnection) -> None:
        """Read or update only the authenticated device's typed-chat name."""
        try:
            request = parse_quick_chat_profile_request(
                await self._first_message(connection)
            )
            device = await self._device_record(
                request.device_id,
                request.device_token,
            )
        except (GuildHubProtocolError, TimeoutError):
            device = None
            request = None
        if device is None or request is None:
            await self._authentication_failed(connection)
            return

        name = device.quick_chat_name
        if request.name is not None:
            name, storage_failed = await self._set_quick_chat_name(
                request.device_id,
                request.name,
            )
            if storage_failed:
                await self._error_and_close(
                    connection,
                    "profile_update_failed",
                    "The typed-chat profile could not be saved. Try again.",
                    1011,
                )
                return
            if name is None:
                # A concurrent revocation must not look like a successful
                # self-service profile update.
                await self._authentication_failed(connection)
                return

        await connection.send(encode_message(quick_chat_profile(name)))
        await connection.close(code=1000, reason="Quick chat profile ready")

    async def _quick_chat_worker(self, connection: ServerConnection) -> None:
        """Keep the deployed worker's authenticated health socket available.

        PRIVACY: this compatibility channel never receives Message Group text.
        A pre-upgrade worker may send a stale delivery receipt after a relay
        restart; valid receipts are ignored and malformed frames remain fatal.
        """
        try:
            payload = await self._first_message(connection)
            provided = parse_service_authentication(payload, "publisher_token")
            authenticated = _constant_token_equal(
                provided, self.config.publisher_token
            )
        except (GuildHubProtocolError, TimeoutError):
            authenticated = False
        if not authenticated:
            await self._authentication_failed(connection)
            return

        # A worker must observe its authentication acknowledgement before the
        # relay can make it dispatchable. Otherwise a replacement could be
        # handed a command while it was still waiting for this exact frame.
        await connection.send(encode_message({"type": "authenticated"}))
        session = _QuickChatWorkerSession(connection)
        async with self._quick_chat_worker_lock:
            previous = self._quick_chat_worker_session
            if previous is not None:
                self._deactivate_quick_chat_worker(previous)
            self._quick_chat_worker_session = session
        try:
            if previous is not None and previous.connection is not connection:
                try:
                    await previous.connection.close(
                        code=1008, reason="Connection closed"
                    )
                except ConnectionClosed:
                    pass
            async for raw in connection:
                try:
                    parse_quick_chat_delivery(decode_message(raw))
                except GuildHubProtocolError:
                    await self._error_and_close(
                        connection,
                        "invalid_request",
                        "Invalid request.",
                        1008,
                    )
                    return
                # Compatibility only. The relay never issues a corresponding
                # command, so no receipt can authorize or complete work.
                continue
        finally:
            async with self._quick_chat_worker_lock:
                if self._quick_chat_worker_session is session:
                    self._quick_chat_worker_session = None
                self._deactivate_quick_chat_worker(session)

    async def _quick_chat_submission(
        self, request: QuickChatRequest, device: DeviceRecord
    ) -> tuple[_QuickChatCacheEntry | None, _QuickChatOutcome | None]:
        now = time.monotonic()
        key = (request.device_id, request.request_id)
        fingerprint = hashlib.sha256(
            (
                request.device_id
                + "\0"
                + request.request_id
                + "\0"
                + request.text
            ).encode("utf-8")
        ).digest()
        async with self._quick_chat_state_lock:
            self._prune_quick_chat_state(now)
            cached = self._quick_chat_cache.get(key)
            if cached is not None:
                if not hmac.compare_digest(cached.fingerprint, fingerprint):
                    return None, _QuickChatOutcome(
                        code="request_conflict",
                        message="That request ID was already used for another message.",
                    )
                return cached, None
            removable: list[tuple[str, str]] = []
            needed = len(self._quick_chat_cache) - QUICK_CHAT_CACHE_LIMIT + 1
            if needed > 0:
                removable = [
                    existing_key
                    for existing_key, existing in self._quick_chat_cache.items()
                    if existing.task.done()
                ][:needed]
                if len(removable) < needed:
                    return None, None
            if not self._consume_quick_chat_rate(request.device_id, now):
                return None, _QuickChatOutcome(
                    code="rate_limited",
                    message="Wait a moment before sending more quick-chat messages.",
                )
            for removable_key in removable:
                self._quick_chat_cache.pop(removable_key, None)
            task = asyncio.create_task(
                self._process_quick_chat(request, device)
            )
            entry = _QuickChatCacheEntry(fingerprint, task)
            self._quick_chat_cache[key] = entry
            task.add_done_callback(
                lambda completed, cache_key=key: self._discard_failed_quick_chat(
                    cache_key, completed
                )
            )
            return entry, None

    def _discard_failed_quick_chat(
        self,
        key: tuple[str, str],
        task: asyncio.Task[_QuickChatOutcome],
    ) -> None:
        """Retain successes for ack-loss retry; let processing failures retry."""
        try:
            successful = task.result().cursor is not None
        except BaseException:
            successful = False
        if successful:
            return
        current = self._quick_chat_cache.get(key)
        if current is not None and current.task is task:
            self._quick_chat_cache.pop(key, None)

    def _prune_quick_chat_state(self, now: float) -> None:
        for device_id, window in tuple(self._quick_chat_rate_windows.items()):
            while window and now - window[0] >= QUICK_CHAT_RATE_WINDOW_SECONDS:
                window.popleft()
            if not window:
                self._quick_chat_rate_windows.pop(device_id, None)

    def _consume_quick_chat_rate(self, device_id: str, now: float) -> bool:
        window = self._quick_chat_rate_windows.get(device_id)
        if window is None:
            # Pruning happens immediately before this call. If every bounded
            # slot still represents a live window, fail closed instead of
            # evicting one and letting that device bypass its own limit.
            if len(self._quick_chat_rate_windows) >= QUICK_CHAT_RATE_DEVICE_LIMIT:
                return False
            window = deque()
            self._quick_chat_rate_windows[device_id] = window
        else:
            self._quick_chat_rate_windows.move_to_end(device_id)
        while window and now - window[0] >= QUICK_CHAT_RATE_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= QUICK_CHAT_RATE_LIMIT:
            return False
        window.append(now)
        return True

    async def _process_quick_chat(
        self, request: QuickChatRequest, device: DeviceRecord
    ) -> _QuickChatOutcome:
        # Typed text is the raid's fallback when hearing fails, so it publishes
        # directly to the authorized feed. The compatibility worker connection
        # remains available for deployed hosts, but Message Group content never
        # enters that control channel.
        #
        # Nothing here gates acceptance on a session being live. Shino, 2026-09-04:
        # "ability to type when voice goes down is a feature not a bug."
        # The relay accepts the line without the session, publisher, or worker.
        # Visible delivery still uses the current authorized audience. Publisher
        # loss clears that audience and replay, so nobody receives stale text.
        # Never substitute an absent or previous roster for that empty audience.
        #
        # There is deliberately no await between selecting the sequence and
        # publishing, so another caption cannot take the cursor in between.
        event = CaptionEvent(
            speaker_id=(
                "quick-chat-"
                + hashlib.sha256(request.device_id.encode("utf-8")).hexdigest()[:32]
            ),
            speaker=str(device.quick_chat_name) + " [typed]",
            character="",
            role="UNKNOWN",
            text=request.text,
            final=True,
            sequence=self.broker.cursor + 1,
            timestamp=time.time(),
            wow_class=device.quick_chat_wow_class,
        )
        cursor = self.broker.publish(
            event,
            self._current_audience(),
            session_bound=False,
        )
        return _QuickChatOutcome(cursor=cursor)

    async def _await_quick_chat_response(
        self, entry: _QuickChatCacheEntry
    ) -> _QuickChatOutcome:
        """Wait for shared relay work, or for the member response deadline."""
        return await asyncio.wait_for(
            asyncio.shield(entry.task),
            timeout=self.config.quick_chat_response_timeout,
        )

    @staticmethod
    def _deactivate_quick_chat_worker(
        session: _QuickChatWorkerSession,
    ) -> None:
        session.active = False

    async def _first_message(self, connection: ServerConnection) -> dict[str, object]:
        raw = await asyncio.wait_for(
            connection.recv(), timeout=self.config.auth_timeout
        )
        return decode_message(raw)

    async def _send_caption_frame(
        self,
        connection: ServerConnection,
        subscriber: _Subscriber,
        encoded: str,
        *,
        replay_generation: int | None = None,
    ) -> bool:
        if not subscriber.active:
            return False
        if not self._viewer_is_current(subscriber.viewer_token):
            self.broker.unsubscribe(subscriber)
            await self._error_and_close(
                connection,
                *self._ineligible_close(subscriber.understands_stop),
                1008,
            )
            return False
        if (
            replay_generation is not None
            and replay_generation != self.broker.replay_generation
        ):
            # clear_replay() may run after subscribe() copied these frames but
            # before authentication finished. Preserve cursor continuity while
            # replacing every invalidated frame with a text-free advance.
            stale = decode_message(encoded)
            cursor = stale.get("cursor")
            if not isinstance(cursor, int):
                self.broker.unsubscribe(subscriber)
                await connection.close(code=1011, reason="Service unavailable")
                return False
            encoded = encode_message(cursor_advance(self.broker.stream_id, cursor))
        # No await may separate the access check from starting this send.
        await connection.send(encoded)
        return True

    async def _send_captions(
        self,
        connection: ServerConnection,
        subscriber: _Subscriber,
    ) -> None:
        while subscriber.active:
            encoded = await subscriber.queue.get()
            if not await self._send_caption_frame(connection, subscriber, encoded):
                return

    async def _revalidate_device(
        self,
        connection: ServerConnection,
        device_id: str,
        device_token: str,
    ) -> None:
        while True:
            await asyncio.sleep(self.config.revalidation_seconds)
            if not await self._device_is_valid(device_id, device_token):
                await connection.close(code=1008, reason="Connection closed")
                return

    async def _revalidate_caption_access(
        self,
        connection: ServerConnection,
        device_id: str,
        device_token: str,
        subscriber: _Subscriber,
    ) -> None:
        expected_viewer = subscriber.viewer_token
        interval = min(
            self.config.revalidation_seconds, self.config.audience_lease_seconds / 2
        )
        while True:
            await asyncio.sleep(interval)
            device = await self._device_record(device_id, device_token)
            if device is None:
                self.broker.unsubscribe(subscriber)
                await connection.close(code=1008, reason="Connection closed")
                return
            if (
                device.viewer_token != expected_viewer
                or not self._viewer_is_current(expected_viewer)
            ):
                self.broker.unsubscribe(subscriber)
                await self._error_and_close(
                    connection,
                    *self._ineligible_close(subscriber.understands_stop),
                    1008,
                )
                return

    async def _device_is_valid(
        self,
        device_id: str,
        device_token: str,
        client_version: str = "",
    ) -> bool:
        operation = asyncio.create_task(
            asyncio.to_thread(
                self.store.authenticate_device,
                device_id,
                device_token,
                client_version,
            )
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Canceling to_thread doesn't stop its worker. Join it before this
            # handler exits so SQLite handles and auth tasks cannot outlive the
            # connection or server shutdown.
            await asyncio.gather(operation, return_exceptions=True)
            raise
        except Exception:
            # Storage failures must fail closed without putting any credential
            # or database diagnostic into a server log.
            return False

    async def _device_record(
        self,
        device_id: str,
        device_token: str,
        client_version: str = "",
    ) -> DeviceRecord | None:
        operation = asyncio.create_task(
            asyncio.to_thread(
                self.store.authenticate_device_record,
                device_id,
                device_token,
                client_version,
            )
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            await asyncio.gather(operation, return_exceptions=True)
            raise
        except Exception:
            return None

    async def _set_quick_chat_name(
        self, device_id: str, name: str
    ) -> tuple[str | None, bool]:
        operation = asyncio.create_task(
            asyncio.to_thread(self.store.set_quick_chat_name, device_id, name)
        )
        try:
            return await asyncio.shield(operation), False
        except asyncio.CancelledError:
            await asyncio.gather(operation, return_exceptions=True)
            raise
        except Exception:
            # The submitted name and storage diagnostics stay out of logs.
            return None, True

    async def _authentication_failed(self, connection: ServerConnection) -> None:
        await self._error_and_close(
            connection,
            "authentication_failed",
            "Authentication failed.",
            1008,
        )

    @staticmethod
    async def _error_and_close(
        connection: ServerConnection,
        code: str,
        message: str,
        close_code: int,
    ) -> None:
        await connection.send(encode_message(error_message(code, message)))
        await connection.close(code=close_code, reason=message)


def _constant_token_equal(provided: str, expected: str) -> bool:
    """Compare fixed-size digests so token length doesn't shortcut equality."""
    provided_hash = hashlib.sha256(provided.encode("utf-8")).digest()
    expected_hash = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(provided_hash, expected_hash)


async def _housekeeping_loop(application: GuildHubServer) -> None:
    """Retire credentials nobody uses, on a timer, out loud.

    Revoking is reversible - the person pairs again - and nothing here
    touches a device that has been seen recently. The counts are logged
    because a credential disappearing silently is exactly the kind of
    thing that turns into "it just stopped working" a week later.
    """
    config = application.config
    interval = config.housekeeping_seconds
    if interval <= 0:
        LOGGER.info("credential housekeeping is disabled")
        return
    while True:
        await asyncio.sleep(interval)
        try:
            enrollments = await asyncio.to_thread(application.store.prune_enrollments)
            never_used, idle = await asyncio.to_thread(
                application.store.revoke_unused_devices,
                unused_after=config.unused_device_seconds,
                idle_after=config.idle_device_seconds,
            )
            forgotten = await asyncio.to_thread(
                application.store.forget_revoked_devices,
                older_than=config.revoked_retention_seconds,
            )
        except Exception as error:
            # Housekeeping is not worth taking the relay down for.
            LOGGER.warning("credential housekeeping failed: %s", type(error).__name__)
            continue
        if enrollments or never_used or idle or forgotten:
            LOGGER.info(
                "credential housekeeping: %d spent invitations, %d never-used "
                "and %d idle devices revoked, %d long-revoked forgotten",
                enrollments, never_used, idle, forgotten,
            )


async def _wait_for_shutdown() -> None:
    """Block until the service is asked to stop.

    nssm stops a service by sending a console Ctrl+C, which reaches Python
    as SIGINT. The default handler raises KeyboardInterrupt straight out of
    the loop, which is why the relay used to disappear without a word: by
    the time anything could be said, the loop was already unwinding. Taking
    the signal ourselves converts it into an ordinary awaited event, so the
    announcement happens while the server is still up and able to send it.

    The quarter-second poll is a Windows accommodation, not decoration. The
    proactor loop can sit in a blocking syscall that a signal does not
    interrupt, so without a periodic wakeup the handler's callback is not
    processed until the next unrelated event - which during a quiet raid
    night may be after nssm has given up waiting and terminated us.
    """
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_shutdown(signum, frame):  # noqa: ARG001 - signal signature
        loop.call_soon_threadsafe(shutdown.set)

    installed: list[tuple[int, object]] = []
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            installed.append((number, signal.signal(number, request_shutdown)))
        except (OSError, ValueError):
            # Not the main thread, or not supported on this platform. The
            # relay still runs; it just loses the graceful announcement.
            continue
    try:
        while not shutdown.is_set():
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=0.25)
            except (asyncio.TimeoutError, TimeoutError):
                continue
    finally:
        for number, previous in installed:
            try:
                signal.signal(number, previous)
            except (OSError, ValueError):
                pass


async def run_server(
    application: GuildHubServer,
    bind: str,
    port: int,
) -> None:
    non_loopback = not _is_loopback(bind)
    if non_loopback and not application.config.trust_proxy:
        raise RuntimeError(
            "non-loopback binding requires VEMALS_EARS_TRUST_PROXY=1"
        )
    if non_loopback:
        LOGGER.warning(
            "Plain WebSocket is bound beyond loopback; terminate TLS upstream."
        )
    async with serve(
        application.handle,
        bind,
        port,
        subprotocols=list(CAPTION_SUBPROTOCOLS),
        process_request=application.process_request,
        compression=None,
        max_size=MAX_WIRE_BYTES,
        max_queue=8,
        write_limit=32_768,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
        logger=WEBSOCKET_LOGGER,
    ) as websocket_server:
        LOGGER.info("Vemal's Ears listening on %s:%d", bind, port)
        LOGGER.info(voice_gate_notice())
        housekeeping = asyncio.create_task(_housekeeping_loop(application))
        try:
            await _wait_for_shutdown()
        finally:
            housekeeping.cancel()
            await asyncio.gather(housekeeping, return_exceptions=True)
            # Inside the context manager on purpose: the server has to
            # still be listening for these close frames to go out. Never
            # allowed to prevent the shutdown it is announcing.
            try:
                await application.announce_maintenance()
            except Exception:  # noqa: BLE001 - shutdown must not be blocked
                LOGGER.exception("could not announce planned maintenance")


def voice_gate_notice() -> str:
    """The one line every relay start says about who can read captions.

    There is nothing to configure; the line exists so the log records the
    policy the process is running, next to the code that enforces it.
    """
    return (
        "PRIVACY: captions and typed chat reach only paired devices inside "
        "the active Discord voice channel. Fixed in code, not a setting - "
        "see the PRIVACY note in GuildHub/server.py."
    )


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _canonical_netloc(parsed: object) -> tuple[str, bool]:
    hostname = getattr(parsed, "hostname", None)
    if not isinstance(hostname, str) or not hostname:
        raise ValueError("URL must include a host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            rendered_host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError("URL host is invalid") from None
        if (
            not re.fullmatch(r"[a-z0-9.-]+", rendered_host)
            or ".." in rendered_host
            or rendered_host.startswith((".", "-"))
            or rendered_host.endswith((".", "-"))
        ):
            raise ValueError("URL host is invalid")
        loopback = rendered_host == "localhost"
    else:
        if getattr(address, "scope_id", None) is not None:
            raise ValueError("URL host is invalid")
        rendered_host = address.compressed
        loopback = address.is_loopback
        if address.version == 6:
            rendered_host = "[" + rendered_host + "]"
    try:
        port = getattr(parsed, "port")
    except ValueError:
        raise ValueError("URL port is invalid") from None
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("URL port is invalid")
    scheme = str(getattr(parsed, "scheme", "")).lower()
    if (scheme, port) in {("wss", 443), ("ws", 80), ("https", 443), ("http", 80)}:
        port = None
    if port is not None:
        rendered_host += ":" + str(port)
    return rendered_host, loopback


def _pair_server_url(value: str, label: str = "--server-url") -> str:
    if not isinstance(value, str):
        raise ValueError(label + " is invalid")
    raw = value.strip()
    if not raw or len(raw) > 320 or any(char.isspace() for char in raw):
        raise ValueError(label + " is invalid")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise ValueError(label + " is invalid") from None
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError(label + " must be a WebSocket server origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(label + " must not include credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError(label + " must not include a path")
    try:
        netloc, loopback = _canonical_netloc(parsed)
    except ValueError as error:
        raise ValueError(label + " " + str(error).removeprefix("URL ")) from None
    scheme = parsed.scheme.lower()
    if scheme == "ws" and not loopback:
        raise ValueError("a remote " + label + " must use encrypted wss")
    return scheme + "://" + netloc


def _download_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("client download URL is invalid")
    raw = value.strip()
    if (
        not raw
        or len(raw) > 2_048
        or any(unicodedata.category(char).startswith("C") for char in raw)
        or "\\" in raw
    ):
        raise ValueError("client download URL is invalid")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise ValueError("client download URL is invalid") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("client download URL must use https")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("client download URL cannot contain credentials or a fragment")
    if parsed.path in {"", "/"}:
        raise ValueError("client download URL must include an installer path")
    try:
        netloc, loopback = _canonical_netloc(parsed)
    except ValueError as error:
        raise ValueError(
            "client download URL " + str(error).removeprefix("URL ")
        ) from None
    scheme = parsed.scheme.lower()
    if scheme == "http" and not loopback:
        raise ValueError("a remote client download URL must use https")
    path = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&%:@!$'()*+,;/?-._~")
    return urlunsplit((scheme, netloc, path, query, ""))


def _public_http_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("--join-url is invalid")
    raw = value.strip()
    if not raw or len(raw) > 320 or any(char.isspace() for char in raw):
        raise ValueError("--join-url is invalid")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise ValueError("--join-url is invalid") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("--join-url must be an HTTP server origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("--join-url must not include credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("--join-url must not include a path")
    try:
        netloc, loopback = _canonical_netloc(parsed)
    except ValueError as error:
        raise ValueError("--join-url " + str(error).removeprefix("URL ")) from None
    scheme = parsed.scheme.lower()
    if scheme == "http" and not loopback:
        raise ValueError("a remote --join-url must use encrypted https")
    return scheme + "://" + netloc


def _public_http_from_server(public_server_url: str) -> str:
    parsed = urlsplit(public_server_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return scheme + "://" + parsed.netloc


# Secrets can arrive as the value itself or as a path to a mounted secret
# file, exactly as host.py's _load_setting does for the worker. The file
# form is what makes `docker exec ... enroll/list/revoke` work:
# exec builds its environment from the container's stored config, never
# from PID 1, so a value exported by the entrypoint script is invisible
# there. A path in the stored environment is not a secret; the mounted
# file it names is readable by any exec session already inside the
# container's user, which is the same trust boundary as the value.
MAX_SECRET_FILE_BYTES = 4096


def _required_environment(name: str, parser: argparse.ArgumentParser) -> str:
    value = os.environ.get(name, "")
    file_path = os.environ.get(name + "_FILE", "")
    if value and file_path:
        parser.error("Set " + name + " or " + name + "_FILE, not both")
    if not value and file_path:
        try:
            with open(file_path, "rb") as handle:
                raw = handle.read(MAX_SECRET_FILE_BYTES + 1)
        except OSError:
            parser.error(name + "_FILE could not be read")
        if len(raw) > MAX_SECRET_FILE_BYTES:
            parser.error(name + "_FILE is larger than a secret should be")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            parser.error(name + "_FILE is not readable text")
    if not value:
        parser.error(name + " must be set")
    return value


def _timestamp(value: float | None) -> str:
    if value is None:
        return "never"
    return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds")




def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vemals-ears",
        description="Run and administer the Vemal's Ears guild caption service.",
        epilog=(
            "The server speaks plain WebSocket. Bind to loopback or terminate "
            "TLS at a reverse proxy before exposing it outside the host."
        ),
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("VEMALS_EARS_DATABASE", "guild-hub.sqlite3"),
        help="SQLite path (VEMALS_EARS_DATABASE; container default /data/guild-hub.sqlite3)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="run the caption service")
    serve_parser.add_argument(
        "--bind", default=os.environ.get("VEMALS_EARS_BIND", "127.0.0.1")
    )
    serve_parser.add_argument(
        "--port", type=int, default=os.environ.get("VEMALS_EARS_PORT", DEFAULT_PORT)
    )
    serve_parser.add_argument(
        "--guild-id", default=os.environ.get("VEMALS_EARS_GUILD_ID", "guild")
    )
    serve_parser.add_argument(
        "--guild-name",
        default=os.environ.get("VEMALS_EARS_GUILD_NAME", DEFAULT_GUILD_NAME),
    )
    serve_parser.add_argument(
        "--public-server-url",
        default=os.environ.get("VEMALS_EARS_PUBLIC_SERVER_URL", ""),
        help="public wss origin embedded in member pairing links",
    )
    serve_parser.add_argument(
        "--client-download-url",
        default=os.environ.get("VEMALS_EARS_CLIENT_DOWNLOAD_URL", ""),
        help="HTTPS installer URL served by the front proxy",
    )
    serve_parser.add_argument("--auth-timeout", type=float, default=5.0)
    serve_parser.add_argument("--replay-limit", type=int, default=256)
    serve_parser.add_argument("--client-queue", type=int, default=64)
    serve_parser.add_argument(
        "--connection-limit",
        type=int,
        default=os.environ.get("VEMALS_EARS_CONNECTION_LIMIT", 256),
    )
    serve_parser.add_argument(
        "--revalidation-seconds",
        type=float,
        default=os.environ.get("VEMALS_EARS_REVALIDATION_SECONDS", 15.0),
    )
    # Credential housekeeping. Any of these at 0 disables that half.
    for flag, variable, fallback in (
        ("--housekeeping-seconds", "VEMALS_EARS_HOUSEKEEPING_SECONDS", 3600.0),
        ("--unused-device-seconds", "VEMALS_EARS_UNUSED_DEVICE_SECONDS", 48 * 3600.0),
        ("--idle-device-seconds", "VEMALS_EARS_IDLE_DEVICE_SECONDS", 30 * 24 * 3600.0),
        ("--revoked-retention-seconds", "VEMALS_EARS_REVOKED_RETENTION_SECONDS", 30 * 24 * 3600.0),
    ):
        serve_parser.add_argument(
            flag, type=float, default=os.environ.get(variable, fallback)
        )
    serve_parser.add_argument(
        "--download-file",
        default=os.environ.get("VEMALS_EARS_DOWNLOAD_FILE", ""),
        help="local file served at /download (VEMALS_EARS_DOWNLOAD_FILE)",
    )
    serve_parser.add_argument(
        "--client-version",
        default=os.environ.get("VEMALS_EARS_CLIENT_VERSION", ""),
        help="version the published download is, so members can be told "
             "an update exists (VEMALS_EARS_CLIENT_VERSION)",
    )
    serve_parser.add_argument(
        "--trust-proxy",
        action="store_true",
        default=os.environ.get("VEMALS_EARS_TRUST_PROXY", "") == "1",
        help="acknowledge TLS termination before a non-loopback bind",
    )

    enroll_parser = subparsers.add_parser(
        "enroll", help="create one short-lived, one-use enrollment"
    )
    enroll_parser.add_argument("--minutes", type=float, default=30.0)
    enroll_parser.add_argument(
        "--server-url",
        default=os.environ.get("VEMALS_EARS_PUBLIC_SERVER_URL", ""),
        help="public ws:// or wss:// URL to include in a vemalsears://pair link",
    )
    enroll_parser.add_argument(
        "--join-url",
        default=os.environ.get("VEMALS_EARS_PUBLIC_HTTP_URL", ""),
        help="public HTTP origin used to print the /join#<code> landing page",
    )

    subparsers.add_parser("list", help="list enrolled devices without secrets")
    profile_parser = subparsers.add_parser(
        "profile", help="assign one device's quick-chat display identity"
    )
    profile_parser.add_argument("device_id")
    profile_parser.add_argument("--name", required=True)
    profile_parser.add_argument("--wow-class", default="")
    revoke_parser = subparsers.add_parser("revoke", help="revoke one device")
    revoke_parser.add_argument("device_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    pepper = _required_environment("VEMALS_EARS_PEPPER", parser)
    try:
        store = CredentialStore(Path(arguments.database), pepper)
        store.initialize()
    except (OSError, ValueError) as error:
        parser.error(str(error))

    if arguments.command == "serve":
        publisher_token = _required_environment(
            "VEMALS_EARS_PUBLISHER_TOKEN", parser
        )
        try:
            if not 1 <= arguments.port <= 65_535:
                raise ValueError("port must be between 1 and 65535")
            config = ServerConfig(
                guild_id=arguments.guild_id,
                guild_name=arguments.guild_name,
                publisher_token=publisher_token,
                public_server_url=arguments.public_server_url,
                client_download_url=arguments.client_download_url,
                auth_timeout=arguments.auth_timeout,
                replay_limit=arguments.replay_limit,
                client_queue_limit=arguments.client_queue,
                connection_limit=arguments.connection_limit,
                revalidation_seconds=arguments.revalidation_seconds,
                housekeeping_seconds=arguments.housekeeping_seconds,
                unused_device_seconds=arguments.unused_device_seconds,
                idle_device_seconds=arguments.idle_device_seconds,
                revoked_retention_seconds=arguments.revoked_retention_seconds,
                trust_proxy=arguments.trust_proxy,
                download_file=arguments.download_file,
                client_version=arguments.client_version,
            )
        except ValueError as error:
            parser.error(str(error))
        if not _is_loopback(arguments.bind) and not config.trust_proxy:
            parser.error(
                "a non-loopback bind requires VEMALS_EARS_TRUST_PROXY=1"
            )
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        try:
            asyncio.run(run_server(GuildHubServer(store, config), arguments.bind, arguments.port))
        except KeyboardInterrupt:
            return 0
        return 0

    if arguments.command == "enroll":
        server_url = None
        if arguments.server_url:
            try:
                server_url = _pair_server_url(arguments.server_url, "--server-url")
            except ValueError as error:
                parser.error(str(error))
        join_url = None
        if arguments.join_url:
            try:
                join_url = _public_http_url(arguments.join_url)
            except ValueError as error:
                parser.error(str(error))
        elif server_url:
            join_url = _public_http_from_server(server_url)
        try:
            enrollment = store.create_enrollment(arguments.minutes * 60)
        except ValueError as error:
            parser.error(str(error))
        print("Enrollment code: " + enrollment.code)
        print("Expires: " + _timestamp(enrollment.expires_at))
        if server_url:
            query = urlencode(
                {"server": server_url, "code": enrollment.code}
            )
            print("Pairing link: vemalsears://pair?" + query)
        if join_url:
            print(
                "Join page: "
                + join_url
                + JOIN_PATH
                + "#"
                + quote(enrollment.code, safe="")
            )
        return 0

    if arguments.command == "list":
        devices = store.list_devices()
        if not devices:
            print("No devices enrolled.")
            return 0
        for device in devices:
            state = "revoked" if device.revoked_at is not None else "active"
            quick_chat = (
                "quick chat "
                + str(device.quick_chat_name)
                + (
                    " (" + device.quick_chat_wow_class + ")"
                    if device.quick_chat_wow_class
                    else ""
                )
                if device.quick_chat_name
                else "quick chat unassigned"
            )
            print(
                "\t".join(
                    [
                        device.device_id,
                        state,
                        device.device_name,
                        device.client_version,
                        "last seen " + _timestamp(device.last_seen_at),
                        quick_chat,
                    ]
                )
            )
        return 0

    if arguments.command == "profile":
        try:
            saved = store.set_quick_chat_profile(
                arguments.device_id,
                arguments.name,
                arguments.wow_class,
            )
        except ValueError as error:
            parser.error(str(error))
        if saved:
            print("Quick-chat profile assigned: " + arguments.device_id)
            return 0
        print("No active device found: " + arguments.device_id)
        return 1

    if store.revoke_device(arguments.device_id):
        print("Device revoked: " + arguments.device_id)
        return 0
    print("No active device found: " + arguments.device_id)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
