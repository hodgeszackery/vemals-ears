import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MappingLedger } from "../src/mapping_ledger.js";
import {
  SessionManager,
  createProductionSessionManager,
} from "../src/session_manager.js";

const GUILD = "100000000000000001";
const ALICE = "200000000000000001";
const BOB = "200000000000000002";

const HEALER = {
  characterName: "Mirala",
  realm: "Proudmoore",
  wowClass: "priest",
  role: "healer",
};

function scratch(t) {
  const folder = mkdtempSync(join(tmpdir(), "mapping-ledger-"));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  return folder;
}

test("a mapping survives a restart, which is the whole point", (t) => {
  const path = join(scratch(t), "mapping-state.json");
  const first = new MappingLedger({ path });
  first.remember(GUILD, ALICE, HEALER);

  const reloaded = new MappingLedger({ path });
  const entry = reloaded.recall(GUILD, ALICE);
  assert.equal(entry.characterName, "Mirala");
  assert.equal(entry.role, "healer");
  assert.ok(entry.mappedAt, "the entry records when it was made");
});

test("a damaged file starts empty instead of taking the bot down", (t) => {
  const path = join(scratch(t), "mapping-state.json");
  for (const garbage of ["not json", "null", '{"version":1,"entries":null}']) {
    writeFileSync(path, garbage, "utf8");
    const ledger = new MappingLedger({ path });
    assert.equal(ledger.recall(GUILD, ALICE), null);
  }
});

test("an uncertain load never claims an absent mapping was erased", (t) => {
  const path = join(scratch(t), "mapping-state.json");
  writeFileSync(path, "not json", "utf8");
  const ledger = new MappingLedger({ path });

  assert.deepEqual(ledger.forget(GUILD, ALICE), {
    existed: false,
    persisted: false,
  });
  writeFileSync(
    path,
    JSON.stringify({
      version: 1,
      entries: { [`${GUILD}:${ALICE}`]: HEALER },
    }),
    "utf8",
  );
  assert.deepEqual(ledger.forget(GUILD, ALICE), {
    existed: true,
    persisted: true,
  });
  assert.equal(new MappingLedger({ path }).recall(GUILD, ALICE), null);
});

test("an uncertain load is never overwritten with a partial roster", (t) => {
  const path = join(scratch(t), "mapping-state.json");
  writeFileSync(path, "not json", "utf8");
  const ledger = new MappingLedger({ path });

  assert.equal(ledger.remember(GUILD, ALICE, HEALER), false);
  writeFileSync(
    path,
    JSON.stringify({
      version: 1,
      entries: { [`${GUILD}:${BOB}`]: HEALER },
    }),
    "utf8",
  );
  assert.equal(ledger.remember(GUILD, ALICE, HEALER), true);

  const reloaded = new MappingLedger({ path });
  assert.equal(reloaded.recall(GUILD, ALICE).characterName, "Mirala");
  assert.equal(reloaded.recall(GUILD, BOB).characterName, "Mirala");
});

test("a failed mapping cannot hitchhike on somebody else's later save", (t) => {
  const path = join(scratch(t), "mapping-state.json");
  let clockCalls = 0;
  const ledger = new MappingLedger({
    path,
    now: () => {
      clockCalls += 1;
      // remember() timestamps the entry first; #save() asks for the second
      // timestamp. Fail only the durable write, after the in-memory add.
      if (clockCalls === 2) {
        throw new Error("simulated persistence failure");
      }
      return new Date("2026-08-31T12:00:00.000Z");
    },
  });

  assert.equal(ledger.remember(GUILD, ALICE, HEALER), false);
  assert.equal(ledger.recall(GUILD, ALICE), null);
  assert.equal(ledger.remember(GUILD, BOB, HEALER), true);

  const reloaded = new MappingLedger({ path });
  assert.equal(reloaded.recall(GUILD, ALICE), null);
  assert.equal(reloaded.recall(GUILD, BOB).characterName, "Mirala");
});

test("a failed mapping erase stays retryable", (t) => {
  const path = join(scratch(t), "mapping-state.json");
  let failWrites = false;
  const ledger = new MappingLedger({
    path,
    now: () => {
      if (failWrites) {
        throw new Error("simulated persistence failure");
      }
      return new Date("2026-08-31T12:00:00.000Z");
    },
  });
  assert.equal(ledger.remember(GUILD, ALICE, HEALER), true);

  failWrites = true;
  assert.deepEqual(
    ledger.forget(GUILD, ALICE),
    { existed: true, persisted: false },
  );
  assert.equal(ledger.recall(GUILD, ALICE).characterName, "Mirala");
  failWrites = false;
  assert.deepEqual(
    ledger.forget(GUILD, ALICE),
    { existed: true, persisted: true },
    "a second erase retries the stale on-disk copy",
  );
  assert.equal(new MappingLedger({ path }).recall(GUILD, ALICE), null);
});

