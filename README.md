# Vemal's Ears

Live captions and guild chat for World of Warcraft voice channels, on Windows.

Made for a friend named Vemal, who is deaf, so he could follow the raid
voice chat everyone else took for granted. It is named after him.

## What it does

- Captures a Discord voice channel through a small adapter, sends the audio
  to a self-hosted whisper worker, and shows
  the captions as an overlay in the game.
- Captions go only to paired devices inside the active voice channel. That
  consent gate is a hard rule of the design, not a setting.
- Guild chat and member pairing use the guild relay. A standalone VE relay
  is included for self-hosting; Downtime's shared relay and operator tools
  remain in their separate project.
- Ships as an Authenticode-signed Windows installer with an automatic update
  path, and every release carries its matching source (GPL-3.0).

## Layout

- `VoiceCaptions/` - Discord capture adapter (Node), speech runtime, overlay
- `GuildHub/` - member wire client, pairing helpers, voice worker, standalone relay
- `Hosting/` - Windows GPU host setup, service installer and guide
- `Installer/`, `packaging/`, `Windows/` - installer and native launcher
- `tests/` - 70 test modules; `ci/` - the release checks
- `docs/` - development, CI, source-distribution and migration notes
- `ADR-001.txt` - ownership and boundaries; `SOURCE-IMPORT.json` - provenance

## Run it from source

    python -m pip install -r GuildHub/requirements-client.txt
    npm --prefix VoiceCaptions/discord_adapter ci
    python Vemals-Ears.pyw

`dev.py` runs local validation; `build_release.py` builds the member ZIP.
See `docs/SOURCE.txt` to modify and rebuild without signing keys.

## Host your own guild

For setup without commands, run `Vemals-Ears-Host-Setup.exe` and follow the
graphical wizard. It includes private Python, Node and service tools, prepares
GPU speech recognition, and can install your Cloudflare Tunnel connector.
NVIDIA (CUDA) is the tested path. AMD GPUs can use a new Vulkan engine in
builds that include it; it is expected to be slower than CUDA and has not
yet been validated on real AMD hardware.
See [the host quick start](Hosting/QUICKSTART.txt).

The wizard also supports an encrypted handoff from the original Windows
caption worker to a replacement PC. Existing bot identity and member
connections are preserved. The original PC must remain online for the shared
relay, public connection and Downtime; only the caption worker moves.
See the [illustrated hosting guide](https://ears.xenon.solutions/hosting).

Build the Windows installer with `python packaging/build_host_installer.py`.

Build a shareable Windows GPU host package:

    python packaging/build_host.py

Extract `dist/VemalsEars-Host.zip` and follow [the hosting guide](Hosting/README.txt).
It includes the relay, pairing pages and Discord speech worker. Setup preserves
the speech and privacy defaults while creating fresh credentials for new hosts.
The host supplies their own Discord bot, HTTPS address and member installer.
No Downtime checkout or private repository access is needed.

The human host can leave the voice channel while captions continue for members
who remain. The host computer and bot must stay running; recipients still need
to be in the active voice channel.

## License

GPL-3.0-only. Copyright (C) 2026 Zackery Hodges. See `LICENSE`.
