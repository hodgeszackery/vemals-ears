import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { MessageFlags } from "discord.js";

import { DiscordAdapterRuntime } from "../src/discord_runtime.js";
import {
  parseQuickChatCommand,
  QuickChatCommandQueue,
} from "../src/quick_chat_input.js";
import { JsonlEmitter } from "../src/protocol.js";
import { SessionManager } from "../src/session_manager.js";

const GUILD = "123456789012345678";
const VOICE = "223456789012345678";
const CONTROL = "323456789012345678";
const CONTROL_TWO = "323456789012345679";
const USER = "423456789012345678";
const REQUEST = "device_request_1234567890";
const REQUEST_TWO = "device_request_1234567891";
const REQUEST_THREE = "device_request_1234567892";

class FakeClient extends EventEmitter {
  constructor(guild) {
    super();
    this.guilds = { cache: new Map([[GUILD, guild]]), fetch: async () => guild };
    this.application = { commands: { set: async () => {} } };
    this.user = { id: "523456789012345678" };
  }

  async login() {}
  destroy() {}
}

function channelFor(channelId, send) {
  return {
    isTextBased: () => true,
    send: async (payload) => {
      const result = await send(payload);
      return result && typeof result === "object"
        ? result
        : { channelId, content: payload.content };
    },
  };
}

function runtimeHarness(
  channelSends,
  { start = true, controlChannelId = CONTROL } = {},
) {
  const guild = {
    id: GUILD,
    commands: { set: async () => {} },
    channels: {
      cache: new Map(
        [...channelSends].map(([channelId, send]) => [
          channelId,
          channelFor(channelId, send),
        ]),
      ),
    },
  };
  const sessions = new SessionManager({ captureHealthy: true });
  if (start) {
    sessions.startSession({
      guildId: GUILD,
      voiceChannelId: VOICE,
      controlChannelId,
      startedBy: USER,
      participantIds: [USER],
    });
  }
  const emitter = new JsonlEmitter({ writer: { write: () => true } });
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "623456789012345678",
      guildId: GUILD,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter,
    audioSink: { isReady: () => true, acceptOpusStream: async () => {} },
    client: new FakeClient(guild),
    sessionManager: sessions,
  });
  return { guild, runtime, sessions };
}

function runtimeWithSession(send, options = {}) {
  return runtimeHarness(new Map([[CONTROL, send]]), options).runtime;
}

test("quick-chat stdin accepts only the strict bounded command", () => {
  const line = JSON.stringify({
    version: 1,
    type: "quick_chat.command",
    requestId: REQUEST,
    speaker: "Vemal",
    wowClass: "paladin",
    text: "Stack on me",
  });
  assert.deepEqual(parseQuickChatCommand(line), {
    requestId: REQUEST,
    speaker: "Vemal",
    wowClass: "paladin",
    text: "Stack on me",
  });

  for (const value of [
    { ...JSON.parse(line), extra: true },
    { ...JSON.parse(line), requestId: "short" },
    { ...JSON.parse(line), text: "bad\nline" },
    { ...JSON.parse(line), wowClass: "bard" },
  ]) {
    assert.throws(() => parseQuickChatCommand(JSON.stringify(value)));
  }
});

test("quick-chat text bounds count Unicode code points like Python", () => {
  const emoji = String.fromCodePoint(0x1f600);
  const command = {
    version: 1,
    type: "quick_chat.command",
    requestId: REQUEST,
    speaker: emoji.repeat(32),
    wowClass: "mage",
    text: emoji.repeat(320),
  };

  assert.deepEqual(parseQuickChatCommand(JSON.stringify(command)), {
    requestId: REQUEST,
    speaker: emoji.repeat(32),
    wowClass: "mage",
    text: emoji.repeat(320),
  });
  assert.throws(() => parseQuickChatCommand(JSON.stringify({
    ...command,
    speaker: emoji.repeat(33),
  })));
  assert.throws(() => parseQuickChatCommand(JSON.stringify({
    ...command,
    text: emoji.repeat(321),
  })));
});

