import { dirname, join } from "node:path";

import { ConsentLedger } from "./consent_ledger.js";
import { MappingLedger } from "./mapping_ledger.js";
import { SessionLedger } from "./session_ledger.js";

export const SessionPhase = Object.freeze({
  ACTIVE: "active",
  AWAITING_CONSENT: "awaiting_consent",
  PAUSED: "paused",
  STOPPED: "stopped",
});

export const ConsentState = Object.freeze({
  DECLINED: "declined",
  GRANTED: "granted",
  PENDING: "pending",
});

const WOW_CLASSES = new Set([
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
]);
const WOW_ROLES = new Set(["damage", "healer", "tank"]);
const ID_PATTERN = /^\d{1,20}$/;
const CHARACTER_PATTERN = /^[\p{L}][\p{L}'-]{1,23}$/u;
const REALM_PATTERN = /^[\p{L}\p{N}][\p{L}\p{N} '-]{0,31}$/u;

export class SessionStateError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "SessionStateError";
    this.code = code;
  }
}

function requireId(value, field) {
  if (typeof value !== "string" || !ID_PATTERN.test(value)) {
    throw new SessionStateError(`invalid_${field}`, `${field} is invalid.`);
  }
  return value;
}

function uniqueIds(values, field) {
  if (!Array.isArray(values)) {
    throw new SessionStateError(`invalid_${field}`, `${field} must be a list.`);
  }
  return [...new Set(values.map((value) => requireId(value, field)))];
}

function cloneMapping(mapping) {
  return mapping ? { ...mapping } : null;
}

export class SessionManager {
  constructor({
    clock = () => new Date(),
    captureHealthy = false,
    consentLedger = null,
    mappingLedger = null,
    sessionLedger = null,
  } = {}) {
    this.clock = clock;
    this.captureHealthy = Boolean(captureHealthy);
    this.captureHealthReason = this.captureHealthy ? null : "capture_pipeline_unavailable";
    this.sessions = new Map();
    this.nextSessionGeneration = 1;
    this.mappings = new Map();
    // Optional: without one, consent lasts exactly as long as the session,
    // which is the behaviour every existing caller already expects.
    this.consentLedger = consentLedger;
    // Where the adapter was, so a restart can put it back. Never consent.
    this.sessionLedger = sessionLedger;
    // Same deal for character mappings. Seeded eagerly rather than read on
    // demand because the audio path stamps each segment's character from
    // this.mappings synchronously - the restore has to be done before the
    // first speaker opens their mouth, not discovered lazily after.
    this.mappingLedger = mappingLedger;
    if (mappingLedger) {
      for (const { guildId, userId, mapping } of mappingLedger.recallAll()) {
        const guildMappings = this.mappings.get(guildId) || new Map();
        guildMappings.set(userId, mapping);
        this.mappings.set(guildId, guildMappings);
      }
    }
  }

