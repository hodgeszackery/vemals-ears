import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { Events, MessageFlags } from "discord.js";
import { VoiceConnectionStatus } from "@discordjs/voice";

import { captionsCommandJson } from "../src/command_schema.js";
import { DiscordAdapterRuntime } from "../src/discord_runtime.js";
import { JsonlEmitter } from "../src/protocol.js";
import { SessionManager } from "../src/session_manager.js";

class FakeClient extends EventEmitter {
  constructor() {
    super();
    this.user = { id: "923456789012345678" };
    this.application = { commands: { set: async () => {} } };
    this.guilds = { cache: new Map(), fetch: async () => null };
    this.destroyed = false;
  }

  async login() {
    return "test-login-result";
  }

  destroy() {
    this.destroyed = true;
  }
}

function emitterEvents() {
  const chunks = [];
  const emitter = new JsonlEmitter({ writer: { write: (chunk) => chunks.push(chunk) } });
  return { emitter, events: () => chunks.map((chunk) => JSON.parse(chunk)) };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function healthySink() {
  return { isReady: () => true, acceptOpusStream: async () => {} };
}

test("service stop waits for guild command registration before destroying Discord", async () => {
  const guildId = "123456789012345678";
  const output = emitterEvents();
  const client = new FakeClient();
  const registrationStarted = deferred();
  const releaseRegistration = deferred();
  let restTokenAvailable = true;
  let destroyCalls = 0;

  const guild = {
    commands: {
      set: async (commands) => {
        assert.deepEqual(commands, [captionsCommandJson]);
        registrationStarted.resolve();
        await releaseRegistration.promise;
        if (!restTokenAvailable) {
          throw new Error("Expected token to be set for this request");
        }
      },
    },
  };
  client.guilds.fetch = async () => guild;
  client.application.commands = {
    fetch: async () => new Map(),
    delete: async () => {},
    set: async () => {
      throw new Error("guild-scoped startup must not replace global commands");
    },
  };
  client.destroy = async () => {
    destroyCalls += 1;
    restTokenAvailable = false;
    client.destroyed = true;
  };

  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "223456789012345678",
      guildId,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: healthySink(),
    client,
  });

  const startup = runtime.start();
  await registrationStarted.promise;
  const shutdown = runtime.shutdown("service_stop");
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(destroyCalls, 0, "shutdown must not clear the REST token mid-registration");
  assert.equal(restTokenAvailable, true);

  releaseRegistration.resolve();
  await Promise.all([startup, shutdown]);

  assert.equal(destroyCalls, 1);
  assert.equal(client.destroyed, true);
  const lifecycle = output
    .events()
    .filter(({ type }) => ["adapter.ready", "adapter.failed", "adapter.stopped"].includes(type));
  assert.deepEqual(
    lifecycle.map(({ type }) => type),
    ["adapter.stopped"],
    "a requested stop during startup is clean, not ready or failed",
  );
  assert.equal(lifecycle[0].payload.reason, "service_stop");
});

test("service stop during login skips command registration", async () => {
  const guildId = "323456789012345678";
  const output = emitterEvents();
  const client = new FakeClient();
  const loginStarted = deferred();
  const releaseLogin = deferred();
  let guildFetches = 0;
  let globalFetches = 0;
  let destroyCalls = 0;

  client.login = async () => {
    loginStarted.resolve();
    await releaseLogin.promise;
    return "test-login-result";
  };
  client.guilds.fetch = async () => {
    guildFetches += 1;
    return { commands: { set: async () => {} } };
  };
  client.application.commands = {
    fetch: async () => {
      globalFetches += 1;
      return new Map();
    },
    delete: async () => {},
    set: async () => {},
  };
  client.destroy = async () => {
    destroyCalls += 1;
  };

  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "423456789012345678",
      guildId,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: healthySink(),
    client,
  });

  const startup = runtime.start();
  await loginStarted.promise;
  const shutdown = runtime.shutdown("service_stop");
  releaseLogin.resolve();
  await Promise.all([startup, shutdown]);

  assert.equal(guildFetches, 0);
  assert.equal(globalFetches, 0);
  assert.equal(destroyCalls, 1);
  assert.equal(output.events().some(({ type }) => type === "adapter.ready"), false);
  assert.equal(output.events().some(({ type }) => type === "adapter.failed"), false);
});

test("guild registration deletes only the obsolete global captions command", async () => {
  const guildId = "523456789012345678";
  const output = emitterEvents();
  const client = new FakeClient();
  const calls = [];
  const guild = {
    commands: {
      set: async (commands) => {
        calls.push(["guild.set", commands.map(({ name }) => name)]);
      },
    },
  };
  client.guilds.fetch = async (requestedGuildId) => {
    assert.equal(requestedGuildId, guildId);
    return guild;
  };
  client.application.commands = {
    fetch: async () => {
      calls.push(["global.fetch"]);
      return new Map([
        ["legacy", { id: "legacy", name: "captions" }],
        ["current", { id: "current", name: "vears" }],
        ["other", { id: "other", name: "raid-helper" }],
      ]);
    },
    delete: async (commandId) => {
      calls.push(["global.delete", commandId]);
    },
    set: async () => {
      calls.push(["global.set"]);
    },
  };

  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "623456789012345678",
      guildId,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: healthySink(),
    client,
  });

  await runtime.start();

  assert.deepEqual(calls, [
    ["guild.set", ["vem"]],
    ["global.fetch"],
    // Both of this app's old global names go: /captions from before the
    // rename to /vears, and /vears itself now that it is /vem. A member
    // who still saw either would get an entry that answers nothing.
    ["global.delete", "legacy"],
    ["global.delete", "current"],
  ]);
  // Somebody else's command under the same application is never touched.
  assert.equal(
    calls.some(([kind, id]) => kind === "global.delete" && id === "other"),
    false,
  );
  assert.equal(output.events().some(({ type }) => type === "adapter.ready"), true);
  await runtime.shutdown("test_complete");
});

test("runtime with no transcription sink reports blocked and cannot capture", async () => {
  const output = emitterEvents();
  const client = new FakeClient();
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId: null,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: {
      isReady: () => false,
      acceptOpusStream: () => {
        throw new Error("should never be called");
      },
    },
    client,
  });

  await runtime.start();
  const health = output
    .events()
    .filter(({ type }) => type === "health.changed")
    .at(-1);
  assert.equal(health.payload.status, "blocked");
  assert.equal(health.payload.reason, "audio_sink_unavailable");
  assert.equal(runtime.sessions.captureHealthy, false);

  await runtime.shutdown("test_complete");
  assert.equal(client.destroyed, true);
});

