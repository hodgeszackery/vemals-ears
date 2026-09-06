import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

// Where the adapter WAS - guild, voice channel, control channel, who started
// it and who was there - so a process restart (a deploy, a crash) can put
// the bot back without a human re-running /vem start mid-raid.
//
// This is deliberately NOT consent state. Restoring runs through
// SessionManager.startSession exactly like a fresh command would, and every
// participant's consent comes from the consent ledger at that moment. If
// this file is lost or corrupt the only cost is one manual /vem start;
// it can never cause anyone to be captured who did not consent. That is
// why a bad file simply reads as empty instead of blocking startup the way
// an untrusted consent ledger does.
export const SESSION_LEDGER_VERSION = 1;

const ID_PATTERN = /^\d{1,20}$/;
const MAX_PARTICIPANTS = 512;

// Consent prompt nonces are randomUUID() strings. Anything else reads as
// "no prompt remembered", which resume answers with a fresh nonce.
const NONCE_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function isId(value) {
  return typeof value === "string" && ID_PATTERN.test(value);
}

function validEntry(value) {
  if (!value || typeof value !== "object") {
    return null;
  }
  const {
    voiceChannelId,
    controlChannelId,
    startedBy,
    participantIds,
    savedAt,
    consentPromptNonce,
  } = value;
  if (![voiceChannelId, controlChannelId, startedBy].every(isId)) {
    return null;
  }
  if (
    !Array.isArray(participantIds) ||
    participantIds.length > MAX_PARTICIPANTS ||
    !participantIds.every(isId)
  ) {
    return null;
  }
  if (typeof savedAt !== "string" || Number.isNaN(Date.parse(savedAt))) {
    return null;
  }
  return {
    voiceChannelId,
    controlChannelId,
    startedBy,
    participantIds: [...new Set(participantIds)],
    savedAt,
    consentPromptNonce:
      typeof consentPromptNonce === "string" && NONCE_PATTERN.test(consentPromptNonce)
        ? consentPromptNonce
        : null,
  };
}

export class SessionLedger {
  constructor({ path = null, now = () => new Date() } = {}) {
    this.path = path;
    this.now = now;
    this.entries = new Map();
    if (path) {
      this.#load();
    }
  }

  #load() {
    let raw;
    try {
      raw = readFileSync(this.path, "utf8");
    } catch {
      this.entries = new Map();
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      this.entries = new Map();
      return;
    }
    const loaded = new Map();
    if (
      parsed &&
      parsed.version === SESSION_LEDGER_VERSION &&
      parsed.sessions &&
      typeof parsed.sessions === "object"
    ) {
      for (const [guildId, value] of Object.entries(parsed.sessions)) {
        const entry = validEntry(value);
        if (isId(guildId) && entry) {
          loaded.set(guildId, entry);
        }
      }
    }
    this.entries = loaded;
  }

  #save() {
    if (!this.path) {
      return true;
    }
    try {
      const payload = {
        version: SESSION_LEDGER_VERSION,
        updatedAt: this.now().toISOString(),
        sessions: Object.fromEntries(this.entries),
      };
      mkdirSync(dirname(this.path), { recursive: true });
      const temporary = join(dirname(this.path), `.sessions-${process.pid}.tmp`);
      writeFileSync(temporary, JSON.stringify(payload, null, 2), "utf8");
      renameSync(temporary, this.path);
      return true;
    } catch {
      return false;
    }
  }

  /** Record a live session from its SessionManager snapshot. */
  remember(session) {
    if (!session || !isId(session.guildId)) {
      return false;
    }
    const entry = validEntry({
      voiceChannelId: session.voiceChannelId,
      controlChannelId: session.controlChannelId,
      startedBy: session.startedBy,
      participantIds: (session.participants || []).map(({ userId }) => userId),
      savedAt: this.now().toISOString(),
      consentPromptNonce: session.consentPromptNonce ?? null,
    });
    if (!entry) {
      return false;
    }
    this.entries.set(session.guildId, entry);
    return this.#save();
  }

  forget(guildId) {
    if (!this.entries.delete(guildId)) {
      return true;
    }
    return this.#save();
  }

  recallAll() {
    return [...this.entries.entries()].map(([guildId, entry]) => ({
      guildId,
      ...entry,
      participantIds: [...entry.participantIds],
    }));
  }
}
