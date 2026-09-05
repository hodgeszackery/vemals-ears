import { randomUUID } from "node:crypto";
import {
  chmodSync,
  closeSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join } from "node:path";

/*
Remembers who has consented to being captioned, so the same group does not
re-consent every session.

The rules here are the whole reason persisting consent is defensible at all,
so they are worth stating plainly:

  A remembered DECLINE outranks everything. Somebody who said no stays no on
  every rejoin until they personally say otherwise. Silence is never taken
  as a change of mind.

  Revocation never requires being in the room. You can withdraw while the
  bot is running, while it is idle, or after you have left the channel -
  needing to be present to say no would make consent a trap rather than a
  choice.

  Nothing is remembered for a user who never answered. An absent record is
  PENDING, which pauses capture, which is the safe direction.

  Forgetting is available and complete. /vem forget erases the record
  rather than flipping it, so a user can leave no trace at all.
*/

export const LEDGER_VERSION = 1;
const MAX_LEDGER_BYTES = 1_048_576;
const MAX_LEDGER_ENTRIES = 10_000;
// A temp file younger than this may belong to a write happening right
// now, in this process or another.
const ABANDONED_WRITE_MS = 60 * 60 * 1000;
const MAX_SWEEP_FILES = 500;
const CONSENT_KEY = /^\d{1,20}:\d{1,20}$/;

export class ConsentStateSafetyError extends Error {
  constructor(message) {
    super(message);
    this.name = "ConsentStateSafetyError";
    this.code = "consent_state_unavailable";
  }
}

function syncDirectory(directory) {
  if (process.platform === "win32") {
    return;
  }
  let descriptor = null;
  try {
    descriptor = openSync(directory, "r");
    fsyncSync(descriptor);
  } finally {
    if (descriptor !== null) {
      closeSync(descriptor);
    }
  }
}

function validatePrivateFile(path) {
  const information = lstatSync(path);
  if (!information.isFile() || information.isSymbolicLink()) {
    throw new ConsentStateSafetyError("Consent state must be a private regular file.");
  }
  if (process.platform !== "win32") {
    chmodSync(path, 0o600);
  }
  return information;
}

function writeAtomicPrivateFile(path, contents) {
  const directory = dirname(path);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const temporary = join(directory, `.consent-${process.pid}-${randomUUID()}.tmp`);
  let descriptor = null;
  try {
    descriptor = openSync(temporary, "wx", 0o600);
    writeFileSync(descriptor, contents, { encoding: "utf8" });
    fsyncSync(descriptor);
    closeSync(descriptor);
    descriptor = null;
    renameSync(temporary, path);
    if (process.platform !== "win32") {
      chmodSync(path, 0o600);
    }
    syncDirectory(directory);
  } finally {
    if (descriptor !== null) {
      try {
        closeSync(descriptor);
      } catch {
        // Cleanup continues below.
      }
    }
    try {
      rmSync(temporary, { force: true });
    } catch {
      // A stale 0600 temporary file is never loaded.
    }
  }
}

function key(guildId, userId) {
  return `${guildId}:${userId}`;
}

export class ConsentLedger {
  /**
   * @param {object} options
   * @param {string|null} options.path Where to persist. null keeps it in
   *   memory only, which is what the tests and a first run use.
   */
  constructor({ path = null, now = () => new Date() } = {}) {
    if (path !== null && (typeof path !== "string" || !isAbsolute(path))) {
      throw new TypeError("Consent state path must be an absolute file path.");
    }
    this.path = path;
    this.now = now;
    this.entries = new Map();
    this.closed = false;
    this.runMarkerPath = path ? `${path}.dirty` : null;
    this.runId = randomUUID();
    if (path) {
      // A dirty marker means the previous run did not shut down cleanly.
      // Only when it also says the last write never landed might a grant
      // on disk already have been withdrawn; those grants are dropped and
      // their owners asked again.
      //
      // Declines are kept. They are the protective answer, dropping one
      // is the failure that matters, and the file is rewritten wholesale
      // on the next save - so skipping the load entirely did not merely
      // ignore the old answers, it erased them, a few at a time, until
      // the ledger was empty. That is what made consent persistence look
      // like it was not working at all.
      //
      // Shino, 2026-09-01: "Everyone has to reconsent when the discord bot
      // disconnects." Every unclean exit - a disconnect that failed closed
      // and was restarted by the worker, a kill, a crash - used to drop
      // every grant on the next start, because the marker only said "a
      // run was in progress", never whether the file on disk was faithful.
      // The marker now carries that: every write flips it to unsynced
      // first and back to synced after the atomic save lands, so a run
      // that died between writes loads everything, and only a run whose
      // LAST write never reached disk loads declines only.
      const prior = this.#beginRun();
      this.#load({ declinesOnly: prior.dirty && !prior.synced });
      this.#sweepAbandonedWrites();
    }
  }

