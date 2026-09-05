import assert from "node:assert/strict";
import test from "node:test";

import {
  ConsentState,
  SessionManager,
  SessionPhase,
  SessionStateError,
} from "../src/session_manager.js";

const GUILD = "123456789012345678";
const VOICE = "223456789012345678";
const CONTROL = "323456789012345678";
const ALICE = "423456789012345678";
const BOB = "523456789012345678";
const CAROL = "623456789012345678";

function start(manager, participantIds = [ALICE, BOB]) {
  return manager.startSession({
    guildId: GUILD,
    voiceChannelId: VOICE,
    controlChannelId: CONTROL,
    startedBy: ALICE,
    participantIds,
  });
}

test("audience generation and revision change only at membership boundaries", () => {
  const manager = new SessionManager({ captureHealthy: true });
  const opened = start(manager, [ALICE]);
  assert.equal(opened.generation, 1);
  assert.equal(opened.audienceRevision, 1);

  const unchanged = manager.updateParticipants({
    guildId: GUILD,
    participantIds: [ALICE],
  });
  assert.equal(unchanged.audienceRevision, 1);

  const joined = manager.updateParticipants({
    guildId: GUILD,
    participantIds: [ALICE, BOB],
  });
  assert.equal(joined.audienceRevision, 2);

  const stopped = manager.stopSession({ guildId: GUILD, requestedBy: ALICE });
  assert.equal(stopped.audienceRevision, 3);
  const reopened = start(manager, [ALICE]);
  assert.equal(reopened.generation, 2);
  assert.equal(reopened.audienceRevision, 1);
});

test("each speaker is captured exactly when they themselves consented", () => {
  const manager = new SessionManager({ captureHealthy: true });

  assert.equal(start(manager).phase, SessionPhase.AWAITING_CONSENT);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: ALICE }), false);
  const first = manager.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
  // One consent runs the room - for that one person only.
  assert.equal(first.phase, SessionPhase.ACTIVE);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: ALICE }), true);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: BOB }), false);
  const active = manager.setConsent({ guildId: GUILD, userId: BOB, granted: true });

  assert.equal(active.phase, SessionPhase.ACTIVE);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: ALICE }), true);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: BOB }), true);
});

test("a new participant is uncaptured until they consent, and pauses nobody", () => {
  const manager = new SessionManager({ captureHealthy: true });
  start(manager);
  manager.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
  manager.setConsent({ guildId: GUILD, userId: BOB, granted: true });

  const joined = manager.updateParticipants({
    guildId: GUILD,
    participantIds: [ALICE, BOB, CAROL],
  });
  // The room keeps running; the joiner alone is ungated later.
  assert.equal(joined.phase, SessionPhase.ACTIVE);
  assert.equal(
    joined.participants.find(({ userId }) => userId === CAROL).consent,
    ConsentState.PENDING,
  );
  assert.equal(manager.canCapture({ guildId: GUILD, userId: ALICE }), true);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: CAROL }), false);

  const declined = manager.setConsent({ guildId: GUILD, userId: CAROL, granted: false });
  // A decline binds the decliner absolutely - and only the decliner.
  assert.equal(declined.phase, SessionPhase.ACTIVE);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: CAROL }), false);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: BOB }), true);

  const left = manager.updateParticipants({ guildId: GUILD, participantIds: [ALICE, BOB] });
  assert.equal(left.phase, SessionPhase.ACTIVE);
});

test("pipeline health fails closed and can recover without losing consent", () => {
  const manager = new SessionManager({ captureHealthy: true });
  start(manager);
  manager.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
  manager.setConsent({ guildId: GUILD, userId: BOB, granted: true });

  manager.setCaptureHealth(false, "voice_connection_error");
  assert.equal(manager.getSession(GUILD).phase, SessionPhase.PAUSED);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: ALICE }), false);

  manager.setCaptureHealth(true);
  assert.equal(manager.getSession(GUILD).phase, SessionPhase.ACTIVE);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: ALICE }), true);
});

