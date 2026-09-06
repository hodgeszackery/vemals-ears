import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Events } from "discord.js";
import { VoiceConnectionStatus } from "@discordjs/voice";

import { DiscordAdapterRuntime } from "../src/discord_runtime.js";
import { JsonlEmitter } from "../src/protocol.js";
import { SessionLedger } from "../src/session_ledger.js";
import { SessionManager } from "../src/session_manager.js";

// Shino's ask, 2026-09-01: "can we have the bot just disconnect and
// reconnect after a deploy so it's not manual?" Every deploy restarts the
// adapter process; before this the bot sat outside voice until somebody
// re-ran /vem start. These tests pin the shape of the fix: where the bot
// WAS is remembered, and it goes back there through the same startSession
// a human command uses - so consent is re-derived, never carried.

const GUILD = "123456789012345678";
const VOICE = "423456789012345678";
const CONTROL = "433456789012345678";
const ALICE = "323456789012345678";
const BOB = "333456789012345678";

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

// The consent ledger's own file semantics (private file, dirty-run marker,
// grants discarded after an unclean exit) have their own coverage. Here it
// is a plain shared map, so what is under test is whether RESUME leans on
// remembered consent - and only on that.
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

function buildGuild({ memberIds = [ALICE], sends = [] } = {}) {
  const members = new Map(
    memberIds.map((id) => [id, { id, user: { id, bot: false } }]),
  );
  // A bot in the channel must never count as a participant.
  members.set("999999999999999999", {
    id: "999999999999999999",
    user: { id: "999999999999999999", bot: true },
  });
  return {
    id: GUILD,
    voiceAdapterCreator: () => ({ sendPayload: () => true, destroy: () => {} }),
    channels: {
      cache: new Map([
        [VOICE, { isVoiceBased: () => true, members }],
        [
          CONTROL,
          {
            isTextBased: () => true,
            send: async (message) => {
              sends.push(message);
            },
          },
        ],
      ]),
    },
  };
}

function buildRuntime({ sessionManager, guild, connections, emitter }) {
  const client = new FakeClient();
  client.guilds.cache.set(GUILD, guild);
  return new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "123456789012345678",
      guildId: null,
      silenceMs: 750,
      connectTimeoutMs: 2_000,
      maxSegmentMs: 30_000,
    },
    emitter,
    audioSink: { isReady: () => true, acceptOpusStream: async () => {} },
    client,
    sessionManager,
    connectionFactory: () => {
      const connection = new FakeVoiceConnection();
      connections.push(connection);
      setTimeout(() => connection.setStatus(VoiceConnectionStatus.Ready), 0);
      return connection;
    },
  });
}

function withLedgerDir(run) {
  const directory = mkdtempSync(join(tmpdir(), "vears-resume-"));
  return run(join(directory, "sessions.json")).finally(() => {
    rmSync(directory, { recursive: true, force: true });
  });
}