  /** What the previous run's marker claims about the file on disk. */
  #markerSaysSynced() {
    try {
      const parsed = JSON.parse(readFileSync(this.runMarkerPath, "utf8"));
      return Boolean(parsed) && parsed.synced === true;
    } catch {
      return false;
    }
  }

  /**
   * Record whether the ledger file currently equals memory. In place and
   * fsynced; the marker is a hint about one file, and a torn write parses
   * as "not synced", which is the safe reading.
   */
  #writeMarker(synced) {
    if (!this.runMarkerPath) {
      return true;
    }
    let descriptor = null;
    try {
      descriptor = openSync(this.runMarkerPath, "w", 0o600);
      writeFileSync(
        descriptor,
        JSON.stringify({ version: 1, runId: this.runId, synced: synced === true }),
        { encoding: "utf8" },
      );
      fsyncSync(descriptor);
      closeSync(descriptor);
      descriptor = null;
      return true;
    } catch {
      if (descriptor !== null) {
        try {
          closeSync(descriptor);
        } catch {
          // Nothing more to release.
        }
      }
      return false;
    }
  }

  #beginRun() {
    const directory = dirname(this.runMarkerPath);
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    try {
      validatePrivateFile(this.runMarkerPath);
      const prior = { dirty: true, synced: this.#markerSaysSynced() };
      // Take the marker over: our run id, and synced, because what we are
      // about to load is exactly what is on disk.
      if (!this.#writeMarker(true)) {
        throw new ConsentStateSafetyError("Consent state could not be marked dirty.");
      }
      return prior;
    } catch (error) {
      if (error.code !== "ENOENT") {
        throw error instanceof ConsentStateSafetyError
          ? error
          : new ConsentStateSafetyError("Consent state run marker is unavailable.");
      }
    }

    let descriptor = null;
    try {
      descriptor = openSync(this.runMarkerPath, "wx", 0o600);
      writeFileSync(
        descriptor,
        JSON.stringify({ version: 1, runId: this.runId, synced: true }),
        { encoding: "utf8" },
      );
      fsyncSync(descriptor);
      closeSync(descriptor);
      descriptor = null;
      syncDirectory(directory);
      return { dirty: false, synced: true };
    } catch (error) {
      if (descriptor !== null) {
        try {
          closeSync(descriptor);
        } catch {
          // The adapter will refuse to load grants below.
        }
      }
      if (error.code === "EEXIST") {
        try {
          validatePrivateFile(this.runMarkerPath);
          return { dirty: true, synced: this.#markerSaysSynced() };
        } catch {
          // Fall through to the fail-closed startup error.
        }
      }
      throw new ConsentStateSafetyError("Consent state could not be marked dirty.");
    }
  }

  /**
   * Remove temp files left by a write that was killed before its rename.
   *
   * The atomic write cleans up after itself, but only if its `finally`
   * runs - and a process killed with TerminateProcess never reaches one.
   * Ten of these had accumulated in the live state folder, one per hard
   * stop. They hold consent data, so they are worth clearing rather than
   * leaving around.
   *
   * Age-gated and capped: a temp file younger than the cutoff may belong
   * to a process that is writing right now, and this runs on a start-up
   * path that must never be slow or fatal.
   */
  #sweepAbandonedWrites() {
    const directory = dirname(this.path);
    const cutoff = this.now().getTime() - ABANDONED_WRITE_MS;
    try {
      let examined = 0;
      for (const name of readdirSync(directory)) {
        if (!name.startsWith(".consent-") || !name.endsWith(".tmp")) {
          continue;
        }
        if (++examined > MAX_SWEEP_FILES) {
          return;
        }
        const candidate = join(directory, name);
        try {
          if (lstatSync(candidate).mtimeMs < cutoff) {
            rmSync(candidate);
          }
        } catch {
          // Another process may have just claimed or removed it.
        }
      }
    } catch {
      // Housekeeping must never stop the ledger from loading.
    }
  }

  #load({ declinesOnly = false } = {}) {
    let raw;
    try {
      const information = validatePrivateFile(this.path);
      if (information.size > MAX_LEDGER_BYTES) {
        return;
      }
      raw = readFileSync(this.path, "utf8");
    } catch (error) {
      if (error.code === "ENOENT") {
        return;
      }
      // A ledger that cannot be read must not take the bot down, and must
      // not be treated as "everyone consented" either: starting empty means
      // everyone is asked again, which is the safe failure.
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return;
    }
    // typeof null is "object", and Object.entries(null) throws, which would
    // take the bot down on exactly the damaged file this guard exists for.
    if (
      !parsed ||
      Array.isArray(parsed) ||
      parsed.version !== LEDGER_VERSION ||
      typeof parsed.updatedAt !== "string" ||
      !Number.isFinite(Date.parse(parsed.updatedAt)) ||
      !parsed.entries ||
      typeof parsed.entries !== "object" ||
      Array.isArray(parsed.entries)
    ) {
      return;
    }
    const savedEntries = Object.entries(parsed.entries);
    if (
      savedEntries.length > MAX_LEDGER_ENTRIES ||
      savedEntries.some(
        ([entryKey, value]) =>
          !CONSENT_KEY.test(entryKey) || (value !== "granted" && value !== "declined"),
      )
    ) {
      // Never salvage grants from a partially malformed file. If any part of
      // the authorization state is untrustworthy, every user is pending.
      return;
    }
    for (const [entryKey, value] of savedEntries) {
      if (declinesOnly && value !== "declined") {
        continue;
      }
      this.entries.set(entryKey, value);
    }
  }

  #save() {
    if (!this.path) {
      return true;
    }
    try {
      const payload = {
        version: LEDGER_VERSION,
        updatedAt: this.now().toISOString(),
        entries: Object.fromEntries(this.entries),
      };
      writeAtomicPrivateFile(this.path, JSON.stringify(payload, null, 2));
      return true;
    } catch {
      // The run marker deliberately stays behind. This process keeps the
      // safer in-memory answer, and the next process discards every grant.
      return false;
    }
  }

  /** "granted", "declined", or null when the user has never answered. */
  recall(guildId, userId) {
    return this.entries.get(key(guildId, userId)) ?? null;
  }

  /**
   * Returns whether the answer is now DURABLE, not whether it was
   * accepted - memory always accepts. A false from a decline is the
   * one the caller must not paper over: telling somebody "recorded"
   * about a no that never reached disk is the lie this ledger
   * exists to prevent.
   */
  remember(guildId, userId, state) {
    if (state !== "granted" && state !== "declined") {
      return false;
    }
    // Unsynced first: if this process dies between here and the save,
    // the next run must not trust a file that may be one answer behind.
    // If even that cannot be written, nothing durable can be promised,
    // and memory stays equal to disk so the next run cannot resurrect
    // an answer this one meant to replace.
    if (!this.#writeMarker(false)) {
      return false;
    }
    this.entries.set(key(guildId, userId), state);
    if (!this.#save()) {
      return false;
    }
    this.#writeMarker(true);
    return true;
  }

  /**
   * Erase the record entirely, leaving no trace of the answer either
   * way. persisted reports whether the erasure is durable; a false
   * means the old answer is still on disk and only the dirty marker
   * stands between it and the next run.
   */
  forget(guildId, userId) {
    const entryKey = key(guildId, userId);
    if (!this.entries.has(entryKey)) {
      return { existed: false, persisted: true };
    }
    if (!this.#writeMarker(false)) {
      return { existed: true, persisted: false };
    }
    this.entries.delete(entryKey);
    const persisted = this.#save();
    if (persisted) {
      this.#writeMarker(true);
    }
    return { existed: true, persisted };
  }

  /**
   * Persist the final in-memory answer before declaring this run clean.
   * A failed save or marker removal leaves the dirty marker in place, so a
   * later process cannot resurrect a stale grant.
   */
  close() {
    if (this.closed || !this.path) {
      this.closed = true;
      return true;
    }
    if (!this.#save()) {
      return false;
    }
    try {
      validatePrivateFile(this.runMarkerPath);
      rmSync(this.runMarkerPath);
      syncDirectory(dirname(this.runMarkerPath));
      this.closed = true;
      return true;
    } catch {
      return false;
    }
  }

  /** Everything remembered for one guild, for a status readout. */
  summarize(guildId) {
    let granted = 0;
    let declined = 0;
    for (const [entryKey, value] of this.entries) {
      if (!entryKey.startsWith(`${guildId}:`)) {
        continue;
      }
      if (value === "granted") {
        granted += 1;
      } else if (value === "declined") {
        declined += 1;
      }
    }
    return { granted, declined };
  }
}
