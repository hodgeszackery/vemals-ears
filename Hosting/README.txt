Vemal's Ears - Windows NVIDIA Host
=================================

WINDOWS INSTALLER

For setup without commands, run Vemals-Ears-Host-Setup.exe and follow the
graphical wizard. Read Hosting/QUICKSTART.txt for the short operator guide.
The installer includes private Python, Node and Windows service tools. The
wizard prepares NVIDIA speech recognition and optional Cloudflare Tunnel.

Choose Set up a new host for a new bot, hostname and member pairing. To move
the original Windows caption worker, choose Export from the current host on
the original PC, then Import on the replacement PC. The encrypted .vehost
handoff uses a passphrase of at least 12 characters and preserves the bot,
publisher identity, public addresses, consent choices and character mappings.
Members keep their existing connections; they do not pair again.

The original PC must stay online for its shared relay, pairing, downloads,
public hostname/tunnel and Downtime. Export stops and disables only the
original VemalsEarsWorker. After the operator checks the imported IDs/URL and
confirms the old worker is stopped, the replacement starts only its caption
worker. This export supports the original VemalsEarsWorker registration, not
arbitrary standalone Host Setup installations. Import never replaces an
existing destination state directory.

The encrypted handoff contains no relay database, pepper, tunnel credentials,
audio, transcripts or session-resume state. Transfer the file privately and
give its passphrase separately. Never publish it with the installer. Cancel
unused handoff is only for a replacement that has never started. After it has
run, returning needs its current consent/mapping state and a coordinated
worker switch. Never run two workers with the same bot identity.

Illustrated guide: https://ears.xenon.solutions/hosting
Host download: https://ears.xenon.solutions/download/vemals-ears-host
Member download: https://ears.xenon.solutions/download

To build the installer from this source package:
  python packaging/build_host_installer.py

The instructions below are the alternative source/ZIP setup for a NEW
standalone host. Existing-host takeover uses the graphical workflow above.

Host your own guild's captions and typed chat with the same speech model,
NVIDIA acceleration and privacy behavior as the original host. This package
includes the relay, one-use pairing pages, Discord worker and setup tools.
It runs without a Downtime checkout or access to a private source repository.

You need Windows x64, Python 3.12 or newer, Node.js 22.12 or newer, an NVIDIA
GPU with a CUDA 12.4-capable driver, and a Discord server you can add a bot to.
Allow about 1.2 GB of downloads for the speech engines and models, plus room
for extraction. Keep this host awake while captions are in use. You also need
a stable HTTPS hostname that forwards WebSocket traffic to this computer.

The defaults preserve whisper.cpp b4938, large-v3-turbo q5_0, Silero v6.2.0,
English recognition, streaming captions, 500 ms silence segmentation and a
30-second segment cap. GPU setup fails visibly if only CPU mode is available.
Consent, character mappings and session-resume state survive service restarts.
Only paired devices in the active Discord voice channel receive captions and
typed chat. This is enforced in code. The bot's public messages are limited
to consent/association flows; it does not mirror typed chat into Discord.

The human host does not need to be in the voice channel. Any member in that
channel can start captions with /vem start, and the starter may leave while
the remaining members continue. The host computer, services and bot must
stay running. An absent host cannot view captions outside the active channel.

1. EXTRACT AND INSTALL DEPENDENCIES

Extract VemalsEars-Host.zip somewhere permanent, for example C:/VemalsEars-Host.
Open PowerShell in the extracted folder containing Hosting/ and GuildHub/.
Commands below assume that location and a
NEW state directory C:/VemalsEarsHostState. Change both paths if needed.

  python -m venv .venv
  ./.venv/Scripts/python.exe -m pip install -r Hosting/requirements.txt

The state directory must be outside the package. Configure refuses existing
directories so it cannot overwrite another host. It restricts the directory
to the current Windows account, Administrators and SYSTEM before saving any
secrets. Do not use your existing production state directory for a trial.

2. CREATE YOUR DISCORD APPLICATION AND CONFIGURE THE HOST

Create your own application and bot at https://discord.com/developers/applications.
Copy its Application ID and your Discord server ID. Discord Developer Mode
allows Copy Server ID. Obtain the bot token and keep it private.

Replace the example hostname and IDs in this command:

  ./.venv/Scripts/python.exe -m Hosting.setup --state-directory C:/VemalsEarsHostState configure --hostname ears.example.org --guild-id 123456789012345678 --application-id 234567890123456789

Type the bot token into the hidden prompt. Never put it in a command line,
chat, source file or shared ZIP. Setup creates fresh relay keys and prints a
bot installation link. Open that link to add the bot to your Discord server.
The scopes are bot and applications.commands; permissions are View Channel,
Send Messages and Connect. Allow those permissions in your control and voice
channels. Administrator, Message Content and Server Members intents are not
required by this adapter. Discord setup reference:
https://github.com/discord/discord-api-docs/blob/main/developers/quick-start/getting-started.mdx

Optional configure arguments: --guild-name "Your Guild" and --port 8770.
The default member installer URL is https://YOUR-HOSTNAME/download. Copy an
existing Vemals-Ears-Setup.exe supplied by the project into
C:/VemalsEarsHostState/downloads/Vemals-Ears-Setup.exe. The host ZIP does not
contain the member installer. Alternatively pass --client-download-url with
an HTTPS link to the installer when configuring. Leave CLIENT_VERSION blank
unless you are deliberately publishing a matching client update.

3. PREPARE THE NVIDIA SPEECH RUNTIME

  ./.venv/Scripts/python.exe -m Hosting.setup --state-directory C:/VemalsEarsHostState prepare

