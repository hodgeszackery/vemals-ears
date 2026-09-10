# Vemal's Ears Discord adapter

This process is the Discord-side trust boundary for Vemal's Ears. It joins a
voice channel when at least one current human participant has consented. It
captures each speaker only when that speaker has consented. A new, pending, or
declined participant remains uncaptured while consenting speakers continue.
Any participant can stop the session.

Audio and transcript text stay in memory and are never written to disk. Three
purpose-specific local ledgers are kept beside the operator's other state:

- **consent decisions** (`src/consent_ledger.js`) use a Discord guild ID and
  user ID as the key and store `granted` or `declined`, plus the file's update
  time. `Remember my answer` is on by default. A remembered answer has no
  automatic expiry; it remains until that person changes it or runs `/vem
  forget`. Turning Remember off removes any older durable answer and keeps the
  new choice only in the live process for the current caption session.
- **character mappings** (`src/mapping_ledger.js`) use a Discord guild ID and
  user ID as the key and store character name, optional realm, WoW class, role,
  and mapping time. A mapping has no automatic expiry; `/vem unmap` or `/vem
  forget` erases it.
- **session resume state** (`src/session_ledger.js`) stores, per Discord guild,
  the voice-channel ID, control-channel ID, starter's user ID, current
  participant user IDs, save time, and consent-prompt nonce. It contains no
  consent answer, audio, or transcript. The default restart window is 30
  minutes. `/vem stop`, expiry, an empty or missing target voice channel, or a
  failed resume erases the entry.

Each ledger carries a schema version and file update time. The consent ledger
also uses a separate dirty-run marker containing only a schema version, random
run ID, and sync status; a clean shutdown removes that marker.

The ledger files themselves are not uploaded. The adapter does send the live
fields needed by the pipeline over its local JSONL pipe. A session snapshot
contains its generation and audience revision; guild, voice-channel,
control-channel, starter, and stopper IDs; start and stop times; phase and pause
reason; consent-prompt nonce; and each participant's user ID, join time, consent
state, and mapping. Audience snapshots contain current participant Discord IDs.
Speaker audio metadata contains the Discord user ID, safe display name, and
mapped character name, optional realm, class, and role.

Before anything goes to the relay, the coordinator replaces Discord IDs with
stable guild-scoped opaque tokens. The relay receives session state, audience
generation and revision, and opaque viewer tokens. Caption events contain an
opaque speaker ID, display name, mapped character name, class, and role. Raw
Discord IDs, session timestamps, prompt nonce, participant consent state, and
join time are not sent to the relay. The optional realm is not sent to the
relay; it remains in the adapter's local mapping state, private Discord mapping
flow, and transcription pipeline.

Structured status events are emitted as JSON Lines on standard output.
Standard input is an inert legacy quick-chat compatibility terminal: current
relays never send a typed body there, and accepted member Message Group lines
have no path to it. If an older relay sends a command, the adapter validates it
and returns a terminal status without a Discord output sink. Diagnostics go to
standard error and must never contain credentials, audio, captions, or typed
message bodies.

## Local setup

Node.js 22.12.0 or newer is required. Install the exact locked dependencies:

```text
npm ci
```

Set `DISCORD_TOKEN` and `DISCORD_APPLICATION_ID` through the companion's local
credential/configuration layer, then run `npm start`. `DISCORD_GUILD_ID` is
optional and makes command registration immediate in one development guild.
Never put a real token in this folder or paste one into chat.

The Discord application needs only the `bot` and `applications.commands`
scopes. Runtime gateway intents are limited to guild and guild voice-state
events. The bot must be able to view and connect to the selected voice channel
and send messages in the text channel where `/vem start` was used.

## Commands

- `/vem start` opens a visible consent session with shared
  `Manage my consent` and `Choose your class` buttons. Each button opens a
  private Discord modal for the person who selected it.