test("a fabricated entry cannot steer focus mode", (t) => {
  // Inverted from the consent ledger on purpose: valid entries are KEPT
  // (re-mapping a guild is the friction this file removes), but each field
  // is re-validated so a hand-edited role of "boss" or a script-injected
  // name never reaches the caption pipeline.
  const path = join(scratch(t), "mapping-state.json");
  writeFileSync(
    path,
    JSON.stringify({
      version: 1,
      entries: {
        [`${GUILD}:${ALICE}`]: { ...HEALER },
        [`${GUILD}:200000000000000002`]: { ...HEALER, role: "boss" },
        [`${GUILD}:200000000000000003`]: { ...HEALER, characterName: "x" },
        "not-ids": { ...HEALER },
      },
    }),
    "utf8",
  );
  const ledger = new MappingLedger({ path });
  assert.equal(ledger.recall(GUILD, ALICE).role, "healer");
  assert.equal(ledger.recall(GUILD, "200000000000000002"), null);
  assert.equal(ledger.recall(GUILD, "200000000000000003"), null);
  assert.equal(ledger.recallAll().length, 1);
});

test("a rejected entry stays non-functional but can still be erased", (t) => {
  const path = join(scratch(t), "mapping-state.json");
  writeFileSync(
    path,
    JSON.stringify({
      version: 1,
      entries: {
        [`${GUILD}:${ALICE}`]: { ...HEALER, role: "boss" },
        [`${GUILD}:${ALICE}:extra`]: HEALER,
        [`${GUILD}:${BOB}`]: HEALER,
      },
    }),
    "utf8",
  );
  const ledger = new MappingLedger({ path });

  assert.equal(ledger.recall(GUILD, ALICE), null);
  assert.equal(ledger.recall(GUILD, BOB).role, "healer");
  assert.deepEqual(ledger.forget(GUILD, ALICE), {
    existed: true,
    persisted: true,
  });

  const stored = JSON.parse(readFileSync(path, "utf8")).entries;
  assert.equal(Object.hasOwn(stored, `${GUILD}:${ALICE}`), false);
  assert.equal(Object.hasOwn(stored, `${GUILD}:${ALICE}:extra`), false);
  assert.equal(Object.hasOwn(stored, `${GUILD}:${BOB}`), true);
});

test("the session manager seeds from the ledger before the first segment", (t) => {
  const path = join(scratch(t), "mapping-state.json");
  const before = new SessionManager({ mappingLedger: new MappingLedger({ path }) });
  before.mapCharacter({ guildId: GUILD, userId: ALICE, ...HEALER });

  // A new process: the audio path reads getCharacter synchronously, so the
  // restore must be complete at construction, not discovered lazily.
  const after = new SessionManager({ mappingLedger: new MappingLedger({ path }) });
  const restored = after.getCharacter({ guildId: GUILD, userId: ALICE });
  assert.equal(restored.characterName, "Mirala");
  assert.equal(restored.role, "healer");
});

test("unmap erases the stored copy too, even one memory never saw", (t) => {
  const path = join(scratch(t), "mapping-state.json");
  const first = new SessionManager({ mappingLedger: new MappingLedger({ path }) });
  first.mapCharacter({ guildId: GUILD, userId: ALICE, ...HEALER });

  const second = new SessionManager({ mappingLedger: new MappingLedger({ path }) });
  assert.equal(second.clearCharacter({ guildId: GUILD, userId: ALICE }), true);

  const third = new SessionManager({ mappingLedger: new MappingLedger({ path }) });
  assert.equal(third.getCharacter({ guildId: GUILD, userId: ALICE }), null);
});

test("forget erases the mapping along with the consent record", (t) => {
  const folder = scratch(t);
  const manager = createProductionSessionManager({
    consentStatePath: join(folder, "consent-state.json"),
    mappingStatePath: join(folder, "mapping-state.json"),
  });
  manager.mapCharacter({ guildId: GUILD, userId: ALICE, ...HEALER });
  manager.forgetConsent({ guildId: GUILD, userId: ALICE });

  const reloaded = createProductionSessionManager({
    consentStatePath: join(folder, "consent-state.json"),
    mappingStatePath: join(folder, "mapping-state.json"),
  });
  assert.equal(
    reloaded.getCharacter({ guildId: GUILD, userId: ALICE }),
    null,
    "a mapping is a trace of somebody who asked to be forgotten",
  );
});

test("production wiring refuses a config that cannot say where mappings live", () => {
  assert.throws(
    () => createProductionSessionManager({ consentStatePath: null }),
    TypeError,
  );
});

test("the ledger's validation mirrors the session manager's exactly", () => {
  // The constants are duplicated to avoid a circular import; this is the
  // guard that keeps the copies from drifting apart silently.
  const managerSource = readFileSync(
    new URL("../src/session_manager.js", import.meta.url),
    "utf8",
  );
  const ledgerSource = readFileSync(
    new URL("../src/mapping_ledger.js", import.meta.url),
    "utf8",
  );
  const classes = managerSource.match(/WOW_CLASSES = new Set\(\[[^\]]+\]\)/u)[0]
    .replace("WOW_CLASSES", "");
  const ledgerClasses = ledgerSource.match(/VALID_CLASSES = new Set\(\[[^\]]+\]\)/u)[0]
    .replace("VALID_CLASSES", "");
  assert.equal(ledgerClasses, classes, "class lists have drifted");
  for (const pattern of [
    /^[\p{L}][\p{L}'-]{1,23}$/u.source,
    /^[\p{L}\p{N}][\p{L}\p{N} '-]{0,31}$/u.source,
  ]) {
    assert.ok(managerSource.includes(pattern), "manager lost " + pattern);
    assert.ok(ledgerSource.includes(pattern), "ledger lost " + pattern);
  }
});
