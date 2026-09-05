import assert from "node:assert/strict";
import test from "node:test";

import { SpeakerStateError, SpeakerTracker } from "../src/speaker_tracker.js";

const GUILD = "123456789012345678";
const ALICE = "423456789012345678";
const BOB = "523456789012345678";
const AUDIENCE = { audienceGeneration: 7, audienceRevision: 11 };

test("simultaneous speakers keep independent user, SSRC, and sequence state", () => {
  const tracker = new SpeakerTracker({
    clock: () => new Date("2026-08-29T21:00:00.000Z"),
  });

  const aliceOne = tracker.begin({ guildId: GUILD, userId: ALICE, ssrc: 101, ...AUDIENCE });
  const bobOne = tracker.begin({ guildId: GUILD, userId: BOB, ssrc: 202, ...AUDIENCE });

  assert.equal(aliceOne.speakerSequence, 1);
  assert.equal(bobOne.speakerSequence, 1);
  assert.equal(aliceOne.globalSequence, 1);
  assert.equal(bobOne.globalSequence, 2);
  assert.equal(aliceOne.ssrc, 101);
  assert.equal(bobOne.ssrc, 202);
  assert.equal(aliceOne.audienceGeneration, 7);
  assert.equal(aliceOne.audienceRevision, 11);
  assert.equal(tracker.snapshot().length, 2);

  tracker.end(aliceOne.segmentId, "silence");
  assert.equal(tracker.activeFor({ guildId: GUILD, userId: BOB }), bobOne);
  const aliceTwo = tracker.begin({
    guildId: GUILD,
    userId: ALICE,
    ssrc: 303,
    audienceGeneration: 7,
    audienceRevision: 12,
  });
  assert.equal(aliceTwo.speakerSequence, 2);
  assert.equal(aliceTwo.globalSequence, 3);
  assert.equal(tracker.activeFor({ guildId: GUILD, userId: BOB }), bobOne);

  // A late completion from Alice's first stream cannot erase her second.
  assert.equal(tracker.end(aliceOne.segmentId, "late_close"), null);
  assert.equal(tracker.activeFor({ guildId: GUILD, userId: ALICE }), aliceTwo);
});

test("SSRC identity collisions fail closed instead of merging speakers", () => {
  const tracker = new SpeakerTracker();
  tracker.begin({ guildId: GUILD, userId: ALICE, ssrc: 101, ...AUDIENCE });

  assert.throws(
    () => tracker.begin({ guildId: GUILD, userId: BOB, ssrc: 101, ...AUDIENCE }),
    (error) => error instanceof SpeakerStateError && error.code === "ssrc_already_active",
  );
  assert.equal(tracker.snapshot().length, 1);
});

test("clearing one guild ends every overlapping stream in that guild", () => {
  const tracker = new SpeakerTracker();
  tracker.begin({ guildId: GUILD, userId: ALICE, ssrc: 101, ...AUDIENCE });
  tracker.begin({ guildId: GUILD, userId: BOB, ssrc: 202, ...AUDIENCE });

  const ended = tracker.clearGuild(GUILD, "consent_required");
  assert.equal(ended.length, 2);
  assert.ok(ended.every(({ reason }) => reason === "consent_required"));
  assert.equal(tracker.snapshot().length, 0);
});

test("a segment cannot begin without an exact audience revision", () => {
  const tracker = new SpeakerTracker();
  assert.throws(
    () => tracker.begin({ guildId: GUILD, userId: ALICE, ssrc: 101 }),
    (error) =>
      error instanceof SpeakerStateError && error.code === "invalid_audience_generation",
  );
  assert.throws(
    () => tracker.begin({
      guildId: GUILD,
      userId: ALICE,
      ssrc: 101,
      audienceGeneration: 1,
      audienceRevision: 0,
    }),
    (error) =>
      error instanceof SpeakerStateError && error.code === "invalid_audience_revision",
  );
});