test("typed chat is never mirrored into Discord", async () => {
  // Shino, 2026-09-06: no outbound VE messages except the consent and
  // association dialogues. This retained compatibility terminal validates
  // and acknowledges an old command shape without having a Discord sink.
  const posts = [];
  const runtime = runtimeWithSession(async (payload) => posts.push(payload));
  const status = await runtime.postQuickChat({
    requestId: REQUEST,
    speaker: "Vemal",
    wowClass: "paladin",
    text: "Stack @everyone on <@423456789012345678> https://example.test",
  });

  assert.equal(status, "delivered");
  assert.equal(posts.length, 0, "the control channel must not receive the line");
});

test("runtime quick-chat bounds also count Unicode code points", async () => {
  const emoji = String.fromCodePoint(0x1f600);
  const posts = [];
  const runtime = runtimeWithSession(async (payload) => posts.push(payload));
  const command = {
    requestId: REQUEST,
    speaker: emoji.repeat(32),
    wowClass: "mage",
    text: emoji.repeat(320),
  };

  assert.equal(await runtime.postQuickChat(command), "delivered");
  assert.equal(
    await runtime.postQuickChat({ ...command, speaker: emoji.repeat(33) }),
    "delivery_failed",
  );
  assert.equal(
    await runtime.postQuickChat({ ...command, text: emoji.repeat(321) }),
    "delivery_failed",
  );
  assert.equal(posts.length, 0);
});

test("a delivered request ID is idempotent but a changed body is rejected", async () => {
  const posts = [];
  const runtime = runtimeWithSession(async (payload) => posts.push(payload));
  const command = {
    requestId: REQUEST,
    speaker: "Vemal",
    wowClass: "paladin",
    text: "Move left",
  };

  assert.equal(await runtime.postQuickChat(command), "delivered");
  assert.equal(await runtime.postQuickChat(command), "delivered");
  assert.equal(
    await runtime.postQuickChat({ ...command, text: "Move right" }),
    "delivery_failed",
  );
  assert.equal(posts.length, 0);
});

test("a delivered request ID is not replayed into a later session", async () => {
  const posts = [];
  const { runtime, sessions } = runtimeHarness(new Map([
    [CONTROL, async (payload) => posts.push(payload)],
  ]));
  const command = {
    requestId: REQUEST,
    speaker: "Vemal",
    wowClass: "paladin",
    text: "Old pull",
  };

  assert.equal(await runtime.postQuickChat(command), "delivered");
  sessions.stopSession({ guildId: GUILD, requestedBy: USER });
  sessions.startSession({
    guildId: GUILD,
    voiceChannelId: VOICE,
    controlChannelId: CONTROL,
    startedBy: USER,
    participantIds: [USER],
  });

  assert.equal(await runtime.postQuickChat(command), "delivery_failed");
  assert.equal(posts.length, 0);
});

test("no session is truthful, and Discord is never asked either way", async () => {
  const absent = runtimeWithSession(async () => {}, { start: false });
  assert.equal(
    await absent.postQuickChat({
      requestId: REQUEST,
      speaker: "Vemal",
      wowClass: "paladin",
      text: "Hello",
    }),
    "no_active_session",
  );

  let attempts = 0;
  const runtime = runtimeWithSession(async () => {
    attempts += 1;
    throw new Error("Discord must not be reached for typed chat");
  });
  const command = {
    requestId: REQUEST,
    speaker: "Vemal",
    wowClass: "paladin",
    text: "Hello",
  };
  assert.equal(await runtime.postQuickChat(command), "delivered");
  assert.equal(await runtime.postQuickChat(command), "delivered");
  assert.equal(attempts, 0);
});

test("a bound command cannot cross a stopped session generation", async () => {
  const posts = [];
  const { runtime, sessions } = runtimeHarness(new Map([
    [CONTROL, async (payload) => posts.push(payload)],
  ]));
  const command = {
    requestId: REQUEST,
    speaker: "Vemal",
    wowClass: "paladin",
    text: "Old pull",
  };
  const bound = runtime.bindQuickChat(command);

  sessions.stopSession({ guildId: GUILD, requestedBy: USER });
  sessions.startSession({
    guildId: GUILD,
    voiceChannelId: VOICE,
    controlChannelId: CONTROL,
    startedBy: USER,
    participantIds: [USER],
  });

  assert.equal(await runtime.postQuickChat(bound), "no_active_session");
  assert.equal(posts.length, 0);
});

