# Vemal's Ears

Live captions and guild chat for World of Warcraft voice channels, on Windows.

Made for a friend named Vemal, who is deaf, so he could follow the raid
voice chat everyone else took for granted. It is named after him.

## What it does

- Captures a Discord voice channel through a small adapter, sends the audio
  to a self-hosted whisper worker (CPU container, source included), and shows
  the captions as an overlay in the game.
- Captions go only to paired devices inside the active voice channel. That
  consent gate is a hard rule of the design, not a setting.
- Guild chat and member pairing ride on the same relay the Downtime addons
  use; the relay, operator console and addons live in a separate project.
- Ships as an Authenticode-signed Windows installer with an automatic update
  path, and every release carries its matching source (GPL-3.0).

## Layout

- `VoiceCaptions/` - Discord capture adapter (Node), speech runtime, overlay
- `GuildHub/` - member wire client, pairing helpers, voice worker
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

## License

GPL-3.0-only. Copyright (C) 2026 Zackery Hodges. See `LICENSE`.
