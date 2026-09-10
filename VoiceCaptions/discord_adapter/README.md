# Vemal's Ears Discord adapter

This process is the Discord-side trust boundary for Vemal's Ears. It joins a
voice channel only after every current human participant has opted in. A new
participant immediately pauses capture until they opt in. Any participant can
stop the session.

Audio, transcripts and session state stay in memory: none of them is ever
written to disk. Two things are deliberately remembered across restarts, and
both name real people, so they are worth stating plainly rather than burying:

- **consent decisions** (`src/consent_ledger.js`), so the same group is not
  asked to consent again every single evening;
- **character mappings** (`src/mapping_ledger.js`), so `/vem map` survives
  the bot restarting mid-raid.

Both are local files written atomically, kept beside the operator's other
state and never sent anywhere. Either can be erased by the person it
describes, with `/vem forget` and `/vem unmap`.

Structured status events are emitted as JSON Lines on standard output.
Standard input accepts only bounded, relay-authorized quick-chat commands.
Diagnostics go to standard error and must never contain credentials, audio,
captions, or typed message bodies.

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
  answer` is off for a new user: unchecked answers last for the current caption
  session,
  including a brief voice disconnect. Checked answers are saved for future
  sessions. Unchecking it also removes any older saved answer without touching
  the caller's character mapping.
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

## Audio boundary

Discord audio receive is not a documented stable Discord API. The runtime
therefore isolates it behind an injected audio sink and fails closed whenever
that sink, the voice connection, or standard output is unhealthy. Each speaker
receives an independent stream and per-speaker segment sequence; overlapping
speakers are never merged.

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

Audio and transcripts are never written to disk or included in diagnostics.
If the decoder, stream, or JSONL writer fails, the sink destroys every active
speaker stream and remains blocked for the lifetime of the process.

Run the offline tests with `npm test`. They do not need Discord, a network
connection, or a token.