test("a restart puts the bot back where it was, with consent intact", async () => {
  await withLedgerDir(async (ledgerPath) => {
    const consent = new MemoryConsent();

    // --- the process before the deploy ---
    const before = emitterEvents();
    const manager1 = new SessionManager({
      captureHealthy: true,
      consentLedger: consent,
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    const runtime1 = buildRuntime({
      sessionManager: manager1,
      guild: buildGuild(),
      connections: [],
      emitter: before.emitter,
    });
    await runtime1.start();
    manager1.startSession({
      guildId: GUILD,
      voiceChannelId: VOICE,
      controlChannelId: CONTROL,
      startedBy: ALICE,
      participantIds: [ALICE],
    });
    assert.equal(
      manager1.setConsent({ guildId: GUILD, userId: ALICE, granted: true }).phase,
      "active",
    );
    await runtime1.shutdown("sigterm");

    // --- the process after the deploy: new everything, same disk ---
    const after = emitterEvents();
    const sends = [];
    const connections = [];
    const manager2 = new SessionManager({
      captureHealthy: true,
      consentLedger: consent,
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    const runtime2 = buildRuntime({
      sessionManager: manager2,
      guild: buildGuild({ sends }),
      connections,
      emitter: after.emitter,
    });
    await runtime2.start();
    try {
      const started = after.events().filter((event) => event.type === "session.started");
      assert.equal(started.length, 1, "the remembered session must come back on its own");
      assert.equal(started[0].payload.resumed, true, "a resumed start must say so");
      assert.equal(started[0].payload.session.voiceChannelId, VOICE);
      assert.equal(
        manager2.getSession(GUILD)?.phase,
        "active",
        "Alice's remembered consent makes the resumed session active",
      );
      assert.equal(connections.length, 1, "the bot must actually rejoin voice");
      // Shino, 2026-09-06: reconnects stay silent. The consent prompt
      // lives on without a notice to carry it.
      assert.equal(sends.length, 0, "a restart posts nothing");
      const prompt = runtime2.consentPrompts.get(GUILD);
      assert.ok(prompt?.nonce, "a consent prompt is live for anyone still pending");
      assert.equal(prompt.voiceChannelId, VOICE);
      const remembered = JSON.parse(readFileSync(ledgerPath, "utf8"));
      assert.equal(
        remembered.sessions[GUILD].consentPromptNonce,
        prompt.nonce,
        "the live prompt is remembered for the next restart",
      );
    } finally {
      await runtime2.shutdown("test_complete");
    }
  });
});

test("resume never carries consent: without a remembered grant the bot waits", async () => {
  await withLedgerDir(async (ledgerPath) => {
    // The session is remembered, but the consent store comes back empty -
    // exactly what the consent ledger does after an unclean exit, when it
    // reloads declines only. The bot may come back; capture may not.
    const remembered = new SessionLedger({ path: ledgerPath });
    const manager1 = new SessionManager({
      captureHealthy: true,
      consentLedger: new MemoryConsent(),
      sessionLedger: remembered,
    });
    manager1.startSession({
      guildId: GUILD,
      voiceChannelId: VOICE,
      controlChannelId: CONTROL,
      startedBy: ALICE,
      participantIds: [ALICE, BOB],
    });

    const after = emitterEvents();
    const sends = [];
    const connections = [];
    const manager2 = new SessionManager({
      captureHealthy: true,
      consentLedger: new MemoryConsent(),
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    const runtime2 = buildRuntime({
      sessionManager: manager2,
      guild: buildGuild({ memberIds: [ALICE, BOB], sends }),
      connections,
      emitter: after.emitter,
    });
    await runtime2.start();
    try {
      const started = after.events().filter((event) => event.type === "session.started");
      assert.equal(started.length, 1);
      assert.equal(started[0].payload.resumed, true);
      assert.equal(
        manager2.getSession(GUILD)?.phase,
        "awaiting_consent",
        "no remembered grant means nobody is captioned until they consent again",
      );
      assert.equal(connections.length, 0, "no consent, no voice connection");
      assert.equal(sends.length, 0, "nobody is told: reconnects stay silent");
      for (const participant of manager2.getSession(GUILD).participants) {
        assert.equal(participant.consent, "pending");
      }
    } finally {
      await runtime2.shutdown("test_complete");
    }
  });
});

test("the consent prompt minted before a restart is the one live after it", async () => {
  // A restart posts no replacement prompt, so the buttons on the original
  // /vem start message have to keep answering. The ledger carries the
  // nonce across; a ledger written before nonces were kept gets a fresh
  // one (the test above), and /vem consent works either way.
  await withLedgerDir(async (ledgerPath) => {
    const consent = new MemoryConsent();
    const manager1 = new SessionManager({
      captureHealthy: true,
      consentLedger: consent,
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    manager1.startSession({
      guildId: GUILD,
      voiceChannelId: VOICE,
      controlChannelId: CONTROL,
      startedBy: ALICE,
      participantIds: [ALICE, BOB],
    });
    const nonce = "0f9d4c1e-6b7a-4e0d-9c3b-2a1f5e8d7c6b";
    assert.equal(manager1.rememberConsentPrompt(GUILD, nonce), true);
    assert.equal(
      JSON.parse(readFileSync(ledgerPath, "utf8")).sessions[GUILD].consentPromptNonce,
      nonce,
    );

    const sends = [];
    const manager2 = new SessionManager({
      captureHealthy: true,
      consentLedger: consent,
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    const runtime2 = buildRuntime({
      sessionManager: manager2,
      guild: buildGuild({ memberIds: [ALICE, BOB], sends }),
      connections: [],
      emitter: emitterEvents().emitter,
    });
    await runtime2.start();
    try {
      assert.equal(sends.length, 0);
      assert.equal(
        runtime2.consentPrompts.get(GUILD)?.nonce,
        nonce,
        "the buttons on the original /vem start message still answer",
      );
      assert.equal(manager2.getSession(GUILD)?.consentPromptNonce, nonce);
    } finally {
      await runtime2.shutdown("test_complete");
    }
  });
});

test("a malformed remembered nonce reads as no prompt", () => {
  const ledger = new SessionLedger();
  assert.equal(
    ledger.remember({
      guildId: GUILD,
      voiceChannelId: VOICE,
      controlChannelId: CONTROL,
      startedBy: ALICE,
      participants: [{ userId: ALICE }],
      consentPromptNonce: "not-a-uuid",
    }),
    true,
  );
  assert.equal(ledger.recallAll()[0].consentPromptNonce, null);
});

test("a session remembered hours ago belongs to a raid that ended", async () => {
  await withLedgerDir(async (ledgerPath) => {
    const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000);
    const stale = new SessionLedger({ path: ledgerPath, now: () => twoHoursAgo });
    stale.remember({
      guildId: GUILD,
      voiceChannelId: VOICE,
      controlChannelId: CONTROL,
      startedBy: ALICE,
      participants: [{ userId: ALICE }],
    });
    assert.equal(stale.recallAll().length, 1);

    const after = emitterEvents();
    const connections = [];
    const manager2 = new SessionManager({
      captureHealthy: true,
      consentLedger: new MemoryConsent(),
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    const runtime2 = buildRuntime({
      sessionManager: manager2,
      guild: buildGuild(),
      connections,
      emitter: after.emitter,
    });
    await runtime2.start();
    try {
      assert.equal(
        after.events().some((event) => event.type === "session.started"),
        false,
        "a stale session must not be resurrected",
      );
      assert.equal(connections.length, 0);
      assert.equal(
        new SessionLedger({ path: ledgerPath }).recallAll().length,
        0,
        "the stale entry is forgotten so it cannot resurface on the next restart",
      );
    } finally {
      await runtime2.shutdown("test_complete");
    }
  });
});

test("an empty voice channel is not resumed", async () => {
  await withLedgerDir(async (ledgerPath) => {
    new SessionLedger({ path: ledgerPath }).remember({
      guildId: GUILD,
      voiceChannelId: VOICE,
      controlChannelId: CONTROL,
      startedBy: ALICE,
      participants: [{ userId: ALICE }],
    });

    const after = emitterEvents();
    const connections = [];
    const sends = [];
    const manager2 = new SessionManager({
      captureHealthy: true,
      consentLedger: new MemoryConsent(),
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    const runtime2 = buildRuntime({
      sessionManager: manager2,
      guild: buildGuild({ memberIds: [], sends }),
      connections,
      emitter: after.emitter,
    });
    await runtime2.start();
    try {
      assert.equal(after.events().some((event) => event.type === "session.started"), false);
      assert.equal(connections.length, 0);
      assert.equal(sends.length, 0, "nobody is there to tell");
      assert.equal(new SessionLedger({ path: ledgerPath }).recallAll().length, 0);
    } finally {
      await runtime2.shutdown("test_complete");
    }
  });
});

test("/vem stop forgets the session so it does not come back after a restart", async () => {
  await withLedgerDir(async (ledgerPath) => {
    const manager = new SessionManager({
      captureHealthy: true,
      consentLedger: new MemoryConsent(),
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    manager.startSession({
      guildId: GUILD,
      voiceChannelId: VOICE,
      controlChannelId: CONTROL,
      startedBy: ALICE,
      participantIds: [ALICE],
    });
    assert.equal(new SessionLedger({ path: ledgerPath }).recallAll().length, 1);
    manager.stopSession({ guildId: GUILD, requestedBy: ALICE });
    assert.equal(
      new SessionLedger({ path: ledgerPath }).recallAll().length,
      0,
      "a deliberate stop is final across restarts",
    );
  });
});

test("participant changes are remembered so a restart sees the current room", async () => {
  await withLedgerDir(async (ledgerPath) => {
    const manager = new SessionManager({
      captureHealthy: true,
      consentLedger: new MemoryConsent(),
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    manager.startSession({
      guildId: GUILD,
      voiceChannelId: VOICE,
      controlChannelId: CONTROL,
      startedBy: ALICE,
      participantIds: [ALICE],
    });
    manager.updateParticipants({ guildId: GUILD, participantIds: [ALICE, BOB] });
    const [entry] = new SessionLedger({ path: ledgerPath }).recallAll();
    assert.deepEqual(entry.participantIds.sort(), [ALICE, BOB].sort());
  });
});

test("restart does not resurrect a departed starter as a phantom participant", async () => {
  await withLedgerDir(async (ledgerPath) => {
    const consent = new MemoryConsent();
    const manager1 = new SessionManager({
      captureHealthy: true,
      consentLedger: consent,
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    manager1.startSession({
      guildId: GUILD,
      voiceChannelId: VOICE,
      controlChannelId: CONTROL,
      startedBy: ALICE,
      participantIds: [ALICE, BOB],
    });
    manager1.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
    manager1.setConsent({ guildId: GUILD, userId: BOB, granted: true });
    manager1.updateParticipants({ guildId: GUILD, participantIds: [BOB] });

    const sends = [];
    const connections = [];
    const guild = buildGuild({ memberIds: [BOB], sends });
    const manager2 = new SessionManager({
      captureHealthy: true,
      consentLedger: consent,
      sessionLedger: new SessionLedger({ path: ledgerPath }),
    });
    const runtime2 = buildRuntime({
      sessionManager: manager2,
      guild,
      connections,
      emitter: emitterEvents().emitter,
    });
    await runtime2.start();
    try {
      assert.deepEqual(
        manager2.getSession(GUILD).participants.map(({ userId }) => userId),
        [BOB],
        "the old starter is stop authority, not current voice membership",
      );
      assert.equal(manager2.getSession(GUILD).phase, "active");
      assert.equal(connections.length, 1);

      guild.channels.cache.get(VOICE).members.delete(BOB);
      const bob = { id: BOB, user: { id: BOB, bot: false } };
      runtime2.client.emit(
        Events.VoiceStateUpdate,
        { id: BOB, member: bob, guild, channelId: VOICE },
        { id: BOB, member: bob, guild, channelId: null },
      );
      await new Promise((resolve) => setTimeout(resolve, 50));

      const empty = manager2.getSession(GUILD);
      assert.deepEqual(empty.participants, []);
      assert.equal(empty.phase, "paused");
      assert.equal(empty.pauseReason, "voice_channel_empty");
      assert.equal(runtime2.connections.has(GUILD), false);
    } finally {
      await runtime2.shutdown("test_complete");
    }
  });
});

test("the session ledger is a convenience record: a bad file reads as empty", async () => {
  await withLedgerDir(async (ledgerPath) => {
    writeFileSync(ledgerPath, "{ this is not json", "utf8");
    const ledger = new SessionLedger({ path: ledgerPath });
    assert.deepEqual(ledger.recallAll(), []);
    // And it heals on the next write instead of refusing.
    assert.equal(
      ledger.remember({
        guildId: GUILD,
        voiceChannelId: VOICE,
        controlChannelId: CONTROL,
        startedBy: ALICE,
        participants: [{ userId: ALICE }],
      }),
      true,
    );
    assert.equal(JSON.parse(readFileSync(ledgerPath, "utf8")).version, 1);

    // Entries that are not Discord snowflakes are dropped on load.
    writeFileSync(
      ledgerPath,
      JSON.stringify({
        version: 1,
        sessions: {
          [GUILD]: {
            voiceChannelId: VOICE,
            controlChannelId: CONTROL,
            startedBy: ALICE,
            participantIds: [ALICE],
            savedAt: new Date().toISOString(),
          },
          "not-a-guild": {
            voiceChannelId: VOICE,
            controlChannelId: CONTROL,
            startedBy: ALICE,
            participantIds: [ALICE],
            savedAt: new Date().toISOString(),
          },
          "223456789012345678": { voiceChannelId: "DROP TABLE", participantIds: [] },
        },
      }),
      "utf8",
    );
    const reloaded = new SessionLedger({ path: ledgerPath }).recallAll();
    assert.deepEqual(reloaded.map((entry) => entry.guildId), [GUILD]);
  });
});

test("a memory-only session ledger never touches disk", () => {
  const ledger = new SessionLedger({ path: null });
  assert.equal(
    ledger.remember({
      guildId: GUILD,
      voiceChannelId: VOICE,
      controlChannelId: CONTROL,
      startedBy: ALICE,
      participants: [{ userId: ALICE }],
    }),
    true,
  );
  assert.equal(ledger.recallAll().length, 1);
  assert.equal(ledger.forget(GUILD), true);
  assert.deepEqual(ledger.recallAll(), []);
});
