Vemal's Ears - Guild Host Capture Worker
========================================

Vemal's Ears turns consenting Discord voice chat into a compact local
caption overlay above World of Warcraft. It does not type into the game or
interact with combat controls.

DEPLOYMENT ROLES

The generic per-user Vemal's Ears installer is member software. It includes
the unified companion and thin text-only caption receiver, not this Discord
capture/STT worker, the relay, a speech model, or any guild secret. Every guild
uses the same signed member binary; a guild host is only a download mirror.
Guild-specific settings arrive later through the one-use pairing link.

The installer and protected signing/checksum pipeline are implemented, but a
final public build must wait until the Vemal's Ears publisher identity is
approved and configured. The release checksum is generated only after the
installer is signed and its signature is verified. Unsigned builds are for
local testing only.

For a guild host, use the shared host documentation in the downtime-addons
repository and set VE_PROJECT_ROOT to this standalone checkout for the worker
image. See docs/WORKER-MIGRATION.txt for the Windows service source migration.

WINDOWS SOURCE-HOST SETUP

1. On the Windows guild-host computer, open VoiceCaptions\setup.py.
2. Install the speech engine from that window.
3. Paste the Application ID and Bot Token from your Discord application.
   The token is stored in Windows Credential Manager, never in this folder.
4. Save the details, copy the install link, and add the bot to your server.
5. Start the source pilot with python -m GuildHub.host --watch.

Pair and Move in the installed Vemal's Ears app configure only the thin
caption receiver, not this capture worker.

MEMBER INSTALLS AND UPGRADES

There is no automatic updater yet. A member upgrades by running a newer copy
of the same publisher-signed generic installer. It upgrades the existing
per-user installation and refuses to replace a newer version with an older
one. Uninstall removes the local pairing credential, but the guild
administrator must still revoke that device on the server.

The member installer also carries Gear Helper and Boss Talents, but their
external setup remains separate. Gear Helper needs the Gear Helper and
SimulationCraft WoW addons plus an authenticated GitHub CLI for its first local
SimulationCraft download. Its Defender-safe source fallback needs Microsoft
Visual Studio Build Tools. Boss Talents needs the member's own permitted
Warcraft Logs OAuth client credentials, configured once with the installed
Set up Boss Talents shortcut. No such credentials are bundled.

USE IN DISCORD

Run /vem start while you are in the voice channel, then use the shared
Manage my consent button. Its private window lets each person allow or decline
captioning for only their own voice. Remember my answer is off for a new user.
Leave it unchecked for a choice that ends with this caption session, or check
it to reuse the answer in future sessions. A brief disconnect does not erase a
session-only choice. Any current participant can run /vem stop.

Use /vem map to connect your Discord speaker to a character name, class,
and role for the overlay. Mappings are remembered until you run /vem unmap.

PLACEMENT

Choose Move in Vemal's Ears. Drag the sample caption
stack, then choose Save. Reset restores the lower-center safe default. Closing
the Move window or pressing Escape keeps the last saved position. The live
overlay is click-through so it cannot block ground targeting or combat clicks.

DISPLAY

Open the companion's gear menu to choose how long finished text stays visible
and how many speakers may appear at once. The choices apply immediately: 2,
3.6, 6, or 10 seconds and one through four speaker cards. These are local
reader preferences and never change what the guild captures or sends.

PRIVACY

- A red REC tally means an active caption session may be listening to
  consenting speakers. It does not mean audio is saved.
- REC? means this computer temporarily cannot confirm the guild session.
  The warning stays visible because remote listening may still be active.
- Voice audio and transcript text stay in memory and are not recorded.
- Speech recognition runs on this PC through a loopback-only local service.
- Caption text is never written to the companion log or an addon file.
- The Discord token is stored only in Windows Credential Manager.
- New participants pause capture instead of being recorded without consent.
- A pipeline error stops capture rather than continuing in an uncertain state.

The Discord voice receive interface is less stable than its normal bot APIs.
Vemal's Ears isolates that boundary and stops safely if Discord changes it.

WINDOWS SOURCE-HOST REQUIREMENTS

- Windows 11
- Python 3
- Node.js 22.12 or newer
- Internet access for initial setup and the Discord connection

These requirements are for the source-host capture worker, not for a member who
only receives captions. The local speech-engine download is about 160 MB. It is
installed only after you choose the install action in Setup.
