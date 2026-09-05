import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

/*
Remembers who plays what, so /vem map survives a bot restart.

A mapping names a real person's character, class and role, which makes it
personal data with the same handling rules as the consent ledger beside it:
local file, atomic writes, forgetting is complete. It also feeds focus mode,
which silences everyone whose role it does not know - so losing this file on
every restart meant focus mode started every session muting the whole group.

One rule is deliberately INVERTED from the consent ledger. There, a damaged
file must start empty because a phantom grant is the dangerous failure. Here
the dangerous failure is a stale or fabricated entry steering focus mode, so
every loaded entry is re-validated field by field and anything that does not
hold is dropped - but entries that do hold are kept, because re-asking a
whole guild to re-map is exactly the friction this file exists to end.
*/

export const MAPPING_LEDGER_VERSION = 1;

// Mirrors session_manager.js. Kept locally because importing them from there
// would make the two modules circular; the test suite pins the two sets
// against each other so they cannot drift apart silently.
const VALID_CLASSES = new Set([
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
const VALID_ROLES = new Set(["damage", "healer", "tank"]);
const ID_PATTERN = /^\d{1,20}$/;
const CHARACTER_PATTERN = /^[\p{L}][\p{L}'-]{1,23}$/u;
const REALM_PATTERN = /^[\p{L}\p{N}][\p{L}\p{N} '-]{0,31}$/u;

function key(guildId, userId) {
  return `${guildId}:${userId}`;
}

function validEntry(value) {
  if (!value || typeof value !== "object") {
    return null;
  }
  const { characterName, realm, wowClass, role, mappedAt } = value;
  if (typeof characterName !== "string" || !CHARACTER_PATTERN.test(characterName)) {
    return null;
  }
  if (realm !== null && (typeof realm !== "string" || !REALM_PATTERN.test(realm))) {
    return null;
  }
  if (!VALID_CLASSES.has(wowClass) || !VALID_ROLES.has(role)) {
    return null;
  }
  return Object.freeze({
    characterName,
    realm: realm ?? null,
    wowClass,
    role,
    mappedAt: typeof mappedAt === "string" ? mappedAt : null,
  });
}

export class MappingLedger {
  /**
   * @param {object} options
   * @param {string|null} options.path Where to persist. null keeps it in
   *   memory only, which is what the tests and a first run use.
   */
  constructor({ path = null, now = () => new Date() } = {}) {
    this.path = path;
    this.now = now;
    this.entries = new Map();
    // Structurally valid JSON can still contain an entry that must never
    // reach captions. Preserve those raw values for lossless round-tripping
    // and self-erasure without treating them as usable mappings.
    this.opaqueEntries = new Map();
    this.loadTrusted = !path;
    if (path) {
      this.#load();
    }
  }

  #load() {
    let raw;
    try {
      raw = readFileSync(this.path, "utf8");
    } catch (error) {
      if (error?.code === "ENOENT") {
        this.entries = new Map();
        this.opaqueEntries = new Map();
        this.loadTrusted = true;
        return true;
      }
      this.loadTrusted = false;
      return false;
    }
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      this.loadTrusted = false;
      return false;
    }
    if (
      !parsed ||
      parsed.version !== MAPPING_LEDGER_VERSION ||
      typeof parsed.entries !== "object" ||
      parsed.entries === null
    ) {
      this.loadTrusted = false;
      return false;
    }
    const loaded = new Map();
    const opaque = new Map();
    for (const [entryKey, value] of Object.entries(parsed.entries)) {
      const [guildId, userId] = entryKey.split(":");
      const entry = validEntry(value);
      if (
        entryKey === key(guildId, userId) &&
        ID_PATTERN.test(guildId ?? "") &&
        ID_PATTERN.test(userId ?? "") &&
        entry
      ) {
        loaded.set(entryKey, entry);
      } else {
        opaque.set(entryKey, value);
      }
    }
    this.entries = loaded;
    this.opaqueEntries = opaque;
    this.loadTrusted = true;
    return true;
  }

  #ensureTrusted() {
    return this.loadTrusted || this.#load();
  }

  #save() {
    if (!this.path) {
      return true;
    }
    if (!this.loadTrusted) {
      return false;
    }
    try {
      const payload = {
        version: MAPPING_LEDGER_VERSION,
        updatedAt: this.now().toISOString(),
        entries: Object.fromEntries(
          new Map([...this.opaqueEntries, ...this.entries]),
        ),
      };
      mkdirSync(dirname(this.path), { recursive: true });
      // Written beside the target and renamed, so an interrupted write
      // cannot leave a half-file that quietly drops half a roster.
      const temporary = join(dirname(this.path), `.mapping-${process.pid}.tmp`);
      writeFileSync(temporary, JSON.stringify(payload, null, 2), "utf8");
      renameSync(temporary, this.path);
      return true;
    } catch {
      // The live session can still use the in-memory mapping. The caller
      // receives false so it never claims the mapping survived a restart.
      return false;
    }
  }

  /** The stored mapping, or null. Frozen; callers clone before mutating. */
  recall(guildId, userId) {
    return this.entries.get(key(guildId, userId)) ?? null;
  }

  /** Every stored mapping, for seeding a fresh session manager. */
  recallAll() {
    const result = [];
    for (const [entryKey, entry] of this.entries) {
      const [guildId, userId] = entryKey.split(":");
      result.push({ guildId, userId, mapping: entry });
    }
    return result;
  }

  /** Accept into memory and report whether the mapping is now durable. */
  remember(guildId, userId, mapping) {
    if (!this.#ensureTrusted()) {
      return false;
    }
    let mappedAt;
    try {
      mappedAt = this.now().toISOString();
    } catch {
      return false;
    }
    const entry = validEntry({
      ...mapping,
      mappedAt,
    });
    if (!entry) {
      return null;
    }
    const entryKey = key(guildId, userId);
    const previous = this.entries.get(entryKey);
    const previousOpaque = this.opaqueEntries.get(entryKey);
    this.entries.set(entryKey, entry);
    this.opaqueEntries.delete(entryKey);
    const persisted = this.#save();
    if (!persisted) {
      // SessionManager keeps the live mapping separately. Rolling this ledger
      // back prevents a later successful write for somebody else from making
      // the failed mapping durable without the user being told.
      if (previous) {
        this.entries.set(entryKey, previous);
      } else {
        this.entries.delete(entryKey);
      }
      if (previousOpaque !== undefined) {
        this.opaqueEntries.set(entryKey, previousOpaque);
      }
    }
    return persisted;
  }

  /** Erase the record entirely - forgetting must be complete. */
  forget(guildId, userId) {
    if (!this.#ensureTrusted()) {
      return { existed: false, persisted: false };
    }
    const entryKey = key(guildId, userId);
    const previous = this.entries.get(entryKey);
    const previousOpaque = new Map(
      [...this.opaqueEntries].filter(([opaqueKey]) => {
        const parts = opaqueKey.split(":");
        return parts[0] === guildId && parts[1] === userId;
      }),
    );
    const validExisted = this.entries.delete(entryKey);
    for (const opaqueKey of previousOpaque.keys()) {
      this.opaqueEntries.delete(opaqueKey);
    }
    const opaqueExisted = previousOpaque.size > 0;
    const existed = validExisted || opaqueExisted;
    const persisted = existed ? this.#save() : true;
    if (existed && !persisted) {
      // Keep a retryable copy in memory. If the entry vanished here,
      // a second unmap would see nothing and falsely claim the stale disk
      // copy had been erased.
      if (previous) {
        this.entries.set(entryKey, previous);
      }
      for (const [opaqueKey, value] of previousOpaque) {
        this.opaqueEntries.set(opaqueKey, value);
      }
    }
    return { existed, persisted };
  }
}
