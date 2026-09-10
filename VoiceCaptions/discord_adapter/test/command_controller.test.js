import assert from "node:assert/strict";
import test from "node:test";

import { captionsCommandJson } from "../src/command_schema.js";
import {
  CaptionCommandController,
  ENROLLMENT_TTL_SECONDS,
  newEnrollmentCode,
  publicCommandError,
} from "../src/command_controller.js";
import { SessionManager, SessionPhase } from "../src/session_manager.js";

const GUILD = "123456789012345678";
const VOICE = "223456789012345678";
const CONTROL = "323456789012345678";
const ALICE = "423456789012345678";
const BOB = "523456789012345678";
const CAROL = "623456789012345678";

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

test("slash command schema exposes the consent and mapping workflow", () => {
  assert.equal(captionsCommandJson.name, "vem");
  assert.deepEqual(
    captionsCommandJson.options.map(({ name }) => name),
    [
      "start",
      "consent",
      "decline",
      "forget",
      "stop",
      "status",
      "setup",
      "invite",
      "map",
      "unmap",
    ],
  );
  assert.match(
    captionsCommandJson.options.find(({ name }) => name === "forget").description,
    /consent and character mapping/i,
  );
});

test("setup hands out a private pairing invitation that carries who asked", () => {
  // Shino, 2026-09-01: the Link Discord button is the wrong way round.
  // Pairing is the step people already take, so the invitation carries
  // their identity and captions can follow voice presence afterwards.
  const controller = new CaptionCommandController(
    new SessionManager({ captureHealthy: true }),
  );
  const first = controller.execute(input("setup"));
  assert.equal(first.action, "viewer.enroll.requested");
  assert.equal(first.ephemeral, true, "a pairing link must never be public");
  assert.match(first.enrollment.code, /^ghe_[A-Za-z0-9_-]{32}$/);
  assert.equal(first.enrollment.ttlSeconds, ENROLLMENT_TTL_SECONDS);
  // Every invitation is its own; one person's link can never be another's.
  const second = controller.execute(input("setup"));
  assert.notEqual(second.enrollment.code, first.enrollment.code);
  // And the codes are unguessable: 24 random bytes, base64url.
  const codes = new Set(
    Array.from({ length: 50 }, () => newEnrollmentCode()),
  );
  assert.equal(codes.size, 50);
});

test("the link-code step is gone: pairing carries the identity", () => {
  // Shino, 2026-09-01: "'Link discord' button is the wrong way to go."
  // The code exchange it fed is retired with it; anything still sending
  // `link` is an old client and must be told, not quietly accepted.
  const controller = new CaptionCommandController(
    new SessionManager({ captureHealthy: true }),
  );
  assert.equal(
    captionsCommandJson.options.some(({ name }) => name === "link"),
    false,
  );
  assert.throws(
    () => controller.execute({ ...input("link"), linkCode: "AB12CD34" }),
    (error) => error.code === "unknown_subcommand",
  );
});

test("an operator can create a pairing link for someone else", () => {
  // Shino distributes links himself rather than asking every raider to
  // run a command. The link still has to belong to the person it is for,
  // or presence can never match their device when they leave voice.
  const controller = new CaptionCommandController(
    new SessionManager({ captureHealthy: true }),
  );
  const invited = controller.execute({
    ...input("invite"),
    canInvite: true,
    inviteeId: "555555555555555555",
  });
  assert.equal(invited.action, "viewer.enroll.requested");
  assert.equal(invited.ephemeral, true, "a credential must never be public");
  assert.equal(invited.inviteeId, "555555555555555555");
  assert.match(invited.enrollment.code, /^ghe_[A-Za-z0-9_-]{32}$/);
});

test("only a guild manager may create a link for another person", () => {
  const controller = new CaptionCommandController(
    new SessionManager({ captureHealthy: true }),
  );
  assert.throws(
    () => controller.execute({
      ...input("invite"),
      canInvite: false,
      inviteeId: "555555555555555555",
    }),
    (error) => error.code === "invite_not_allowed",
    "anyone could mint a credential for another computer",
  );
  // A missing target, or a bot, is refused rather than guessed.
  assert.throws(
    () => controller.execute({ ...input("invite"), canInvite: true, inviteeId: null }),
    (error) => error.code === "invitee_required",
  );
  assert.throws(
    () => controller.execute({
      ...input("invite"),
      canInvite: true,
      inviteeId: "555555555555555555",
      inviteeIsBot: true,
    }),
    (error) => error.code === "invitee_is_a_bot",
  );
});