test("runtime keeps capture blocked when its JSONL boundary fails", async () => {
  const emitter = new JsonlEmitter({
    writer: {
      on() {},
      write() {
        throw new Error("private output detail");
      },
    },
  });
  const client = new FakeClient();
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId: null,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter,
    audioSink: {
      isReady: () => true,
      acceptOpusStream: async () => {},
    },
    client,
  });

  await runtime.start();
  assert.equal(runtime.outputFailed, true);
  assert.equal(runtime.sessions.captureHealthy, false);
  assert.equal(runtime.sessions.captureHealthReason, "jsonl_output_unavailable");
  await runtime.shutdown("test_complete");
});

test("guild-scoped runtime rejects a foreign guild before any session mutation", async () => {
  const configuredGuild = "123456789012345678";
  const foreignGuild = "223456789012345678";
  const userId = "323456789012345678";
  const voiceChannelId = "423456789012345678";
  const output = emitterEvents();
  const client = new FakeClient();
  const sessions = new SessionManager({ captureHealthy: true });
  let closeCalls = 0;
  const originalClose = sessions.close.bind(sessions);
  sessions.close = () => {
    closeCalls += 1;
    return originalClose();
  };
  let connectionAttempts = 0;
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "523456789012345678",
      guildId: configuredGuild,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: {
      isReady: () => true,
      acceptOpusStream: async () => {},
    },
    client,
    sessionManager: sessions,
    connectionFactory: () => {
      connectionAttempts += 1;
      throw new Error("foreign guild must not reach voice connection setup");
    },
  });

  const replies = [];
  const participant = { id: userId, user: { bot: false } };
  const voiceChannel = { id: voiceChannelId, members: new Map([[userId, participant]]) };
  const foreignGuildObject = {
    members: { cache: new Map([[userId, { voice: { channel: voiceChannel } }]]) },
  };
  const interaction = {
    commandName: "vem",
    guildId: foreignGuild,
    guild: foreignGuildObject,
    channelId: "623456789012345678",
    user: { id: userId },
    options: { getSubcommand: () => "start" },
    replied: false,
    deferred: false,
    isChatInputCommand: () => true,
    inGuild: () => true,
    reply: async (response) => {
      interaction.replied = true;
      replies.push(response);
    },
    followUp: async (response) => {
      replies.push(response);
    },
  };

  client.emit(Events.InteractionCreate, interaction);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(sessions.getSession(foreignGuild), null);
  assert.equal(sessions.getSession(configuredGuild), null);
  assert.equal(sessions.listSessions().length, 0);
  assert.equal(connectionAttempts, 0);
  assert.equal(replies.length, 1);
  assert.equal(replies[0].flags, MessageFlags.Ephemeral);
  assert.match(replies[0].content, /not configured for this server/i);
  const rejection = output.events().find(({ type }) => type === "command.rejected");
  assert.equal(rejection.payload.guildId, foreignGuild);
  assert.equal(rejection.payload.reason, "guild_not_allowed");

  await runtime.shutdown("test_complete");
  assert.equal(closeCalls, 1, "runtime shutdown must cleanly close consent state");
});

test("runtime survives a connection torn down while its handshake is pending", async () => {
  const guildId = "111111111111111111";
  const voiceChannelId = "222222222222222222";
  const controlChannelId = "333333333333333333";
  const userId = "444444444444444444";

  class FakeVoiceConnection extends EventEmitter {
    constructor() {
      super();
      this.state = { status: VoiceConnectionStatus.Signalling };
      this.destroyCount = 0;
      this.receiver = { speaking: new EventEmitter(), ssrcMap: new Map(), subscribe: () => null };
    }

    destroy() {
      if (this.state.status === VoiceConnectionStatus.Destroyed) {
        throw new Error("Cannot destroy VoiceConnection - it has already been destroyed");
      }
      this.destroyCount += 1;
      this.state = { status: VoiceConnectionStatus.Destroyed };
    }
  }

  const member = { id: userId, user: { id: userId, bot: false } };
  const guild = {
    id: guildId,
    voiceAdapterCreator: () => ({ sendPayload: () => true, destroy: () => {} }),
    channels: {
      cache: new Map([
        [voiceChannelId, { isVoiceBased: () => true, members: new Map([[userId, member]]) }],
        [controlChannelId, { isTextBased: () => true, send: async () => {} }],
      ]),
    },
  };

  const output = emitterEvents();
  const client = new FakeClient();
  client.guilds.cache.set(guildId, guild);
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
    emitter: output.emitter,
    audioSink: {
      isReady: () => true,
      acceptOpusStream: async () => {},
    },
    client,
    connectionFactory: () => {
      const connection = new FakeVoiceConnection();
      connections.push(connection);
      // The voice websocket closing mid-handshake is what makes the runtime
      // fail closed and destroy this connection before entersState gives up.
      setTimeout(() => {
        connection.emit(
          "stateChange",
          { status: VoiceConnectionStatus.Signalling },
          { status: VoiceConnectionStatus.Disconnected },
        );
      }, 0);
      return connection;
    },
  });

  await runtime.start();
  runtime.sessions.startSession({
    guildId,
    voiceChannelId,
    controlChannelId,
    startedBy: userId,
    participantIds: [userId],
  });
  assert.equal(
    runtime.sessions.setConsent({ guildId, userId, granted: true }).phase,
    "active",
  );

  const rejections = [];
  const onRejection = (reason) => rejections.push(reason);
  process.on("unhandledRejection", onRejection);
  try {
    client.emit(
      Events.VoiceStateUpdate,
      { guild, channelId: null },
      { guild, channelId: voiceChannelId },
    );
    await new Promise((resolve) => setTimeout(resolve, 200));
  } finally {
    process.off("unhandledRejection", onRejection);
  }

  assert.equal(connections.length, 1);
  assert.equal(connections[0].destroyCount, 1);
  assert.deepEqual(rejections, []);
  assert.equal(runtime.connections.size, 0);
  assert.equal(runtime.sessions.captureHealthy, false);
  await runtime.shutdown("test_complete");
});

