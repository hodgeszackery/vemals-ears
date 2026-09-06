import {
  EndBehaviorType,
  VoiceConnectionStatus,
  entersState,
  joinVoiceChannel,
} from "@discordjs/voice";
import { randomUUID } from "node:crypto";
import {
  ActionRowBuilder,
  ButtonBuilder,
  ButtonStyle,
  CheckboxBuilder,
  Client,
  Events,
  GatewayIntentBits,
  LabelBuilder,
  MessageFlags,
  ModalBuilder,
  PermissionFlagsBits,
  RadioGroupBuilder,
  RadioGroupOptionBuilder,
  StringSelectMenuBuilder,
  StringSelectMenuOptionBuilder,
  TextInputBuilder,
  TextInputStyle,
} from "discord.js";

import { validateAudioSink } from "./audio_sink.js";
import { captionsCommandJson } from "./command_schema.js";
import {
  ENROLLMENT_TTL_SECONDS,
  newEnrollmentCode,
} from "./command_controller.js";
import {
  CaptionCommandController,
  CommandInputError,
  publicCommandError,
} from "./command_controller.js";
import { SessionManager, SessionPhase } from "./session_manager.js";
import { SpeakerStateError, SpeakerTracker } from "./speaker_tracker.js";

export const AUDIENCE_HEARTBEAT_MS = 10_000;

// The "vears:" prefix below is the component custom_id namespace, not the
// slash command. It keeps the old word on purpose: buttons already posted
// in the guild carry it, and renaming would leave them inert for nothing a
// member would ever notice.
function parseConsentButton(customId) {
  if (typeof customId !== "string") {
    return null;
  }
  const parts = customId.split(":");
  if (parts[0] !== "vears") {
    return null;
  }
  if (parts.length === 3 && parts[1] === "manage" && parts[2]) {
    return { action: "manage", nonce: parts[2], subcommand: null };
  }
  return null;
}

function parseConsentModal(customId) {
  if (typeof customId !== "string") {
    return null;
  }
  const parts = customId.split(":");
  return parts.length === 3 &&
    parts[0] === "vears" &&
    parts[1] === "consent-modal" &&
    parts[2]
    ? { nonce: parts[2] }
    : null;
}

// Character mapping shares the customId namespace with consent, but carries
// no capture authority. An older class button may still update the clicking
// user's own mapping after a raid session ends.
const MAPPING_CLASSES = [
  ["Death Knight", "death-knight"],
  ["Demon Hunter", "demon-hunter"],
  ["Druid", "druid"],
  ["Evoker", "evoker"],
  ["Hunter", "hunter"],
  ["Mage", "mage"],
  ["Monk", "monk"],
  ["Paladin", "paladin"],
  ["Priest", "priest"],
  ["Rogue", "rogue"],
  ["Shaman", "shaman"],
  ["Warlock", "warlock"],
  ["Warrior", "warrior"],
];
const MAPPING_ROLES = [
  ["Tank", "tank"],
  ["Healer", "healer"],
  ["Damage", "damage"],
];
// Shown once, at the moment consent is granted. It says what the person
// gets rather than what the system wants, because the panel is optional
// and a prompt that reads as a chore gets dismissed.
const MAPPING_AFTER_CONSENT_PROMPT =
  "Choose your WoW class and character so captions can show your character " +
  "name in the correct class color. It is remembered until you run /vem unmap.";

const QUICK_CHAT_BINDING = Symbol("quickChatBinding");
// Global copies this app has published under other names. Guild-scoped
// set() replaces the guild's whole list, so a renamed command disappears
// there by itself - but a global registration from a deployment that had
// no guild configured would linger forever, leaving members a second
// stale entry in the picker that answers nothing.
const LEGACY_GLOBAL_COMMAND_NAMES = ["captions", "vears"];

function parseMappingComponent(customId) {
  if (typeof customId !== "string") {
    return null;
  }
  const parts = customId.split(":");
  if (parts[0] !== "vears" || parts.length !== 3 || !parts[2]) {
    return null;
  }
  if (["map", "mapclass", "maprole", "mapname", "mapmodal"].includes(parts[1])) {
    return { action: parts[1], nonce: parts[2] };
  }
  return null;
}

function mappingButtonRow(nonce) {
  return new ActionRowBuilder().addComponents(
    new ButtonBuilder()
      .setCustomId(`vears:map:${nonce}`)
      .setLabel("Choose your class")
      .setStyle(ButtonStyle.Primary),
  );
}

function mappingSelectRow(kind, nonce, options, chosen) {
  const menu = new StringSelectMenuBuilder()
    .setCustomId(`vears:${kind}:${nonce}`)
    .setPlaceholder(kind === "mapclass" ? "Choose your class" : "Choose your role")
    .addOptions(
      options.map(([label, value]) =>
        new StringSelectMenuOptionBuilder()
          .setLabel(label)
          .setValue(value)
          // Discord shows the current choice as selected, so the panel
          // reads as "here is what you have" rather than "start again".
          .setDefault(value === chosen),
      ),
    );
  return new ActionRowBuilder().addComponents(menu);
}

function mappingNameRow(nonce, draft) {
  return new ActionRowBuilder().addComponents(
    new ButtonBuilder()
      .setCustomId(`vears:mapname:${nonce}`)
      .setLabel(draft.characterName ? "Change character name" : "Set character name")
      .setStyle(draft.characterName ? ButtonStyle.Secondary : ButtonStyle.Primary),
  );
}

function mappingPanelRows(nonce, draft) {
  return [
    mappingSelectRow("mapclass", nonce, MAPPING_CLASSES, draft.wowClass),
    mappingSelectRow("maprole", nonce, MAPPING_ROLES, draft.role),
    mappingNameRow(nonce, draft),
  ];
}

function mappingPanelContent(draft, persisted = true) {
  const classLabel = MAPPING_CLASSES.find(([, value]) => value === draft.wowClass)?.[0];
  const roleLabel = MAPPING_ROLES.find(([, value]) => value === draft.role)?.[0];
  const name = draft.characterName
    ? `${draft.characterName}${draft.realm ? `-${draft.realm}` : ""}`
    : "not set";
  const missing = [
    draft.characterName ? null : "a character name",
    draft.wowClass ? null : "a class",
    draft.role ? null : "a role",
  ].filter(Boolean);
  const lines = [
    `Character: ${name}`,
    `Class: ${classLabel || "not set"}`,
    `Role: ${roleLabel || "not set"}`,
  ];
  lines.push(
    missing.length
      ? `Still needed: ${missing.join(", ")}.`
      : persisted
        ? "Saved. Your name will show in your class colour."
        : "Active now, but Vemal's Ears could not save this mapping for the next bot restart. " +
          "Open Choose your class to retry.",
  );
  return lines.join("\n");
}

function modalSelectValue(fields, customId) {
  try {
    return fields?.getStringSelectValues?.(customId)?.[0] || "";
  } catch {
    // A modal opened before an update may not contain the new select.
    return "";
  }
}

function modalTextValue(fields, customId) {
  try {
    return fields?.getTextInputValue?.(customId)?.trim() || "";
  } catch {
    return "";
  }
}

function modalCheckboxValue(fields, customId) {
  try {
    return fields?.getCheckbox?.(customId) === true;
  } catch {
    return false;
  }
}

function mappingModal(nonce, draft) {
  const classMenu = new StringSelectMenuBuilder()
    .setCustomId("wowClass")
    .setPlaceholder("Choose your class")
    .setMinValues(1)
    .setMaxValues(1)
    .setRequired(true)
    .addOptions(
      MAPPING_CLASSES.map(([label, value]) =>
        new StringSelectMenuOptionBuilder()
          .setLabel(label)
          .setValue(value)
          .setDefault(value === draft.wowClass),
      ),
    );
  const roleMenu = new StringSelectMenuBuilder()
    .setCustomId("role")
    .setPlaceholder("Choose your role")
    .setMinValues(1)
    .setMaxValues(1)
    .setRequired(true)
    .addOptions(
      MAPPING_ROLES.map(([label, value]) =>
        new StringSelectMenuOptionBuilder()
          .setLabel(label)
          .setValue(value)
          .setDefault(value === draft.role),
      ),
    );
  const characterName = new TextInputBuilder()
    .setCustomId("characterName")
    .setStyle(TextInputStyle.Short)
    .setMinLength(2)
    .setMaxLength(24)
    .setRequired(true);
  const realm = new TextInputBuilder()
    .setCustomId("realm")
    .setStyle(TextInputStyle.Short)
    .setMaxLength(32)
    .setRequired(false);
  if (draft.characterName) {
    characterName.setValue(draft.characterName);
  }
  if (draft.realm) {
    realm.setValue(draft.realm);
  }

  return new ModalBuilder()
    .setCustomId(`vears:mapmodal:${nonce}`)
    .setTitle("Choose your class")
    .addLabelComponents(
      new LabelBuilder()
        .setLabel("Class")
        .setDescription("Remembered until /vem unmap; sets your caption name color.")
        .setStringSelectMenuComponent(classMenu),
      new LabelBuilder()
        .setLabel("Role")
        .setDescription("Focus mode keeps tank and healer captions visible.")
        .setStringSelectMenuComponent(roleMenu),
      new LabelBuilder()
        .setLabel("Character name")
        .setDescription("Shown beside your captions in game.")
        .setTextInputComponent(characterName),
      new LabelBuilder()
        .setLabel("Realm (optional)")
        .setDescription("Add this only when it helps identify your character.")
        .setTextInputComponent(realm),
      new LabelBuilder()
        .setLabel("Clear my saved mapping")
        .setDescription("Removes your character, class, role, and realm.")
        .setCheckboxComponent(
          new CheckboxBuilder()
            .setCustomId("clear-mapping")
            .setDefault(false),
        ),
    );
}