test("quick-chat queue is bounded and does not bind an overflow body", async () => {
  let releaseFirst;
  const firstBlocked = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const boundTexts = [];
  const deliveredTexts = [];
  const results = [];
  const queue = new QuickChatCommandQueue({
    limit: 2,
    bind: (command) => {
      boundTexts.push(command.text);
      return { ...command };
    },
    deliver: async (command) => {
      deliveredTexts.push(command.text);
      if (command.requestId === REQUEST) {
        await firstBlocked;
      }
      return "delivered";
    },
    onResult: (result) => results.push(result),
  });

  assert.equal(queue.enqueue({ requestId: REQUEST, text: "first" }), true);
  assert.equal(queue.enqueue({ requestId: REQUEST_TWO, text: "second" }), true);
  assert.equal(
    queue.enqueue({ requestId: REQUEST_THREE, text: "overflow-secret" }),
    false,
  );
  assert.deepEqual(boundTexts, ["first", "second"]);
  assert.doesNotMatch(JSON.stringify(queue.pending), /overflow-secret/);
  assert.deepEqual(results, [{
    requestId: REQUEST_THREE,
    status: "delivery_failed",
  }]);

  releaseFirst();
  await queue.whenIdle();
  assert.deepEqual(deliveredTexts, ["first", "second"]);
});

test("quick-chat queue expires a stale buffered command", async () => {
  let now = 0;
  let releaseFirst;
  const firstBlocked = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const delivered = [];
  const results = [];
  const queue = new QuickChatCommandQueue({
    maxAgeMs: 100,
    clock: () => now,
    bind: (command) => ({ ...command }),
    deliver: async (command) => {
      delivered.push(command.requestId);
      if (command.requestId === REQUEST) {
        await firstBlocked;
      }
      return "delivered";
    },
    onResult: (result) => results.push(result),
  });

  queue.enqueue({ requestId: REQUEST, text: "first" });
  queue.enqueue({ requestId: REQUEST_TWO, text: "stale" });
  now = 100;
  releaseFirst();
  await queue.whenIdle();

  assert.deepEqual(delivered, [REQUEST]);
  assert.deepEqual(results, [
    { requestId: REQUEST, status: "delivered" },
    { requestId: REQUEST_TWO, status: "delivery_failed" },
  ]);
});

test("queued quick chat stays bound across stop and start", async () => {
  let releaseFirst;
  const firstBlocked = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const oldPosts = [];
  const newPosts = [];
  const { runtime, sessions } = runtimeHarness(new Map([
    [CONTROL, async (payload) => {
      oldPosts.push(payload);
      await firstBlocked;
    }],
    [CONTROL_TWO, async (payload) => newPosts.push(payload)],
  ]));
  const results = [];
  const queue = new QuickChatCommandQueue({
    bind: (command) => runtime.bindQuickChat(command),
    deliver: (command) => runtime.postQuickChat(command),
    onResult: (result) => results.push(result),
  });

  queue.enqueue({
    requestId: REQUEST,
    speaker: "Vemal",
    wowClass: "paladin",
    text: "First old pull",
  });
  queue.enqueue({
    requestId: REQUEST_TWO,
    speaker: "Vemal",
    wowClass: "paladin",
    text: "Queued old pull",
  });
  sessions.stopSession({ guildId: GUILD, requestedBy: USER });
  sessions.startSession({
    guildId: GUILD,
    voiceChannelId: VOICE,
    controlChannelId: CONTROL_TWO,
    startedBy: USER,
    participantIds: [USER],
  });
  releaseFirst();
  await queue.whenIdle();

  assert.equal(oldPosts.length, 0, "nothing is posted to the old channel");
  assert.equal(newPosts.length, 0, "and nothing leaks into the new one");
  assert.deepEqual(results, [
    { requestId: REQUEST, status: "delivered" },
    { requestId: REQUEST_TWO, status: "no_active_session" },
  ]);
});