test("a first-join disconnect bounce recovers instead of failing closed", async () => {
  // The measured field failure: the bot joins the channel, Discord
  // bounces the fresh connection through Disconnected about half a
  // second later - voice-server reassignment, or the DAVE upgrade - and
  // the old listener executed the death penalty for it: capture blocked,
  // adapter dead, worker restart. The SECOND join always worked because
  // it rode the warm session the first one left behind. A bounce that
  // re-enters Signalling and reaches Ready again must cost nothing.
  const guildId = "123456789012345678";
  const voiceChannelId = "423456789012345678";
  const controlChannelId = "433456789012345678";
  const userId = "323456789012345678";

  class FakeVoiceConnection extends EventEmitter {
    constructor() {
      super();
      this.state = { status: VoiceConnectionStatus.Signalling };
      this.destroyCount = 0;
      this.receiver = { speaking: new EventEmitter(), ssrcMap: new Map(), subscribe: () => null };
    }

    setStatus(status) {
      const oldState = this.state;
      this.state = { status };
      this.emit("stateChange", oldState, this.state);
      // The real VoiceConnection also emits the new status by NAME, and
      // entersState waits on exactly that event - a fake that only emits
      // stateChange leaves entersState hanging forever, which made the
      // join half of these tests pass vacuously.
      this.emit(status, oldState, this.state);
    }

    destroy() {
      if (this.state.status === VoiceConnectionStatus.Destroyed) {
        throw new Error("Cannot destroy VoiceConnection - it has already been destroyed");
      }
      this.destroyCount += 1;
      this.state = { status: VoiceConnectionStatus.Destroyed };
    }
  }

  const member = { id: userId, user: { id: userId, bot: false } };
  const guild = {
    id: guildId,
    voiceAdapterCreator: () => ({ sendPayload: () => true, destroy: () => {} }),
    channels: {
      cache: new Map([
        [voiceChannelId, { isVoiceBased: () => true, members: new Map([[userId, member]]) }],
        [controlChannelId, { isTextBased: () => true, send: async () => {} }],
      ]),
    },
  };

  const output = emitterEvents();
  const client = new FakeClient();
  client.guilds.cache.set(guildId, guild);
  const connections = [];
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId: null,
      silenceMs: 750,
      connectTimeoutMs: 2_000,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: { isReady: () => true, acceptOpusStream: async () => {} },
    client,
    connectionFactory: () => {
      const connection = new FakeVoiceConnection();
      connections.push(connection);
      // Reach Ready, bounce through Disconnected half a beat later -
      // exactly the field sequence - then recover the way a live
      // connection does: Signalling, then Ready.
      setTimeout(() => connection.setStatus(VoiceConnectionStatus.Ready), 0);
      setTimeout(() => connection.setStatus(VoiceConnectionStatus.Disconnected), 20);
      setTimeout(() => connection.setStatus(VoiceConnectionStatus.Signalling), 40);
      setTimeout(() => connection.setStatus(VoiceConnectionStatus.Ready), 60);
      return connection;
    },
  });

  await runtime.start();
  runtime.sessions.startSession({
    guildId,
    voiceChannelId,
    controlChannelId,
    startedBy: userId,
    participantIds: [userId],
  });
  assert.equal(
    runtime.sessions.setConsent({ guildId, userId, granted: true }).phase,
    "active",
  );

  const rejections = [];
  const onRejection = (reason) => rejections.push(reason);
  process.on("unhandledRejection", onRejection);
  try {
    client.emit(
      Events.VoiceStateUpdate,
      { guild, channelId: null },
      { guild, channelId: voiceChannelId },
    );
    await new Promise((resolve) => setTimeout(resolve, 300));
  } finally {
    process.off("unhandledRejection", onRejection);
  }

  assert.equal(connections.length, 1);
  assert.equal(connections[0].destroyCount, 0, "the bounced connection must survive");
  assert.deepEqual(rejections, []);
  assert.equal(runtime.connections.size, 1, "the connection entry must remain");
  assert.equal(
    runtime.sessions.captureHealthy,
    true,
    "a recovered bounce must not block the capture pipeline",
  );
  // The startup health event is routine (capture is blocked until
  // Discord is ready); the one this test forbids is the bounce being
  // promoted to a disconnect failure.
  const blocked = output.events().filter(
    (event) =>
      event.type === "health.changed" &&
      event.payload.status === "blocked" &&
      event.payload.reason === "voice_connection_disconnected",
  );
  assert.deepEqual(blocked, [], "no fail-closed event for a recovered bounce");
  await runtime.shutdown("test_complete");
});

