import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { ButtonStyle, Events, MessageFlags } from "discord.js";

import { captionsCommandJson } from "../src/command_schema.js";
import { DiscordAdapterRuntime } from "../src/discord_runtime.js";
import { SessionManager } from "../src/session_manager.js";

const GUILD = "123456789012345678";
const FOREIGN_GUILD = "223456789012345678";
const VOICE = "323456789012345678";
const OTHER_VOICE = "423456789012345678";
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

function memoryLedger(initial = []) {
  const entries = new Map(initial);
  const remembers = [];
  const forgets = [];
  return {
    entries,
    remembers,
    forgets,
    ledger: {
      recall: (guildId, userId) => entries.get(`${guildId}:${userId}`) ?? null,
      remember: (guildId, userId, state) => {
        remembers.push({ guildId, userId, state });
        entries.set(`${guildId}:${userId}`, state);
        return true;
      },
      forget: (guildId, userId) => {
        forgets.push({ guildId, userId });
        const existed = entries.delete(`${guildId}:${userId}`);
        return { existed, persisted: true };
      },
    },
  };
}

function createRuntime({
  configuredGuild = null,
  consentLedger = null,
  mappingLedger = null,
  joinBaseUrl = "",
} = {}) {
  const client = new FakeClient();
  const sessions = new SessionManager({ captureHealthy: true, consentLedger, mappingLedger });
  const events = [];
  const runtime = new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "823456789012345678",
      guildId: configuredGuild,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
      joinBaseUrl,
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

function guildContext({
  guildId = GUILD,
  userId = ALICE,
  voiceChannelId = VOICE,
  voiceMemberIds = [userId],
  noticeSink = null,
} = {}) {
  const members = new Map(
    voiceMemberIds.map((id) => [id, { id, user: { id, bot: false } }]),
  );
  const voiceChannel = voiceChannelId ? { id: voiceChannelId, members } : null;
  return {
    id: guildId,
    channels: {
      cache: new Map([
        [
          CONTROL,
          {
            isTextBased: () => true,
            send: async (payload) => {
              if (typeof noticeSink === "function") {
                await noticeSink(payload);
              } else {
                noticeSink?.push(payload);
              }
            },
          },
        ],
      ]),
    },
    members: {
      cache: new Map([[userId, { id: userId, voice: { channel: voiceChannel } }]]),
    },
  };
}

function trackedInteraction(base) {
  const replyFails = base.replyFails === true;
  const showModalFails = base.showModalFails === true;
  delete base.replyFails;
  delete base.showModalFails;
  let finish;
  const response = new Promise((resolve) => {
    finish = resolve;
  });
  const calls = [];
  const interaction = {
    replied: false,
    deferred: false,
    ...base,
    reply: async (payload) => {
      interaction.replied = true;
      calls.push({ method: "reply", payload });
      finish({ method: "reply", payload, failed: replyFails });
      if (replyFails) {
        throw new Error("private response unavailable");
      }
    },
    update: async (payload) => {
      interaction.replied = true;
      calls.push({ method: "update", payload });
      finish({ method: "update", payload });
    },
    followUp: async (payload) => {
      calls.push({ method: "followUp", payload });
      finish({ method: "followUp", payload });
    },
    showModal: async (payload) => {
      calls.push({ method: "showModal", payload });
      if (showModalFails) {
        throw new Error("class modal unavailable");
      }
      interaction.replied = true;
      finish({ method: "showModal", payload });
    },
  };
  return { interaction, response, calls };
}

function slashInteraction({
  subcommand,
  guildId = GUILD,
  userId = ALICE,
  voiceChannelId = VOICE,
  voiceMemberIds = [userId],
  noticeSink = null,
  stringOptions = {},
} = {}) {
  return trackedInteraction({
    commandName: captionsCommandJson.name,
    guildId,
    guild: guildContext({ guildId, userId, voiceChannelId, voiceMemberIds, noticeSink }),
    channelId: CONTROL,
    user: { id: userId },
    options: {
      getSubcommand: () => subcommand,
      getString: (name, required = false) => {
        if (required && !Object.hasOwn(stringOptions, name)) {
          throw new Error(`${name} was requested as a required option`);
        }
        return stringOptions[name] ?? null;
      },
    },
    isChatInputCommand: () => true,
    isButton: () => false,
    isModalSubmit: () => false,
    inGuild: () => true,
  });
}

function buttonInteraction({
  customId,
  guildId = GUILD,
  userId = ALICE,
  voiceChannelId = VOICE,
  voiceMemberIds = [userId],
  noticeSink = null,
  extra = {},
} = {}) {
  return trackedInteraction({
    customId,
    guildId,
    guild: guildContext({ guildId, userId, voiceChannelId, voiceMemberIds, noticeSink }),
    channelId: CONTROL,
    user: { id: userId },
    isChatInputCommand: () => false,
    isButton: () => true,
    isModalSubmit: () => false,
    inGuild: () => true,
    ...extra,
  });
}

function modalInteraction({
  customId,
  answer,
  remember = false,
  mapping = null,
  clearMapping = false,
  guildId = GUILD,
  userId = ALICE,
  voiceChannelId = VOICE,
  voiceMemberIds = [userId],
  noticeSink = null,
  replyFails = false,
} = {}) {
  return trackedInteraction({
    customId,
    guildId,
    guild: guildContext({ guildId, userId, voiceChannelId, voiceMemberIds, noticeSink }),
    channelId: CONTROL,
    user: { id: userId },
    fields: {
      getRadioGroup: () => answer,
      getCheckbox: (customId) =>
        customId === "clear-mapping" ? clearMapping : remember,
      getStringSelectValues: (customId) => {
        const value = customId === "wowClass" ? mapping?.wowClass : mapping?.role;
        if (!value) {
          throw new Error(`${customId} is not present in this modal`);
        }
        return [value];
      },
      getTextInputValue: (customId) => {
        if (!mapping) {
          throw new Error(`${customId} is not present in this modal`);
        }
        return customId === "characterName"
          ? mapping.characterName
          : mapping.realm || "";
      },
    },
    isChatInputCommand: () => false,
    isButton: () => false,
    isModalSubmit: () => true,
    inGuild: () => true,
    replyFails,
  });
}

async function dispatch(client, tracked) {
  client.emit(Events.InteractionCreate, tracked.interaction);
  return tracked.response;
}

async function settle() {
  for (let index = 0; index < 20; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function componentJson(response) {
  return response.payload.components[0].toJSON().components;
}

function modalJson(response) {
  assert.equal(response.method, "showModal");
  return response.payload.toJSON();
}

function modalControl(response, customId) {
  return modalJson(response).components
    .map((label) => label.component)
    .find((component) => component.custom_id === customId);
}

async function startSession(client, options = {}) {
  const response = await dispatch(
    client,
    slashInteraction({ subcommand: "start", ...options }),
  );
  const buttons = componentJson(response);
  return {
    response,
    manageId: buttons.find(({ label }) => label === "Manage my consent").custom_id,
    mapId: buttons.find(({ label }) => label === "Choose your class").custom_id,
  };
}

async function openConsentModal(client, manageId, options = {}) {
  return dispatch(client, buttonInteraction({ customId: manageId, ...options }));
}

async function submitConsent(client, shownModal, options = {}) {
  return dispatch(
    client,
    modalInteraction({ customId: modalJson(shownModal).custom_id, ...options }),
  );
}

test("the shared consent button opens a private native modal with Remember off", async () => {
  const { client } = createRuntime();
  const { response, manageId } = await startSession(client, {
    voiceMemberIds: [ALICE, BOB],
  });

  const startButtons = componentJson(response);
  assert.equal(startButtons.length, 2);
  assert.equal(startButtons[0].label, "Manage my consent");
  // Primary: the one control that turns a person's captions on should
  // not be the dimmest thing in the message. Asserted by name rather
  // than "not Secondary" so a future restyle has to be deliberate.
  assert.equal(startButtons[0].style, ButtonStyle.Primary);
  assert.notEqual(
    startButtons[0].style,
    ButtonStyle.Success,
    "green already means 'captions are on' on the toggle one row deeper",
  );
  assert.equal(startButtons[1].label, "Choose your class");
  assert.equal(startButtons[1].style, ButtonStyle.Primary);
  assert.match(manageId, /^vears:manage:[0-9a-f-]+$/);
  assert.doesNotMatch(manageId, new RegExp(ALICE));

  const shown = await openConsentModal(client, manageId, {
    userId: BOB,
    voiceMemberIds: [ALICE, BOB],
  });
  const modal = modalJson(shown);
  assert.equal(modal.title, "Manage caption consent");
  assert.match(modal.custom_id, /^vears:consent-modal:[0-9a-f-]+$/);
  const answers = modalControl(shown, "consent-answer");
  assert.equal(answers.required, true);
  assert.deepEqual(answers.options.map(({ value }) => value), ["granted", "declined"]);
  assert.equal(answers.options.some((option) => option.default === true), false);
  const remember = modalControl(shown, "remember-answer");
  // A first answer is remembered unless the person unticks it: Shino,
  // 2026-09-01, "Everyone has to reconsent when the discord bot disconnects."
  assert.equal(remember.default, true);
});

test("the active session offers a native Choose your class modal", async () => {
  const { client } = createRuntime();
  const { mapId } = await startSession(client, { voiceMemberIds: [ALICE, BOB] });

  assert.match(mapId, /^vears:map:[0-9a-f-]+$/);
  assert.doesNotMatch(mapId, new RegExp(ALICE));
  const shown = await dispatch(
    client,
    buttonInteraction({ customId: mapId, userId: BOB, voiceMemberIds: [ALICE, BOB] }),
  );
  const modal = modalJson(shown);

  assert.equal(modal.title, "Choose your class");
  assert.match(modal.custom_id, /^vears:mapmodal:[0-9a-f-]+$/);
  const classLabel = modal.components.find(
    ({ component }) => component.custom_id === "wowClass",
  );
  assert.match(classLabel.description, /remembered until \/vem unmap/i);
  const wowClass = modalControl(shown, "wowClass");
  assert.equal(wowClass.required, true);
  assert.equal(wowClass.min_values, 1);
  assert.equal(wowClass.max_values, 1);
  assert.deepEqual(
    wowClass.options.map(({ value }) => value),
    [
      "death-knight",
      "demon-hunter",
      "druid",
      "evoker",
      "hunter",
      "mage",
      "monk",
      "paladin",
      "priest",
      "rogue",
      "shaman",
      "warlock",
      "warrior",
    ],
  );
  const role = modalControl(shown, "role");
  assert.equal(role.required, true);
  assert.deepEqual(role.options.map(({ value }) => value), ["tank", "healer", "damage"]);
  assert.equal(modalControl(shown, "characterName").required, true);
  assert.equal(modalControl(shown, "realm").required, false);
  assert.equal(modalControl(shown, "clear-mapping").default, false);
});

test("a class modal failure returns private recovery instructions and telemetry", async () => {
  const { client, events } = createRuntime();
  const { mapId } = await startSession(client);

  const failed = await dispatch(
    client,
    buttonInteraction({
      customId: mapId,
      extra: { showModalFails: true },
    }),
  );

  assert.equal(failed.method, "reply");
  assert.equal(failed.payload.flags, MessageFlags.Ephemeral);
  assert.match(failed.payload.content, /could not open the class picker/i);
  assert.match(failed.payload.content, /\/vem map/);
  assert.ok(
    events.some(
      ({ type, payload }) =>
        type === "mapping.panel.failed" &&
        payload.userId === ALICE &&
        payload.action === "map",
    ),
  );
});

test("submitting the class modal saves only the authenticated user and replies privately", async () => {
  const remembers = [];
  const mappingLedger = {
    recallAll: () => [],
    remember: (guildId, userId, mapping) => {
      remembers.push({ guildId, userId, mapping });
      return true;
    },
    forget: () => false,
  };
  const { client, events, sessions } = createRuntime({ mappingLedger });
  const { mapId } = await startSession(client, { voiceMemberIds: [ALICE, BOB] });
  const shown = await dispatch(
    client,
    buttonInteraction({ customId: mapId, userId: BOB, voiceMemberIds: [ALICE, BOB] }),
  );
  const submission = modalInteraction({
    customId: modalJson(shown).custom_id,
    userId: BOB,
    voiceMemberIds: [ALICE, BOB],
    mapping: {
      characterName: "Drex",
      realm: "Area 52",
      wowClass: "death-knight",
      role: "tank",
    },
  });

  const saved = await dispatch(client, submission);

  assert.equal(saved.method, "reply");
  assert.equal(saved.payload.flags, MessageFlags.Ephemeral);
  assert.equal(submission.calls.some(({ method }) => method === "update"), false);
  assert.match(saved.payload.content, /Saved/);
  assert.match(saved.payload.content, /Class: Death Knight/);
  assert.deepEqual(sessions.getCharacter({ guildId: GUILD, userId: BOB }), {
    characterName: "Drex",
    realm: "Area 52",
    wowClass: "death-knight",
    role: "tank",
  });
  assert.equal(sessions.getCharacter({ guildId: GUILD, userId: ALICE }), null);
  assert.equal(remembers.length, 1);
  assert.equal(remembers[0].userId, BOB);
  assert.ok(events.some(({ type, payload }) => type === "mapping.changed" && payload.userId === BOB));
});

test("a failed mapping write is reported as current-bot-only", async () => {
  const mappingLedger = {
    recallAll: () => [],
    remember: () => false,
    forget: () => ({ existed: false, persisted: true }),
  };
  const { client, sessions } = createRuntime({ mappingLedger });
  const { mapId } = await startSession(client);
  const shown = await dispatch(
    client,
    buttonInteraction({ customId: mapId }),
  );

  const result = await dispatch(
    client,
    modalInteraction({
      customId: modalJson(shown).custom_id,
      mapping: {
        characterName: "Drex",
        realm: "",
        wowClass: "death-knight",
        role: "tank",
      },
    }),
  );

  assert.equal(result.payload.flags, MessageFlags.Ephemeral);
  assert.match(result.payload.content, /active now/i);
  assert.match(result.payload.content, /could not save.*next bot restart/i);
  assert.doesNotMatch(result.payload.content, /Saved\./);
  assert.equal(
    sessions.getCharacter({ guildId: GUILD, userId: ALICE }).characterName,
    "Drex",
  );
});

test("the class modal lets the authenticated user clear their mapping", async () => {
  const { client, events, sessions } = createRuntime();
  const { mapId } = await startSession(client);
  await dispatch(
    client,
    slashInteraction({
      subcommand: "map",
      stringOptions: {
        character: "Drex",
        class: "death-knight",
        role: "tank",
      },
    }),
  );
  const shown = await dispatch(
    client,
    buttonInteraction({ customId: mapId }),
  );

  const cleared = await dispatch(
    client,
    modalInteraction({
      customId: modalJson(shown).custom_id,
      clearMapping: true,
    }),
  );

  assert.equal(cleared.method, "reply");
  assert.equal(cleared.payload.flags, MessageFlags.Ephemeral);
  assert.match(cleared.payload.content, /mapping was cleared/i);
  assert.equal(sessions.getCharacter({ guildId: GUILD, userId: ALICE }), null);
  assert.ok(
    events.some(
      ({ type, payload }) =>
        type === "mapping.changed" &&
        payload.userId === ALICE &&
        payload.mapping === null,
    ),
  );
});

test("a failed mapping erase never claims the remembered copy was cleared", async () => {
  const remembered = {
    characterName: "Drex",
    realm: null,
    wowClass: "death-knight",
    role: "tank",
  };
  const mappingLedger = {
    recallAll: () => [{ guildId: GUILD, userId: ALICE, mapping: remembered }],
    remember: () => true,
    forget: () => ({ existed: true, persisted: false }),
  };
  const { client, sessions } = createRuntime({ mappingLedger });
  const { mapId } = await startSession(client);
  const shown = await dispatch(
    client,
    buttonInteraction({ customId: mapId }),
  );

  const result = await dispatch(
    client,
    modalInteraction({
      customId: modalJson(shown).custom_id,
      clearMapping: true,
    }),
  );

  assert.equal(result.payload.flags, MessageFlags.Ephemeral);
  assert.match(result.payload.content, /cleared for this bot session/i);
  assert.match(result.payload.content, /could not erase the remembered copy/i);
  assert.doesNotMatch(result.payload.content, /^Your character mapping was cleared\.$/);
  assert.equal(sessions.getCharacter({ guildId: GUILD, userId: ALICE }), null);
});

test("/vem map with no options opens the same native modal", async () => {
  const { client } = createRuntime();

  const shown = await dispatch(client, slashInteraction({ subcommand: "map" }));

  assert.equal(modalJson(shown).title, "Choose your class");
  assert.equal(modalControl(shown, "wowClass").options.some(({ default: chosen }) => chosen), false);
});

test("/vem map accepts partial optional values and pre-fills the modal", async () => {
  const { client } = createRuntime();

  const shown = await dispatch(
    client,
    slashInteraction({
      subcommand: "map",
      stringOptions: { character: "Vemal", class: "priest" },
    }),
  );

  assert.equal(modalJson(shown).title, "Choose your class");
  assert.equal(modalControl(shown, "characterName").value, "Vemal");
  const classes = modalControl(shown, "wowClass").options;
  assert.equal(classes.find(({ value }) => value === "priest").default, true);
  assert.equal(modalControl(shown, "role").options.some(({ default: chosen }) => chosen), false);
});

test("unmap and forget clear a cancelled modal draft", async () => {
  for (const eraseCommand of ["unmap", "forget"]) {
    const { client, sessions } = createRuntime();
    const { mapId } = await startSession(client);
    await dispatch(
      client,
      slashInteraction({
        subcommand: "map",
        stringOptions: {
          character: "Drex",
          class: "death-knight",
          role: "tank",
          realm: "Area 52",
        },
      }),
    );
    const beforeErase = await dispatch(
      client,
      buttonInteraction({ customId: mapId }),
    );
    assert.equal(modalControl(beforeErase, "characterName").value, "Drex");

    await dispatch(client, slashInteraction({ subcommand: eraseCommand }));
    const afterErase = await dispatch(
      client,
      buttonInteraction({ customId: mapId }),
    );

    assert.equal(
      sessions.getCharacter({ guildId: GUILD, userId: ALICE }),
      null,
      eraseCommand + " must erase the stored mapping",
    );
    assert.equal(modalControl(afterErase, "characterName").value, undefined);
    assert.equal(
      modalControl(afterErase, "wowClass").options.some(({ default: chosen }) => chosen),
      false,
      eraseCommand + " must not restore a cached class",
    );
  }
});

test("a configured runtime rejects class controls from another guild privately", async () => {
  const { client, sessions } = createRuntime({ configuredGuild: GUILD });
  const { mapId } = await startSession(client);

  const rejected = await dispatch(
    client,
    buttonInteraction({ customId: mapId, guildId: FOREIGN_GUILD }),
  );

  assert.equal(rejected.method, "reply");
  assert.equal(rejected.payload.flags, MessageFlags.Ephemeral);
  assert.match(rejected.payload.content, /not configured for this server/);
  assert.equal(sessions.getCharacter({ guildId: FOREIGN_GUILD, userId: ALICE }), null);
});

test("an unchecked answer applies only to Discord's authenticated clicking user", async () => {
  const state = memoryLedger();
  const { client, runtime, sessions } = createRuntime({ consentLedger: state.ledger });
  const notices = [];
  const { manageId } = await startSession(client, { voiceMemberIds: [ALICE, BOB] });
  const shown = await openConsentModal(client, manageId, {
    userId: BOB,
    voiceMemberIds: [ALICE, BOB],
  });
  runtime.connections.set(GUILD, {
    voiceChannelId: VOICE,
    connection: { destroy() {} },
  });

  const accepted = await submitConsent(client, shown, {
    answer: "granted",
    remember: false,
    userId: BOB,
    voiceMemberIds: [ALICE, BOB],
    noticeSink: notices,
  });

  assert.equal(accepted.method, "reply");
  assert.equal(accepted.payload.flags, MessageFlags.Ephemeral);
  assert.match(accepted.payload.content, /consent is ON/);
  assert.match(accepted.payload.content, /Remember my answer is OFF/);
  assert.equal(sessions.canCapture({ guildId: GUILD, userId: BOB }), true);
  assert.equal(sessions.canCapture({ guildId: GUILD, userId: ALICE }), false);
  assert.equal(state.entries.has(`${GUILD}:${BOB}`), false);
  assert.deepEqual(state.remembers, []);
  assert.deepEqual(state.forgets, [{ guildId: GUILD, userId: BOB }]);
  assert.equal(notices.length, 1);
  assert.match(notices[0].content, new RegExp(`<@${BOB}> consented`));
});

test("a checked answer is stored and shown as remembered next time", async () => {
  const state = memoryLedger();
  const { client, runtime } = createRuntime({ consentLedger: state.ledger });
  const { manageId } = await startSession(client);
  const shown = await openConsentModal(client, manageId);
  runtime.connections.set(GUILD, {
    voiceChannelId: VOICE,
    connection: { destroy() {} },
  });

  const accepted = await submitConsent(client, shown, {
    answer: "granted",
    remember: true,
  });
  assert.match(accepted.payload.content, /Remember my answer is ON/);
  assert.equal(state.entries.get(`${GUILD}:${ALICE}`), "granted");

  const reopened = await openConsentModal(client, manageId);
  assert.equal(modalControl(reopened, "remember-answer").default, true);
  const options = modalControl(reopened, "consent-answer").options;
  assert.equal(options.find(({ value }) => value === "granted").default, true);
});

test("granting in the modal still offers the optional character mapping panel", async () => {
  const state = memoryLedger();
  const { client, runtime } = createRuntime({ consentLedger: state.ledger });
  const { manageId } = await startSession(client);
  const shown = await openConsentModal(client, manageId);
  runtime.connections.set(GUILD, {
    voiceChannelId: VOICE,
    connection: { destroy() {} },
  });
  const submission = modalInteraction({
    customId: modalJson(shown).custom_id,
    answer: "granted",
    remember: false,
  });

  await dispatch(client, submission);
  await settle();

  const mapping = submission.calls.find(({ method }) => method === "followUp");
  assert.ok(mapping, "the consent modal must preserve character onboarding");
  assert.equal(mapping.payload.flags, MessageFlags.Ephemeral);
  assert.match(mapping.payload.content, /remembered until you run \/vem unmap/i);
  assert.equal(mapping.payload.components.length, 1);
  const button = mapping.payload.components[0].toJSON().components[0];
  assert.equal(button.label, "Choose your class");
  assert.match(button.custom_id, /^vears:map:[0-9a-f-]+$/);
});

test("unchecking Remember clears an old answer without clearing character mapping", async () => {
  const state = memoryLedger([[`${GUILD}:${ALICE}`, "granted"]]);
  const { client, sessions } = createRuntime({ consentLedger: state.ledger });
  sessions.mapCharacter({
    guildId: GUILD,
    userId: ALICE,
    characterName: "Vemal",
    wowClass: "priest",
    role: "healer",
  });
  const { manageId } = await startSession(client);
  const shown = await openConsentModal(client, manageId);
  assert.equal(modalControl(shown, "remember-answer").default, true);

  const declined = await submitConsent(client, shown, {
    answer: "declined",
    remember: false,
  });

  assert.match(declined.payload.content, /Remember my answer is OFF/);
  assert.equal(state.entries.has(`${GUILD}:${ALICE}`), false);
  assert.equal(sessions.getConsentStatus({ guildId: GUILD, userId: ALICE }).remembered, false);
  assert.equal(sessions.getCharacter({ guildId: GUILD, userId: ALICE }).characterName, "Vemal");
  assert.equal(sessions.canCapture({ guildId: GUILD, userId: ALICE }), false);
});

test("a configured runtime rejects consent controls from the wrong guild", async () => {
  const { client, sessions } = createRuntime({ configuredGuild: GUILD });
  const { manageId } = await startSession(client);

  const rejected = await dispatch(
    client,
    buttonInteraction({ customId: manageId, guildId: FOREIGN_GUILD }),
  );

  assert.equal(rejected.method, "reply");
  assert.equal(rejected.payload.flags, MessageFlags.Ephemeral);
  assert.match(rejected.payload.content, /not configured for this server/i);
  assert.equal(sessions.getSession(FOREIGN_GUILD), null);
});

test("stale modal submissions and grants from the wrong voice channel are rejected", async () => {
  const state = memoryLedger();
  const { client, sessions } = createRuntime({ consentLedger: state.ledger });
  const first = await startSession(client);
  const staleModal = await openConsentModal(client, first.manageId);
  await dispatch(client, slashInteraction({ subcommand: "stop" }));
  const second = await startSession(client, { voiceMemberIds: [ALICE, BOB] });

  const stale = await submitConsent(client, staleModal, {
    answer: "granted",
    remember: true,
  });
  assert.match(stale.payload.content, /no longer active/i);
  assert.deepEqual(state.remembers, []);

  const bobModal = await openConsentModal(client, second.manageId, {
    userId: BOB,
    voiceChannelId: OTHER_VOICE,
    voiceMemberIds: [BOB],
  });
  const wrongChannel = await submitConsent(client, bobModal, {
    answer: "granted",
    remember: true,
    userId: BOB,
    voiceChannelId: OTHER_VOICE,
    voiceMemberIds: [BOB],
  });
  assert.match(wrongChannel.payload.content, /join the session voice channel/i);
  assert.equal(sessions.canCapture({ guildId: GUILD, userId: BOB }), false);
  assert.equal(state.entries.has(`${GUILD}:${BOB}`), false);
});

test("a grant is rejected when no session exists and is never remembered", async () => {
  const state = memoryLedger();
  const { client } = createRuntime({ consentLedger: state.ledger });

  const rejected = await dispatch(client, slashInteraction({ subcommand: "consent" }));
  assert.equal(rejected.payload.flags, MessageFlags.Ephemeral);
  assert.match(rejected.payload.content, /no caption session is open/i);
  assert.deepEqual(state.remembers, []);
});

test("declining cuts only that speaker before the private confirmation", async () => {
  const state = memoryLedger();
  const { client, runtime, sessions } = createRuntime({ consentLedger: state.ledger });
  const notices = [];
  const { manageId } = await startSession(client, { voiceMemberIds: [ALICE, BOB] });
  const firstModal = await openConsentModal(client, manageId, {
    voiceMemberIds: [ALICE, BOB],
  });
  runtime.connections.set(GUILD, {
    voiceChannelId: VOICE,
    connection: { destroy() {} },
  });
  await submitConsent(client, firstModal, {
    answer: "granted",
    remember: false,
    voiceMemberIds: [ALICE, BOB],
  });
  sessions.setConsent({ guildId: GUILD, userId: BOB, granted: true, remember: false });

  function liveStream(userId) {
    const session = runtime.sessions.getSession(GUILD);
    const segment = runtime.speakers.begin({
      guildId: GUILD,
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
  }

  const aliceCapture = liveStream(ALICE);
  const bobCapture = liveStream(BOB);
  const declineModal = await openConsentModal(client, manageId, {
    voiceMemberIds: [ALICE, BOB],
  });
  const submission = modalInteraction({
    customId: modalJson(declineModal).custom_id,
    answer: "declined",
    remember: false,
    voiceMemberIds: [ALICE, BOB],
    noticeSink: notices,
  });
  let captureStateAtReply = null;
  const originalReply = submission.interaction.reply;
  submission.interaction.reply = async (payload) => {
    captureStateAtReply = {
      aliceDestroyed: aliceCapture.stream.destroyed,
      bobDestroyed: bobCapture.stream.destroyed,
    };
    await originalReply(payload);
  };

  const declined = await dispatch(client, submission);

  assert.deepEqual(captureStateAtReply, {
    aliceDestroyed: true,
    bobDestroyed: false,
  });
  assert.match(declined.payload.content, /consent is OFF/);
  assert.equal(sessions.canCapture({ guildId: GUILD, userId: ALICE }), false);
  assert.equal(sessions.canCapture({ guildId: GUILD, userId: BOB }), true);
  assert.equal(runtime.streams.has(aliceCapture.segment.segmentId), false);
  assert.equal(runtime.streams.has(bobCapture.segment.segmentId), true);
  assert.equal(notices.length, 1);
  assert.match(notices[0].content, new RegExp(`<@${ALICE}> declined or withdrew consent`));
});

test("a remembered grant can be changed to a remembered decline after leaving voice", async () => {
  const state = memoryLedger();
  const { client, runtime, sessions } = createRuntime({ consentLedger: state.ledger });
  const { manageId } = await startSession(client, { voiceMemberIds: [ALICE, BOB] });
  const firstModal = await openConsentModal(client, manageId, {
    voiceMemberIds: [ALICE, BOB],
  });
  runtime.connections.set(GUILD, {
    voiceChannelId: VOICE,
    connection: { destroy() {} },
  });
  await submitConsent(client, firstModal, {
    answer: "granted",
    remember: true,
    voiceMemberIds: [ALICE, BOB],
  });
  sessions.updateParticipants({ guildId: GUILD, participantIds: [BOB] });

  const awayModal = await openConsentModal(client, manageId, {
    voiceChannelId: OTHER_VOICE,
    voiceMemberIds: [ALICE],
  });
  assert.equal(modalControl(awayModal, "remember-answer").default, true);
  const awayOptions = modalControl(awayModal, "consent-answer").options;
  assert.equal(awayOptions.find(({ value }) => value === "granted").default, true);

  const declined = await submitConsent(client, awayModal, {
    answer: "declined",
    remember: true,
    voiceChannelId: OTHER_VOICE,
    voiceMemberIds: [ALICE],
  });
  assert.match(declined.payload.content, /consent is OFF/);
  assert.equal(state.entries.get(`${GUILD}:${ALICE}`), "declined");
});

test("a failed remembered write is reported as session-only", async () => {
  const entries = new Map();
  const consentLedger = {
    recall: (guildId, userId) => entries.get(`${guildId}:${userId}`) ?? null,
    remember: (guildId, userId, state) => {
      entries.set(`${guildId}:${userId}`, state);
      return false;
    },
    forget: (guildId, userId) => {
      const existed = entries.delete(`${guildId}:${userId}`);
      return { existed, persisted: true };
    },
  };
  const { client, runtime } = createRuntime({ consentLedger });
  const { manageId } = await startSession(client);
  const shown = await openConsentModal(client, manageId);
  runtime.connections.set(GUILD, {
    voiceChannelId: VOICE,
    connection: { destroy() {} },
  });

  const accepted = await submitConsent(client, shown, {
    answer: "granted",
    remember: true,
  });
  assert.match(accepted.payload.content, /could not save the remembered answer/i);
  assert.match(accepted.payload.content, /current choice still applies to this session/i);
  const reopened = await openConsentModal(client, manageId);
  assert.equal(modalControl(reopened, "remember-answer").default, false);
});

test("a concurrent consent cannot replace another user's storage result", async () => {
  const entries = new Map();
  const consentLedger = {
    recall: (guildId, userId) => entries.get(`${guildId}:${userId}`) ?? null,
    remember: (guildId, userId, state) => {
      entries.set(`${guildId}:${userId}`, state);
      return userId !== ALICE;
    },
    forget: (guildId, userId) => {
      const existed = entries.delete(`${guildId}:${userId}`);
      return { existed, persisted: true };
    },
  };
  const { client, runtime } = createRuntime({ consentLedger });
  const { manageId } = await startSession(client, { voiceMemberIds: [ALICE, BOB] });
  const aliceModal = await openConsentModal(client, manageId, {
    voiceMemberIds: [ALICE, BOB],
  });
  const bobModal = await openConsentModal(client, manageId, {
    userId: BOB,
    voiceMemberIds: [ALICE, BOB],
  });
  runtime.connections.set(GUILD, {
    voiceChannelId: VOICE,
    connection: { destroy() {} },
  });

  let releaseNotice;
  let noticeStarted;
  const noticeGate = new Promise((resolve) => {
    releaseNotice = resolve;
  });
  const started = new Promise((resolve) => {
    noticeStarted = resolve;
  });
  const aliceSubmission = modalInteraction({
    customId: modalJson(aliceModal).custom_id,
    answer: "granted",
    remember: true,
    voiceMemberIds: [ALICE, BOB],
    noticeSink: async () => {
      noticeStarted();
      await noticeGate;
    },
  });
  const aliceResponse = dispatch(client, aliceSubmission);
  await started;

  const bobAccepted = await submitConsent(client, bobModal, {
    answer: "granted",
    remember: true,
    userId: BOB,
    voiceMemberIds: [ALICE, BOB],
    noticeSink: [],
  });
  assert.match(bobAccepted.payload.content, /Remember my answer is ON/);
  releaseNotice();
  const aliceAccepted = await aliceResponse;

  assert.match(aliceAccepted.payload.content, /could not save the remembered answer/i);
  assert.doesNotMatch(aliceAccepted.payload.content, /Remember my answer is ON/);
});

test("a failed removal never claims the old remembered answer was removed", async () => {
  const entries = new Map([[`${GUILD}:${ALICE}`, "granted"]]);
  const consentLedger = {
    recall: (guildId, userId) => entries.get(`${guildId}:${userId}`) ?? null,
    remember: () => true,
    forget: (guildId, userId) => {
      const existed = entries.delete(`${guildId}:${userId}`);
      return { existed, persisted: false };
    },
  };
  const { client } = createRuntime({ consentLedger });
  const { manageId } = await startSession(client);
  const shown = await openConsentModal(client, manageId);

  const declined = await submitConsent(client, shown, {
    answer: "declined",
    remember: false,
  });
  assert.match(declined.payload.content, /could not remove the old remembered answer/i);
  assert.doesNotMatch(declined.payload.content, /Remember my answer is OFF/);
});

test("a private confirmation failure does not reverse a publicly announced choice", async () => {
  const state = memoryLedger();
  const { client, events, runtime, sessions } = createRuntime({ consentLedger: state.ledger });
  const notices = [];
  const { manageId } = await startSession(client);
  const shown = await openConsentModal(client, manageId);
  runtime.connections.set(GUILD, {
    voiceChannelId: VOICE,
    connection: { destroy() {} },
  });

  const failedReply = await submitConsent(client, shown, {
    answer: "granted",
    remember: false,
    noticeSink: notices,
    replyFails: true,
  });
  await settle();

  assert.equal(failedReply.failed, true);
  assert.equal(sessions.canCapture({ guildId: GUILD, userId: ALICE }), true);
  assert.equal(notices.length, 1);
  assert.equal(events.some(({ type }) => type === "consent.panel.failed"), true);
});

test("answering the consent prompt also hands out a pairing link", async () => {
  // Shino, 2026-09-01: "can we have the manage consent button also do an
  // invite?" It is the one control everyone in the channel already
  // reaches for. Discord allows one reply per click and the button spends
  // its own opening the modal, so the invitation rides on the answer.
  for (const answer of ["granted", "declined"]) {
    const { client, events } = createRuntime({ joinBaseUrl: "https://ears.example" });
    const { manageId } = await startSession(client, { voiceMemberIds: [ALICE, BOB] });
    const shown = await openConsentModal(client, manageId, {
      userId: BOB,
      voiceMemberIds: [ALICE, BOB],
    });
    const accepted = await submitConsent(client, shown, {
      answer,
      remember: true,
      userId: BOB,
      voiceMemberIds: [ALICE, BOB],
    });
    assert.equal(accepted.method, "reply");
    assert.equal(accepted.payload.flags, MessageFlags.Ephemeral);
    // Consent governs whether YOUR VOICE is captioned, not whether you may
    // read the room, so a decliner is offered a way to read as well.
    assert.match(
      accepted.payload.content,
      /https:\/\/ears\.example\/join#ghe_[A-Za-z0-9_-]{32}/,
      `${answer} was not offered a pairing link`,
    );
    const requested = events.filter(
      ({ type }) => type === "viewer.enroll.requested",
    );
    assert.equal(requested.length, 1, `${answer} minted ${requested.length} invitations`);
    assert.equal(requested[0].payload.userId, BOB, "the link belongs to the answerer");
    assert.match(requested[0].payload.code, /^ghe_[A-Za-z0-9_-]{32}$/);
    // The confirmation itself still says what it always said.
    assert.match(accepted.payload.content, /consent is (ON|OFF)/);
  }
});

test("a host with no published address simply omits the link", async () => {
  const { client, events } = createRuntime({ joinBaseUrl: "" });
  const { manageId } = await startSession(client, { voiceMemberIds: [ALICE, BOB] });
  const shown = await openConsentModal(client, manageId, {
    userId: BOB,
    voiceMemberIds: [ALICE, BOB],
  });
  const accepted = await submitConsent(client, shown, {
    answer: "granted",
    remember: true,
    userId: BOB,
    voiceMemberIds: [ALICE, BOB],
  });
  assert.equal(accepted.method, "reply");
  assert.doesNotMatch(accepted.payload.content, /join#/);
  assert.equal(
    events.some(({ type }) => type === "viewer.enroll.requested"),
    false,
    "an invitation was minted with nowhere to point it",
  );
});