test("any current participant can stop and outsiders cannot", () => {
  const manager = new SessionManager({ captureHealthy: true });
  start(manager);

  assert.throws(
    () => manager.stopSession({ guildId: GUILD, requestedBy: CAROL }),
    (error) => error instanceof SessionStateError && error.code === "stop_not_allowed",
  );
  const stopped = manager.stopSession({ guildId: GUILD, requestedBy: BOB });
  assert.equal(stopped.phase, SessionPhase.STOPPED);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: BOB }), false);
});

test("the starter can leave without owning the remaining session", () => {
  const manager = new SessionManager({ captureHealthy: true });
  start(manager);
  manager.setConsent({ guildId: GUILD, userId: ALICE, granted: true });
  manager.setConsent({ guildId: GUILD, userId: BOB, granted: true });

  const remaining = manager.updateParticipants({ guildId: GUILD, participantIds: [BOB] });
  assert.equal(remaining.phase, SessionPhase.ACTIVE);
  assert.equal(remaining.startedBy, ALICE);
  assert.deepEqual(remaining.participants.map(({ userId }) => userId), [BOB]);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: BOB }), true);

  const stopped = manager.stopSession({ guildId: GUILD, requestedBy: ALICE });
  assert.equal(stopped.phase, SessionPhase.STOPPED);
});

test("character mappings are scoped to memory and included in session snapshots", () => {
  const manager = new SessionManager({ captureHealthy: true });
  manager.mapCharacter({
    guildId: GUILD,
    userId: ALICE,
    characterName: "Mira",
    realm: "Moon Guard",
    wowClass: "priest",
    role: "healer",
  });
  const session = start(manager, [ALICE]);

  assert.deepEqual(session.participants[0].character, {
    characterName: "Mira",
    realm: "Moon Guard",
    wowClass: "priest",
    role: "healer",
  });
  assert.equal(manager.clearCharacter({ guildId: GUILD, userId: ALICE }), true);
  assert.equal(manager.getCharacter({ guildId: GUILD, userId: ALICE }), null);
});

function memoryLedger(initial = []) {
  const entries = new Map(initial);
  return {
    entries,
    ledger: {
      recall: (guildId, userId) => entries.get(`${guildId}:${userId}`) ?? null,
      remember: (guildId, userId, state) => {
        entries.set(`${guildId}:${userId}`, state);
        return true;
      },
      forget: (guildId, userId) => {
        const existed = entries.delete(`${guildId}:${userId}`);
        return { existed, persisted: true };
      },
    },
  };
}

test("session-only answers survive voice rejoin and end with the caption session", () => {
  const state = memoryLedger();
  const manager = new SessionManager({
    captureHealthy: true,
    consentLedger: state.ledger,
  });
  start(manager);
  manager.setConsent({ guildId: GUILD, userId: ALICE, granted: true, remember: false });
  manager.setConsent({ guildId: GUILD, userId: BOB, granted: false, remember: false });

  manager.updateParticipants({ guildId: GUILD, participantIds: [] });
  const rejoined = manager.updateParticipants({
    guildId: GUILD,
    participantIds: [ALICE, BOB],
  });
  assert.equal(
    rejoined.participants.find(({ userId }) => userId === ALICE).consent,
    ConsentState.GRANTED,
  );
  assert.equal(
    rejoined.participants.find(({ userId }) => userId === BOB).consent,
    ConsentState.DECLINED,
  );
  assert.equal(manager.canCapture({ guildId: GUILD, userId: ALICE }), true);
  assert.equal(state.entries.size, 0);

  manager.stopSession({ guildId: GUILD, requestedBy: ALICE });
  const next = start(manager);
  assert.equal(
    next.participants.find(({ userId }) => userId === ALICE).consent,
    ConsentState.PENDING,
  );
  assert.equal(
    next.participants.find(({ userId }) => userId === BOB).consent,
    ConsentState.PENDING,
  );
});