test("a network drop rejoins the same channel with consent intact", async () => {
  // Shino's call, 2026-09-01: reconnect on a network drop. The socket
  // died mid-raid, nobody's consent changed - so the adapter rejoins
  // the SAME channel with the SAME consent ledger instead of waiting
  // for a human to re-invite it. This factory happily hands out a new
  // connection, which is exactly a network blip healing: the rejoin
  // must succeed, health must stay unblocked, and the session must
  // never have left ACTIVE.
  const guildId = "123456789012345678";
  const voiceChannelId = "423456789012345678";
  const controlChannelId = "433456789012345678";
  const userId = "323456789012345678";

  class FakeVoiceConnection extends EventEmitter {
    constructor() {
      super();
      this.state = { status: VoiceConnectionStatus.Signalling };
      this.destroyCount = 0;
      this.receiver = { speaking: new EventEmitter(), ssrcMap: new Map(), subscribe: () => null };
    }

    setStatus(status) {
      const oldState = this.state;
      this.state = { status };
      this.emit("stateChange", oldState, this.state);
      // The real VoiceConnection also emits the new status by NAME, and
      // entersState waits on exactly that event - a fake that only emits
      // stateChange leaves entersState hanging forever, which made the
      // join half of these tests pass vacuously.
      this.emit(status, oldState, this.state);
    }

    destroy() {
      if (this.state.status === VoiceConnectionStatus.Destroyed) {
        throw new Error("Cannot destroy VoiceConnection - it has already been destroyed");
      }
      this.destroyCount += 1;
      this.state = { status: VoiceConnectionStatus.Destroyed };
    }
  }

  const member = { id: userId, user: { id: userId, bot: false } };
  const guild = {
    id: guildId,
    voiceAdapterCreator: () => ({ sendPayload: () => true, destroy: () => {} }),
    channels: {
      cache: new Map([
        [voiceChannelId, { isVoiceBased: () => true, members: new Map([[userId, member]]) }],
        [controlChannelId, { isTextBased: () => true, send: async () => {} }],
      ]),
    },
  };

  const output = emitterEvents();
  const client = new FakeClient();
  client.guilds.cache.set(guildId, guild);
  const connections = [];
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId: null,
      silenceMs: 750,
      // 2s, matching the recovered-bounce test: the join needs a real
      // budget, and the grace window reuses it, so the fail-closed
      // outcome lands at ~2s and the poll below waits it out.
      connectTimeoutMs: 2_000,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: { isReady: () => true, acceptOpusStream: async () => {} },
    client,
    reconnectDelaysMs: [10, 10],
    connectionFactory: () => {
      const connection = new FakeVoiceConnection();
      connections.push(connection);
      // Only reach Ready here. The disconnect is driven by the TEST
      // after voice.connected is actually emitted - timers raced the
      // runtime's own async hops and landed the disconnect mid-join,
      // which exercises the join-failure path instead of this one.
      setTimeout(() => connection.setStatus(VoiceConnectionStatus.Ready), 0);
      return connection;
    },
  });

  await runtime.start();
  runtime.sessions.startSession({
    guildId,
    voiceChannelId,
    controlChannelId,
    startedBy: userId,
    participantIds: [userId],
  });
  assert.equal(
    runtime.sessions.setConsent({ guildId, userId, granted: true }).phase,
    "active",
  );

  const rejections = [];
  const onRejection = (reason) => rejections.push(reason);
  process.on("unhandledRejection", onRejection);
  try {
    client.emit(
      Events.VoiceStateUpdate,
      { guild, channelId: null },
      { guild, channelId: voiceChannelId },
    );
    // Wait for the join to actually complete before disconnecting, so
    // this exercises the post-join grace path and not the join itself.
    const joined = async () => {
      for (let attempt = 0; attempt < 100; attempt += 1) {
        if (output.events().some((event) => event.type === "voice.connected")) {
          return true;
        }
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
      return false;
    };
    assert.equal(await joined(), true, "the join itself must succeed first");
    connections[0].setStatus(VoiceConnectionStatus.Disconnected);
    // Grace (connectTimeoutMs) plus the first retry delay, then the
    // rejoin; poll for it rather than guessing a sleep.
    for (let attempt = 0; attempt < 80; attempt += 1) {
      if (output.events().some((event) => event.type === "voice.reconnected")) {
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  } finally {
    process.off("unhandledRejection", onRejection);
  }

  assert.deepEqual(rejections, []);
  assert.equal(
    output.events().some((event) => event.type === "voice.reconnecting"),
    true,
    "the drop must be announced as a reconnect attempt",
  );
  const reconnected = output.events().filter(
    (event) => event.type === "voice.reconnected",
  );
  assert.equal(reconnected.length, 1, "the rejoin must be reported");
  assert.equal(reconnected[0].payload.voiceChannelId, voiceChannelId);
  assert.equal(connections.length, 2, "a fresh connection must be made");
  assert.equal(
    runtime.sessions.captureHealthy,
    true,
    "a healed network drop must not leave the pipeline blocked",
  );
  assert.equal(
    runtime.sessions.getSession(guildId)?.phase,
    "active",
    "the session must survive the drop - consent never changed",
  );
  await runtime.shutdown("test_complete");
});

test("a drop whose rejoins all fail still fails closed", async () => {
  // The other half, preserved from the pre-rejoin contract: when every
  // rejoin attempt fails too, this is a real outage, and pretending
  // otherwise would leave deaf players watching a silent overlay with
  // the pipeline reporting healthy. A human re-invite is the way back.
  const guildId = "123456789012345678";
  const voiceChannelId = "423456789012345678";
  const controlChannelId = "433456789012345678";
  const userId = "323456789012345678";

  class FakeVoiceConnection extends EventEmitter {
    constructor() {
      super();
      this.state = { status: VoiceConnectionStatus.Signalling };
      this.receiver = { speaking: new EventEmitter(), ssrcMap: new Map(), subscribe: () => null };
    }

    setStatus(status) {
      const oldState = this.state;
      this.state = { status };
      this.emit("stateChange", oldState, this.state);
      this.emit(status, oldState, this.state);
    }

    destroy() {
      if (this.state.status === VoiceConnectionStatus.Destroyed) {
        throw new Error("Cannot destroy VoiceConnection - it has already been destroyed");
      }
      this.state = { status: VoiceConnectionStatus.Destroyed };
    }
  }

  const member = { id: userId, user: { id: userId, bot: false } };
  const guild = {
    id: guildId,
    voiceAdapterCreator: () => ({ sendPayload: () => true, destroy: () => {} }),
    channels: {
      cache: new Map([
        [voiceChannelId, { isVoiceBased: () => true, members: new Map([[userId, member]]) }],
        [controlChannelId, { isTextBased: () => true, send: async () => {} }],
      ]),
    },
  };

  const output = emitterEvents();
  const client = new FakeClient();
  client.guilds.cache.set(guildId, guild);
  const connections = [];
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId: null,
      silenceMs: 750,
      // Short join budget so each failed rejoin attempt resolves fast.
      connectTimeoutMs: 300,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: { isReady: () => true, acceptOpusStream: async () => {} },
    client,
    reconnectDelaysMs: [10, 10],
    connectionFactory: () => {
      const connection = new FakeVoiceConnection();
      connections.push(connection);
      if (connections.length === 1) {
        // Only the FIRST join succeeds; every rejoin stays Signalling
        // until the join budget expires.
        setTimeout(() => connection.setStatus(VoiceConnectionStatus.Ready), 0);
      }
      return connection;
    },
  });

  await runtime.start();
  runtime.sessions.startSession({
    guildId,
    voiceChannelId,
    controlChannelId,
    startedBy: userId,
    participantIds: [userId],
  });
  assert.equal(
    runtime.sessions.setConsent({ guildId, userId, granted: true }).phase,
    "active",
  );

  const rejections = [];
  const onRejection = (reason) => rejections.push(reason);
  process.on("unhandledRejection", onRejection);
  try {
    client.emit(
      Events.VoiceStateUpdate,
      { guild, channelId: null },
      { guild, channelId: voiceChannelId },
    );
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (output.events().some((event) => event.type === "voice.connected")) {
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    connections[0].setStatus(VoiceConnectionStatus.Disconnected);
    // Intermediate rejoin attempts already fail closed, so captureHealthy
    // goes false long before the loop gives up - poll for the TERMINAL
    // report instead, or this finishes mid-loop and proves nothing about
    // the give-up path.
    for (let attempt = 0; attempt < 120; attempt += 1) {
      const done = output.events().some(
        (event) =>
          event.type === "health.changed" &&
          event.payload.reason === "voice_connection_disconnected",
      );
      if (done) {
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  } finally {
    process.off("unhandledRejection", onRejection);
  }

  assert.deepEqual(rejections, []);
  assert.equal(
    runtime.sessions.captureHealthy,
    false,
    "a drop that cannot be healed must still block the pipeline",
  );
  assert.equal(
    connections.length >= 2,
    true,
    "the rejoin must actually have been attempted before giving up",
  );
  // The terminal report names the drop itself, not the intermediate
  // join failures - it is the reason an operator acts on.
  const terminal = output.events().filter(
    (event) =>
      event.type === "health.changed" &&
      event.payload.status === "blocked" &&
      event.payload.reason === "voice_connection_disconnected",
  );
  assert.equal(
    terminal.length >= 1,
    true,
    "exhausted rejoins must report the drop itself, not only join failures",
  );
  await runtime.shutdown("test_complete");
});

test("an operator invitation binds the invitee, never the operator", async () => {
  // Shino hands the links out himself. If the token followed the person
  // who TYPED the command, every device he paired would answer to his
  // identity, and presence would keep or kill everyone's captions
  // together the moment he joined or left voice.
  const guildId = "123456789012345678";
  const operatorId = "444444444444444444";
  const inviteeId = "555555555555555555";
  const output = emitterEvents();
  const client = new FakeClient();
  const sessions = new SessionManager({ captureHealthy: true });
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId: null,
      silenceMs: 750,
      connectTimeoutMs: 2_000,
      maxSegmentMs: 30_000,
      joinBaseUrl: "https://ears.example",
    },
    emitter: output.emitter,
    audioSink: { isReady: () => true, acceptOpusStream: async () => {} },
    client,
    sessionManager: sessions,
    connectionFactory: () => {
      throw new Error("an invitation must not touch voice");
    },
  });
  await runtime.start();
  const replies = [];
  const guild = {
    id: guildId,
    members: { cache: new Map([[operatorId, { voice: { channel: null } }]]) },
    channels: { cache: new Map() },
  };
  const interaction = {
    commandName: "vem",
    guildId,
    guild,
    channelId: "623456789012345678",
    user: { id: operatorId },
    memberPermissions: { has: () => true },
    options: {
      getSubcommand: () => "invite",
      getUser: () => ({ id: inviteeId, bot: false }),
      getString: () => null,
    },
    replied: false,
    deferred: false,
    isChatInputCommand: () => true,
    inGuild: () => true,
    reply: async (response) => {
      interaction.replied = true;
      replies.push(response);
    },
    followUp: async (response) => {
      replies.push(response);
    },
  };
  client.emit(Events.InteractionCreate, interaction);
  await new Promise((resolve) => setImmediate(resolve));

  const requested = output.events().find(
    ({ type }) => type === "viewer.enroll.requested",
  );
  assert.ok(requested, "no invitation was issued");
  assert.equal(
    requested.payload.userId,
    inviteeId,
    "the invitation bound the operator instead of the person it is for",
  );
  assert.notEqual(requested.payload.userId, operatorId);
  assert.match(requested.payload.code, /^ghe_[A-Za-z0-9_-]{32}$/);
  // The reply is private, names the invitee, and carries the real link.
  assert.equal(replies.length, 1);
  assert.equal(replies[0].flags, MessageFlags.Ephemeral);
  assert.match(replies[0].content, new RegExp(inviteeId));
  assert.match(replies[0].content, /https:\/\/ears\.example\/join#ghe_/);
  await runtime.shutdown("test_complete");
});

