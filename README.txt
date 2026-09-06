Vemal's Ears (By Downtime)
==========================

Live, consent-gated captions of your guild's Discord voice chat, shown in a
small movable window above World of Warcraft, plus typed guild chat for
the moments voice is down. Windows only.

Source:  https://github.com/hodgeszackery/vemals-ears
License: GPL-3.0-only; see LICENSE. GNU GPL version 3 only, no warranty.

WHAT IS IN THIS REPOSITORY

  Vemals-Ears.pyw    the member desktop client: caption window and pairing
  GuildHub/          wire client, pairing helpers and the guild voice worker
  VoiceCaptions/     Discord capture adapter (Node), speech runtime, overlay
  Installer/         Windows installer and native launcher build
  packaging/         member ZIP, pinned runtime and source-archive tooling
  tests/ ci/ dev.py  the test suites and local validation

What is NOT here: the shared guild relay, operator console and hosted jobs
live in a separate repository that is not public. A guild needs a running
relay to use Vemal's Ears. Members never need it to build or run the client.

INSTALL (MEMBERS)

Members do not build anything. Your guild administrator sends a download
link for the signed per-user installer and a one-use pairing link. Run the
installer (no administrator rights; Windows 10 version 1809 or newer, or
Windows 11, x64), open Vemal's Ears once, then open the pairing link.
GuildHub/CLIENT-README.txt covers pairing, Discord linking and consent.

RUN FROM SOURCE (DEVELOPERS)

Requirements: 64-bit Python 3.12 or newer, Node.js 22.12 or newer, Git.
CI pins Python 3.12 with hash-locked wheels (ci/requirements-windows.txt).

  git clone https://github.com/hodgeszackery/vemals-ears
  cd vemals-ears
  python -m pip install -r GuildHub/requirements-client.txt
  npm --prefix VoiceCaptions/discord_adapter ci
  python Vemals-Ears.pyw

Validate a change before pushing it:

  python dev.py doctor
  python dev.py check --jobs 8

docs/DEVELOPMENT.txt describes the test layout and the rules tests follow.

BUILD THE MEMBER ZIP AND INSTALLER

  python build_release.py --member                       development ZIP
  python packaging/build_runtime.py                      fetch the pinned runtime
  python build_release.py --member --require-runtime     distributable ZIP
  python packaging/verify_bundle.py

docs/SOURCE.txt explains the runtime lock, the source archive shipped with
every release, and rebuilding from a release. Installer/README.md covers
the signed installer and native launcher; that step needs Inno Setup and a
C compiler.

RUN A GUILD VOICE WORKER (OPERATORS)

The worker joins Discord voice for consenting speakers, runs speech
recognition locally and publishes text to your guild's relay. It needs a
Windows host with a CUDA-capable GPU for usable latency, a Discord bot you
own, and a relay. Preflight first, then install it as a service:

  powershell -File GuildHub/Install-VemalsEars-Worker.ps1 -CheckOnly
  powershell -File GuildHub/Install-VemalsEars-Worker.ps1

The bot token is typed by the operator on the host. It is never a source
input and never belongs in this repository. VoiceCaptions/README.txt covers
consent, privacy and what the worker keeps.

CONTRIBUTING

Pull requests, issues and comments are limited to collaborators. If you
find a bug or a privacy problem, reach the maintainer through the GitHub
profile rather than opening a pull request. Design decisions are recorded
in ADR-001.txt; source provenance is in SOURCE-IMPORT.json.
