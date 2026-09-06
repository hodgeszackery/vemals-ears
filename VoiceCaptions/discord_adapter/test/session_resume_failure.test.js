// A session that cannot rejoin voice is over, not paused.
//
// Shino, 2026-09-06, 08:22: "VE is starting ghost capture sessions that
// cannot be ended." Since 07:11 a member's /vem start had failed its voice
// join; the supervisor restarted the adapter, the restart resumed the same
// session from the ledger, posted "restarted and rejoined this voice
// channel" with a consent button row, and failed the same join again.
// Fifty-two times in seventy minutes. No process lived long enough for
// /vem stop, and every resume re-saved the entry, so its thirty-minute
// window never expired. And both catches on the join path were bare, so
// worker.log said voice_connection_failed and nothing else.
//
// These pin the three halves of the fix: a failed join forgets the
// resumable entry, the "rejoined" notice waits for a live connection, and
// the failure carries the connection's last state as safe words.

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { VoiceConnectionStatus } from "@discordjs/voice";

import { DiscordAdapterRuntime, joinFailureDetail } from "../src/discord_runtime.js";
import { JsonlEmitter } from "../src/protocol.js";
import { SessionLedger } from "../src/session_ledger.js";
import { SessionManager } from "../src/session_manager.js";

const GUILD = "123456789012345678";
const VOICE = "223456789012345678";
const CONTROL = "323456789012345678";
const ALICE = "423456789012345678";

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

class StuckVoiceConnection extends EventEmitter {
  // Signalling forever: the shape of a channel the bot cannot join.
  constructor() {
    super();
    this.state = { status: VoiceConnectionStatus.Signalling };
    this.receiver = { speaking: new EventEmitter(), ssrcMap: new Map(), subscribe: () => null };
  }

  destroy() {
    this.state = { status: VoiceConnectionStatus.Destroyed };
  }
}

class MemoryConsent {
  constructor() {
    this.map = new Map();
  }

  recall(guildId, userId) {
    return this.map.get(`${guildId}:${userId}`) ?? null;
  }

  remember(guildId, userId, state) {
    this.map.set(`${guildId}:${userId}`, state);
    return true;
  }

  forget(guildId, userId) {
    this.map.delete(`${guildId}:${userId}`);
    return true;
  }

  close() {
    return true;
  }
}

function emitterEvents() {
  const chunks = [];
  const emitter = new JsonlEmitter({ writer: { write: (chunk) => chunks.push(chunk) } });
  return { emitter, events: () => chunks.map((chunk) => JSON.parse(chunk)) };
}

function buildGuild(sends) {
  const members = new Map([[ALICE, { id: ALICE, user: { id: ALICE, bot: false } }]]);
  return {
    id: GUILD,
    voiceAdapterCreator: () => ({ sendPayload: () => true, destroy: () => {} }),
    channels: {
      cache: new Map([
        [VOICE, { isVoiceBased: () => true, members }],
        [CONTROL, { isTextBased: () => true, send: async (message) => { sends.push(message); } }],
      ]),
    },
  };
}

function buildRuntime({ sessionManager, guild, emitter, connectionFactory }) {
  const client = new FakeClient();
  client.guilds.cache.set(GUILD, guild);
  return new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId: null,
      silenceMs: 750,
      connectTimeoutMs: 150,
      maxSegmentMs: 30_000,
    },
    emitter,
    audioSink: { isReady: () => true, acceptOpusStream: async () => {} },
    client,
    sessionManager,
    connectionFactory,
  });
}

function withLedgerDir(run) {
  const directory = mkdtempSync(join(tmpdir(), "vears-ghost-"));
  return run(join(directory, "sessions.json")).finally(() => {
    rmSync(directory, { recursive: true, force: true });
  });
}

async function rememberedSession(ledgerPath, consent) {
  // The process before the restart: a real session, Alice consented.
  const before = emitterEvents();
  const manager = new SessionManager({
    captureHealthy: true,
    consentLedger: consent,
    sessionLedger: new SessionLedger({ path: ledgerPath }),
  });
  const runtime = buildRuntime({
    sessionManager: manager,
    guild: buildGuild([]),
    emitter: before.emitter,
    connectionFactory: () => {
      const connection = new StuckVoiceConnection();
      setTimeout(() => {
        const old = connection.state;
        connection.state = { status: VoiceConnectionStatus.Ready };
        connection.emit("stateChange", old, connection.state);
        // entersState() waits on the status-named event, not stateChange.
        connection.emit(VoiceConnectionStatus.Ready, old, connection.state);
      }, 0);
      return connection;
    },
  });
  await runtime.start();
  manager.startSession({
    guildId: GUILD,
    voiceChannelId: VOICE,
    controlChannelId: CONTROL,
    startedBy: ALICE,
    participantIds: [ALICE],
  });
  manager.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
  await runtime.shutdown("sigterm");
  assert.ok(
    readFileSync(ledgerPath, "utf8").includes(GUILD),
    "the session must be remembered before the restart",
  );
}