test("a member without manage-guild gets their own link, not someone else's", async () => {
  const guildId = "123456789012345678";
  const memberId = "444444444444444444";
  const targetId = "555555555555555555";
  const output = emitterEvents();
  const client = new FakeClient();
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId: null,
      silenceMs: 750,
      connectTimeoutMs: 2_000,
      maxSegmentMs: 30_000,
      joinBaseUrl: "https://ears.example",
    },
    emitter: output.emitter,
    audioSink: { isReady: () => true, acceptOpusStream: async () => {} },
    client,
    sessionManager: new SessionManager({ captureHealthy: true }),
    connectionFactory: () => {
      throw new Error("an invitation must not touch voice");
    },
  });
  await runtime.start();
  const replies = [];
  const guild = {
    id: guildId,
    members: { cache: new Map([[memberId, { voice: { channel: null } }]]) },
    channels: { cache: new Map() },
  };
  const interaction = {
    commandName: "vem",
    guildId,
    guild,
    channelId: "623456789012345678",
    user: { id: memberId },
    memberPermissions: { has: () => false },
    options: {
      getSubcommand: () => "invite",
      getUser: () => ({ id: targetId, bot: false }),
      getString: () => null,
    },
    replied: false,
    deferred: false,
    isChatInputCommand: () => true,
    inGuild: () => true,
    reply: async (response) => {
      interaction.replied = true;
      replies.push(response);
    },
    followUp: async (response) => {
      replies.push(response);
    },
  };
  client.emit(Events.InteractionCreate, interaction);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(
    output.events().some(({ type }) => type === "viewer.enroll.requested"),
    false,
    "a credential was minted for another computer without permission",
  );
  assert.equal(replies.length, 1);
  assert.equal(replies[0].flags, MessageFlags.Ephemeral);
  assert.match(replies[0].content, /manage this server/i);
  await runtime.shutdown("test_complete");
});