function consentActionRow(nonce) {
  return new ActionRowBuilder().addComponents(
    new ButtonBuilder()
      .setCustomId(`vears:manage:${nonce}`)
      .setLabel("Manage my consent")
      // Primary, not Secondary. This is the only way a deaf or hard-of-
      // hearing player turns their own captions on, and as Discord's grey
      // it was the dimmest thing in the message it sits on.
      //
      // Primary rather than Success because green is already spoken for
      // one row deeper: consentStateActionRow uses Success for "Turn
      // captions ON" and Secondary for OFF. Green here would mean green
      // sometimes says "captions are on" and sometimes "press me to find
      // out", which is worse than dim.
      .setStyle(ButtonStyle.Primary),
    new ButtonBuilder()
      .setCustomId(`vears:map:${nonce}`)
      .setLabel("Choose your class")
      // This is a direct setup action alongside Manage my consent. Keeping
      // both primary makes the two player-owned choices equally discoverable.
      .setStyle(ButtonStyle.Primary),
  );
}

function consentModal(nonce, { state, remembered }) {
  // "Remember my answer" defaults ON for someone who has never answered.
  // It used to default to whether an answer was already remembered - which
  // for a first-timer is "no" - so most people consented for one session
  // only and were asked again after every restart. Someone who has
  // answered before keeps whatever they chose about remembering.
  const rememberByDefault = remembered || state === "pending";
  const allow = new RadioGroupOptionBuilder()
    .setLabel("Yes, caption my voice")
    .setValue("granted");
  const decline = new RadioGroupOptionBuilder()
    .setLabel("No, do not caption me")
    .setValue("declined");
  if (state === "granted") {
    allow.setDefault(true);
  } else if (state === "declined") {
    decline.setDefault(true);
  }
  return new ModalBuilder()
    .setCustomId(`vears:consent-modal:${nonce}`)
    .setTitle("Manage caption consent")
    .addLabelComponents(
      new LabelBuilder()
        .setLabel("Caption my voice?")
        .setDescription("This choice affects only your voice.")
        .setRadioGroupComponent(
          new RadioGroupBuilder()
            .setCustomId("consent-answer")
            .setRequired(true)
            .addOptions(allow, decline),
        ),
      new LabelBuilder()
        .setLabel("Remember my answer")
        .setDescription("Use this answer in future caption sessions.")
        .setCheckboxComponent(
          new CheckboxBuilder()
            .setCustomId("remember-answer")
            .setDefault(rememberByDefault),
        ),
    );
}

// Names render, nobody is notified. Quick chat already posted under this
// exact shape; the control notices now share it, which is what stops a
// re-join from buzzing everyone's phone.
const SILENT_MENTIONS = Object.freeze({
  parse: [],
  users: [],
  roles: [],
  repliedUser: false,
});


// Every ping the bot can emit passes through here: the join notice builds
// <@id> itself, and the consent and stop notices arrive as content from
// mentionUser(). One suppression covers all of them, and exporting the
// payload keeps that testable without faking the Discord handshake.
export function controlNoticePayload(content, components = undefined) {
  return components
    ? { content, components, allowedMentions: SILENT_MENTIONS }
    : { content, allowedMentions: SILENT_MENTIONS };
}


function consentConfirmationContent(state, rememberAnswer, persisted) {
  const choice = state === "granted"
    ? "Your consent is ON. Your voice may now be captioned."
    : "Your consent is OFF. Your voice is not being captured.";
  if (!persisted) {
    const failed = rememberAnswer
      ? "Vemal's Ears could not save the remembered answer."
      : "Vemal's Ears could not remove the old remembered answer.";
    return `${choice} ${failed} Your current choice still applies to this session.`;
  }
  return rememberAnswer
    ? `${choice} Remember my answer is ON, so this choice will be used in future sessions.`
    : `${choice} Remember my answer is OFF, so this choice ends with the current session.`;
}

function defaultClient() {
  return new Client({
    intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates],
  });
}

// What a failed voice join looked like, as words the Python side can print.
// Only enum-shaped values cross: the connection's last status, its close
// code when Discord closed the socket, and the error's class name. No
// message text - an error message could carry an endpoint or a token.
const SAFE_WORD = /^[A-Za-z][A-Za-z0-9_]{0,48}$/;

export function joinFailureDetail(connection, error) {
  const detail = {};
  const state = connection?.state;
  const status = typeof state?.status === "string" ? state.status : "";
  if (SAFE_WORD.test(status)) {
    detail.voiceState = status;
  }
  const closeCode = state?.closeCode ?? state?.reason?.closeCode;
  if (Number.isInteger(closeCode) && closeCode >= 0 && closeCode <= 9999) {
    detail.closeCode = closeCode;
  }
  const cause = error && typeof error.name === "string" ? error.name : "";
  if (SAFE_WORD.test(cause)) {
    detail.cause = cause;
  }
  return detail;
}

export class DiscordAdapterRuntime {
  constructor({
    config,
    emitter,
    audioSink,
    client = defaultClient(),
    connectionFactory = joinVoiceChannel,
    sessionManager = new SessionManager(),
    speakerTracker = new SpeakerTracker(),
    reconnectDelaysMs = [2_000, 4_000, 8_000, 16_000, 30_000],
    resumeWindowMs = 30 * 60_000,
    audienceHeartbeatMs = AUDIENCE_HEARTBEAT_MS,
  }) {
    if (!config || !emitter) {
      throw new TypeError("config and emitter are required.");
    }
    this.config = config;
    this.reconnectDelaysMs = [...reconnectDelaysMs];
    this.resumeWindowMs = resumeWindowMs;
    if (!Number.isSafeInteger(audienceHeartbeatMs) || audienceHeartbeatMs <= 0) {
      throw new TypeError("audienceHeartbeatMs must be a positive integer.");
    }
    this.audienceHeartbeatMs = audienceHeartbeatMs;
    this.emitter = emitter;
    this.audioSink = validateAudioSink(audioSink);
    this.client = client;
    this.connectionFactory = connectionFactory;
    this.sessions = sessionManager;
    this.speakers = speakerTracker;
    this.commands = new CaptionCommandController(this.sessions);
    this.connections = new Map();
    this.streams = new Map();
    this.consentPrompts = new Map();
    // Partially filled mapping panels, keyed by guild and user. Nothing
    // here is authoritative: it is what someone has picked so far, and it
    // is written to the session manager only once it is complete.
    this.mappingDrafts = new Map();
    // A delivered request stays here briefly so a lost relay acknowledgement
    // can be retried without posting the same line to Discord twice. The map
    // is deliberately bounded and memory-only; message bodies never reach a
    // ledger or diagnostic event.
    this.quickChatDeliveries = new Map();
    this.discordHealthy = false;
    this.gatewayFailed = false;
    this.started = false;
    this.startupPromise = null;
    this.shutdownPromise = null;
    this.shutdownRequested = false;
    this.outputFailed = false;
    this.audienceHeartbeat = null;

    this.client.on(Events.InteractionCreate, (interaction) => {
      void this.#handleInteraction(interaction);
    });
    this.client.on(Events.VoiceStateUpdate, (oldState, newState) => {
      void this.#handleVoiceStateUpdate(oldState, newState);
    });
    for (const [event, reason] of [
      [Events.Error, "discord_client_error"],
      [Events.ShardDisconnect, "discord_gateway_disconnected"],
      [Events.ShardReconnecting, "discord_gateway_reconnecting"],
      [Events.Invalidated, "discord_gateway_invalidated"],
      [Events.ShardError, "discord_gateway_error"],
    ]) {
      this.client.on(event, () => {
        if (this.shutdownRequested || this.gatewayFailed) {
          return;
        }
        // Voice reconnects retain a live gateway roster; gateway reconnects
        // do not. Let the supervisor restart with authoritative Discord state
        // instead of renewing a cached audience after any lost gateway event.
        this.gatewayFailed = true;
        void this.#failClosed(reason);
      });
    }
  }