  /*
  What a participant starts as when they appear.

  A remembered DECLINE is honoured on sight, so somebody who said no is not
  asked again and is never captured by silence. A remembered GRANT saves the
  regular group re-consenting nightly. No record at all is PENDING, which
  pauses capture - the safe direction.
  */
  #initialConsent(guildId, userId) {
    const remembered = this.consentLedger?.recall(guildId, userId) ?? null;
    if (remembered === "granted") {
      return ConsentState.GRANTED;
    }
    if (remembered === "declined") {
      return ConsentState.DECLINED;
    }
    return ConsentState.PENDING;
  }

  setCaptureHealth(healthy, reason = null) {
    this.captureHealthy = Boolean(healthy);
    this.captureHealthReason = this.captureHealthy ? null : reason || "capture_pipeline_unavailable";
    for (const session of this.sessions.values()) {
      this.#recomputePhase(session);
    }
    return this.listSessions();
  }

  startSession({ guildId, voiceChannelId, controlChannelId, startedBy, participantIds }) {
    guildId = requireId(guildId, "guild_id");
    voiceChannelId = requireId(voiceChannelId, "voice_channel_id");
    controlChannelId = requireId(controlChannelId, "control_channel_id");
    startedBy = requireId(startedBy, "started_by");
    const participants = uniqueIds(participantIds, "participant_id");
    if (!participants.includes(startedBy)) {
      participants.push(startedBy);
    }

    const existing = this.sessions.get(guildId);
    if (existing && existing.phase !== SessionPhase.STOPPED) {
      throw new SessionStateError("session_exists", "A caption session is already open here.");
    }

    const now = this.clock().toISOString();
    const initialParticipants = participants.map((userId) => [
      userId,
      { consent: this.#initialConsent(guildId, userId), joinedAt: now },
    ]);
    const session = {
      generation: this.nextSessionGeneration++,
      audienceRevision: 1,
      guildId,
      voiceChannelId,
      controlChannelId,
      startedBy,
      startedAt: now,
      stoppedAt: null,
      stoppedBy: null,
      phase: SessionPhase.AWAITING_CONSENT,
      pauseReason: "consent_required",
      participants: new Map(initialParticipants),
      // Explicit answers for this caption session. This is intentionally
      // separate from the durable ledger: an unremembered answer survives a
      // brief voice disconnect, but it is discarded when the session ends.
      transientConsent: new Map(),
      // The last explicit retention result keeps the private Discord modal
      // honest after a failed disk write. It is never serialized.
      consentRetention: new Map(),
    };
    this.sessions.set(guildId, session);
    this.#recomputePhase(session);
    const snapshot = this.#snapshot(session);
    this.sessionLedger?.remember(snapshot);
    return snapshot;
  }

  /**
   * True immediately after setConsent/forgetConsent when every ledger
   * write they attempted reached disk. Same-tick contract: read it
   * right after the call, nowhere else. A separate flag rather than a
   * changed return shape because thirty-odd call sites pin these
   * returns, and a reshape that big is where honest signals get lost.
   */
  consentWritePersisted = true;

  #recordConsent(guildId, userId, state, remember) {
    if (remember) {
      const saved = this.consentLedger?.remember(guildId, userId, state) ?? false;
      if (!saved) {
        this.consentWritePersisted = false;
        // ConsentLedger accepts into memory before its durable write. Do not
        // let that failed answer leak into a later session or get saved by a
        // later clean close after telling the user it was not remembered.
        this.consentLedger?.forget(guildId, userId);
      }
      return;
    }
    const forgotten = this.consentLedger?.forget(guildId, userId)
      ?? { persisted: true };
    if (!forgotten.persisted) {
      this.consentWritePersisted = false;
    }
  }

  setConsent({ guildId, userId, granted, remember = true }) {
    guildId = requireId(guildId, "guild_id");
    userId = requireId(userId, "user_id");
    if (typeof granted !== "boolean" || typeof remember !== "boolean") {
      throw new SessionStateError(
        "invalid_consent_choice",
        "Consent and remember choices must be true or false.",
      );
    }
    this.consentWritePersisted = true;
    const state = granted ? ConsentState.GRANTED : ConsentState.DECLINED;
    const session = this.sessions.get(guildId);
    if (!session || session.phase === SessionPhase.STOPPED) {
      if (granted) {
        throw new SessionStateError("no_session", "No caption session is open here.");
      }
      // A withdrawal with nothing running is still worth accepting: it is
      // now on record for the next session.
      this.#recordConsent(guildId, userId, state, remember);
      return null;
    }

    const participant = session.participants.get(userId);
    if (!participant) {
      if (granted) {
        throw new SessionStateError(
          "not_participant",
          "Join the session voice channel before recording consent.",
        );
      }
    }
    // Validate presence before retaining a grant. A rejected `/consent`
    // command must never pre-authorize somebody for a later session.
    session.transientConsent.set(userId, state);
    if (participant) {
      participant.consent = state;
    }
    this.#recomputePhase(session);
    // The live choice takes effect before persistence. A slow or failed disk
    // cannot delay a withdrawal, and an unsaved grant remains scoped to this
    // live session.
    this.#recordConsent(guildId, userId, state, remember);
    session.consentRetention.set(
      userId,
      remember && this.consentWritePersisted,
    );
    return this.#snapshot(session);
  }

  /** Erase a remembered answer, leaving the user as if never asked. */
  forgetConsent({ guildId, userId }) {
    guildId = requireId(guildId, "guild_id");
    userId = requireId(userId, "user_id");
    this.consentWritePersisted = true;
    const session = this.sessions.get(guildId);
    let transientExisted = false;
    if (session && session.phase !== SessionPhase.STOPPED) {
      transientExisted = session.transientConsent.delete(userId);
      session.consentRetention.delete(userId);
      const participant = session.participants.get(userId);
      if (participant) {
        // Forgetting inside a live session drops back to PENDING, which
        // pauses capture. It must never read as a grant.
        participant.consent = ConsentState.PENDING;
        this.#recomputePhase(session);
      }
    }
    // As with decline, stop live capture before touching the persistence
    // boundary so a storage failure cannot leave this session authorized.
    const forgotten = this.consentLedger?.forget(guildId, userId)
      ?? { existed: false, persisted: true };
    const consentExisted = transientExisted || forgotten.existed;
    if (!forgotten.persisted) {
      this.consentWritePersisted = false;
    }
    // Forgetting is complete: the character mapping names this person's
    // characters, so it goes with the consent record rather than lingering
    // as a trace of somebody who asked to be forgotten. clearCharacter
    // erases the ledger copy too on this branch of the merge.
    const mappingExisted = this.clearCharacter({ guildId, userId });
    const mappingPersisted = this.mappingWritePersisted;
    return {
      existed: consentExisted || mappingExisted,
      consentExisted,
      mappingExisted,
      consentPersisted: this.consentWritePersisted,
      mappingPersisted,
      persisted: this.consentWritePersisted && mappingPersisted,
      session: session ? this.#snapshot(session) : null,
    };
  }

  updateParticipants({ guildId, participantIds }) {
    const session = this.#liveSession(guildId);
    const nextIds = new Set(uniqueIds(participantIds, "participant_id"));
    const now = this.clock().toISOString();
    let changed = nextIds.size !== session.participants.size;

    for (const userId of session.participants.keys()) {
      if (!nextIds.has(userId)) {
        session.participants.delete(userId);
        changed = true;
      }
    }
    for (const userId of nextIds) {
      if (!session.participants.has(userId)) {
        changed = true;
        session.participants.set(userId, {
          consent:
            session.transientConsent.get(userId)
            ?? this.#initialConsent(session.guildId, userId),
          joinedAt: now,
        });
      }
    }
    if (changed) {
      session.audienceRevision += 1;
    }
    this.#recomputePhase(session);
    const snapshot = this.#snapshot(session);
    this.sessionLedger?.remember(snapshot);
    return snapshot;
  }

  /**
   * Sessions a fresh process may put back: remembered, and saved within
   * maxAgeMs of now. Anything older is forgotten on the spot - a restart
   * takes seconds, and a session hours old belongs to a raid that ended.
   */
  resumableSessions({ maxAgeMs }) {
    if (!this.sessionLedger) {
      return [];
    }
    const now = this.clock().getTime();
    const fresh = [];
    for (const entry of this.sessionLedger.recallAll()) {
      const age = now - Date.parse(entry.savedAt);
      if (age <= maxAgeMs) {
        fresh.push(entry);
      } else {
        this.sessionLedger.forget(entry.guildId);
      }
    }
    return fresh;
  }

  forgetResumable(guildId) {
    return this.sessionLedger?.forget(guildId) ?? true;
  }

  stopSession({ guildId, requestedBy }) {
    const session = this.#liveSession(guildId);
    requestedBy = requireId(requestedBy, "requested_by");
    if (requestedBy !== session.startedBy && !session.participants.has(requestedBy)) {
      throw new SessionStateError(
        "stop_not_allowed",
        "Only a current participant or the session starter can stop captions.",
      );
    }
    session.phase = SessionPhase.STOPPED;
    session.audienceRevision += 1;
    session.pauseReason = "stopped";
    session.stoppedAt = this.clock().toISOString();
    session.stoppedBy = requestedBy;
    const snapshot = this.#snapshot(session);
    session.transientConsent.clear();
    session.consentRetention.clear();
    // A deliberate stop must not come back after the next restart.
    this.sessionLedger?.forget(guildId);
    return snapshot;
  }

  getConsentStatus({ guildId, userId }) {
    guildId = requireId(guildId, "guild_id");
    userId = requireId(userId, "user_id");
    const session = this.sessions.get(guildId);
    const live = session && session.phase !== SessionPhase.STOPPED ? session : null;
    const participant = live?.participants.get(userId);
    const remembered = this.consentLedger?.recall(guildId, userId) ?? null;
    return Object.freeze({
      state:
        participant?.consent
        ?? live?.transientConsent.get(userId)
        ?? (remembered === "granted" || remembered === "declined"
          ? remembered
          : ConsentState.PENDING),
      remembered: live?.consentRetention.has(userId)
        ? live.consentRetention.get(userId)
        : remembered !== null,
    });
  }

  /** Same-tick durability result for mapCharacter/clearCharacter. */
  mappingWritePersisted = true;

  mapCharacter({ guildId, userId, characterName, realm = null, wowClass, role }) {
    guildId = requireId(guildId, "guild_id");
    userId = requireId(userId, "user_id");
    const normalizedName = characterName?.trim();
    const normalizedRealm = realm?.trim() || null;
    if (!normalizedName || !CHARACTER_PATTERN.test(normalizedName)) {
      throw new SessionStateError(
        "invalid_character_name",
        "Character name must be 2-24 letters and may include apostrophe or hyphen.",
      );
    }
    if (normalizedRealm && !REALM_PATTERN.test(normalizedRealm)) {
      throw new SessionStateError("invalid_realm", "Realm name is invalid.");
    }
    if (!WOW_CLASSES.has(wowClass)) {
      throw new SessionStateError("invalid_class", "WoW class is invalid.");
    }
    if (!WOW_ROLES.has(role)) {
      throw new SessionStateError("invalid_role", "WoW role is invalid.");
    }

    const guildMappings = this.mappings.get(guildId) || new Map();
    const mapping = Object.freeze({
      characterName: normalizedName,
      realm: normalizedRealm,
      wowClass,
      role,
    });
    guildMappings.set(userId, mapping);
    this.mappings.set(guildId, guildMappings);
    this.mappingWritePersisted = true;
    if (this.mappingLedger) {
      this.mappingWritePersisted =
        this.mappingLedger.remember(guildId, userId, mapping) === true;
    }
    return cloneMapping(mapping);
  }

  clearCharacter({ guildId, userId }) {
    guildId = requireId(guildId, "guild_id");
    userId = requireId(userId, "user_id");
    this.mappingWritePersisted = true;
    const guildMappings = this.mappings.get(guildId);
    const removed = guildMappings?.delete(userId) || false;
    if (guildMappings?.size === 0) {
      this.mappings.delete(guildId);
    }
    // Either copy counting as "it existed" keeps unmap honest across a
    // restart, where the ledger remembers a mapping memory never saw.
    const rawForgotten = this.mappingLedger?.forget(guildId, userId)
      ?? { existed: false, persisted: true };
    const forgotten =
      typeof rawForgotten === "object" && rawForgotten !== null
        ? rawForgotten
        : { existed: Boolean(rawForgotten), persisted: true };
    if (!forgotten.persisted) {
      this.mappingWritePersisted = false;
    }
    return removed || forgotten.existed;
  }

  getCharacter({ guildId, userId }) {
    return cloneMapping(this.mappings.get(guildId)?.get(userId));
  }

  canCapture({ guildId, userId }) {
    const session = this.sessions.get(guildId);
    return Boolean(
      this.captureHealthy &&
        session?.phase === SessionPhase.ACTIVE &&
        session.participants.get(userId)?.consent === ConsentState.GRANTED,
    );
  }

  getSession(guildId) {
    const session = this.sessions.get(guildId);
    return session ? this.#snapshot(session) : null;
  }

  getSessionBinding(guildId) {
    const session = this.sessions.get(guildId);
    if (!session || session.phase === SessionPhase.STOPPED) {
      return null;
    }
    return Object.freeze({
      generation: session.generation,
      controlChannelId: session.controlChannelId,
    });
  }

  matchesSessionBinding(guildId, binding) {
    const session = this.sessions.get(guildId);
    return Boolean(
      session &&
        session.phase !== SessionPhase.STOPPED &&
        binding &&
        session.generation === binding.generation &&
        session.controlChannelId === binding.controlChannelId,
    );
  }

  listSessions() {
    return [...this.sessions.values()].map((session) => this.#snapshot(session));
  }

  close() {
    return this.consentLedger?.close?.() ?? true;
  }

  #liveSession(guildId) {
    guildId = requireId(guildId, "guild_id");
    const session = this.sessions.get(guildId);
    if (!session || session.phase === SessionPhase.STOPPED) {
      throw new SessionStateError("session_missing", "No caption session is open here.");
    }
    return session;
  }

  #recomputePhase(session) {
    if (session.phase === SessionPhase.STOPPED) {
      return;
    }
    if (!this.captureHealthy) {
      session.phase = SessionPhase.PAUSED;
      session.pauseReason = this.captureHealthReason;
      return;
    }
    const participants = [...session.participants.values()];
    if (participants.length === 0) {
      session.phase = SessionPhase.PAUSED;
      session.pauseReason = "voice_channel_empty";
      return;
    }
    // Per-speaker consent: one grant is enough to run, because only
    // granted speakers are ever subscribed (canCapture gates each
    // stream individually). A decline or a silence no longer pauses
    // the room - it keeps exactly that person uncaptured, which also
    // makes "no" a choice with no cost to anyone else. The all-or-
    // nothing gate this replaces made one holdout equal zero captions
    // for the person the tool exists for.
    if (!participants.some(({ consent }) => consent === ConsentState.GRANTED)) {
      session.phase = SessionPhase.AWAITING_CONSENT;
      session.pauseReason = "consent_required";
      return;
    }
    session.phase = SessionPhase.ACTIVE;
    session.pauseReason = null;
  }

  #snapshot(session) {
    return Object.freeze({
      generation: session.generation,
      audienceRevision: session.audienceRevision,
      guildId: session.guildId,
      voiceChannelId: session.voiceChannelId,
      controlChannelId: session.controlChannelId,
      startedBy: session.startedBy,
      startedAt: session.startedAt,
      stoppedAt: session.stoppedAt,
      stoppedBy: session.stoppedBy,
      phase: session.phase,
      pauseReason: session.pauseReason,
      participants: Object.freeze(
        [...session.participants.entries()].map(([userId, participant]) =>
          Object.freeze({
            userId,
            consent: participant.consent,
            joinedAt: participant.joinedAt,
            character: this.getCharacter({ guildId: session.guildId, userId }),
          }),
        ),
      ),
    });
  }
}

