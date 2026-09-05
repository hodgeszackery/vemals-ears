Vemal's Ears (By Downtime)
=========================

Standalone Windows captions and guild chat project.
Canonical source: https://github.com/hodgeszackery/vemals-ears (private).
Local checkout: E:/Projects/vemals-ears
License: GPL-3.0-only; see LICENSE.

Run the desktop client from this folder:
  python Vemals-Ears.pyw

Install Python dependencies:
  python -m pip install -r GuildHub/requirements-client.txt

The caption worker also needs the Discord adapter dependencies:
  npm --prefix VoiceCaptions/discord_adapter ci

Use dev.py for local validation and build_release.py for the member ZIP.
Installer/ contains the Windows installer and native launcher build.
VoiceCaptions/ contains the Discord capture adapter, speech runtime and overlay.
GuildHub/ contains the member wire client, pairing helpers and voice worker.

The shared guild relay, operator console, hosted jobs and Downtime addons
remain in https://github.com/hodgeszackery/downtime-addons. The projects
communicate through the existing guild protocol. Building and testing VE
does not require a Downtime checkout, addons, Lua or Warpsim.

This repository starts from the existing 0.5.8 source. Extraction is not a
product release and does not change installed applications or live services.
Read ADR-001.txt for ownership and SOURCE-IMPORT.json for source provenance.
Read docs/WORKER-MIGRATION.txt before changing the production worker checkout.
