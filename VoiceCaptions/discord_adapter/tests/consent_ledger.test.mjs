import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { loadConfig } from "../src/config.js";
import { CaptionCommandController } from "../src/command_controller.js";
import { ConsentLedger } from "../src/consent_ledger.js";
import {
  createProductionSessionManager,
  SessionManager,
  SessionPhase,
  ConsentState,
} from "../src/session_manager.js";

const GUILD = "100000000000000001";
const ALICE = "200000000000000001";
const BOB = "200000000000000002";
const DAVE = "200000000000000004";
const ERIN = "200000000000000005";
const CHANNEL = "300000000000000001";
const CONTROL = "300000000000000002";

function manager(ledger) {
  const sessions = new SessionManager({ captureHealthy: true, consentLedger: ledger });
  return sessions;
}

function productionManager(path) {
  const config = loadConfig({
    DISCORD_TOKEN: `not-a-real-token.${"x".repeat(40)}`,
    DISCORD_APPLICATION_ID: "123456789012345678",
    DISCORD_GUILD_ID: GUILD,
    CAPTIONS_CONSENT_STATE_PATH: path,
  });
  const sessions = createProductionSessionManager(config);
  sessions.setCaptureHealth(true);
  return sessions;
}

function start(sessions, participants = [ALICE, BOB]) {
  return sessions.startSession({
    guildId: GUILD,
    voiceChannelId: CHANNEL,
    controlChannelId: CONTROL,
    startedBy: ALICE,
    participantIds: participants,
  });
}

test("remembered grants restore each speaker's prior choice", () => {
  const ledger = new ConsentLedger();
  const first = manager(ledger);
  start(first);
  first.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
  first.setConsent({ guildId: GUILD, userId: BOB, granted: true });
  first.stopSession({ guildId: GUILD, requestedBy: ALICE });

  // A brand new run, same ledger: both speakers keep their choices.
  const second = manager(ledger);
  const session = start(second);
  assert.equal(session.phase, SessionPhase.ACTIVE);
});

test("a remembered decline outranks everything and survives rejoining", () => {
  const ledger = new ConsentLedger();
  const sessions = manager(ledger);
  start(sessions);
  sessions.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
  sessions.setConsent({ guildId: GUILD, userId: BOB, granted: false });

  // Bob leaves and comes back. Silence is never a change of mind.
  sessions.updateParticipants({ guildId: GUILD, participantIds: [ALICE] });
  const rejoined = sessions.updateParticipants({
    guildId: GUILD,
    participantIds: [ALICE, BOB],
  });
  const bob = rejoined.participants.find((entry) => entry.userId === BOB);
  assert.equal(bob.consent, ConsentState.DECLINED);
  // Alice consented, so the room runs - and the remembered decline
  // still binds exactly Bob, silently and absolutely.
  assert.equal(rejoined.phase, SessionPhase.ACTIVE);
  assert.equal(sessions.canCapture({ guildId: GUILD, userId: BOB }), false);
  assert.equal(sessions.canCapture({ guildId: GUILD, userId: ALICE }), true);
});

test("consent can be withdrawn with no session running at all", () => {
  const ledger = new ConsentLedger();
  const sessions = manager(ledger);
  start(sessions);
  sessions.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
  sessions.setConsent({ guildId: GUILD, userId: BOB, granted: true });
  sessions.stopSession({ guildId: GUILD, requestedBy: ALICE });

  // Nothing is running. Withdrawing must still be possible, or the only
  // moment you could take it back would be the moment it is recording.
  const answer = sessions.setConsent({ guildId: GUILD, userId: BOB, granted: false });
  assert.equal(answer, null);
  assert.equal(ledger.recall(GUILD, BOB), "declined");

  const later = manager(ledger);
  const session = start(later);
  const bobLater = session.participants.find((entry) => entry.userId === BOB);
  assert.equal(bobLater.consent, ConsentState.DECLINED, "the withdrawal must hold");
  assert.equal(later.canCapture({ guildId: GUILD, userId: BOB }), false);
});