test("a mid-session decline cuts the decliner's live stream immediately", async () => {
  // The genuinely new mechanism under per-speaker capture: the room-wide
  // teardown that used to end a decliner's stream no longer fires, so the
  // runtime must end exactly that person's stream itself - before the reply
  // posts, not at the next natural silence - while the other speaker's
  // stream keeps flowing untouched.
  const guildId = "123456789012345678";
  const voiceChannelId = "423456789012345678";
  const controlChannelId = "433456789012345678";
  const alice = "323456789012345678";
  const bob = "333456789012345678";
  const output = emitterEvents();
  const client = new FakeClient();
  const sessions = new SessionManager({ captureHealthy: true });
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "523456789012345678",
      guildId,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: { isReady: () => true, acceptOpusStream: async () => {} },
    client,
    sessionManager: sessions,
    connectionFactory: () => {
      throw new Error("no connection needed in this test");
    },
  });

  sessions.startSession({
    guildId,
    voiceChannelId,
    controlChannelId,
    startedBy: alice,
    participantIds: [alice, bob],
  });
  sessions.setConsent({ guildId, userId: alice, granted: true });
  sessions.setConsent({ guildId, userId: bob, granted: true });

  const streamFor = (userId) => {
    const session = sessions.getSession(guildId);
    const segment = runtime.speakers.begin({
      guildId,
      userId,
      audienceGeneration: session.generation,
      audienceRevision: session.audienceRevision,
    });
    const stream = { destroyed: false, destroy() { this.destroyed = true; } };
    runtime.streams.set(segment.segmentId, {
      segment,
      stream,
      timer: null,
      finished: false,
    });
    return { segment, stream };
  };
  const aliceCapture = streamFor(alice);
  const bobCapture = streamFor(bob);

  // A live voice connection, as production always has while anyone speaks -
  // without it #syncGuild would try to CONNECT and fail closed, which is a
  // different scenario than the one under test.
  runtime.connections.set(guildId, {
    connection: { destroy() {} },
    voiceChannelId,
  });

  const aliceMember = { id: alice, user: { id: alice, bot: false } };
  const bobMember = { id: bob, user: { id: bob, bot: false } };
  const voiceChannel = {
    id: voiceChannelId,
    members: new Map([[alice, aliceMember], [bob, bobMember]]),
  };
  const guildStub = {
    id: guildId,
    channels: { cache: new Map() },
    members: { cache: new Map([[alice, { voice: { channel: voiceChannel } }]]) },
  };
  const interaction = {
    guildId,
    guild: guildStub,
    channelId: controlChannelId,
    inGuild: () => true,
    user: { id: alice },
    commandName: "vem",
    options: { getSubcommand: () => "decline" },
    isChatInputCommand: () => true,
    replied: false,
    reply: async () => {
      interaction.replied = true;
    },
  };
  client.emit(Events.InteractionCreate, interaction);
  await new Promise((resolve) => setTimeout(resolve, 100));

  assert.equal(interaction.replied, true, "the decline reply must post");
  assert.equal(aliceCapture.stream.destroyed, true, "the decliner's stream must be cut");
  assert.equal(runtime.streams.has(aliceCapture.segment.segmentId), false);
  assert.equal(bobCapture.stream.destroyed, false, "the other speaker keeps flowing");
  assert.equal(runtime.streams.has(bobCapture.segment.segmentId), true);
  const endedEvents = output
    .events()
    .filter((event) => event.type === "speaker.segment.ended");
  assert.equal(endedEvents.length, 1, "exactly one segment ends: the decliner's");
});

test("session stopped is emitted only after Discord capture is destroyed", async () => {
  const guildId = "723456789012345678";
  const voiceChannelId = "823456789012345678";
  const controlChannelId = "833456789012345678";
  const userId = "923456789012345678";
  const order = [];
  const emitter = new JsonlEmitter({
    writer: {
      write: (chunk) => {
        const event = JSON.parse(chunk);
        if (event.type === "session.stopped") {
          order.push("session.stopped");
        }
      },
    },
  });
  const client = new FakeClient();
  const sessions = new SessionManager({ captureHealthy: true });
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter,
    audioSink: healthySink(),
    client,
    sessionManager: sessions,
  });

  sessions.startSession({
    guildId,
    voiceChannelId,
    controlChannelId,
    startedBy: userId,
    participantIds: [userId],
  });
  sessions.setConsent({ guildId, userId, granted: true });
  runtime.connections.set(guildId, {
    voiceChannelId,
    connection: { destroy: () => order.push("connection.destroyed") },
  });

  const member = { id: userId, user: { id: userId, bot: false } };
  const voiceChannel = {
    id: voiceChannelId,
    members: new Map([[userId, member]]),
  };
  const guild = {
    id: guildId,
    channels: { cache: new Map() },
    members: {
      cache: new Map([[userId, { voice: { channel: voiceChannel } }]]),
    },
  };
  const interaction = {
    guildId,
    guild,
    channelId: controlChannelId,
    inGuild: () => true,
    user: { id: userId },
    commandName: "vem",
    options: { getSubcommand: () => "stop" },
    isChatInputCommand: () => true,
    replied: false,
    reply: async () => {
      interaction.replied = true;
    },
  };

  client.emit(Events.InteractionCreate, interaction);
  await new Promise((resolve) => setTimeout(resolve, 50));

  assert.equal(interaction.replied, true);
  assert.deepEqual(order, ["connection.destroyed", "session.stopped"]);
});

test("participant pause is emitted only after Discord capture is destroyed", async () => {
  const guildId = "623456789012345678";
  const voiceChannelId = "633456789012345678";
  const controlChannelId = "643456789012345678";
  const userId = "653456789012345678";
  const order = [];
  const emitter = new JsonlEmitter({
    writer: {
      write: (chunk) => {
        const event = JSON.parse(chunk);
        if (event.type === "session.participants.changed") {
          order.push("session.paused");
        }
      },
    },
  });
  const client = new FakeClient();
  const sessions = new SessionManager({ captureHealthy: true });
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter,
    audioSink: healthySink(),
    client,
    sessionManager: sessions,
  });

  sessions.startSession({
    guildId,
    voiceChannelId,
    controlChannelId,
    startedBy: userId,
    participantIds: [userId],
  });
  sessions.setConsent({ guildId, userId, granted: true });
  runtime.connections.set(guildId, {
    voiceChannelId,
    connection: { destroy: () => order.push("connection.destroyed") },
  });
  const voiceChannel = {
    isVoiceBased: () => true,
    members: new Map(),
  };
  const guild = {
    id: guildId,
    channels: { cache: new Map([[voiceChannelId, voiceChannel]]) },
  };

  client.emit(
    Events.VoiceStateUpdate,
    { guild, channelId: voiceChannelId },
    { guild, channelId: null },
  );
  await new Promise((resolve) => setTimeout(resolve, 50));

  assert.deepEqual(order, ["connection.destroyed", "session.paused"]);
  assert.equal(sessions.getSession(guildId).phase, "paused");
});

