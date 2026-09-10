import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { ConsentLedger } from "../src/consent_ledger.js";

// The old run marker treated every process that missed close() as though its
// last write had been interrupted. These pin the current rule: what reached
// disk is trusted unless the marker says the last write never landed.

const GUILD = "123456789012345678";
const ALICE = "223456789012345678";
const BOB = "323456789012345678";

function withLedgerDir(run) {
  const directory = mkdtempSync(join(tmpdir(), "vears-consent-"));
  return run(join(directory, "consent-state.json"), directory).finally(() => {
    rmSync(directory, { recursive: true, force: true });
  });
}

test("grants survive a run that never closed", async () => {
  await withLedgerDir(async (path) => {
    const first = new ConsentLedger({ path });
    assert.equal(first.remember(GUILD, ALICE, "granted"), true);
    assert.equal(first.remember(GUILD, BOB, "declined"), true);
    // No close(): the process died - a disconnect that failed closed and
    // was restarted, a kill, a crash. The marker is still there.
    assert.equal(existsSync(path + ".dirty"), true);

    const second = new ConsentLedger({ path });
    assert.equal(second.recall(GUILD, ALICE), "granted", "a grant that reached disk must come back");
    assert.equal(second.recall(GUILD, BOB), "declined");
  });
});

test("a run whose last write never reached disk loads declines only", async () => {
  await withLedgerDir(async (path) => {
    const first = new ConsentLedger({ path });
    first.remember(GUILD, ALICE, "granted");
    first.remember(GUILD, BOB, "declined");
    // The process died between "about to write" and the write landing.
    writeFileSync(path + ".dirty", JSON.stringify({ version: 1, runId: "x", synced: false }), "utf8");

    const second = new ConsentLedger({ path });
    assert.equal(second.recall(GUILD, ALICE), null, "a possibly stale grant is not resurrected");
    assert.equal(second.recall(GUILD, BOB), "declined", "a decline is kept even then");
  });
});

test("a marker from before the synced flag existed is treated as unsynced", async () => {
  await withLedgerDir(async (path) => {
    const first = new ConsentLedger({ path });
    first.remember(GUILD, ALICE, "granted");
    writeFileSync(path + ".dirty", JSON.stringify({ version: 1, runId: "old-format" }), "utf8");
    const second = new ConsentLedger({ path });
    assert.equal(second.recall(GUILD, ALICE), null);
  });
});

test("a write that fails leaves the marker unsynced so the next run is careful", async () => {
  await withLedgerDir(async (path, directory) => {
    const first = new ConsentLedger({ path });
    assert.equal(first.remember(GUILD, ALICE, "granted"), true);
    // Now make the ledger file unwritable by pointing the save somewhere
    // that cannot exist: a path under a regular file.
    writeFileSync(join(directory, "blocker"), "x", "utf8");
    first.path = join(directory, "blocker", "consent-state.json");
    assert.equal(first.remember(GUILD, ALICE, "declined"), false, "the revocation did not reach disk");
    const marker = JSON.parse(readFileSync(path + ".dirty", "utf8"));
    assert.equal(marker.synced, false);

    const second = new ConsentLedger({ path });
    assert.equal(second.recall(GUILD, ALICE), null, "the old grant on disk must not come back");
  });
});

test("a clean close removes the marker and the next run loads everything", async () => {
  await withLedgerDir(async (path) => {
    const first = new ConsentLedger({ path });
    first.remember(GUILD, ALICE, "granted");
    assert.equal(first.close(), true);
    assert.equal(existsSync(path + ".dirty"), false);
    const second = new ConsentLedger({ path });
    assert.equal(second.recall(GUILD, ALICE), "granted");
  });
});

test("the marker says synced again after every successful write and forget", async () => {
  await withLedgerDir(async (path) => {
    const ledger = new ConsentLedger({ path });
    ledger.remember(GUILD, ALICE, "granted");
    assert.equal(JSON.parse(readFileSync(path + ".dirty", "utf8")).synced, true);
    assert.deepEqual(ledger.forget(GUILD, ALICE), { existed: true, persisted: true });
    assert.equal(JSON.parse(readFileSync(path + ".dirty", "utf8")).synced, true);
    assert.deepEqual(ledger.forget(GUILD, ALICE), { existed: false, persisted: true });
    const reloaded = new ConsentLedger({ path });
    assert.equal(reloaded.recall(GUILD, ALICE), null, "forgotten means gone, even after an unclean exit");
  });
});

test("a memory-only ledger never touches the marker", () => {
  const ledger = new ConsentLedger({ path: null });
  assert.equal(ledger.remember(GUILD, ALICE, "granted"), true);
  assert.equal(ledger.recall(GUILD, ALICE), "granted");
  assert.deepEqual(ledger.forget(GUILD, ALICE), { existed: true, persisted: true });
});