test("consent can be withdrawn after leaving the channel", () => {
  const ledger = new ConsentLedger();
  const sessions = manager(ledger);
  start(sessions);
  sessions.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
  sessions.setConsent({ guildId: GUILD, userId: BOB, granted: true });
  sessions.updateParticipants({ guildId: GUILD, participantIds: [ALICE] });

  // Bob is no longer a participant. He must still be able to say no.
  sessions.setConsent({ guildId: GUILD, userId: BOB, granted: false });
  assert.equal(ledger.recall(GUILD, BOB), "declined");
});

test("granting still requires being in the room", () => {
  const ledger = new ConsentLedger();
  const sessions = manager(ledger);
  start(sessions, [ALICE]);
  assert.throws(
    () => sessions.setConsent({ guildId: GUILD, userId: BOB, granted: true }),
    /not_participant|Join the session/,
  );
  // A grant the user was told had failed must leave nothing behind, or it
  // enrols them for every later session without their ever being asked.
  assert.equal(ledger.recall(GUILD, BOB), null);

  sessions.stopSession({ guildId: GUILD, requestedBy: ALICE });
  assert.throws(
    () => sessions.setConsent({ guildId: GUILD, userId: BOB, granted: true }),
    /no_session|No caption session/,
  );
  assert.equal(ledger.recall(GUILD, BOB), null);
});

test("forget erases the record and drops a live participant to pending", () => {
  const ledger = new ConsentLedger();
  const sessions = manager(ledger);
  start(sessions);
  sessions.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
  sessions.setConsent({ guildId: GUILD, userId: BOB, granted: true });

  const { existed, session } = sessions.forgetConsent({ guildId: GUILD, userId: BOB });
  assert.equal(existed, true);
  assert.equal(ledger.recall(GUILD, BOB), null);
  const bob = session.participants.find((entry) => entry.userId === BOB);
  assert.equal(bob.consent, ConsentState.PENDING, "forgetting must never read as a grant");
  assert.equal(sessions.canCapture({ guildId: GUILD, userId: BOB }), false);
  assert.equal(sessions.canCapture({ guildId: GUILD, userId: ALICE }), true);
});

