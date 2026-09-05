import { randomBytes } from "node:crypto";

import { SessionPhase, SessionStateError } from "./session_manager.js";

export const ENROLLMENT_TTL_SECONDS = 30 * 60;

/** A pairing code in the relay's own shape, from a real CSPRNG. */
export function newEnrollmentCode() {
  return "ghe_" + randomBytes(24).toString("base64url");
}

export class CommandInputError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "CommandInputError";
    this.code = code;
  }
}

function mentionUser(userId) {
  return `<@${userId}>`;
}

function consentSummary(session) {
  const counts = { declined: 0, granted: 0, pending: 0 };
  for (const participant of session.participants) {
    counts[participant.consent] += 1;
  }
  return `${counts.granted} consented, ${counts.pending} pending, ${counts.declined} declined`;
}

export function mappingSummary(mapping, persisted = true) {
  const realm = mapping.realm ? `-${mapping.realm}` : "";
  const identity =
    `You are ${mapping.characterName}${realm} (${mapping.role}, ${mapping.wowClass}).`;
  if (!persisted) {
    return (
      identity +
      " Your mapping is active now, but Vemal's Ears could not save it for " +
      "the next bot restart. Run /vem map again to retry."
    );
  }
  return (
    identity + " This is remembered until you run /vem unmap."
  );
}

function capturedOf(session) {
  const granted = session.participants.filter(
    (participant) => participant.consent === "granted",
  ).length;
  return `${granted} of ${session.participants.length}`;
}

// Matches GuildHub's human-safe Crockford-style code alphabet: no I, L, O,
// or U, which are easy to misread aloud or on a small overlay.
const VIEWER_LINK_CODE = /^[0-9A-HJKMNP-TV-Z]{8}$/;

export function normalizeViewerLinkCode(value) {
  const normalized = typeof value === "string" ? value.trim().toUpperCase() : "";
  if (!VIEWER_LINK_CODE.test(normalized)) {
    throw new CommandInputError(
      "invalid_link_code",
      "Enter the 8-character code shown by your caption viewer.",
    );
  }
  return normalized;
}

export class CaptionCommandController {
  constructor(sessionManager) {
    this.sessionManager = sessionManager;
  }

  execute(input) {
    if (!input?.guildId || !input?.userId || !input?.subcommand) {
      throw new CommandInputError("invalid_command_context", "This command requires a server.");
    }

    switch (input.subcommand) {
      case "start":
        return this.#start(input);
      case "consent":
        return this.#consent(input, true);
      case "decline":
        return this.#consent(input, false);
      case "forget":
        return this.#forget(input);
      case "stop":
        return this.#stop(input);
      case "status":
        return this.#status(input);
      case "setup":
        return this.#setup(input);
      case "invite":
        return this.#invite(input);
      case "map":
        return this.#map(input);
      case "unmap":
        return this.#unmap(input);
      default:
        throw new CommandInputError("unknown_subcommand", "Unknown captions command.");
    }
  }