test("the session starter leaving preserves consented members and capture", async () => {
  const guildId = "723456789012345678";
  const voiceChannelId = "733456789012345678";
  const controlChannelId = "743456789012345678";
  const starterId = "753456789012345678";
  const remainingId = "763456789012345678";
  const output = emitterEvents();
  const client = new FakeClient();
  const sessions = new SessionManager({ captureHealthy: true });
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: healthySink(),
    client,
    sessionManager: sessions,
  });

  sessions.startSession({
    guildId,
    voiceChannelId,
    controlChannelId,
    startedBy: starterId,
    participantIds: [starterId, remainingId],
  });
  sessions.setConsent({ guildId, userId: starterId, granted: true });
  sessions.setConsent({ guildId, userId: remainingId, granted: true });

  const captureFor = (userId) => {
    const session = sessions.getSession(guildId);
    const segment = runtime.speakers.begin({
      guildId,
      userId,
      audienceGeneration: session.generation,
      audienceRevision: session.audienceRevision,
    });
    const stream = { destroyed: false, destroy() { this.destroyed = true; } };
    runtime.streams.set(segment.segmentId, {
      segment,
      stream,
      timer: null,
      finished: false,
    });
    return { segment, stream };
  };
  const starterCapture = captureFor(starterId);
  const remainingCapture = captureFor(remainingId);

  let destroyCount = 0;
  runtime.connections.set(guildId, {
    voiceChannelId,
    connection: { destroy: () => { destroyCount += 1; } },
  });
  const guild = {
    id: guildId,
    // discord.js derives channel.members from voice states that also resolve
    // to cached GuildMembers. Without the privileged GuildMembers intent, a
    // remaining voice state can be real while that projection is empty.
    channels: {
      cache: new Map([
        [voiceChannelId, { isVoiceBased: () => true, members: new Map() }],
      ]),
    },
    members: { cache: new Map() },
  };
  const starterMember = { id: starterId, user: { id: starterId, bot: false } };

  client.emit(
    Events.VoiceStateUpdate,
    { id: starterId, member: starterMember, guild, channelId: voiceChannelId },
    { id: starterId, member: starterMember, guild, channelId: null },
  );
  await new Promise((resolve) => setTimeout(resolve, 50));

  const session = sessions.getSession(guildId);
  assert.equal(session.phase, "active");
  assert.equal(session.startedBy, starterId, "leaving does not transfer stop authority");
  assert.deepEqual(
    session.participants.map(({ userId }) => userId),
    [remainingId],
  );
  assert.equal(sessions.canCapture({ guildId, userId: remainingId }), true);
  assert.equal(destroyCount, 0, "the bot must remain in voice for the remaining speaker");
  assert.equal(runtime.connections.has(guildId), true);
  assert.equal(starterCapture.stream.destroyed, true, "the leaver's capture ends immediately");
  assert.equal(runtime.streams.has(starterCapture.segment.segmentId), false);
  assert.equal(remainingCapture.stream.destroyed, false, "remaining capture keeps flowing");
  assert.equal(runtime.streams.has(remainingCapture.segment.segmentId), true);
});

test("blocked pipeline health is emitted only after capture teardown", async () => {
  const guildId = "523456789012345678";
  const voiceChannelId = "533456789012345678";
  const userId = "543456789012345678";
  const order = [];
  let sinkReady = true;
  const emitter = new JsonlEmitter({
    writer: {
      write: (chunk) => {
        const event = JSON.parse(chunk);
        if (
          event.type === "health.changed" &&
          event.payload.status === "blocked"
        ) {
          order.push("health.blocked");
        }
      },
    },
  });
  const sessions = new SessionManager({ captureHealthy: true });
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter,
    audioSink: {
      isReady: () => sinkReady,
      acceptOpusStream: async () => {},
    },
    client: new FakeClient(),
    sessionManager: sessions,
  });
  runtime.discordHealthy = true;
  sessions.startSession({
    guildId,
    voiceChannelId,
    controlChannelId: "553456789012345678",
    startedBy: userId,
    participantIds: [userId],
  });
  sessions.setConsent({ guildId, userId, granted: true });
  runtime.connections.set(guildId, {
    voiceChannelId,
    connection: { destroy: () => order.push("connection.destroyed") },
  });

  sinkReady = false;
  await runtime.refreshAudioSinkHealth();

  assert.deepEqual(order, ["connection.destroyed", "health.blocked"]);
});

test("audience snapshots cover start, membership changes, stop, and heartbeat", async () => {
  const guildId = "123456789012345678";
  const voiceChannelId = "223456789012345678";
  const controlChannelId = "323456789012345678";
  const alice = "423456789012345678";
  const bob = "523456789012345678";
  const output = emitterEvents();
  const client = new FakeClient();
  const voiceMembers = new Map();
  const voiceChannel = {
    id: voiceChannelId,
    isVoiceBased: () => true,
    members: voiceMembers,
  };
  const controlChannel = { isTextBased: () => true, send: async () => {} };
  const guild = {
    id: guildId,
    channels: {
      cache: new Map([
        [voiceChannelId, voiceChannel],
        [controlChannelId, controlChannel],
      ]),
    },
    members: { cache: new Map() },
  };
  const member = (id) => ({ id, user: { id, bot: false } });
  voiceMembers.set(alice, member(alice));
  guild.members.cache.set(alice, { voice: { channel: voiceChannel } });
  client.guilds.cache.set(guildId, guild);

  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "623456789012345678",
      guildId: null,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: healthySink(),
    client,
    audienceHeartbeatMs: 10,
  });
  await runtime.start();

  const command = (subcommand) => {
    const interaction = {
      guildId,
      guild,
      channelId: controlChannelId,
      inGuild: () => true,
      user: { id: alice },
      commandName: "vem",
      options: { getSubcommand: () => subcommand },
      isChatInputCommand: () => true,
      replied: false,
      deferred: false,
      reply: async () => { interaction.replied = true; },
      followUp: async () => {},
    };
    client.emit(Events.InteractionCreate, interaction);
    return interaction;
  };

  const startedInteraction = command("start");
  await new Promise((resolve) => setTimeout(resolve, 35));
  assert.equal(startedInteraction.replied, true);

  voiceMembers.set(bob, member(bob));
  client.emit(
    Events.VoiceStateUpdate,
    { guild, channelId: null },
    { guild, channelId: voiceChannelId },
  );
  await new Promise((resolve) => setTimeout(resolve, 20));

  const stoppedInteraction = command("stop");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(stoppedInteraction.replied, true);

  const snapshots = output.events().filter(({ type }) => type === "audience.snapshot");
  const first = snapshots.find(({ payload }) => payload.revision === 1);
  const changed = snapshots.find(({ payload }) => payload.revision === 2);
  const stopped = snapshots.find(({ payload }) => payload.revision === 3);
  assert.equal(first.payload.generation, 1);
  assert.deepEqual(first.payload.participantIds, [alice]);
  assert.deepEqual(changed.payload.participantIds, [alice, bob]);
  assert.deepEqual(stopped.payload.participantIds, []);
  assert.ok(
    snapshots.filter(({ payload }) => payload.revision === 1).length >= 2,
    "the active audience must be repeated by the heartbeat",
  );

  await runtime.shutdown("test_complete");
});