- The consent modal changes only the caller's capture choice. `Remember my
  answer` is on for a new user. Leave it on to save the answer for future
  sessions. Turn it off to keep the answer for this caption session only,
  including a brief voice disconnect in the same process. A worker restart
  cannot restore a session-only answer. Turning Remember off also removes any
  older saved answer without touching the caller's character mapping.
- `/vem consent` opts the caller in and remembers the answer. `/vem decline`
  opts only the caller out and remembers that answer. These commands remain the
  direct alternatives to the modal.
- `/vem stop` stops capture for everyone; any participant may use it. The
  acknowledgement is private to whoever ran it; the channel is not told.
- `Choose your class` and `/vem map` open the same character modal. It assigns
  a WoW character, class, role, and optional realm, remembered until the player
  selects `Clear my saved mapping` or runs `/vem unmap`.
- `/vem unmap` erases the caller's stored mapping.
- `/vem forget` erases both the caller's remembered consent answer and their
  character mapping, so they are asked again next session.
- `/vem status` reports consent and capture state.
- `/vem setup` creates a one-use pairing invitation bound to the caller.
  `/vem invite` creates one bound to the selected member. The claimed device is
  associated during pairing, so there is no separate Link Discord step.

## What the bot posts

Shino, 2026-09-06: no outbound Vemal's Ears message reaches Discord except
the consent and association dialogues. Concretely, the only things the bot
ever posts are:

- the public `/vem start` prompt with its consent and class buttons, and
  the public notice when someone's consent changes - the channel must
  always be able to see who is being captioned;
- the private replies Discord requires for a command or button: consent
  confirmations, the character-mapping panel and modal, pairing links
  from `/vem setup` and `/vem invite`, `/vem status` on request, the
  private `/vem stop` acknowledgement, and error messages.

Nothing is posted when people join or leave the voice channel, when
capture pauses or resumes, when the bot restarts and rejoins, when a
rejoin fails, or when a member types a quick-chat line. A restart keeps
the original `/vem start` buttons working (their nonce is remembered in
the session ledger) instead of posting a fresh prompt.

## Quick chat boundary

The paired member companion can submit a deliberate typed line to the guild
relay. The relay validates it and assigns a cursor without requiring an active
caption session, publisher, or compatibility worker. That acknowledgement does
not guarantee viewer delivery. Only paired devices in the relay's current
fresh, publisher-owned audience for the exact active voice channel may see or
replay the line. With no such audience, the cursor advances but the body is
delivered to nobody.

The current relay sends no typed body to Discord or to the compatibility
worker. The adapter's legacy quick-chat input remains as an inert compatibility
terminal: if an older relay sends a command, the adapter validates it, uses a
bounded eight-item serial queue, and returns a terminal status without any
Discord output sink. Current relay versions send no such command and ignore
stale compatibility receipts. No typed body is written to the consent or
character-mapping ledgers or placed in a diagnostic event. No Discord TTS,
voice transmission, or text message is produced.

`/vem stop` and publisher loss clear the relay's current audience plus the
session's caption and Message Group replay. A relay restart clears every
session's replay. A Message Group line accepted after the audience was cleared
is delivered to nobody and is not retained for a later publisher or audience.

## Audio boundary

Discord audio receive is not a documented stable Discord API. The runtime
therefore isolates it behind an injected audio sink and fails closed whenever
the shared sink, voice connection, or standard output is unhealthy. Each
speaker receives an independent stream and per-speaker segment sequence;
overlapping speakers are never merged.

The locked `opusscript` 0.0.8 decoder is one of the Opus implementations
supported by the installed Discord voice stack. Each Discord 48 kHz stereo
Opus stream is decoded independently, downmixed, and resampled in memory to
16 kHz mono signed PCM16 little-endian. PCM is emitted to the companion as
base64 JSONL chunks. Raw chunks are at most 8 KiB, and each base64 chunk and
complete chunk event remains at most 16 KiB. Standard-output backpressure uses
a bounded queue and stops every capture stream if that queue cannot drain.

The transcription boundary consumes these event types:

- `audio.segment.started` carries the segment id, global segment sequence,
  safe Discord display name, user id, optional character mapping, and format.
- `audio.segment.chunk` carries the segment id, global sequence, one-based
  chunk sequence, and `pcmBase64`.
- `audio.segment.ended` carries the same speaker metadata plus completion
  status, chunk count, and total PCM bytes.

Audio and transcripts are never written to disk or included in diagnostics. A
corrupt Opus packet or routine source-stream error fails and drops only that
speaker's segment, so other consenting speakers continue. Decoder creation or
cleanup failure, duplicate segments, stream-capacity exhaustion, PCM conversion
failure, or JSONL output failure destroys every active speaker stream and
leaves the sink blocked for the life of the process. Invalid segment metadata
is rejected and makes the runtime fail closed. Voice connection failure also
stops capture safely.

Run the offline tests with `npm test`. They do not need Discord, a network
connection, or a token.
