// One speaker's audio stream erroring must cost that segment and nothing
// else. It used to take the whole adapter down: the bot left the voice
// channel and every member lost captions because one person's opus
// stream hiccuped. Shino's live worker log for 2026-09-04 shows it
// happening a second after joining, over and over:
//
//   22:03:08  Captions active
//   22:03:08  Vemal's Ears left the voice channel
//   22:03:09  Error: ... capture pipeline stopped (adapter_failed:audio_receive_error)
//
// Persistent trouble still fails closed, through the handlers that
// actually measure it - the voice connection's own error and disconnect.

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { Events } from "discord.js";
import { VoiceConnectionStatus } from "@discordjs/voice";

import { DiscordAdapterRuntime } from "../src/discord_runtime.js";
import { JsonlEmitter } from "../src/protocol.js";
import { SessionManager } from "../src/session_manager.js";

const GUILD = "111111111111111111";
const VOICE = "222222222222222222";
const CONTROL = "333333333333333333";
const SPEAKER = "444444444444444444";

class FakeClient extends EventEmitter {
  constructor() {
    super();
    this.user = { id: "923456789012345678" };
    this.application = { commands: { set: async () => {} } };
    this.guilds = { cache: new Map(), fetch: async () => null };
  }

  async login() {
    return "test-login-result";
  }

  destroy() {}
}

class FakeStream extends EventEmitter {
  constructor() {
    super();
    this.destroyed = false;
  }

  destroy() {
    this.destroyed = true;
  }
}

class FakeVoiceConnection extends EventEmitter {
  constructor(stream) {
    super();
    this.state = { status: VoiceConnectionStatus.Ready };
    this.destroyCount = 0;
    this.receiver = {
      speaking: new EventEmitter(),
      ssrcMap: new Map(),
      subscribe: () => stream,
    };
  }

  destroy() {
    this.destroyCount += 1;
    this.state = { status: VoiceConnectionStatus.Destroyed };
  }
}

function build() {
  const chunks = [];
  const emitter = new JsonlEmitter({ writer: { write: (chunk) => chunks.push(chunk) } });
  const stream = new FakeStream();
  const client = new FakeClient();
  const member = { id: SPEAKER, user: { id: SPEAKER, bot: false } };
  const guild = {
    id: GUILD,
    voiceAdapterCreator: () => ({ sendPayload: () => true, destroy: () => {} }),
    channels: {
      cache: new Map([
        [VOICE, { isVoiceBased: () => true, members: new Map([[SPEAKER, member]]) }],
        [CONTROL, { isTextBased: () => true, send: async () => {} }],
      ]),
    },
  };
  client.guilds.cache.set(GUILD, guild);
  const connections = [];
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId: null,
      silenceMs: 750,
      connectTimeoutMs: 25,
      maxSegmentMs: 30_000,
    },
    emitter,
    audioSink: { isReady: () => true, acceptOpusStream: async () => {} },
    client,
    sessions: new SessionManager(),
    connectionFactory: () => {
      const connection = new FakeVoiceConnection(stream);
      connections.push(connection);
      return connection;
    },
  });
  return {
    runtime,
    client,
    guild,
    stream,
    connections,
    events: () => chunks.map((chunk) => JSON.parse(chunk)),
  };
}

async function joinAndSpeak(harness) {
  const { runtime, client, guild } = harness;
  await runtime.start();
  runtime.sessions.startSession({
    guildId: GUILD,
    voiceChannelId: VOICE,
    controlChannelId: CONTROL,
    startedBy: SPEAKER,
    participantIds: [SPEAKER],
  });
  assert.equal(
    runtime.sessions.setConsent({ guildId: GUILD, userId: SPEAKER, granted: true }).phase,
    "active",
  );
  client.emit(
    Events.VoiceStateUpdate,
    { guild, channelId: null },
    { guild, channelId: VOICE },
  );
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.equal(harness.connections.length, 1, "the bot should have joined");
  harness.connections[0].receiver.speaking.emit("start", SPEAKER);
  await new Promise((resolve) => setTimeout(resolve, 20));
}

test("a speaker's stream error ends that segment and nothing else", async () => {
  const harness = build();
  await joinAndSpeak(harness);
  assert.ok(
    harness.events().some((event) => event.type === "speaker.segment.started"),
    "the segment must have started, or this test proves nothing",
  );

  harness.stream.emit("error", new Error("opus receive blew up"));
  await new Promise((resolve) => setTimeout(resolve, 40));

  const events = harness.events();
  const ended = events.filter((event) => event.type === "speaker.segment.ended");
  assert.equal(ended.length, 1, "the segment ends");
  assert.equal(ended[0].payload.reason, "stream_error");
  assert.ok(
    !events.some((event) => event.type === "adapter.failed"),
    "one stream's error must not take the adapter down",
  );
  assert.equal(
    harness.runtime.sessions.captureHealthy,
    true,
    "capture stays healthy, so the bot stays in the channel",
  );
  assert.equal(harness.connections[0].destroyCount, 0, "the bot did not leave");
  await harness.runtime.shutdown("test_complete");
});

test("the next speaker is still captured after one stream error", async () => {
  // The point of surviving: captions keep working for everyone else.
  const harness = build();
  await joinAndSpeak(harness);
  harness.stream.emit("error", new Error("opus receive blew up"));
  await new Promise((resolve) => setTimeout(resolve, 40));

  harness.connections[0].receiver.speaking.emit("start", SPEAKER);
  await new Promise((resolve) => setTimeout(resolve, 40));

  const started = harness
    .events()
    .filter((event) => event.type === "speaker.segment.started");
  assert.equal(started.length, 2, "a new segment begins after the failed one");
  await harness.runtime.shutdown("test_complete");
});