async function audienceHeartbeatFixture(t) {
  const guildId = "123456789012345678";
  const voiceChannelId = "223456789012345678";
  const userId = "323456789012345678";
  const output = emitterEvents();
  const client = new FakeClient();
  let heartbeat;
  t.mock.method(globalThis, "setInterval", (callback) => {
    heartbeat = callback;
    return { unref() {} };
  });
  t.mock.method(globalThis, "clearInterval", () => {});
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "423456789012345678",
      guildId: null,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: healthySink(),
    client,
  });
  await runtime.start();
  t.after(() => runtime.shutdown("test_complete"));
  runtime.sessions.startSession({
    guildId,
    voiceChannelId,
    controlChannelId: "523456789012345678",
    startedBy: userId,
    participantIds: [userId],
  });
  runtime.sessions.setConsent({ guildId, userId, granted: true });
  const snapshots = () => output.events().filter(({ type }) => type === "audience.snapshot");
  heartbeat();
  assert.deepEqual(snapshots().at(-1).payload.participantIds, [userId]);
  return { client, guildId, heartbeat, output, runtime, snapshots, userId, voiceChannelId };
}

for (const [event, reason, args] of [
  [Events.ShardDisconnect, "discord_gateway_disconnected", [{ code: 4007 }, 0]],
  [Events.ShardReconnecting, "discord_gateway_reconnecting", [0]],
  [Events.Invalidated, "discord_gateway_invalidated", []],
  [Events.ShardError, "discord_gateway_error", [new Error("test gateway failure"), 0]],
  [Events.Error, "discord_client_error", [new Error("test client failure")]],
]) {
  test(`${event} stops audience renewal and fails closed until adapter restart`, async (t) => {
    const fixture = await audienceHeartbeatFixture(t);
    const { client, guildId, heartbeat, output, runtime, snapshots, userId, voiceChannelId } = fixture;
    let snapshotCountAtTeardown = null;
    runtime.connections.set(guildId, {
      voiceChannelId,
      connection: {
        destroy: () => {
          // Even a heartbeat queued before clearInterval cannot renew the roster.
          heartbeat();
          snapshotCountAtTeardown = snapshots().length;
        },
      },
    });
    const before = snapshots().length;
    client.emit(event, ...args);
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(snapshotCountAtTeardown, before);
    assert.equal(runtime.audienceHeartbeat, null);
    assert.equal(runtime.sessions.canCapture({ guildId, userId }), false);
    assert.ok(output.events().some(({ type, payload }) =>
      type === "health.changed" && payload.status === "blocked" && payload.reason === reason));

    // Discord's resume/ready notifications do not independently verify the room.
    client.emit(Events.ShardResume, 0, 1);
    client.emit(Events.ShardReady, 0, new Set());
    await runtime.refreshAudioSinkHealth();
    heartbeat();
    assert.equal(runtime.sessions.canCapture({ guildId, userId }), false);
    assert.equal(snapshots().length, before);
  });
}

for (const startupStage of ["login", "registration"]) {
  test(`a gateway failure during ${startupStage} cannot become a healthy startup`, async (t) => {
    const output = emitterEvents();
    const client = new FakeClient();
    const entered = deferred();
    const release = deferred();
    const pending = async () => {
      entered.resolve();
      await release.promise;
    };
    if (startupStage === "login") {
      client.login = pending;
    } else {
      client.application.commands.set = pending;
    }
    const runtime = new DiscordAdapterRuntime({
      config: { token: "unused-test-token", applicationId: "123456789012345678", guildId: null },
      emitter: output.emitter,
      audioSink: healthySink(),
      client,
    });
    t.after(() => runtime.shutdown("test_complete"));
    const startup = runtime.start();
    await entered.promise;
    client.emit(Events.ShardReconnecting, 0);
    release.resolve();
    await startup;

    assert.equal(runtime.discordHealthy, false);
    assert.equal(runtime.sessions.captureHealthy, false);
    assert.equal(runtime.audienceHeartbeat, null);
    assert.equal(output.events().some(({ type }) => type === "adapter.ready"), false);
  });
}

test("shutdown blocks an already queued audience heartbeat while Discord destruction waits", async (t) => {
  const { client, heartbeat, runtime, snapshots } = await audienceHeartbeatFixture(t);
  const release = deferred();
  client.destroy = async () => { await release.promise; };
  const before = snapshots().length;
  const shutdown = runtime.shutdown("service_stop");
  heartbeat();
  release.resolve();
  await shutdown;
  heartbeat();

  assert.equal(runtime.audienceHeartbeat, null);
  assert.equal(snapshots().length, before);
});

test("a failed Discord start says why on stderr, with the token redacted", async () => {
  // A bare catch here once discarded the Discord error entirely, so an
  // invalid token, a missing intent and a wrong guild id all reached the
  // operator as the same four words while the bot looped. The reason has
  // to survive; the token must not travel with it.
  const output = emitterEvents();
  const client = new FakeClient();
  const token = "not-a-real-token." + "x".repeat(40);
  client.login = async () => {
    throw new Error("Unknown Guild while using " + token);
  };

  const written = [];
  const realWrite = process.stderr.write;
  process.stderr.write = (chunk) => {
    written.push(String(chunk));
    return true;
  };

  const runtime = new DiscordAdapterRuntime({
    config: {
      token,
      applicationId: "123456789012345678",
      guildId: "1543774537659981844",
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter: output.emitter,
    audioSink: { isReady: () => true, acceptOpusStream: () => {} },
    client,
  });

  try {
    await assert.rejects(() => runtime.start());
  } finally {
    process.stderr.write = realWrite;
  }

  const diagnostics = written.join("");
  assert.match(diagnostics, /could not start Discord/);
  assert.match(diagnostics, /Unknown Guild/,
    "the actual Discord reason must reach the operator");
  assert.ok(!diagnostics.includes(token),
    "the bot token must never be written to a log");
  assert.match(diagnostics, /<redacted>/);

  const failed = output.events().filter(({ type }) => type === "adapter.failed");
  assert.equal(failed.length, 1);
  assert.equal(failed[0].payload.reason, "discord_start_failed");
});