test("the ledger survives a restart, and a damaged one drops grants", () => {
  const directory = mkdtempSync(join(tmpdir(), "consent-"));
  const path = join(directory, "consent.json");
  try {
    const first = new ConsentLedger({ path });
    first.remember(GUILD, ALICE, "granted");
    first.remember(GUILD, BOB, "declined");
    assert.equal(first.close(), true);

    const reloaded = new ConsentLedger({ path });
    assert.equal(reloaded.recall(GUILD, ALICE), "granted");
    assert.equal(reloaded.recall(GUILD, BOB), "declined");
    assert.deepEqual(reloaded.summarize(GUILD), { granted: 1, declined: 1 });
    assert.equal(reloaded.close(), true);

    // A file that cannot be trusted must not be read as consent.
    writeFileSync(path, "{ not json", "utf8");
    const damaged = new ConsentLedger({ path });
    assert.equal(damaged.recall(GUILD, ALICE), null,
      "an unreadable ledger must ask again, never assume yes");
    assert.equal(damaged.close(), true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("production wiring persists decline and forget with a private state file", () => {
  const directory = mkdtempSync(join(tmpdir(), "consent-production-"));
  const path = join(directory, "consent.json");
  try {
    const first = productionManager(path);
    start(first, [ALICE]);
    assert.equal(
      first.setConsent({ guildId: GUILD, userId: ALICE, granted: true }).phase,
      SessionPhase.ACTIVE,
    );
    if (process.platform !== "win32") {
      assert.equal(statSync(path).mode & 0o777, 0o600);
    }
    assert.equal(first.close(), true);

    const second = productionManager(path);
    assert.equal(start(second, [ALICE]).phase, SessionPhase.ACTIVE);
    assert.equal(
      second.setConsent({ guildId: GUILD, userId: ALICE, granted: false }).phase,
      SessionPhase.AWAITING_CONSENT,
    );
    assert.equal(second.canCapture({ guildId: GUILD, userId: ALICE }), false);
    assert.equal(second.close(), true);

    const third = productionManager(path);
    const thirdSession = start(third, [ALICE]);
    assert.equal(
      thirdSession.participants.find((entry) => entry.userId === ALICE).consent,
      ConsentState.DECLINED,
      "the decline survives the restart and still binds its owner",
    );
    assert.equal(third.canCapture({ guildId: GUILD, userId: ALICE }), false);
    assert.equal(third.forgetConsent({ guildId: GUILD, userId: ALICE }).existed, true);
    assert.equal(third.close(), true);

    const fourth = productionManager(path);
    assert.equal(start(fourth, [ALICE]).phase, SessionPhase.AWAITING_CONSENT);
    assert.equal(fourth.close(), true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("session-only answers remove old records and do not cross a restart", () => {
  const directory = mkdtempSync(join(tmpdir(), "consent-session-only-"));
  const path = join(directory, "consent.json");
  try {
    const first = productionManager(path);
    start(first);
    first.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
    first.setConsent({ guildId: GUILD, userId: BOB, granted: false });
    assert.equal(first.close(), true);

    const second = productionManager(path);
    const remembered = start(second);
    assert.equal(
      remembered.participants.find(({ userId }) => userId === ALICE).consent,
      ConsentState.GRANTED,
    );
    assert.equal(
      remembered.participants.find(({ userId }) => userId === BOB).consent,
      ConsentState.DECLINED,
    );

    second.setConsent({
      guildId: GUILD,
      userId: ALICE,
      granted: false,
      remember: false,
    });
    second.setConsent({
      guildId: GUILD,
      userId: BOB,
      granted: true,
      remember: false,
    });
    assert.equal(second.consentLedger.recall(GUILD, ALICE), null);
    assert.equal(second.consentLedger.recall(GUILD, BOB), null);
    assert.equal(second.canCapture({ guildId: GUILD, userId: ALICE }), false);
    assert.equal(second.canCapture({ guildId: GUILD, userId: BOB }), true);
    assert.equal(second.close(), true);

    const third = productionManager(path);
    const fresh = start(third);
    assert.equal(
      fresh.participants.find(({ userId }) => userId === ALICE).consent,
      ConsentState.PENDING,
    );
    assert.equal(
      fresh.participants.find(({ userId }) => userId === BOB).consent,
      ConsentState.PENDING,
    );
    assert.equal(third.close(), true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("failed decline and forget writes cannot resurrect stale grants", () => {
  const directory = mkdtempSync(join(tmpdir(), "consent-stale-grant-"));
  const path = join(directory, "consent.json");
  const runMarker = `${path}.dirty`;
  const fixedTime = new Date("2026-08-30T12:00:00.000Z");
  try {
    const firstLedger = new ConsentLedger({ path, now: () => fixedTime });
    const first = manager(firstLedger);
    start(first);
    first.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
    first.setConsent({ guildId: GUILD, userId: BOB, granted: true });
    assert.equal(first.close(), true);
    assert.equal(existsSync(runMarker), false, "a durable clean close clears the marker");

    let failWrites = false;
    const secondLedger = new ConsentLedger({
      path,
      now: () => {
        if (failWrites) {
          throw new Error("simulated persistence failure");
        }
        return fixedTime;
      },
    });
    const second = manager(secondLedger);
    assert.equal(start(second).phase, SessionPhase.ACTIVE);

    failWrites = true;
    const declined = second.setConsent({ guildId: GUILD, userId: ALICE, granted: false });
    const forgotten = second.forgetConsent({ guildId: GUILD, userId: BOB });
    assert.equal(
      declined.participants.find(({ userId }) => userId === ALICE).consent,
      ConsentState.DECLINED,
      "decline takes effect in memory immediately",
    );
    assert.equal(second.canCapture({ guildId: GUILD, userId: ALICE }), false);
    assert.equal(
      forgotten.session.participants.find(({ userId }) => userId === BOB).consent,
      ConsentState.PENDING,
      "forget pauses in memory immediately",
    );
    assert.equal(
      secondLedger.recall(GUILD, ALICE),
      null,
      "a failed remembered answer remains scoped to the live session",
    );
    assert.equal(secondLedger.recall(GUILD, BOB), null);

    const stale = JSON.parse(readFileSync(path, "utf8"));
    assert.equal(stale.entries[`${GUILD}:${ALICE}`], "granted");
    assert.equal(stale.entries[`${GUILD}:${BOB}`], "granted");
    assert.equal(existsSync(runMarker), true, "a failed run stays marked dirty");

    // This is the exact restart boundary that used to resurrect both grants.
    const restartedLedger = new ConsentLedger({ path });
    assert.equal(restartedLedger.recall(GUILD, ALICE), null);
    assert.equal(restartedLedger.recall(GUILD, BOB), null);
    const restarted = manager(restartedLedger);
    assert.equal(start(restarted).phase, SessionPhase.AWAITING_CONSENT);
    assert.equal(restarted.close(), true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a failed remembered grant cannot authorize the next in-process session", () => {
  const directory = mkdtempSync(join(tmpdir(), "consent-failed-grant-"));
  const path = join(directory, "consent.json");
  let failWrites = false;
  try {
    const ledger = new ConsentLedger({
      path,
      now: () => {
        if (failWrites) {
          throw new Error("simulated persistence failure");
        }
        return new Date("2026-08-30T12:00:00.000Z");
      },
    });
    const sessions = manager(ledger);
    start(sessions, [ALICE]);

    failWrites = true;
    sessions.setConsent({
      guildId: GUILD,
      userId: ALICE,
      granted: true,
      remember: true,
    });
    assert.equal(sessions.canCapture({ guildId: GUILD, userId: ALICE }), true);
    assert.equal(sessions.consentWritePersisted, false);
    assert.equal(ledger.recall(GUILD, ALICE), null);

    sessions.stopSession({ guildId: GUILD, requestedBy: ALICE });
    const next = start(sessions, [ALICE]);
    assert.equal(next.phase, SessionPhase.AWAITING_CONSENT);
    assert.equal(next.participants[0].consent, ConsentState.PENDING);
    assert.equal(sessions.canCapture({ guildId: GUILD, userId: ALICE }), false);

    failWrites = false;
    assert.equal(sessions.close(), true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("production wiring never salvages a grant from partially bad state", () => {
  const directory = mkdtempSync(join(tmpdir(), "consent-invalid-"));
  const path = join(directory, "consent.json");
  try {
    writeFileSync(
      path,
      JSON.stringify({
        version: 1,
        updatedAt: new Date().toISOString(),
        entries: {
          [`${GUILD}:${ALICE}`]: "granted",
          malformed: "declined",
        },
      }),
      { encoding: "utf8", mode: 0o600 },
    );
    const sessions = productionManager(path);
    assert.equal(start(sessions, [ALICE]).phase, SessionPhase.AWAITING_CONSENT);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a ledger whose entries are null starts empty instead of throwing", () => {
  const directory = mkdtempSync(join(tmpdir(), "consent-"));
  const path = join(directory, "consent.json");
  try {
    writeFileSync(path, '{"version":1,"entries":null}', "utf8");
    const ledger = new ConsentLedger({ path });
    assert.equal(ledger.recall(GUILD, ALICE), null);
    assert.deepEqual(ledger.summarize(GUILD), { granted: 0, declined: 0 });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a session with no remembered grants waits for consent", () => {
  const ledger = new ConsentLedger();
  assert.equal(ledger.recall(GUILD, ALICE), null);
  const sessions = manager(ledger);
  const session = start(sessions);
  assert.equal(session.phase, SessionPhase.AWAITING_CONSENT);
});

test("production wiring gets a real ledger, not a silent memory-only one", async (t) => {
  // The regression this guards: consent persistence was implemented, tested
  // and documented, but index.js never passed a session manager, the runtime
  // defaulted one, and that default carried a null ledger. Every call site is
  // an optional-chain, so it failed in complete silence.
  const folder = mkdtempSync(join(tmpdir(), "consent-wiring-"));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  const path = join(folder, "consent-state.json");

  const manager = createProductionSessionManager({
    consentStatePath: path,
    mappingStatePath: null,
  });
  assert.ok(manager.consentLedger, "production manager must carry a ledger");

  manager.setConsent({ guildId: "1", userId: "2", granted: false });
  // The merged ledger trusts a file only after a clean close - the dirty
  // marker otherwise makes the next run start empty, the safe direction.
  // Production reaches close() through the SIGTERM chain; the test does it
  // directly.
  manager.close();

  const reloaded = createProductionSessionManager({
    consentStatePath: path,
    mappingStatePath: null,
  });
  assert.equal(
    reloaded.consentLedger.recall("1", "2"),
    "declined",
    "a decline must survive a restart, which is the whole point",
  );
});

test("production wiring refuses a config that cannot say where consent lives", () => {
  assert.throws(() => createProductionSessionManager(undefined), TypeError);
  assert.throws(() => createProductionSessionManager({}), TypeError);
});

/*
A withdrawal whose durable write fails must say so. The injection is
deterministic: the ledger FILE path is created as a DIRECTORY, so the
constructor succeeds (the run marker lands beside it, the load reads EISDIR
and starts empty) while every atomic save fails on the final rename.
*/
function commandInput(subcommand, userId) {
  return {
    subcommand,
    guildId: GUILD,
    userId,
    voiceChannelId: CHANNEL,
    controlChannelId: CONTROL,
    participantIds: [ALICE, BOB],
  };
}

function unwritableLedger(t) {
  const folder = mkdtempSync(join(tmpdir(), "consent-unwritable-"));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  const path = join(folder, "consent-state.json");
  mkdirSync(path);
  return new ConsentLedger({ path });
}

test("a decline that could not be saved is not called recorded", (t) => {
  const sessions = new SessionManager({
    captureHealthy: true,
    consentLedger: unwritableLedger(t),
  });
  const controller = new CaptionCommandController(sessions);
  start(sessions);

  const reply = controller.execute(commandInput("decline", BOB));

  assert.equal(reply.action, "consent.declined");
  // The live half stays true: this speaker is excluded now.
  assert.notEqual(reply.session.phase, SessionPhase.ACTIVE);
  // The durable half must not be claimed.
  assert.match(reply.content, /could NOT be saved/i);
  assert.doesNotMatch(reply.content, /^Recorded\./);
});

test("a decline with nothing running warns when the write fails", (t) => {
  const sessions = new SessionManager({
    captureHealthy: true,
    consentLedger: unwritableLedger(t),
  });
  const controller = new CaptionCommandController(sessions);

  const reply = controller.execute(commandInput("decline", BOB));

  assert.equal(reply.session, null);
  assert.match(reply.content, /could NOT be saved/i);
  assert.match(reply.content, /may not be remembered after a restart/i);
  assert.doesNotMatch(reply.content, /everyone|capture remains off/i);
});

test("forget names the consent answer that could not be erased", (t) => {
  const folder = mkdtempSync(join(tmpdir(), "consent-forget-"));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  const path = join(folder, "consent-state.json");

  // A real durable grant first, cleanly closed...
  const before = new ConsentLedger({ path });
  before.remember(GUILD, BOB, "granted");
  before.close();

  // Load the durable grant, then make subsequent saves fail while the old
  // grant remains on disk.
  let failWrites = false;
  const ledger = new ConsentLedger({
    path,
    now: () => {
      if (failWrites) {
        throw new Error("simulated persistence failure");
      }
      return new Date("2026-08-30T12:00:00.000Z");
    },
  });
  const sessions = new SessionManager({
    captureHealthy: true,
    consentLedger: ledger,
  });
  const controller = new CaptionCommandController(sessions);
  start(sessions);
  failWrites = true;

  const reply = controller.execute(commandInput("forget", BOB));

  assert.equal(reply.action, "consent.forgotten");
  assert.match(reply.content, /could not erase.*consent answer/i);
  assert.doesNotMatch(reply.content, /^Forgotten\./);
  assert.doesNotMatch(reply.content, /capture remains off/i);
});

test("a healthy disk keeps every reply exactly as before", (t) => {
  const folder = mkdtempSync(join(tmpdir(), "consent-healthy-"));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  const sessions = new SessionManager({
    captureHealthy: true,
    consentLedger: new ConsentLedger({ path: join(folder, "consent.json") }),
  });
  const controller = new CaptionCommandController(sessions);

  const reply = controller.execute(commandInput("decline", BOB));
  assert.match(reply.content, /^Recorded\./);
  assert.doesNotMatch(reply.content, /could NOT be saved/i);
});

test("a clean restart remembers every answer", async (t) => {
  const folder = mkdtempSync(join(tmpdir(), "consent-clean-"));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  const path = join(folder, "consent.json");

  const first = new ConsentLedger({ path });
  first.remember(GUILD, ALICE, "granted");
  first.remember(GUILD, DAVE, "declined");
  assert.equal(first.close(), true);

  const second = new ConsentLedger({ path });
  assert.equal(second.recall(GUILD, ALICE), "granted");
  assert.equal(second.recall(GUILD, DAVE), "declined");
});

test("a dirty restart drops grants but never erases a decline", async (t) => {
  // Remembering each speaker's choice avoids asking that person again every
  // evening. A crash may re-ask for grants - one might have been withdrawn in
  // a change that never reached the file - but a decline is the protective
  // answer and has to survive.
  const folder = mkdtempSync(join(tmpdir(), "consent-dirty-"));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  const path = join(folder, "consent.json");

  const first = new ConsentLedger({ path });
  first.remember(GUILD, ALICE, "granted");
  first.remember(GUILD, DAVE, "declined");
  assert.equal(first.close(), true);

  writeFileSync(`${path}.dirty`, "", { mode: 0o600 });

  const second = new ConsentLedger({ path });
  assert.equal(second.recall(GUILD, ALICE), null, "a grant is re-asked");
  assert.equal(second.recall(GUILD, DAVE), "declined", "a decline holds");
});

test("a dirty restart does not erase the file it refused to trust", async (t) => {
  // The old run marker could not distinguish a live process from an
  // interrupted write. A restart skipped every entry, then the next save
  // overwrote the file with the partial in-memory map. The synced marker now
  // preserves a fully written file even when close() never ran.
  const folder = mkdtempSync(join(tmpdir(), "consent-erase-"));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  const path = join(folder, "consent.json");

  const first = new ConsentLedger({ path });
  first.remember(GUILD, DAVE, "declined");
  first.remember(GUILD, ERIN, "declined");
  assert.equal(first.close(), true);

  writeFileSync(`${path}.dirty`, "", { mode: 0o600 });
  const second = new ConsentLedger({ path });
  second.remember(GUILD, ALICE, "granted");
  assert.equal(second.close(), true);

  const third = new ConsentLedger({ path });
  assert.equal(third.recall(GUILD, DAVE), "declined");
  assert.equal(third.recall(GUILD, ERIN), "declined");
});