  async start() {
    if (this.started) {
      throw new Error("Discord adapter already started.");
    }
    this.started = true;
    const startup = this.#runStartup();
    this.startupPromise = startup;
    try {
      await startup;
    } finally {
      if (this.startupPromise === startup) {
        this.startupPromise = null;
      }
    }
  }

  async #runStartup() {
    this.#emit("adapter.starting", {
      commandScope: this.config.guildId ? "guild" : "global",
      captureEnabled: false,
    });
    this.#refreshCaptureHealth("startup");

    try {
      await this.client.login(this.config.token);
      if (this.shutdownRequested || this.gatewayFailed) {
        return;
      }
      await this.#registerCommands();
      if (this.shutdownRequested || this.gatewayFailed) {
        return;
      }
      this.discordHealthy = true;
      this.#refreshCaptureHealth("discord_ready");
      this.#emit("adapter.ready", {
        discordUserId: this.client.user?.id || null,
        commandScope: this.config.guildId ? "guild" : "global",
        captureEnabled: this.sessions.captureHealthy,
      });
      await this.#resumeSessions();
      this.#startAudienceHeartbeat();
    } catch (error) {
      if (this.shutdownRequested) {
        return;
      }
      this.discordHealthy = false;
      this.#refreshCaptureHealth("discord_start_failed");
      this.#emit("adapter.failed", { reason: "discord_start_failed" });
      // Say WHY, on stderr, where the operator can read it.
      //
      // This was a bare catch. An invalid token, a missing gateway
      // intent, a revoked application and a dropped network all reached
      // the operator as the same four words - "source is unavailable" -
      // while the bot sat in a restart loop. It cost an evening of
      // uptime to learn nothing, because the reason was discarded here
      // before anything downstream had a chance to carry it.
      //
      // The token is redacted defensively. discord.js does not put it in
      // these messages, but this is not the line to be wrong on.
      const detail = String(
        (error && (error.message || error.code)) || error || "unknown",
      );
      const safe = this.config.token
        ? detail.split(this.config.token).join("<redacted>")
        : detail;
      process.stderr.write(
        "Vemal's Ears could not start Discord: " + safe.slice(0, 300) + "\n",
      );
      throw new Error("Discord adapter could not start.");
    }
  }

  shutdown(reason = "shutdown") {
    this.shutdownRequested = true;
    if (!this.shutdownPromise) {
      this.shutdownPromise = this.#runShutdown(reason);
    }
    return this.shutdownPromise;
  }

  async #runShutdown(reason) {
    this.#stopAudienceHeartbeat();
    this.discordHealthy = false;
    this.sessions.setCaptureHealth(false, reason);
    for (const guildId of [...this.connections.keys()]) {
      this.#stopGuildCapture(guildId, reason);
    }
    this.consentPrompts.clear();
    let consentStateSafe = true;
    try {
      consentStateSafe = this.sessions.close?.() !== false;
    } catch {
      consentStateSafe = false;
    }
    if (!consentStateSafe) {
      this.#emit("consent.state.dirty", { reason: "clean_close_failed" });
    }
    // client.destroy() clears discord.js's REST token. If service shutdown
    // arrives while command registration is in flight, destroying first makes
    // that request fail with a misleading missing-token error. Let the startup
    // boundary settle, then tear the client down exactly once.
    const startup = this.startupPromise;
    if (startup) {
      await startup.catch(() => {});
    }
    await this.client.destroy();
    this.#emit("adapter.stopped", { reason });
  }

  async refreshAudioSinkHealth() {
    const health = this.#refreshCaptureHealth(
      "audio_sink_health_changed",
      { emit: false },
    );
    if (health.captureHealthy) {
      // Active is announced before a new receiver can subscribe.
      this.#emit("health.changed", health.event);
      await this.#syncAllGuilds();
    } else {
      // Inactive is announced only after every receiver is destroyed.
      await this.#syncAllGuilds();
      this.#emit("health.changed", health.event);
    }
  }

  bindQuickChat(command) {
    const normalized = this.#validateQuickChat(command);
    if (!normalized) {
      return Object.freeze({
        [QUICK_CHAT_BINDING]: true,
        requestId: command?.requestId,
        status: "delivery_failed",
      });
    }
    const guildId = this.config.guildId;
    const sessionBinding = guildId ? this.sessions.getSessionBinding(guildId) : null;
    if (!sessionBinding) {
      return Object.freeze({
        [QUICK_CHAT_BINDING]: true,
        requestId: normalized.requestId,
        status: "no_active_session",
      });
    }
    return Object.freeze({
      [QUICK_CHAT_BINDING]: true,
      ...normalized,
      guildId,
      sessionBinding,
    });
  }

  async postQuickChat(command) {
    // One typed message, one status, whatever happens.
    //
    // This is the boundary between a member's typed line and everything
    // that keeps a voice session alive. A throw crossing it reached the
    // command queue as an error, and the adapter answered by closing its
    // shutdown channel: the bot left the channel and the room lost its
    // captions because somebody sent a message. Nothing that happens to
    // one message is worth that, so nothing gets out of here but a
    // status word.
    try {
      return await this.#quickChatStatus(command);
    } catch {
      return "delivery_failed";
    }
  }

  async #quickChatStatus(command) {
    const bound = command?.[QUICK_CHAT_BINDING]
      ? command
      : this.bindQuickChat(command);
    if (bound.status) {
      return bound.status;
    }
    if (!this.sessions.matchesSessionBinding(bound.guildId, bound.sessionBinding)) {
      return "no_active_session";
    }

    const signature = JSON.stringify([
      bound.speaker,
      bound.wowClass,
      bound.text,
    ]);
    const existing = this.quickChatDeliveries.get(bound.requestId);
    if (existing) {
      if (existing.signature !== signature) {
        return "delivery_failed";
      }
      if (
        existing.guildId !== bound.guildId ||
        existing.generation !== bound.sessionBinding.generation ||
        existing.controlChannelId !== bound.sessionBinding.controlChannelId
      ) {
        return "delivery_failed";
      }
      const status = await existing.delivery;
      if (
        status === "delivered" &&
        !this.sessions.matchesSessionBinding(bound.guildId, bound.sessionBinding)
      ) {
        return "no_active_session";
      }
      return status;
    }

    // Never a rejected promise, and never a rejected promise in the cache.
    // #postQuickChat guards the Discord call itself, but the lookups either
    // side of it are not inside that try. A throw there rejected this
    // promise, which reached the queue as an error - and the queue's error
    // path used to shut the adapter down. Worse, the rejection was stored:
    // the relay retries the same request ID three times, so every retry
    // awaited the same rejected promise and threw again.
    const delivery = this.#postQuickChat(bound).catch(() => "delivery_failed");
    this.quickChatDeliveries.set(bound.requestId, {
      signature,
      delivery,
      guildId: bound.guildId,
      generation: bound.sessionBinding.generation,
      controlChannelId: bound.sessionBinding.controlChannelId,
    });
    this.#pruneQuickChatDeliveries();
    const status = await delivery;
    if (status !== "delivered") {
      // A missing session or transient Discord failure is safe to retry with
      // the same request ID. Successful posts remain cached so an ack-loss
      // retry cannot duplicate them.
      this.quickChatDeliveries.delete(bound.requestId);
    }
    return status;
  }

  #validateQuickChat(command) {
    const safeId = /^[A-Za-z0-9_-]{16,128}$/;
    const controls = /[\p{Cc}\p{Cf}\p{Cs}]/u;
    const classes = new Set(MAPPING_CLASSES.map(([, value]) => value));
    if (!command || typeof command !== "object" || Array.isArray(command)) {
      return null;
    }
    const { requestId, speaker, wowClass = "", text } = command;
    if (
      typeof requestId !== "string" ||
      !safeId.test(requestId) ||
      typeof speaker !== "string" ||
      typeof text !== "string" ||
      typeof wowClass !== "string"
    ) {
      return null;
    }
    const cleanSpeaker = speaker.normalize("NFKC").trim();
    const cleanText = text.normalize("NFKC").trim();
    if (
      !cleanSpeaker ||
      [...cleanSpeaker].length > 32 ||
      !cleanText ||
      [...cleanText].length > 320 ||
      controls.test(cleanSpeaker) ||
      controls.test(cleanText) ||
      (wowClass && !classes.has(wowClass))
    ) {
      return null;
    }
    return {
      requestId,
      speaker: cleanSpeaker,
      wowClass,
      text: cleanText,
    };
  }

  async #postQuickChat(command) {
    const { guildId, sessionBinding } = command;
    if (!this.sessions.matchesSessionBinding(guildId, sessionBinding)) {
      return "no_active_session";
    }
    const guild = this.client.guilds.cache.get(guildId);
    const channel = guild?.channels?.cache?.get(sessionBinding.controlChannelId);
    if (!channel?.isTextBased?.() || typeof channel.send !== "function") {
      return "delivery_failed";
    }
    const escape = (value) => value.replace(/([\\`*_{}\[\]()<>#+\-.!|])/g, "\\$1");
    const content =
      `**Quick chat - ${escape(command.speaker)}:** ` + escape(command.text);
    try {
      const message = await channel.send({
        content,
        // Discord enforces this nonce across worker reconnects and process
        // restarts for its dedupe window. The in-memory map is the fast path;
        // this is the final guard if the post succeeds as the control socket
        // drops before its delivery result reaches the relay.
        nonce: command.requestId.slice(0, 25),
        enforceNonce: true,
        allowedMentions: SILENT_MENTIONS,
        flags: MessageFlags.SuppressEmbeds,
        tts: false,
      });
      if (
        message?.channelId !== sessionBinding.controlChannelId ||
        message?.content !== content
      ) {
        return "delivery_failed";
      }
      if (!this.sessions.matchesSessionBinding(guildId, sessionBinding)) {
        return "no_active_session";
      }
      return "delivered";
    } catch {
      return "delivery_failed";
    }
  }

  #pruneQuickChatDeliveries() {
    while (this.quickChatDeliveries.size > 512) {
      const oldest = this.quickChatDeliveries.keys().next().value;
      this.quickChatDeliveries.delete(oldest);
    }
  }

  async #registerCommands() {
    if (this.config.guildId) {
      const guild = await this.client.guilds.fetch(this.config.guildId);
      await guild.commands.set([captionsCommandJson]);
      // `/captions` used to be registered globally. Guild-scoped set() cannot
      // remove it, and replacing the whole global set would erase unrelated
      // application commands. Delete only that exact obsolete command.
      const globalCommands = await this.client.application.commands.fetch();
      for (const command of globalCommands.values()) {
        if (LEGACY_GLOBAL_COMMAND_NAMES.includes(command.name)) {
          await this.client.application.commands.delete(command.id);
        }
      }
      return;
    }
    await this.client.application.commands.set([captionsCommandJson]);
  }

  #refreshCaptureHealth(reason, { emit = true } = {}) {
    let sinkReady = false;
    try {
      sinkReady = this.audioSink.isReady() === true;
    } catch {
      sinkReady = false;
    }
    const captureHealthy = !this.outputFailed && !this.shutdownRequested &&
      !this.gatewayFailed && this.discordHealthy && sinkReady;
    const blockedReason = this.outputFailed
      ? "jsonl_output_unavailable"
      : !this.discordHealthy
        ? reason
        : sinkReady
          ? null
          : "audio_sink_unavailable";
    this.sessions.setCaptureHealth(captureHealthy, blockedReason);
    const event = {
      component: "capture_pipeline",
      status: captureHealthy ? "ready" : "blocked",
      reason: blockedReason,
    };
    if (emit) {
      this.#emit("health.changed", event);
    }
    return { captureHealthy, event };
  }

  #draftKey(guildId, userId) {
    return `${guildId}:${userId}`;
  }

  #mappingDraft(guildId, userId, seed) {
    const key = this.#draftKey(guildId, userId);
    if (seed) {
      this.mappingDrafts.set(key, { ...seed });
    }
    if (!this.mappingDrafts.has(key)) {
      const stored = this.sessions.getCharacter({ guildId, userId }) || {};
      this.mappingDrafts.set(key, {
        characterName: stored.characterName || "",
        realm: stored.realm || "",
        wowClass: stored.wowClass || "",
        role: stored.role || "",
      });
    }
    return this.mappingDrafts.get(key);
  }

  // Consent is the one moment we know the person is present, willing, and
  // about to be captioned, so it is the cheapest possible moment to ask
  // who they are. Ephemeral, and a separate message from the public grant
  // notice: the notice belongs to the channel, this belongs to one person.
  async #offerMappingPanel(interaction, input) {
    if (!input.guildId || !input.userId) {
      return;
    }
    const stored = this.sessions.getCharacter?.({
      guildId: input.guildId,
      userId: input.userId,
    });
    // Already identified. Re-asking on every consent would turn a useful
    // offer into something people learn to dismiss without reading, and
    // the next one that matters gets dismissed with it.
    if (stored?.characterName && stored?.wowClass && stored?.role) {
      return;
    }
    const nonce = randomUUID();
    try {
      await interaction.followUp({
        content: MAPPING_AFTER_CONSENT_PROMPT,
        components: [mappingButtonRow(nonce)],
        flags: MessageFlags.Ephemeral,
      });
    } catch {
      // Load-bearing. "consent.granted" is a non-ephemeral result, and the
      // outer catch calls #failClosed for those - so letting this throw
      // would stop the capture the person just agreed to, because an
      // optional panel failed to render. Consent stands; the panel is
      // reachable any time with /vem map.
      this.#emit("mapping.panel.failed", {
        guildId: input.guildId,
        userId: input.userId,
      });
    }
  }

  /**
   * Save as soon as the draft is complete, and report what happened.
   *
   * Saving on completion rather than behind a separate Save button means
   * there is no state that looks finished but is not - the panel either
   * says what is still needed or says it is saved.
   */
  #commitMappingDraft(guildId, userId, draft) {
    if (!draft.characterName || !draft.wowClass || !draft.role) {
      return null;
    }
    return this.sessions.mapCharacter({
      guildId,
      userId,
      characterName: draft.characterName,
      realm: draft.realm || null,
      wowClass: draft.wowClass,
      role: draft.role,
    });
  }

  async #handleMappingComponent(interaction, component) {
    const guildId = interaction.guildId;
    const userId = interaction.user?.id;
    if (!guildId || !userId) {
      await interaction.reply?.({
        content: "Choose your class from inside a Discord server.",
        flags: MessageFlags.Ephemeral,
      }).catch(() => {});
      return;
    }
    if (this.config.guildId && guildId !== this.config.guildId) {
      await interaction.reply?.({
        content: "Vemal's Ears is not configured for this server.",
        flags: MessageFlags.Ephemeral,
      }).catch(() => {});
      return;
    }
    try {
      if (component.action === "map" || component.action === "mapname") {
        const draft = this.#mappingDraft(guildId, userId);
        await interaction.showModal(mappingModal(component.nonce, draft));
        return;
      }

      const draft = this.#mappingDraft(guildId, userId);
      if (component.action === "mapclass") {
        draft.wowClass = interaction.values?.[0] || "";
      } else if (component.action === "maprole") {
        draft.role = interaction.values?.[0] || "";
      } else if (component.action === "mapmodal") {
        if (modalCheckboxValue(interaction.fields, "clear-mapping")) {
          const removed = this.sessions.clearCharacter({ guildId, userId });
          const persisted = this.sessions.mappingWritePersisted;
          this.mappingDrafts.delete(this.#draftKey(guildId, userId));
          this.#emit("mapping.changed", {
            guildId,
            userId,
            session: this.sessions.getSession(guildId),
            mapping: null,
          });
          await interaction.reply({
            content: !persisted
              ? "Your mapping is cleared for this bot session, but Vemal's Ears could not " +
                "erase the remembered copy. Open Choose your class and try again before " +
                "the bot restarts."
              : removed
                ? "Your character mapping was cleared."
                : "You had no saved character mapping.",
            flags: MessageFlags.Ephemeral,
          });
          return;
        }
        const wowClass = modalSelectValue(interaction.fields, "wowClass");
        const role = modalSelectValue(interaction.fields, "role");
        if (wowClass) {
          draft.wowClass = wowClass;
        }
        if (role) {
          draft.role = role;
        }
        draft.characterName = modalTextValue(interaction.fields, "characterName");
        draft.realm = modalTextValue(interaction.fields, "realm");
      }

      let saved = null;
      let persisted = true;
      try {
        saved = this.#commitMappingDraft(guildId, userId, draft);
        if (saved) {
          persisted = this.sessions.mappingWritePersisted;
        }
      } catch (error) {
        // A rejected value must not be silently kept in the draft, or the
        // panel would claim a mapping the session manager refused.
        if (component.action === "mapmodal") {
          this.mappingDrafts.delete(this.#draftKey(guildId, userId));
          await interaction.reply({
            content: `${error.message}\n\nNothing changed. Open Choose your class and try again.`,
            flags: MessageFlags.Ephemeral,
          });
          return;
        }
        await interaction.update({
          content: `${error.message}\n\n${mappingPanelContent(draft)}`,
          components: mappingPanelRows(component.nonce, draft),
        });
        return;
      }
      if (saved) {
        this.#emit("mapping.changed", {
          guildId,
          userId,
          session: this.sessions.getSession(guildId),
          mapping: saved,
        });
        this.mappingDrafts.delete(this.#draftKey(guildId, userId));
      }
      if (component.action === "mapmodal") {
        await interaction.reply({
          content: saved
            ? mappingPanelContent(draft, persisted)
            : `${mappingPanelContent(draft)}\n\nChoose your class to finish this setup.`,
          ...(saved ? {} : { components: [mappingButtonRow(component.nonce)] }),
          flags: MessageFlags.Ephemeral,
        });
        return;
      }
      await interaction.update({
        content: mappingPanelContent(draft, persisted),
        components: mappingPanelRows(component.nonce, draft),
      });
    } catch {
      this.#emit("mapping.panel.failed", {
        guildId,
        userId,
        action: component.action,
      });
      const response = {
        content: "Vemal's Ears could not open the class picker. Run /vem map and try again.",
        flags: MessageFlags.Ephemeral,
      };
      if (interaction.replied || interaction.deferred) {
        await interaction.followUp(response).catch(() => {});
      } else {
        await interaction.reply(response).catch(() => {});
      }
    }
  }

  async #handleInteraction(interaction) {
    const isCaptionCommand =
      interaction.isChatInputCommand?.() === true &&
      interaction.commandName === captionsCommandJson.name;
    const button =
      interaction.isButton?.() === true ? parseConsentButton(interaction.customId) : null;
    const submittedConsent =
      interaction.isModalSubmit?.() === true
        ? parseConsentModal(interaction.customId)
        : null;
    // Character mapping is separate from consent authority. Discord supplies
    // the authenticated user for the modal, and only that user's mapping is
    // saved; choosing a class never grants voice capture consent.
    const mapping =
      interaction.isStringSelectMenu?.() === true ||
      interaction.isModalSubmit?.() === true ||
      interaction.isButton?.() === true
        ? parseMappingComponent(interaction.customId)
        : null;
    if (mapping) {
      await this.#handleMappingComponent(interaction, mapping);
      return;
    }
    if (!isCaptionCommand && !button && !submittedConsent) {
      return;
    }

    let input;
    let result;
    let submittedConsentStatus;
    let submittedConsentPersisted;
    try {
      if (this.config.guildId && interaction.guildId !== this.config.guildId) {
        throw new CommandInputError(
          "guild_not_allowed",
          "Vemal's Ears is not configured for this server.",
        );
      }
      let componentSubcommand = button ? button.subcommand : undefined;
      let rememberAnswer;
      if (submittedConsent) {
        const answer = interaction.fields.getRadioGroup("consent-answer", true);
        if (answer !== "granted" && answer !== "declined") {
          throw new CommandInputError(
            "invalid_consent_choice",
            "Choose whether your voice may be captioned.",
          );
        }
        componentSubcommand = answer === "granted" ? "consent" : "decline";
        rememberAnswer = interaction.fields.getCheckbox("remember-answer");
      }
      input = this.#interactionInput(interaction, componentSubcommand);
      if (submittedConsent) {
        input.rememberAnswer = rememberAnswer;
      }
      if (button || submittedConsent) {
        this.#assertCurrentConsentPrompt(
          input.guildId,
          button?.nonce ?? submittedConsent.nonce,
        );
      }
      if (button?.action === "manage") {
        const status = this.sessions.getConsentStatus({
          guildId: input.guildId,
          userId: input.userId,
        });
        await interaction.showModal(consentModal(button.nonce, status));
        return;
      }
      if (input.subcommand === "consent") {
        this.#assertGrantFromCurrentVoice(input);
      }
      result = this.commands.execute(input);
      if (
        input.guildId &&
        input.userId &&
        (result.action === "mapping.changed" || result.action === "consent.forgotten")
      ) {
        // A cancelled modal may have a pre-filled draft in memory. Unmap and
        // forget promise to erase the person's mapping completely, so an old
        // draft must not repopulate the next modal after the ledger is clear.
        this.mappingDrafts.delete(this.#draftKey(input.guildId, input.userId));
      }
      // consentWritePersisted is deliberately a same-tick signal. Capture it
      // before the first await so another Discord interaction cannot replace
      // the answer while this one is posting its notices.
      if (submittedConsent) {
        submittedConsentStatus = this.sessions.getConsentStatus({
          guildId: input.guildId,
          userId: input.userId,
        });
        submittedConsentPersisted = this.sessions.consentWritePersisted;
      }
      if (result.action === "viewer.enroll.requested") {
        result.content = this.#pairingInvitation(result.enrollment, result.inviteeId);
      }
      const resultEvent = result.action === "viewer.enroll.requested"
        ? {
            // The token belongs to whoever the link is FOR, which is the
            // invitee when an operator made it and the caller otherwise.
            userId: result.inviteeId || input.userId,
            code: result.enrollment.code,
            ttlSeconds: result.enrollment.ttlSeconds,
          }
        : {
            guildId: input.guildId,
            userId: input.userId,
            session: result.session,
            mapping: result.mapping,
          };
      if (result.action === "session.started" || result.action === "session.stopped") {
        this.#emitAudienceSnapshot(result.session);
      }
      if (
        (result.action === "consent.declined" ||
          result.action === "consent.forgotten") &&
        input.guildId &&
        input.userId
      ) {
        // Privacy before anything else waits on Discord: the
        // withdrawer's live stream ends before the reply posts.
        this.#stopSpeakerCapture(input.guildId, input.userId, "consent_withdrawn");
      }
      const requiresConnection =
        result.session?.phase === SessionPhase.ACTIVE && !this.connections.has(input.guildId);
      if (requiresConnection) {
        // Show active before the receiver subscribes. For every inactive
        // state the inverse ordering below keeps REC visible until teardown
        // has actually destroyed streams and the voice connection.
        this.#emit(result.action, resultEvent);
      }
      if (!requiresConnection) {
        // Consent withdrawal, a new session, and stop all close capture before
        // waiting on Discord to deliver their notice.
        await this.#syncGuild(input.guildId);
        this.#emit(result.action, resultEvent);
      }
      let components;
      if (result.action === "session.started") {
        const prompt = {
          nonce: randomUUID(),
          voiceChannelId: result.session.voiceChannelId,
        };
        this.consentPrompts.set(input.guildId, prompt);
        components = [consentActionRow(prompt.nonce)];
      } else if (result.action === "session.stopped") {
        this.consentPrompts.delete(input.guildId);
      } else if (result.action === "mapping.panel") {
        const nonce = randomUUID();
        const draft = this.#mappingDraft(input.guildId, input.userId, result.draft);
        await interaction.showModal(mappingModal(nonce, draft));
        if (requiresConnection) {
          await this.#syncGuild(input.guildId);
        }
        return;
      }
      if (submittedConsent) {
        const noticeDelivered = await this.#sendControlNotice(
          interaction.guild,
          result.session.controlChannelId,
          result.content,
        );
        if (!noticeDelivered) {
          throw new CommandInputError(
            "consent_notice_delivery_failed",
            "Vemal's Ears could not post the consent update. Capture remains off.",
          );
        }
        let panelDelivered = true;
        // Shino, 2026-09-01: the consent button should hand out a pairing
        // link too. This is the moment everyone in the channel already
        // reaches for, and Discord allows one reply per click - the button
        // spent its own opening the modal - so the invitation rides on the
        // confirmation. It goes to everyone who answers, including someone
        // who declined: consent governs whether YOUR VOICE is captioned,
        // not whether you may read the room.
        const invitation = this.#consentInvitation(input.userId);
        try {
          await interaction.reply({
            content: consentConfirmationContent(
              submittedConsentStatus.state,
              rememberAnswer,
              submittedConsentPersisted,
            ) + invitation.content,
            flags: MessageFlags.Ephemeral,
          });
        } catch {
          // The public notice already made the accepted state visible. A
          // private panel refresh failure must not reverse it and make that
          // notice false; the same action is safe to retry.
          this.#emit("consent.panel.failed", {
            guildId: input.guildId,
            userId: input.userId,
          });
          panelDelivered = false;
        }
        if (panelDelivered && result.action === "consent.granted") {
          await this.#offerMappingPanel(interaction, input);
        }
      } else {
        await interaction.reply({
          content: result.content,
          ...(components ? { components } : {}),
          ...(result.ephemeral ? { flags: MessageFlags.Ephemeral } : {}),
        });
        if (result.action === "consent.granted") {
          await this.#offerMappingPanel(interaction, input);
        }
      }
      if (requiresConnection) {
        // Deliver the visible "active" notice before joining and subscribing.
        await this.#syncGuild(input.guildId);
      }
    } catch (error) {
      if (result && !result.ephemeral) {
        await this.#failClosed("command_notice_delivery_failed");
      }
      const safe = publicCommandError(error);
      this.#emit("command.rejected", {
        guildId: interaction.guildId || null,
        userId: interaction.user?.id || null,
        reason: safe.code,
      });
      const response = { content: safe.message, flags: MessageFlags.Ephemeral };
      if (interaction.replied || interaction.deferred) {
        await interaction.followUp(response).catch(() => {});
      } else {
        await interaction.reply(response).catch(() => {});
      }
    }
  }

  #interactionInput(interaction, buttonSubcommand = undefined) {
    const subcommand = buttonSubcommand === undefined
      ? interaction.options.getSubcommand()
      : buttonSubcommand;
    if (!interaction.inGuild() || !interaction.guild || !interaction.channelId) {
      return {
        subcommand,
        guildId: null,
        userId: interaction.user.id,
      };
    }
    const member = interaction.guild.members.cache.get(interaction.user.id);
    const voiceChannel = member?.voice?.channel || null;
    const participantIds = voiceChannel
      ? [...voiceChannel.members.values()]
          .filter((participant) => !participant.user.bot)
          .map((participant) => participant.id)
      : [];

    return {
      subcommand,
      guildId: interaction.guildId,
      userId: interaction.user.id,
      controlChannelId: interaction.channelId,
      voiceChannelId: voiceChannel?.id || null,
      participantIds,
      mapping:
        subcommand === "map"
          ? {
              characterName: interaction.options.getString("character", false),
              realm: interaction.options.getString("realm", false),
              wowClass: interaction.options.getString("class", false),
              role: interaction.options.getString("role", false),
            }
          : null,
      inviteeId:
        subcommand === "invite"
          ? interaction.options.getUser("member", true)?.id || null
          : null,
      inviteeIsBot:
        subcommand === "invite"
          ? interaction.options.getUser("member", true)?.bot === true
          : false,
      // Handing someone else a pairing link is an operator act: it creates
      // a credential for another person's computer.
      canInvite: Boolean(
        interaction.memberPermissions?.has?.(PermissionFlagsBits.ManageGuild),
      ),
    };
  }

  #assertCurrentConsentPrompt(guildId, nonce) {
    const prompt = guildId ? this.consentPrompts.get(guildId) : null;
    const session = guildId ? this.sessions.getSession(guildId) : null;
    if (
      !prompt ||
      prompt.nonce !== nonce ||
      !session ||
      session.phase === SessionPhase.STOPPED ||
      prompt.voiceChannelId !== session.voiceChannelId
    ) {
      throw new CommandInputError(
        "stale_consent_button",
        "This consent prompt is no longer active. Use the latest Vemal's Ears message.",
      );
    }
  }

  #assertGrantFromCurrentVoice(input) {
    const session = input.guildId ? this.sessions.getSession(input.guildId) : null;
    if (!session || session.phase === SessionPhase.STOPPED) {
      return;
    }
    if (
      input.voiceChannelId !== session.voiceChannelId ||
      !input.participantIds.includes(input.userId)
    ) {
      throw new CommandInputError(
        "not_participant",
        "Join the session voice channel before recording consent.",
      );
    }
  }

  async #handleVoiceStateUpdate(oldState, newState) {
    const guildId = newState.guild.id;
    const session = this.sessions.getSession(guildId);
    if (!session || session.phase === SessionPhase.STOPPED) {
      return;
    }
    if (
      oldState.channelId !== session.voiceChannelId &&
      newState.channelId !== session.voiceChannelId
    ) {
      return;
    }
    const previousPhase = session.phase;
    const previousParticipants = new Set(session.participants.map(({ userId }) => userId));
    // discord.js patches guild.voiceStates and GuildMembers before emitting
    // VoiceStateUpdate. BaseGuildVoiceChannel.members is therefore the
    // authoritative post-event roster and also repairs any prior local drift.
    const channel = newState.guild.channels.cache.get(session.voiceChannelId);
    const participantIds = channel?.isVoiceBased()
      ? [...channel.members.values()]
          .filter((participant) => !participant.user.bot)
          .map((participant) => participant.id)
      : [];
    const updated = this.sessions.updateParticipants({ guildId, participantIds });
    const remainingParticipants = new Set(
      updated.participants.map(({ userId }) => userId),
    );
    for (const userId of previousParticipants) {
      if (!remainingParticipants.has(userId)) {
        // Leaving voice ends this person's capture immediately even when the
        // rest of the session stays active. End the old audience revision
        // before publishing the new roster so no post-leave audio can cross
        // the membership boundary.
        this.#stopSpeakerCapture(guildId, userId, "voice_left");
      }
    }
    this.#emitAudienceSnapshot(updated);
    const requiresConnection =
      updated.phase === SessionPhase.ACTIVE && !this.connections.has(guildId);
    if (requiresConnection) {
      this.#emit("session.participants.changed", { guildId, session: updated });
    }
    await this.#syncGuild(guildId);
    if (!requiresConnection) {
      this.#emit("session.participants.changed", {
        guildId,
        session: this.sessions.getSession(guildId) || updated,
      });
    }

    // #syncGuild can fail closed and pause the session, so the notices below
    // describe the phase as it stands now rather than the pre-sync snapshot.
    const phase = this.sessions.getSession(guildId)?.phase ?? updated.phase;
    const joined = updated.participants
      .map(({ userId }) => userId)
      .filter((userId) => !previousParticipants.has(userId));
    if (joined.length > 0) {
      const mentions = joined.map((userId) => `<@${userId}>`).join(", ");
      // A joiner whose consent is already on record leaves the session ACTIVE,
      // so the notice has to follow the resulting phase. Announcing a pause
      // that did not happen tells the channel capture is off while it is on,
      // which is the one thing these notices exist to get right.
      //
      // This branch is newly reachable HERE: this is the first commit to hand
      // the session manager a real consent ledger, so a remembered grant now
      // seeds a joiner as GRANTED and leaves capture running. Before it, every
      // joiner was PENDING and the pause the old text announced was true.
      // Joining pauses nobody under per-speaker capture. The notice says
      // exactly what is true for the joiner: captured because their
      // consent is on record, or not captured until they give it.
      const joinedGranted = updated.participants
        .filter((participant) => joined.includes(participant.userId))
        .every((participant) => participant.consent === "granted");
      let content;
      if (phase !== SessionPhase.ACTIVE) {
        content =
          `${mentions} joined. Nobody in this session has consented yet, ` +
          "so audio capture is OFF.";
      } else if (joinedGranted) {
        content = `${mentions} joined with consent already on record and are now captioned.`;
      } else {
        content =
          `${mentions} joined and are not being captioned. ` +
          "Captions continue for those who consented.";
      }
      await this.#sendControlNotice(newState.guild, updated.controlChannelId, content);
    } else if (previousPhase !== SessionPhase.ACTIVE && phase === SessionPhase.ACTIVE) {
      await this.#sendControlNotice(
        newState.guild,
        updated.controlChannelId,
        "Vemal's Ears resumed - captioning those who consented.",
      );
    }
  }

  async #resumeSessions() {
    // Shino's ask, 2026-09-01: every deploy restarts this process, and the
    // bot used to sit outside voice until somebody re-ran /vem start.
    // The session ledger remembers where it was. Consent is NOT carried
    // here - it comes back through startSession from the consent ledger,
    // exactly as a fresh command would, so nobody is captioned on the
    // strength of a file. Bounded by resumeWindowMs: a restart takes
    // seconds, and a session hours old belongs to a raid that ended.
    if (typeof this.sessions.resumableSessions !== "function") {
      return;
    }
    for (const entry of this.sessions.resumableSessions({ maxAgeMs: this.resumeWindowMs })) {
      if (this.shutdownRequested) {
        return;
      }
      const guild = this.client.guilds.cache.get(entry.guildId);
      const channel = guild?.channels?.cache?.get(entry.voiceChannelId);
      const participantIds = channel?.isVoiceBased?.()
        ? [...channel.members.values()]
            .filter((participant) => !participant.user.bot)
            .map((participant) => participant.id)
        : [];
      if (!guild || participantIds.length === 0) {
        // Nobody left to caption: that session is over, not paused.
        this.sessions.forgetResumable?.(entry.guildId);
        continue;
      }
      let session;
      try {
        session = this.sessions.startSession({
          guildId: entry.guildId,
          voiceChannelId: entry.voiceChannelId,
          controlChannelId: entry.controlChannelId,
          startedBy: entry.startedBy,
          participantIds,
        });
        if (!participantIds.includes(entry.startedBy)) {
          // startSession normally repairs a command snapshot by inserting its
          // starter. During restart the old starter may have left already:
          // retain their explicit stop authority, then immediately restore
          // membership to Discord's current voice roster before emitting or
          // opening capture.
          session = this.sessions.updateParticipants({
            guildId: entry.guildId,
            participantIds,
          });
        }
      } catch {
        this.sessions.forgetResumable?.(entry.guildId);
        continue;
      }
      // The same event a /vem start emits, flagged, so the Python side
      // needs no new vocabulary (an unknown event type is fatal there).
      this.#emitAudienceSnapshot(session);
      this.#emit("session.started", { guildId: entry.guildId, session, resumed: true });
      await this.#syncGuild(entry.guildId);
      // "Rejoined" is only true once the voice connection is Ready. This
      // notice used to go out regardless, so a join that failed still
      // announced a session that never existed in voice - and did so on
      // every restart. The failed join has already forgotten the entry,
      // so this message is posted once, not fifty-two times.
      const live =
        this.connections.get(entry.guildId)?.connection?.state?.status ===
        VoiceConnectionStatus.Ready;
      if (!live) {
        await this.#sendControlNotice(
          guild,
          entry.controlChannelId,
          "Vemal's Ears restarted but could not rejoin this voice channel, " +
            "so the session was not resumed. Run `/vem start` when you want " +
            "captions again.",
        );
        continue;
      }
      // A fresh consent prompt, so the buttons on this notice work for
      // anyone who has not consented yet - the old prompt died with the
      // old process.
      const prompt = { nonce: randomUUID(), voiceChannelId: entry.voiceChannelId };
      this.consentPrompts.set(entry.guildId, prompt);
      await this.#sendControlNotice(
        guild,
        entry.controlChannelId,
        "Vemal's Ears restarted and rejoined this voice channel. Consent is " +
          "unchanged: anyone who had not consented is still not captioned, " +
          "and anyone in the session can run `/vem stop`.",
        [consentActionRow(prompt.nonce)],
      );
    }
  }

  #consentInvitation(userId) {
    // A pairing link to append to a consent confirmation, when the host
    // publishes an address to point at.
    if (!this.config.joinBaseUrl) {
      return { content: "" };
    }
    const enrollment = {
      code: newEnrollmentCode(),
      ttlSeconds: ENROLLMENT_TTL_SECONDS,
    };
    this.#emit("viewer.enroll.requested", {
      userId,
      code: enrollment.code,
      ttlSeconds: enrollment.ttlSeconds,
    });
    const minutes = Math.max(1, Math.round(enrollment.ttlSeconds / 60));
    const base = String(this.config.joinBaseUrl).replace(/\/+$/, "");
    return {
      content:
        "\n\nTo READ captions on this computer, open "
        + `${base}/join#${enrollment.code} - yours alone, works once, `
        + `expires in ${minutes} minutes. Skip it if this computer already `
        + "shows captions.",
    };
  }

  #pairingInvitation(enrollment, inviteeId = null) {
    // One link does both jobs: it pairs the app AND records whose it is,
    // so leaving the voice channel stops that person's captions and
    // rejoining starts them again, with nothing else to set up.
    const minutes = Math.max(1, Math.round(enrollment.ttlSeconds / 60));
    const base = String(this.config.joinBaseUrl || "").replace(/\/+$/, "");
    if (!base) {
      return (
        "Vemal's Ears has no public pairing address configured, so this "
        + "link cannot be built. Ask the guild host to set one, then run "
        + "`/vem setup` again."
      );
    }
    const link = `${base}/join#${enrollment.code}`;
    if (inviteeId) {
      return (
        `Pairing link for <@${inviteeId}>: ${link}\n`
        + `Send it to them privately. It works once, expires in ${minutes} `
        + "minutes, and belongs to them alone - captions then follow the "
        + "voice channel, stopping when they leave and starting when they "
        + "rejoin."
      );
    }
    return (
      "Open this on the computer you want captioned: "
      + `${link}\n`
      + `It is yours alone, works once, and expires in ${minutes} minutes. `
      + "After that captions follow the voice channel: they stop when you "
      + "leave it and start again when you rejoin."
    );
  }

  async #sendControlNotice(guild, channelId, content, components = undefined) {
    const channel = guild.channels.cache.get(channelId);
    if (!channel?.isTextBased() || typeof channel.send !== "function") {
      this.#emit("notice.failed", { guildId: guild.id, channelId, reason: "channel_missing" });
      return false;
    }
    try {
      await channel.send(controlNoticePayload(content, components));
      return true;
    } catch {
      this.#emit("notice.failed", { guildId: guild.id, channelId, reason: "send_failed" });
      return false;
    }
  }

  async #syncAllGuilds() {
    await Promise.all(this.sessions.listSessions().map((session) => this.#syncGuild(session.guildId)));
  }

  #emitAudienceSnapshot(session) {
    if (!session || !this.discordHealthy || this.gatewayFailed ||
      this.shutdownRequested || this.outputFailed) {
      return null;
    }
    const participantIds = session.phase === SessionPhase.STOPPED
      ? []
      : session.participants.map(({ userId }) => userId);
    return this.#emit("audience.snapshot", {
      generation: session.generation,
      revision: session.audienceRevision,
      participantIds,
    });
  }

  #startAudienceHeartbeat() {
    if (this.audienceHeartbeat !== null || !this.discordHealthy ||
      this.gatewayFailed || this.shutdownRequested || this.outputFailed) {
      return;
    }
    this.audienceHeartbeat = setInterval(() => {
      for (const session of this.sessions.listSessions()) {
        if (session.phase !== SessionPhase.STOPPED) {
          this.#emitAudienceSnapshot(session);
        }
      }
    }, this.audienceHeartbeatMs);
    this.audienceHeartbeat.unref?.();
  }

  #stopAudienceHeartbeat() {
    if (this.audienceHeartbeat !== null) {
      clearInterval(this.audienceHeartbeat);
      this.audienceHeartbeat = null;
    }
  }

  async #syncGuild(guildId) {
    if (this.shutdownRequested || this.gatewayFailed) {
      return;
    }
    const session = this.sessions.getSession(guildId);
    if (!session || session.phase !== SessionPhase.ACTIVE) {
      this.#stopGuildCapture(guildId, session?.pauseReason || "session_inactive");
      return;
    }
    if (this.connections.get(guildId)?.voiceChannelId === session.voiceChannelId) {
      return;
    }
    this.#stopGuildCapture(guildId, "voice_channel_changed");

    const guild = this.client.guilds.cache.get(guildId);
    if (!guild) {
      await this.#failClosed("guild_cache_missing");
      return;
    }

    let connection;
    try {
      connection = this.connectionFactory({
        channelId: session.voiceChannelId,
        guildId,
        adapterCreator: guild.voiceAdapterCreator,
        selfDeaf: false,
        selfMute: true,
        daveEncryption: true,
      });
      const entry = { connection, voiceChannelId: session.voiceChannelId };
      this.connections.set(guildId, entry);
      connection.on("error", () => {
        void this.#failClosed("voice_connection_error");
      });
      connection.on("stateChange", (_oldState, newState) => {
        if (
          newState.status === VoiceConnectionStatus.Disconnected &&
          this.connections.get(guildId)?.connection === connection
        ) {
          // NOT instantly fatal. Discord bounces a fresh connection
          // through Disconnected routinely - voice-server reassignment,
          // and the DAVE encryption upgrade moments after a first join -
          // and treating that as failure made the bot join, drop out
          // after half a second, and only work on the SECOND try, which
          // rides the warm session the first one left behind. The
          // discord.js-documented recovery: a briefly-disconnected
          // connection re-enters Signalling or Connecting on its own;
          // give it that chance, then require Ready again. No audio
          // flows while Disconnected, so nothing can be captured during
          // the grace - and a connection that stays down still fails
          // closed.
          if (entry.reconnecting) {
            return;
          }
          entry.reconnecting = true;
          void (async () => {
            try {
              const grace = Math.min(5_000, this.config.connectTimeoutMs);
              await Promise.race([
                entersState(connection, VoiceConnectionStatus.Signalling, grace),
                entersState(connection, VoiceConnectionStatus.Connecting, grace),
              ]);
              await entersState(
                connection,
                VoiceConnectionStatus.Ready,
                this.config.connectTimeoutMs,
              );
            } catch {
              if (this.connections.get(guildId)?.connection === connection) {
                // Shino's call, 2026-09-01: reconnect on a network drop.
                // Fail-closed remains the capture posture - nothing is
                // recorded while down - but the SESSION survives: the
                // socket died, nobody's consent changed, so the adapter
                // rejoins the same channel with the same consent ledger
                // instead of waiting for a human to re-invite it
                // mid-raid.
                await this.#rejoinAfterDrop(guildId);
              }
              return;
            } finally {
              entry.reconnecting = false;
            }
          })();
        }
      });
      await entersState(connection, VoiceConnectionStatus.Ready, this.config.connectTimeoutMs);
      connection.receiver.speaking.on("start", (userId) => {
        this.#handleSpeakerStart(guildId, userId, connection);
      });
      this.#emit("voice.connected", {
        guildId,
        voiceChannelId: session.voiceChannelId,
        daveEncryption: true,
      });
    } catch (error) {
      // What the join looked like when it gave up. Both catches on this
      // path used to be bare, so 52 consecutive failures on 2026-09-06
      // reached the operator as the same three words and nothing else.
      const detail = joinFailureDetail(connection, error);
      // Any teardown while entersState is pending - the fail-closed path fired
      // by the stateChange listener above, most often - has already destroyed
      // this connection, and destroying it a second time throws.
      try {
        if (connection && connection.state?.status !== VoiceConnectionStatus.Destroyed) {
          connection.destroy();
        }
      } catch {
        // Already torn down by the fail-closed path; nothing left to release.
      }
      // Only the entry this call created may be dropped; a later sync may
      // already have replaced it.
      if (this.connections.get(guildId)?.connection === connection) {
        this.connections.delete(guildId);
      }
      // A session whose join failed must not come back on the next start.
      // The supervisor restarts this process after a fail-closed, the
      // restart resumed the same session, the same join failed again:
      // one member's /vem start in a channel the bot could not join
      // became 52 "restarted and rejoined" notices in seventy minutes,
      // with consent buttons on every one, and no process alive long
      // enough for /vem stop to reach. The next /vem start is a fresh
      // decision by a person, which is what a failed join deserves.
      this.sessions.forgetResumable?.(guildId);
      await this.#failClosed("voice_connection_failed", detail);
    }
  }

  #handleSpeakerStart(guildId, userId, connection) {
    const session = this.sessions.getSession(guildId);
    if (
      this.connections.get(guildId)?.connection !== connection ||
      !session ||
      session.phase !== SessionPhase.ACTIVE ||
      !this.sessions.canCapture({ guildId, userId })
    ) {
      return;
    }

    let sinkReady = false;
    try {
      sinkReady = this.audioSink.isReady() === true;
    } catch {
      sinkReady = false;
    }
    if (!sinkReady) {
      void this.#failClosed("audio_sink_unavailable");
      return;
    }

    const ssrc = connection.receiver.ssrcMap.get(userId)?.audioSSRC || null;
    let segment;
    try {
      segment = this.speakers.begin({
        guildId,
        userId,
        ssrc,
        audienceGeneration: session.generation,
        audienceRevision: session.audienceRevision,
      });
    } catch (error) {
      if (error instanceof SpeakerStateError && error.code === "speaker_already_active") {
        return;
      }
      void this.#failClosed("speaker_identity_conflict");
      return;
    }

    const stream = connection.receiver.subscribe(userId, {
      end: {
        behavior: EndBehaviorType.AfterSilence,
        duration: this.config.silenceMs,
      },
    });
    const entry = {
      stream,
      segment,
      stopReason: null,
      timer: null,
      finished: false,
    };
    this.streams.set(segment.segmentId, entry);
    entry.timer = setTimeout(() => {
      entry.stopReason = "segment_limit";
      stream.destroy();
    }, this.config.maxSegmentMs);
    entry.timer.unref?.();

    const finish = (reason) => this.#finishSegment(segment.segmentId, reason);
    stream.once("end", () => finish("silence"));
    stream.once("close", () => finish(entry.stopReason || "closed"));
    stream.once("error", () => {
      // One speaker's stream, one segment. finish() above already ends
      // it and destroys the stream, so nothing unconsented survives -
      // and tearing the whole adapter down instead meant the bot left
      // the voice channel and every member lost captions because one
      // person's audio hiccuped. Shino's live worker log, 2026-09-04,
      // shows exactly that happening a second after joining, over and
      // over. Persistent trouble still fails closed through the paths
      // that measure it: the voice connection's own error and
      // disconnect handlers.
      finish("stream_error");
    });

    const character = this.sessions.getCharacter({ guildId, userId });
    const member = this.client.guilds.cache.get(guildId)?.members?.cache?.get(userId);
    const displayName =
      member?.displayName || member?.user?.globalName || member?.user?.username || null;
    this.#emit("speaker.segment.started", { ...segment, character });
    try {
      Promise.resolve(
        this.audioSink.acceptOpusStream({
          ...segment,
          character,
          displayName,
          stream,
        }),
      ).catch(() => {
        void this.#failClosed("audio_sink_error");
      });
    } catch {
      void this.#failClosed("audio_sink_error");
    }
  }

  #finishSegment(segmentId, reason) {
    const entry = this.streams.get(segmentId);
    if (!entry || entry.finished) {
      return;
    }
    entry.finished = true;
    clearTimeout(entry.timer);
    this.streams.delete(segmentId);
    const ended = this.speakers.end(segmentId, reason);
    if (ended) {
      this.#emit("speaker.segment.ended", ended);
    }
  }

  /*
  The immediate cut behind a mid-session withdrawal. Under the old
  all-or-nothing phase a decline paused the room and #syncGuild tore
  everything down; per-speaker capture keeps the room running, so the
  withdrawer's own live stream has to be ended here, now, not at the
  next natural silence. Audio captured before this point was captured
  under consent and its caption may still arrive; audio after it never
  enters the process.
  */
  #stopSpeakerCapture(guildId, userId, reason) {
    for (const [segmentId, entry] of [...this.streams.entries()]) {
      if (entry.segment.guildId !== guildId || entry.segment.userId !== userId) {
        continue;
      }
      entry.stopReason = reason;
      entry.stream.destroy();
      this.#finishSegment(segmentId, reason);
    }
    for (const ended of this.speakers.clearSpeaker(guildId, userId, reason)) {
      this.#emit("speaker.segment.ended", ended);
    }
  }

  async #rejoinAfterDrop(guildId) {
    // Tear the dead connection down first, or #syncGuild would see a
    // connection for the right channel and no-op forever.
    this.#stopGuildCapture(guildId, "voice_reconnecting");
    this.#emit("voice.reconnecting", { guildId });
    for (const delayMs of this.reconnectDelaysMs) {
      await new Promise((resolve) => setTimeout(resolve, delayMs));
      if (this.shutdownRequested || this.gatewayFailed) {
        return;
      }
      const session = this.sessions.getSession(guildId);
      if (
        !session ||
        session.phase === SessionPhase.STOPPED ||
        session.phase === SessionPhase.AWAITING_CONSENT
      ) {
        // A human ended it while we waited - an explicit stop, or the
        // last consenter withdrawing. Those are decisions, not a network
        // drop, so stop trying. But NOT a plain PAUSED: our own
        // fail-closed from the previous attempt sets exactly that, and
        // bailing on it would abandon the rejoin after one try - which
        // is the bug this loop exists to fix.
        return;
      }
      await this.#syncGuild(guildId);
      const entry = this.connections.get(guildId);
      if (entry) {
        // A #syncGuild failure inside the loop fail-closed the capture
        // health; a successful rejoin has to say the opposite.
        this.discordHealthy = true;
        this.#refreshCaptureHealth("voice_reconnected");
        this.#emit("voice.reconnected", {
          guildId,
          voiceChannelId: entry.voiceChannelId,
        });
        return;
      }
    }
    // Every attempt failed. Now it is a real outage, and it fails closed
    // exactly as it always did - a human re-invite remains the way back.
    await this.#failClosed("voice_connection_disconnected");
  }

  #stopGuildCapture(guildId, reason) {
    for (const [segmentId, entry] of [...this.streams.entries()]) {
      if (entry.segment.guildId !== guildId) {
        continue;
      }
      entry.stopReason = reason;
      entry.stream.destroy();
      this.#finishSegment(segmentId, reason);
    }
    for (const ended of this.speakers.clearGuild(guildId, reason)) {
      this.#emit("speaker.segment.ended", ended);
    }
    const connection = this.connections.get(guildId);
    if (connection) {
      this.connections.delete(guildId);
      connection.connection.destroy();
      this.#emit("voice.disconnected", { guildId, reason });
    }
  }

  async #failClosed(reason, detail = null) {
    this.discordHealthy = false;
    this.#stopAudienceHeartbeat();
    this.sessions.setCaptureHealth(false, reason);
    for (const guildId of [...this.connections.keys()]) {
      this.#stopGuildCapture(guildId, reason);
    }
    this.#emit("health.changed", {
      component: "capture_pipeline",
      status: "blocked",
      reason,
      ...(detail || {}),
    });
  }

  #emit(type, payload) {
    if (this.outputFailed) {
      return null;
    }
    try {
      return this.emitter.emit(type, payload);
    } catch {
      this.outputFailed = true;
      this.discordHealthy = false;
      this.#stopAudienceHeartbeat();
      this.sessions.setCaptureHealth(false, "jsonl_output_unavailable");
      for (const guildId of [...this.connections.keys()]) {
        this.#stopGuildCapture(guildId, "jsonl_output_unavailable");
      }
      return null;
    }
  }
}