/*
The one constructor production is allowed to use.

Consent persistence was implemented, documented and tested well before it was
ever connected: index.js built a runtime without a session manager, the
runtime defaulted one, and that default carried a null ledger. Because every
call site here goes through `this.consentLedger?.`, the whole feature no-opped
in silence - the group re-consented every session while the tests stayed
green, because the tests build a ledger directly.

So the wiring gets a named entry point rather than a default. A config that
cannot say where consent lives is a programming error, not a reason to fall
back to remembering nothing.
*/
export function createProductionSessionManager(config) {
  if (
    !config ||
    !(config.consentStatePath === null || typeof config.consentStatePath === "string") ||
    !(config.mappingStatePath === null || typeof config.mappingStatePath === "string")
  ) {
    throw new TypeError("A validated Discord adapter configuration is required.");
  }
  return new SessionManager({
    consentLedger: new ConsentLedger({ path: config.consentStatePath }),
    mappingLedger: new MappingLedger({ path: config.mappingStatePath }),
    // Beside the consent ledger, so the companion supplies nothing new
    // and a memory-only test configuration stays memory-only.
    sessionLedger: new SessionLedger({
      path: config.consentStatePath
        ? join(dirname(config.consentStatePath), "sessions.json")
        : null,
    }),
  });
}