test("a resumed session whose join fails is forgotten and not announced as rejoined", async () => {
  await withLedgerDir(async (ledgerPath) => {
    const consent = new MemoryConsent();
    await rememberedSession(ledgerPath, consent);

    // The process after the restart: the join never reaches Ready.
    const after = emitterEvents();
    const sends = [];
    const manager = new SessionManager({
      captureHealthy: true,
      consentLedger: consent,
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    const runtime = buildRuntime({
      sessionManager: manager,
      guild: buildGuild(sends),
      emitter: after.emitter,
      connectionFactory: () => new StuckVoiceConnection(),
    });
    await runtime.start();
    try {
      // The startup "blocked" (capture not yet healthy) is not this failure.
      const blocked = after.events().filter(
        (event) =>
          event.type === "health.changed" &&
          event.payload.status === "blocked" &&
          event.payload.reason === "voice_connection_failed",
      );
      assert.equal(
        blocked.length,
        1,
        "the failed join fails closed exactly once; saw " +
          JSON.stringify(after.events().map((event) => [event.type, event.payload?.reason])),
      );
      assert.equal(blocked[0].payload.reason, "voice_connection_failed");
      assert.equal(
        blocked[0].payload.voiceState,
        "signalling",
        "the log must say what the connection looked like when it gave up",
      );
      assert.equal(blocked[0].payload.cause, "AbortError", "entersState timed out");

      // The ledger no longer holds the session: the next restart starts clean.
      const remembered = JSON.parse(readFileSync(ledgerPath, "utf8"));
      assert.deepEqual(
        Object.keys(remembered.sessions ?? {}),
        [],
        "a session whose join failed must not resume again",
      );

      // No "rejoined" announcement, no dead consent buttons; one honest notice.
      assert.equal(sends.length, 1);
      assert.doesNotMatch(sends[0].content, /rejoined this voice channel/);
      assert.match(sends[0].content, /could not rejoin/);
      assert.match(sends[0].content, /\/vem start/);
      assert.ok(
        !sends[0].components || sends[0].components.length === 0,
        "no consent prompt for a session that does not exist",
      );
    } finally {
      await runtime.shutdown("test_complete");
    }
  });
});

test("a join that succeeds still resumes and still announces, exactly as before", async () => {
  await withLedgerDir(async (ledgerPath) => {
    const consent = new MemoryConsent();
    await rememberedSession(ledgerPath, consent);

    const after = emitterEvents();
    const sends = [];
    const manager = new SessionManager({
      captureHealthy: true,
      consentLedger: consent,
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    const runtime = buildRuntime({
      sessionManager: manager,
      guild: buildGuild(sends),
      emitter: after.emitter,
      connectionFactory: () => {
        const connection = new StuckVoiceConnection();
        setTimeout(() => {
          const old = connection.state;
          connection.state = { status: VoiceConnectionStatus.Ready };
          connection.emit("stateChange", old, connection.state);
        // entersState() waits on the status-named event, not stateChange.
        connection.emit(VoiceConnectionStatus.Ready, old, connection.state);
        }, 0);
        return connection;
      },
    });
    await runtime.start();
    try {
      assert.equal(manager.getSession(GUILD)?.phase, "active");
      assert.equal(sends.length, 1);
      assert.match(sends[0].content, /restarted and rejoined/);
      assert.equal(sends[0].components?.length, 1, "the consent prompt still ships on a real rejoin");
      const remembered = JSON.parse(readFileSync(ledgerPath, "utf8"));
      assert.ok(remembered.sessions?.[GUILD], "a live session stays resumable");
    } finally {
      await runtime.shutdown("test_complete");
    }
  });
});

test("joinFailureDetail passes only enum-shaped words and a close code", () => {
  const connection = {
    state: {
      status: VoiceConnectionStatus.Disconnected,
      closeCode: 4014,
    },
  };
  const error = new Error("The operation was aborted");
  error.name = "AbortError";
  assert.deepEqual(joinFailureDetail(connection, error), {
    voiceState: "disconnected",
    closeCode: 4014,
    cause: "AbortError",
  });
  // Nothing free-text ever crosses: a message, an endpoint, a token.
  const hostile = { state: { status: "wss://voice.example/?token=abc", closeCode: 123456 } };
  const named = new Error("x");
  named.name = "Error with spaces";
  assert.deepEqual(joinFailureDetail(hostile, named), {});
  assert.deepEqual(joinFailureDetail(undefined, undefined), {});
});