This downloads hash-verified, pinned speech assets and runs npm ci using the
adapter lockfile. It does not connect the bot or start a caption session.
The NVIDIA runtime belongs to this host's private state folder. Preparation
can be rerun after a failed download. It does not recreate keys or consent.

4. CONNECT YOUR PUBLIC HTTPS ADDRESS

Use one of these routes, with the hostname you entered in step 2:

- Cloudflare Tunnel, matching the original host's topology: create a tunnel
  in your own account, install its Windows connector as a service, and map
  your public hostname to http://127.0.0.1:8770 on this computer. Use a stable
  hostname, not a temporary Quick Tunnel. Keep tunnel credentials local.
  https://developers.cloudflare.com/tunnel/get-started/

- Caddy: point public DNS to this computer, allow inbound TCP 80 and 443,
  and run Caddy with the generated C:/VemalsEarsHostState/Caddyfile:
    caddy run --config C:/VemalsEarsHostState/Caddyfile
  For unattended operation, install Caddy as a Windows service following:
  https://caddyserver.com/docs/running

Keep the relay bound to 127.0.0.1; the public edge terminates HTTPS/WSS.
Do not add an interactive login challenge in front of the relay's API, because
the desktop client cannot complete it. The relay provides its own pairing and
device authentication. No tunnel account or DNS setting is included here.

5. PREFLIGHT AND START

For a foreground trial, run each command in its own PowerShell window, from
the package folder. Ctrl+C stops that process.

  ./.venv/Scripts/python.exe -m Hosting.service --state-directory C:/VemalsEarsHostState relay
  ./.venv/Scripts/python.exe -m Hosting.service --state-directory C:/VemalsEarsHostState worker

For automatic startup, install NSSM from https://nssm.cc/download and run the
following preflight. Substitute its actual nssm.exe path in the install step.

  powershell -NoProfile -File Hosting/Install-Host.ps1 -StateDirectory C:/VemalsEarsHostState -PythonPath C:/VemalsEars-Host/.venv/Scripts/python.exe -CheckOnly

Stop foreground trials before installing services. In an Administrator
PowerShell, run from the same package folder:

  powershell -NoProfile -File Hosting/Install-Host.ps1 -StateDirectory C:/VemalsEarsHostState -PythonPath C:/VemalsEars-Host/.venv/Scripts/python.exe -NssmPath C:/Tools/nssm.exe

This installs VemalsEarsHostRelay and VemalsEarsHostWorker. It does not install
Downtime jobs or an operator web console. Logs are in the state folder's logs
directory. Configuration lives in relay.env; secrets remain separate files.
Re-running the installer can restart these two services for this state folder.

6. VERIFY AND PAIR MEMBERS

Check http://127.0.0.1:8770/healthz, then https://YOUR-HOSTNAME/healthz.
Open https://YOUR-HOSTNAME/join and test the installer download. A healthy
relay alone does not prove Discord capture or speech recognition works.

In Discord, use /vem setup for your personal one-use pairing link, install the
member app, and follow the link to pair it. Join the voice channel, use
/vem start, and opt into the consent prompt. Verify actual captions and typed
chat with another consenting, paired member in that same voice channel. Leave
the channel on a test device and confirm it no longer receives captions.
Restart the worker once during your own test and verify consent and mappings
remain, then confirm the session resumes as expected.

The service launcher also provides local administration commands:

  ./.venv/Scripts/python.exe -m Hosting.service --state-directory C:/VemalsEarsHostState admin list
  ./.venv/Scripts/python.exe -m Hosting.service --state-directory C:/VemalsEarsHostState admin revoke DEVICE_ID

Prefer Discord /vem setup for pairing because it associates the Discord
identity needed by the voice-channel gate. Admin enroll can create a one-use
code, but the member must still associate their Discord account.

OPERATING AND SHARING

The encrypted import workflow writes VEMALS_EARS_HOST_MODE=remote-worker.
This uses the existing public WSS relay and matching HTTPS origin without
local relay/DB/pepper/port/tunnel settings. Service arguments include
--host-mode remote-worker so an existing standalone service cannot be adopted.
Protected host-settings.json confirmation is required before activation.
Preparation does not start services. Upgrade, stop and uninstall manage only
the exact owned worker service; they never operate the original shared host.

The following backup and source-update instructions apply to standalone hosts.

Back up the complete state directory privately while both host services are
stopped. It contains device credentials, relay keys, consent and mappings.
Restore it only for the same host; do not send it to another guild. Keep the
original publisher token when restoring so viewer associations remain valid.
Restore file permissions too. Do not rotate the pepper as a routine update.

To update the source, stop these two services, extract a new package into a
new permanent folder, install dependencies and run prepare against the same
state directory, then run the service installer with the new Python path.
Keep the previous source folder for rollback. Avoid mixing individual files.

To share or rebuild a clean source package:
  python packaging/build_host.py
  python packaging/build_host.py --verify dist/VemalsEars-Host.zip

Share only dist/VemalsEars-Host.zip and the separately supplied member
installer. The builder uses an explicit file list, ignores local state and
dependencies, and records every included file's SHA-256. Git is not required.

Application source is GPL-3.0-only; see LICENSE. Model and third-party notices
are in GuildHub/third-party. Speech engines, models, npm and Python packages
are downloaded during setup under their respective terms. This source ZIP
does not redistribute a combined NVIDIA runtime bundle. Relay origin and
upstream source hashes are recorded in Hosting/RELAY-IMPORT.json.