test("controller creates a visible consent session before capture", () => {
  const manager = new SessionManager({ captureHealthy: true });

test("start tells the channel capture is ON when consent is remembered", () => {
  // The existing test above builds a manager with no ledger, so its
  // AWAITING_CONSENT and its "capture is OFF" are both correct. This is
  // the other path, which the hosted worker makes real: at least one
  // participant has a remembered grant, startSession returns ACTIVE, and the
  // runtime subscribes immediately after. Saying capture is OFF there is
  // the one thing this reply must never do.
  const remembered = {
    recall: () => "granted",
    remember: () => {},
    forget: () => false,
  };
  const manager = new SessionManager({
    captureHealthy: true,
    consentLedger: remembered,
  });
  const controller = new CaptionCommandController(manager);

  const started = controller.execute(input("start"));

  assert.equal(started.session.phase, SessionPhase.ACTIVE);
  assert.doesNotMatch(started.content, /capture is OFF/);
  assert.match(started.content, /capture is ON/);
});
  const controller = new CaptionCommandController(manager);

  const started = controller.execute(input("start"));
  assert.equal(started.ephemeral, false);
  assert.equal(started.session.phase, SessionPhase.AWAITING_CONSENT);
  assert.match(started.content, /Audio capture is OFF/);

  const first = controller.execute(input("consent", ALICE));
  // The first consent already runs the room, for that person only.
  assert.equal(first.session.phase, SessionPhase.ACTIVE);
  assert.match(first.content, /Capturing 1 of 2/);
  const active = controller.execute(input("consent", BOB));
  assert.equal(active.session.phase, SessionPhase.ACTIVE);
  assert.match(active.content, /Capturing 2 of 2/);
});

test("declining from outside the session announces no pause that did not happen", () => {
  const manager = new SessionManager({ captureHealthy: true });
  const controller = new CaptionCommandController(manager);

  controller.execute(input("start"));
  controller.execute(input("consent", ALICE));
  const active = controller.execute(input("consent", BOB));
  assert.equal(active.session.phase, SessionPhase.ACTIVE);

  const declined = controller.execute(input("decline", CAROL));
  assert.equal(declined.ephemeral, true);
  assert.doesNotMatch(declined.content, /paused/i);
  assert.equal(declined.session.phase, SessionPhase.ACTIVE);
  assert.equal(manager.canCapture({ guildId: GUILD, userId: ALICE }), true);
});

test("mapping is private command feedback and remains memory-only", () => {
  const manager = new SessionManager({ captureHealthy: true });
  const controller = new CaptionCommandController(manager);
  const mapped = controller.execute({
    ...input("map"),
    mapping: {
      characterName: "Drex",
      realm: null,
      wowClass: "warrior",
      role: "tank",
    },
  });

  assert.equal(mapped.ephemeral, true);
  assert.equal(mapped.mapping.characterName, "Drex");
  assert.equal(manager.getCharacter({ guildId: GUILD, userId: ALICE }).role, "tank");
});

test("running map with nothing opens the panel instead of failing", () => {
  // Typing four options on one line is the shortcut, not the main path.
  // Someone who just runs /vem map should get menus to pick from.
  const manager = new SessionManager({ captureHealthy: true });
  const controller = new CaptionCommandController(manager);

  const opened = controller.execute({ ...input("map") });

  assert.equal(opened.action, "mapping.panel");
  assert.equal(opened.ephemeral, true);
  assert.deepEqual(opened.draft, {
    characterName: "",
    realm: "",
    wowClass: "",
    role: "",
  });
  assert.equal(manager.getCharacter({ guildId: GUILD, userId: ALICE }), null);
});

test("the panel is pre-filled from what is already stored", () => {
  const manager = new SessionManager({ captureHealthy: true });
  const controller = new CaptionCommandController(manager);
  controller.execute({
    ...input("map"),
    mapping: {
      characterName: "Drex",
      realm: "Area 52",
      wowClass: "warrior",
      role: "tank",
    },
  });

  const reopened = controller.execute({ ...input("map") });

  assert.equal(reopened.action, "mapping.panel");
  assert.equal(reopened.draft.characterName, "Drex");
  assert.equal(reopened.draft.realm, "Area 52");
  assert.equal(reopened.draft.wowClass, "warrior");
  assert.equal(reopened.draft.role, "tank");
});