test("session-only answers replace old records without clearing character mappings", () => {
  const state = memoryLedger([
    [`${GUILD}:${ALICE}`, "granted"],
    [`${GUILD}:${BOB}`, "declined"],
  ]);
  const manager = new SessionManager({
    captureHealthy: true,
    consentLedger: state.ledger,
  });
  manager.mapCharacter({
    guildId: GUILD,
    userId: ALICE,
    characterName: "Mira",
    wowClass: "priest",
    role: "healer",
  });
  start(manager);

  manager.setConsent({ guildId: GUILD, userId: ALICE, granted: false, remember: false });
  manager.setConsent({ guildId: GUILD, userId: BOB, granted: true, remember: false });
  assert.equal(state.entries.size, 0);
  assert.equal(manager.getCharacter({ guildId: GUILD, userId: ALICE }).characterName, "Mira");

  manager.updateParticipants({ guildId: GUILD, participantIds: [] });
  const rejoined = manager.updateParticipants({
    guildId: GUILD,
    participantIds: [ALICE, BOB],
  });
  assert.equal(
    rejoined.participants.find(({ userId }) => userId === ALICE).consent,
    ConsentState.DECLINED,
  );
  assert.equal(
    rejoined.participants.find(({ userId }) => userId === BOB).consent,
    ConsentState.GRANTED,
  );
});

test("an absent user cannot grant, but a session-only decline waits for their rejoin", () => {
  const state = memoryLedger();
  const manager = new SessionManager({
    captureHealthy: true,
    consentLedger: state.ledger,
  });
  start(manager, [ALICE]);

  assert.throws(
    () => manager.setConsent({
      guildId: GUILD,
      userId: BOB,
      granted: true,
      remember: false,
    }),
    (error) => error instanceof SessionStateError && error.code === "not_participant",
  );
  manager.setConsent({ guildId: GUILD, userId: BOB, granted: false, remember: false });
  const joined = manager.updateParticipants({
    guildId: GUILD,
    participantIds: [ALICE, BOB],
  });
  assert.equal(
    joined.participants.find(({ userId }) => userId === BOB).consent,
    ConsentState.DECLINED,
  );
  assert.equal(state.entries.size, 0);
});

test("forget also clears a transient answer and makes rejoin pending", () => {
  const state = memoryLedger();
  const manager = new SessionManager({
    captureHealthy: true,
    consentLedger: state.ledger,
  });
  start(manager);
  manager.setConsent({ guildId: GUILD, userId: ALICE, granted: true, remember: false });

  const forgotten = manager.forgetConsent({ guildId: GUILD, userId: ALICE });
  assert.equal(forgotten.existed, true);
  assert.equal(
    forgotten.session.participants.find(({ userId }) => userId === ALICE).consent,
    ConsentState.PENDING,
  );
  manager.updateParticipants({ guildId: GUILD, participantIds: [BOB] });
  const rejoined = manager.updateParticipants({
    guildId: GUILD,
    participantIds: [ALICE, BOB],
  });
  assert.equal(
    rejoined.participants.find(({ userId }) => userId === ALICE).consent,
    ConsentState.PENDING,
  );
});

test("retention failures keep the live answer but never report it remembered", () => {
  const entries = new Map();
  const manager = new SessionManager({
    captureHealthy: true,
    consentLedger: {
      recall: (guildId, userId) => entries.get(`${guildId}:${userId}`) ?? null,
      remember: (guildId, userId, state) => {
        entries.set(`${guildId}:${userId}`, state);
        return false;
      },
      forget: (guildId, userId) => {
        const existed = entries.delete(`${guildId}:${userId}`);
        return { existed, persisted: false };
      },
    },
  });
  start(manager, [ALICE]);

  manager.setConsent({ guildId: GUILD, userId: ALICE, granted: true, remember: true });
  assert.equal(manager.canCapture({ guildId: GUILD, userId: ALICE }), true);
  assert.equal(manager.consentWritePersisted, false);
  assert.equal(manager.getConsentStatus({ guildId: GUILD, userId: ALICE }).remembered, false);

  manager.setConsent({ guildId: GUILD, userId: ALICE, granted: false, remember: false });
  assert.equal(manager.canCapture({ guildId: GUILD, userId: ALICE }), false);
  assert.equal(manager.consentWritePersisted, false);
  assert.equal(manager.getConsentStatus({ guildId: GUILD, userId: ALICE }).remembered, false);
});
