import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { Events, MessageFlags } from "discord.js";

import { captionsCommandJson } from "../src/command_schema.js";
import { DiscordAdapterRuntime } from "../src/discord_runtime.js";
import { SessionManager } from "../src/session_manager.js";

const GUILD = "123456789012345678";
const VOICE = "323456789012345678";
const CONTROL = "523456789012345678";
const ALICE = "623456789012345678";
const BOB = "723456789012345678";

class FakeClient extends EventEmitter {
  constructor() {
    super();
    this.guilds = { cache: new Map() };
  }

  destroy() {}
}

function createRuntime() {
  const client = new FakeClient();
  const sessions = new SessionManager({ captureHealthy: true });
  const events = [];
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "823456789012345678",
      guildId: null,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter: { emit: (type, payload) => events.push({ type, payload }) },
    audioSink: { isReady: () => true, acceptOpusStream: async () => {} },
    client,
    sessionManager: sessions,
    connectionFactory: () => {
      throw new Error("tests pin an existing connection before enabling capture");
    },
  });
  return { client, events, runtime, sessions };
}

function guildContext({ userId = ALICE, voiceMemberIds = [userId] } = {}) {
  const members = new Map(
    voiceMemberIds.map((id) => [id, { id, user: { id, bot: false } }]),
  );
  const voiceChannel = { id: VOICE, members };
  return {
    id: GUILD,
    channels: {
      cache: new Map([
        [CONTROL, { isTextBased: () => true, send: async () => {} }],
      ]),
    },
    members: {
      cache: new Map(
        voiceMemberIds.map((id) => [id, { id, voice: { channel: voiceChannel } }]),
      ),
    },
  };
}

/**
 * Records EVERY response, not just the first.
 *
 * The shared harness in consent_buttons.test.js resolves a one-shot
 * promise on the first reply, which is exactly the thing under test here:
 * consent now answers with a public reply AND a private follow-up, and a
 * recorder that stops at the reply cannot tell whether the second one
 * happened at all.
 */
function recordingInteraction({
  subcommand,
  userId = ALICE,
  voiceMemberIds = [userId],
  followUpFails = false,
} = {}) {
  const calls = [];
  const interaction = {
    replied: false,
    deferred: false,
    commandName: captionsCommandJson.name,
    guildId: GUILD,
    guild: guildContext({ userId, voiceMemberIds }),
    channelId: CONTROL,
    user: { id: userId },
    options: { getSubcommand: () => subcommand, getString: () => null },
    isChatInputCommand: () => true,
    isButton: () => false,
    inGuild: () => true,
    reply: async (payload) => {
      interaction.replied = true;
      calls.push({ method: "reply", payload });
    },
    update: async (payload) => {
      interaction.replied = true;
      calls.push({ method: "update", payload });
    },
    followUp: async (payload) => {
      calls.push({ method: "followUp", payload });
      if (followUpFails) {
        throw new Error("Discord rejected the follow-up");
      }
    },
  };
  return { interaction, calls };
}

/** Let the handler's promise chain finish. Nothing here uses timers. */
async function settle() {
  for (let index = 0; index < 50; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

async function dispatch(client, tracked) {
  client.emit(Events.InteractionCreate, tracked.interaction);
  await settle();
  return tracked.calls;
}

async function startedSession(client, voiceMemberIds = [ALICE]) {
  await dispatch(client, recordingInteraction({ subcommand: "start", voiceMemberIds }));
}

function followUps(calls) {
  return calls.filter((call) => call.method === "followUp");
}

function rowsOf(payload) {
  return (payload.components || []).map((row) => row.toJSON().components[0]);
}

test("granting consent offers one Choose your class button", async () => {
  const { client } = createRuntime();
  await startedSession(client);

  const calls = await dispatch(client, recordingInteraction({ subcommand: "consent" }));
  const extra = followUps(calls);

  assert.equal(extra.length, 1, "consent should offer the panel exactly once");
  const components = rowsOf(extra[0].payload);
  assert.equal(components.length, 1);
  assert.match(components[0].custom_id, /^vears:map:[0-9a-f-]+$/);
  assert.equal(components[0].label, "Choose your class");
  assert.equal(components[0].type, 2);
});

test("the panel is private and the grant notice stays public", async () => {
  const { client } = createRuntime();
  await startedSession(client);

  const calls = await dispatch(client, recordingInteraction({ subcommand: "consent" }));
  const reply = calls.find((call) => call.method === "reply");
  const panel = followUps(calls)[0];

  // The channel needs to see who consented; only Alice needs the panel.
  assert.ok(reply, "the public grant notice must still be the reply");
  assert.notEqual(reply.payload.flags, MessageFlags.Ephemeral);
  assert.equal(panel.payload.flags, MessageFlags.Ephemeral);
  assert.ok(!reply.payload.components, "the notice must not carry the panel");
});

test("somebody already mapped is not asked again", async () => {
  const { client, sessions } = createRuntime();
  await startedSession(client);
  sessions.mapCharacter({
    guildId: GUILD,
    userId: ALICE,
    characterName: "Vemal",
    wowClass: "priest",
    role: "healer",
  });

  const calls = await dispatch(client, recordingInteraction({ subcommand: "consent" }));

  assert.equal(
    followUps(calls).length,
    0,
    "re-asking a mapped player teaches them to dismiss the panel unread",
  );
});

test("unmapping makes the next consent offer the panel again", async () => {
  const { client, sessions } = createRuntime();
  await startedSession(client);
  sessions.mapCharacter({
    guildId: GUILD,
    userId: ALICE,
    characterName: "Vemal",
    wowClass: "priest",
    role: "healer",
  });
  sessions.clearCharacter({ guildId: GUILD, userId: ALICE });

  const calls = await dispatch(client, recordingInteraction({ subcommand: "consent" }));

  // The skip must key on what is stored right now, not on having been
  // mapped at some point. Somebody who deliberately cleared their
  // character is precisely somebody who wants to set a new one.
  assert.equal(followUps(calls).length, 1);
});

test("declining does not offer the panel", async () => {
  const { client } = createRuntime();
  await startedSession(client);

  const calls = await dispatch(client, recordingInteraction({ subcommand: "decline" }));

  assert.equal(
    followUps(calls).length,
    0,
    "somebody who just refused capture is not in the mood to be onboarded",
  );
});

test("a panel that fails to render never revokes the consent", async () => {
  const { client, events, sessions } = createRuntime();
  await startedSession(client);

  const calls = await dispatch(
    client,
    recordingInteraction({ subcommand: "consent", followUpFails: true }),
  );

  // This is the load-bearing case. "consent.granted" is a non-ephemeral
  // result, and the outer catch in the runtime calls #failClosed for
  // those - so an unguarded follow-up would stop the capture the person
  // had just agreed to, because a cosmetic panel failed to render.
  assert.equal(followUps(calls).length, 1, "it should have tried");
  const session = sessions.getSession(GUILD);
  assert.ok(session, "the session must survive a failed panel");
  const alice = session.participants.find(({ userId }) => userId === ALICE);
  assert.equal(alice.consent, "granted", "consent must stand");
  assert.ok(
    events.some((event) => event.type === "mapping.panel.failed"),
    "the failure must be reported, not swallowed silently",
  );
  assert.ok(
    !events.some((event) => event.type === "command.rejected"),
    "a cosmetic panel failure must not surface as a rejected command",
  );
});