test("a partly typed map keeps what was typed and asks for the rest", () => {
  const manager = new SessionManager({ captureHealthy: true });
  const controller = new CaptionCommandController(manager);

  const opened = controller.execute({
    ...input("map"),
    mapping: { characterName: "Drex", wowClass: "rogue" },
  });

  assert.equal(opened.action, "mapping.panel");
  assert.equal(opened.draft.characterName, "Drex");
  assert.equal(opened.draft.wowClass, "rogue");
  assert.equal(opened.draft.role, "");
  // Nothing incomplete is ever written.
  assert.equal(manager.getCharacter({ guildId: GUILD, userId: ALICE }), null);
});

test("a complete map still saves in one shot", () => {
  const manager = new SessionManager({ captureHealthy: true });
  const controller = new CaptionCommandController(manager);

  const saved = controller.execute({
    ...input("map"),
    mapping: {
      characterName: "Drex",
      realm: null,
      wowClass: "warrior",
      role: "tank",
    },
  });

  assert.equal(saved.action, "mapping.changed");
  assert.match(saved.content, /remembered until you run \/vem unmap/);
  assert.doesNotMatch(saved.content, /cleared when the service stops/);
});

test("a complete map reports when it is active but not durable", () => {
  const mappingLedger = {
    recallAll: () => [],
    remember: () => false,
    forget: () => ({ existed: false, persisted: true }),
  };
  const manager = new SessionManager({ captureHealthy: true, mappingLedger });
  const controller = new CaptionCommandController(manager);

  const saved = controller.execute({
    ...input("map"),
    mapping: {
      characterName: "Drex",
      realm: null,
      wowClass: "warrior",
      role: "tank",
    },
  });

  assert.equal(saved.persisted, false);
  assert.match(saved.content, /active now/i);
  assert.match(saved.content, /could not save it for the next bot restart/i);
  assert.doesNotMatch(saved.content, /remembered until/i);
});

test("forget reports a mapping-only erase failure as a mapping failure", () => {
  const consentLedger = {
    recall: () => null,
    remember: () => true,
    forget: () => ({ existed: false, persisted: true }),
  };
  const mappingLedger = {
    recallAll: () => [],
    remember: () => true,
    forget: () => ({ existed: true, persisted: false }),
  };
  const manager = new SessionManager({
    captureHealthy: true,
    consentLedger,
    mappingLedger,
  });
  const controller = new CaptionCommandController(manager);
  controller.execute({
    ...input("map"),
    mapping: {
      characterName: "Drex",
      realm: null,
      wowClass: "warrior",
      role: "tank",
    },
  });

  const forgotten = controller.execute(input("forget"));

  assert.doesNotMatch(forgotten.content, /Nothing to forget/i);
  assert.match(forgotten.content, /could not erase.*character mapping/i);
  assert.doesNotMatch(forgotten.content, /consent answer/i);
  assert.equal(manager.getCharacter({ guildId: GUILD, userId: ALICE }), null);
});

test("forget never calls an uncertain mapping ledger empty", () => {
  const consentLedger = {
    recall: () => null,
    remember: () => true,
    forget: () => ({ existed: false, persisted: true }),
  };
  const mappingLedger = {
    recallAll: () => [],
    remember: () => true,
    forget: () => ({ existed: false, persisted: false }),
  };
  const controller = new CaptionCommandController(
    new SessionManager({ consentLedger, mappingLedger }),
  );

  const forgotten = controller.execute(input("forget"));

  assert.doesNotMatch(forgotten.content, /Nothing to forget/i);
  assert.match(forgotten.content, /could not erase.*character mapping/i);
});

test("an unexpected command error does not invent a capture state", () => {
  const safe = publicCommandError(new Error("private detail"));

  assert.deepEqual(safe, {
    code: "command_failed",
    message: "Vemal's Ears could not complete that command.",
  });
  assert.doesNotMatch(safe.message, /capture/i);
});