  #start(input) {
    if (!input.voiceChannelId) {
      throw new CommandInputError(
        "voice_channel_required",
        "Join the voice channel you want captioned, then run this command again.",
      );
    }
    const session = this.sessionManager.startSession({
      guildId: input.guildId,
      voiceChannelId: input.voiceChannelId,
      controlChannelId: input.controlChannelId,
      startedBy: input.userId,
      participantIds: input.participantIds,
    });
    // The reply has to follow the phase startSession actually produced.
    // A group whose consent is all on record starts ACTIVE, and the
    // runtime joins and subscribes straight after this returns - so a
    // fixed "capture is OFF" would be the channel's only durable record
    // of the session, and it would be false. This reply is not
    // ephemeral, so everyone reads it.
    const tail =
      "New participants are not captioned unless they consent, and anyone " +
      "in the session can run `/vem stop`.";
    return {
      action: "session.started",
      ephemeral: false,
      session,
      content:
        session.phase === SessionPhase.ACTIVE
          ? `Vemal's Ears is captioning <#${session.voiceChannelId}>. ` +
            `Audio capture is ON for ${capturedOf(session)} participants - ` +
            "only those who consented. " +
            tail
          : `Vemal's Ears is waiting for consent in <#${session.voiceChannelId}>. ` +
            "Audio capture is OFF; each person who consents is captioned, " +
            "and nobody else is. " +
            tail,
    };
  }

  // The warning a withdrawal reply carries when the durable write
  // failed. Capture is already paused in memory - that part is true -
  // but nothing may claim the choice will be remembered. The next run
  // starts from an empty ledger (the dirty marker sees to it) and asks
  // everybody again, so the honest promise is exactly that.
  static UNSAVED_WITHDRAWAL =
    " Your choice could NOT be saved to disk, so it may not be" +
    " remembered after a restart - if that happens everyone is asked" +
    " again before any capture. Tell the operator, and re-run the" +
    " command to retry.";

  #consent(input, granted) {
    const session = this.sessionManager.setConsent({
      guildId: input.guildId,
      userId: input.userId,
      granted,
      remember: input.rememberAnswer ?? true,
    });
    // A withdrawal with nothing running returns no session: it was recorded
    // for next time. Withdrawing must not require a live session, or the
    // only moment you could take it back would be the moment it is running.
    if (!session) {
      const saved = this.sessionManager.consentWritePersisted;
      return {
        action: "consent.declined",
        ephemeral: true,
        session: null,
        content: saved
          ? "Recorded. You will not be captioned, and Vemal's Ears will not " +
            "ask again."
          : "You will not be captioned right now." +
            CaptionCommandController.UNSAVED_WITHDRAWAL,
      };
    }
    // A withdrawal from somebody outside the session is recorded for next
    // time, but announcing a pause here would be a lie: capture is unchanged
    // for the people actually being recorded, and they would hear an
    // all-clear that never happened.
    const inSession = session.participants.some(({ userId }) => userId === input.userId);
    if (!granted && !inSession) {
      const saved = this.sessionManager.consentWritePersisted;
      return {
        action: "consent.declined",
        ephemeral: true,
        session,
        content: saved
          ? "Recorded. You are not in this caption session, so nothing about " +
            "capture changed for the people in it. Vemal's Ears will not " +
            "capture you if you join."
          : "You are not in this caption session, and nothing about capture " +
            "changed for the people in it." +
            CaptionCommandController.UNSAVED_WITHDRAWAL,
      };
    }
    return {
      action: granted ? "consent.granted" : "consent.declined",
      ephemeral: false,
      session,
      content: granted
        ? `${mentionUser(input.userId)} consented and is now captioned. ` +
          `Capturing ${capturedOf(session)} participants.`
        : `${mentionUser(input.userId)} declined or withdrew consent and is no longer captioned. Captions continue for ${capturedOf(session)} participants who consented.${
            this.sessionManager.consentWritePersisted
              ? ""
              : CaptionCommandController.UNSAVED_WITHDRAWAL
          }`,
    };
  }

  #forget(input) {
    const forgotten = this.sessionManager.forgetConsent({
      guildId: input.guildId,
      userId: input.userId,
    });
    const failedRecords = [
      forgotten.consentPersisted ? null : "consent answer",
      forgotten.mappingPersisted ? null : "character mapping",
    ].filter(Boolean);
    return {
      action: "consent.forgotten",
      ephemeral: true,
      session: forgotten.session,
      content: failedRecords.length > 0
        ? "Cleared for this bot session, but Vemal's Ears could not erase your " +
          `remembered ${failedRecords.join(" and ")} from disk. Run /vem forget ` +
          "again before the bot restarts. Capture remains off unless you consent again."
        : !forgotten.existed
          ? "Nothing to forget - Vemal's Ears holds no remembered consent answer or " +
            "character mapping for you."
          : "Forgotten. Vemal's Ears holds no remembered consent answer or character " +
            "mapping for you, and will ask again before capturing you.",
    };
  }

  #stop(input) {
    const session = this.sessionManager.stopSession({
      guildId: input.guildId,
      requestedBy: input.userId,
    });
    return {
      action: "session.stopped",
      ephemeral: false,
      session,
      content: `${mentionUser(input.userId)} stopped Vemal's Ears. No more audio will be captured.`,
    };
  }

  #status(input) {
    const session = this.sessionManager.getSession(input.guildId);
    if (!session || session.phase === SessionPhase.STOPPED) {
      return {
        action: "session.status",
        ephemeral: true,
        session: null,
        content: "No Vemal's Ears session is open in this server.",
      };
    }
    const reason = session.pauseReason ? ` (${session.pauseReason.replaceAll("_", " ")})` : "";
    return {
      action: "session.status",
      ephemeral: true,
      session,
      content: `Vemal's Ears: ${session.phase}${reason}; ${consentSummary(session)}.`,
    };
  }

  #setup(input) {
    // Shino, 2026-09-01: no "link Discord" step. The invitation carries
    // who asked for it, so captions can follow voice presence afterwards
    // with nothing left to link. The code is minted here because it is
    // shown here, privately, in the same reply.
    return {
      action: "viewer.enroll.requested",
      ephemeral: true,
      session: this.sessionManager.getSession(input.guildId),
      enrollment: {
        code: newEnrollmentCode(),
        ttlSeconds: ENROLLMENT_TTL_SECONDS,
      },
      content: "",
    };
  }

  #invite(input) {
    // Shino distributes pairing links himself rather than asking every
    // raider to run a command. A link still has to carry WHO it is for,
    // or the device that claims it can never be matched to the person
    // who left the voice channel - so the invitee is named here, and the
    // link that comes back is theirs alone.
    if (!input.canInvite) {
      throw new CommandInputError(
        "invite_not_allowed",
        "Only someone who can manage this server may create a pairing link "
          + "for another person. Run `/vem setup` to get your own.",
      );
    }
    if (!input.inviteeId) {
      throw new CommandInputError(
        "invitee_required",
        "Choose the person this pairing link is for.",
      );
    }
    if (input.inviteeIsBot) {
      throw new CommandInputError(
        "invitee_is_a_bot",
        "A bot cannot read captions.",
      );
    }
    return {
      action: "viewer.enroll.requested",
      ephemeral: true,
      session: this.sessionManager.getSession(input.guildId),
      enrollment: {
        code: newEnrollmentCode(),
        ttlSeconds: ENROLLMENT_TTL_SECONDS,
      },
      inviteeId: input.inviteeId,
      content: "",
    };
  }

  #map(input) {
    const requested = {
      characterName: input.mapping?.characterName || "",
      realm: input.mapping?.realm || "",
      wowClass: input.mapping?.wowClass || "",
      role: input.mapping?.role || "",
    };
    // Nothing, or only part of it, opens the modal rather than failing.
    // Picking a class from a menu is what most people will want; typing
    // all four options in one line is the shortcut, not the main path.
    if (!requested.characterName || !requested.wowClass || !requested.role) {
      const stored = this.sessionManager.getCharacter({
        guildId: input.guildId,
        userId: input.userId,
      });
      return {
        action: "mapping.panel",
        ephemeral: true,
        // What is filled in so far: whatever they typed wins, then
        // whatever they already had stored.
        draft: {
          characterName: requested.characterName || stored?.characterName || "",
          realm: requested.realm || stored?.realm || "",
          wowClass: requested.wowClass || stored?.wowClass || "",
          role: requested.role || stored?.role || "",
        },
        session: this.sessionManager.getSession(input.guildId),
        content: "Choose your class below. It is remembered until you unmap.",
      };
    }
    const mapping = this.sessionManager.mapCharacter({
      guildId: input.guildId,
      userId: input.userId,
      characterName: requested.characterName,
      realm: requested.realm || null,
      wowClass: requested.wowClass,
      role: requested.role,
    });
    const persisted = this.sessionManager.mappingWritePersisted;
    return {
      action: "mapping.changed",
      ephemeral: true,
      mapping,
      persisted,
      session: this.sessionManager.getSession(input.guildId),
      content: mappingSummary(mapping, persisted),
    };
  }

  #unmap(input) {
    const removed = this.sessionManager.clearCharacter({
      guildId: input.guildId,
      userId: input.userId,
    });
    const persisted = this.sessionManager.mappingWritePersisted;
    return {
      action: "mapping.changed",
      ephemeral: true,
      mapping: null,
      persisted,
      session: this.sessionManager.getSession(input.guildId),
      content: !persisted
        ? "Your mapping is cleared for this bot session, but Vemal's Ears could not erase " +
          "the remembered copy. Run /vem unmap again before the bot restarts."
        : removed
          ? "Your character mapping was cleared."
          : "You had no character mapping.",
    };
  }
}

export function publicCommandError(error) {
  if (error instanceof CommandInputError || error instanceof SessionStateError) {
    return { code: error.code, message: error.message };
  }
  return {
    code: "command_failed",
    message: "Vemal's Ears could not complete that command. Capture remains off.",
  };
}
