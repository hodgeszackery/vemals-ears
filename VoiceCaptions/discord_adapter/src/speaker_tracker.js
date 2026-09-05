export class SpeakerStateError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "SpeakerStateError";
    this.code = code;
  }
}

function speakerKey(guildId, userId) {
  return `${guildId}:${userId}`;
}

function ssrcKey(guildId, ssrc) {
  return `${guildId}:${ssrc}`;
}

export class SpeakerTracker {
  constructor({ clock = () => new Date() } = {}) {
    this.clock = clock;
    this.activeBySpeaker = new Map();
    this.activeBySsrc = new Map();
    this.sequenceBySpeaker = new Map();
    this.globalSequence = 0;
  }

  begin({
    guildId,
    userId,
    ssrc = null,
    audienceGeneration,
    audienceRevision,
  }) {
    const key = speakerKey(guildId, userId);
    if (this.activeBySpeaker.has(key)) {
      throw new SpeakerStateError("speaker_already_active", "Speaker already has an active segment.");
    }
    if (ssrc !== null) {
      if (!Number.isSafeInteger(ssrc) || ssrc <= 0) {
        throw new SpeakerStateError("invalid_ssrc", "SSRC must be a positive integer.");
      }
      if (this.activeBySsrc.has(ssrcKey(guildId, ssrc))) {
        throw new SpeakerStateError("ssrc_already_active", "SSRC already belongs to an active segment.");
      }
    }
    if (!Number.isSafeInteger(audienceGeneration) || audienceGeneration <= 0) {
      throw new SpeakerStateError(
        "invalid_audience_generation",
        "Audience generation must be a positive integer.",
      );
    }
    if (!Number.isSafeInteger(audienceRevision) || audienceRevision <= 0) {
      throw new SpeakerStateError(
        "invalid_audience_revision",
        "Audience revision must be a positive integer.",
      );
    }

    const speakerSequence = (this.sequenceBySpeaker.get(key) || 0) + 1;
    const globalSequence = ++this.globalSequence;
    this.sequenceBySpeaker.set(key, speakerSequence);
    const segment = Object.freeze({
      segmentId: `${guildId}.${userId}.${speakerSequence}.${globalSequence}`,
      guildId,
      userId,
      ssrc,
      speakerSequence,
      globalSequence,
      audienceGeneration,
      audienceRevision,
      startedAt: this.clock().toISOString(),
    });
    this.activeBySpeaker.set(key, segment);
    if (ssrc !== null) {
      this.activeBySsrc.set(ssrcKey(guildId, ssrc), segment.segmentId);
    }
    return segment;
  }

  end(segmentId, reason = "completed") {
    const segment = [...this.activeBySpeaker.values()].find(
      (candidate) => candidate.segmentId === segmentId,
    );
    if (!segment) {
      return null;
    }
    this.activeBySpeaker.delete(speakerKey(segment.guildId, segment.userId));
    if (segment.ssrc !== null) {
      this.activeBySsrc.delete(ssrcKey(segment.guildId, segment.ssrc));
    }
    return Object.freeze({
      ...segment,
      endedAt: this.clock().toISOString(),
      reason,
    });
  }

  activeFor({ guildId, userId }) {
    return this.activeBySpeaker.get(speakerKey(guildId, userId)) || null;
  }

  /** End one speaker's active segment, if any - the immediate cut a
   * mid-session consent withdrawal requires. */
  clearSpeaker(guildId, userId, reason = "consent_withdrawn") {
    const ended = [];
    for (const segment of [...this.activeBySpeaker.values()]) {
      if (segment.guildId === guildId && segment.userId === userId) {
        ended.push(this.end(segment.segmentId, reason));
      }
    }
    return ended;
  }

  clearGuild(guildId, reason = "capture_paused") {
    const ended = [];
    for (const segment of [...this.activeBySpeaker.values()]) {
      if (segment.guildId === guildId) {
        ended.push(this.end(segment.segmentId, reason));
      }
    }
    return ended;
  }

  snapshot() {
    return Object.freeze([...this.activeBySpeaker.values()]);
  }
}
