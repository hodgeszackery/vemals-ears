import assert from "node:assert/strict";
import test from "node:test";

import { controlNoticePayload } from "../src/discord_runtime.js";
import { CaptionCommandController } from "../src/command_controller.js";
import { SessionManager, SessionPhase } from "../src/session_manager.js";

const GUILD = "123456789012345678";
const VOICE = "223456789012345678";
const CONTROL = "323456789012345678";
const ALICE = "423456789012345678";
const BOB = "423456789012345679";

function input(subcommand, userId = ALICE) {
  return {
    subcommand,
    guildId: GUILD,
    userId,
    voiceChannelId: VOICE,
    controlChannelId: CONTROL,
    participantIds: [ALICE, BOB],
  };
}

// Shino, 2026-09-02: "remove any pings VE does to users when they re-enter
// or anywhere else." Walking back into a voice channel must not buzz
// anybody's phone. The mention TEXT stays - Discord renders <@id> as the
// display name whether or not it notifies - so the notice stays readable
// and simply stops being a notification.
//
// Every control notice goes through this payload: the join notice builds
// <@id> itself, and the consent and stop notices arrive as content from
// mentionUser() in the controller.
test("a control notice never notifies anyone", () => {
  const payload = controlNoticePayload("<@423456789012345678> joined.");
  assert.deepEqual(payload.allowedMentions.parse, []);
  assert.deepEqual(payload.allowedMentions.users, []);
  assert.deepEqual(payload.allowedMentions.roles, []);
  assert.equal(payload.allowedMentions.repliedUser, false);
  // The text is untouched - suppression is not censorship.
  assert.equal(payload.content, "<@423456789012345678> joined.");
});

test("a notice carrying buttons is suppressed too", () => {
  const components = [{ type: 1, components: [] }];
  const payload = controlNoticePayload("consent?", components);
  assert.deepEqual(payload.allowedMentions.parse, []);
  assert.equal(payload.components, components);
});

test("the start notice no longer tells people to run /vem consent", () => {
  const manager = new SessionManager({ captureHealthy: true });
  const controller = new CaptionCommandController(manager);

  const started = controller.execute(input("start"));

  assert.equal(started.session.phase, SessionPhase.AWAITING_CONSENT);
  assert.doesNotMatch(started.content, /\/vem consent/);
  // The factual state still has to be said - dropping the nagging is not
  // the same as hiding that capture is off.
  assert.match(started.content, /Audio capture is OFF/);
});

test("a withdrawal no longer tells people to run /vem consent", () => {
  // A ledger that actually persists, so this exercises the "Recorded."
  // branch that carried the instruction. Without one the controller falls
  // through to its could-not-save wording, which never nagged - the test
  // passed while proving nothing, and a sabotage run caught it.
  const manager = new SessionManager({
    captureHealthy: true,
    consentLedger: {
      recall: () => null,
      remember: () => true,
      forget: () => ({ persisted: true }),
    },
  });
  const controller = new CaptionCommandController(manager);

  const declined = controller.execute(input("decline"));

  assert.match(declined.content, /Recorded\./);
  assert.doesNotMatch(declined.content, /\/vem consent/);
});

test("the consent command itself still exists", async () => {
  // Shino chose "keep the command, drop the nagging". Removing it would
  // leave no way at all to grant consent, so this guards the half of the
  // decision that is easy to lose in a later cleanup.
  const schema = await import("../src/command_schema.js");
  const json = JSON.stringify(schema);
  assert.match(json, /"consent"/);
});
