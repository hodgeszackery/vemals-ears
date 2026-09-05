import OpusScript from "opusscript";

const DISCORD_SAMPLE_RATE_HZ = 48_000;
const DISCORD_CHANNELS = 2;
const STT_SAMPLE_RATE_HZ = 16_000;
const STT_CHANNELS = 1;
const PCM_SAMPLE_BYTES = 2;
const RESAMPLE_RATIO = DISCORD_SAMPLE_RATE_HZ / STT_SAMPLE_RATE_HZ;
const RESAMPLE_GROUP_BYTES = DISCORD_CHANNELS * PCM_SAMPLE_BYTES * RESAMPLE_RATIO;
const DEFAULT_CHUNK_BYTES = 8 * 1024;
const MAX_BASE64_BYTES = 16 * 1024;
const DEFAULT_MAX_ACTIVE_STREAMS = 32;
const USER_ID_PATTERN = /^\d{1,20}$/;
const SEGMENT_ID_PATTERN = /^[a-zA-Z0-9._-]{1,128}$/;
const WOW_CLASSES = new Set([
  "death-knight",
  "demon-hunter",
  "druid",
  "evoker",
  "hunter",
  "mage",
  "monk",
  "paladin",
  "priest",
  "rogue",
  "shaman",
  "warlock",
  "warrior",
]);
const WOW_ROLES = new Set(["damage", "healer", "tank"]);

export const PCM_FORMAT = Object.freeze({
  sampleRateHz: STT_SAMPLE_RATE_HZ,
  channels: STT_CHANNELS,
  encoding: "pcm_s16le",
});

export class AudioSinkError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "AudioSinkError";
    this.code = code;
  }
}

function safeText(value, maximumCharacters) {
  if (typeof value !== "string") {
    return null;
  }
  const withoutControls = value
    .normalize("NFKC")
    .replace(/[\p{Cc}\p{Cf}]/gu, " ")
    .replace(/\s+/gu, " ")
    .trim();
  if (!withoutControls) {
    return null;
  }
  return Array.from(withoutControls).slice(0, maximumCharacters).join("");
}

export function safeDiscordDisplayName(value) {
  return safeText(value, 64) || "Discord speaker";
}

function safeCharacterMapping(character) {
  if (!character || typeof character !== "object") {
    return null;
  }
  const characterName = safeText(character.characterName, 24);
  const realm = character.realm === null ? null : safeText(character.realm, 32);
  if (
    !characterName ||
    (character.realm !== null && !realm) ||
    !WOW_CLASSES.has(character.wowClass) ||
    !WOW_ROLES.has(character.role)
  ) {
    return null;
  }
  return Object.freeze({
    characterName,
    realm,
    wowClass: character.wowClass,
    role: character.role,
  });
}

function defaultDecoderFactory() {
  return new OpusScript(
    DISCORD_SAMPLE_RATE_HZ,
    DISCORD_CHANNELS,
    OpusScript.Application.AUDIO,
  );
}

function requireBinaryChunk(value, label) {
  if (!Buffer.isBuffer(value) && !ArrayBuffer.isView(value)) {
    throw new AudioSinkError("invalid_audio", `${label} must be binary audio.`);
  }
  return Buffer.from(value.buffer, value.byteOffset, value.byteLength);
}

export class Pcm48StereoTo16Mono {
  constructor() {
    this.carry = Buffer.alloc(0);
  }

  push(pcm) {
    pcm = requireBinaryChunk(pcm, "Decoded PCM");
    if (pcm.length === 0) {
      return Buffer.alloc(0);
    }
    const input = this.carry.length > 0 ? Buffer.concat([this.carry, pcm]) : pcm;
    const completeBytes = input.length - (input.length % RESAMPLE_GROUP_BYTES);
    this.carry = Buffer.from(input.subarray(completeBytes));
    const output = Buffer.allocUnsafe((completeBytes / RESAMPLE_GROUP_BYTES) * PCM_SAMPLE_BYTES);

    let outputOffset = 0;
    for (let inputOffset = 0; inputOffset < completeBytes; inputOffset += RESAMPLE_GROUP_BYTES) {
      let sum = 0;
      for (let sampleOffset = 0; sampleOffset < RESAMPLE_GROUP_BYTES; sampleOffset += 2) {
        sum += input.readInt16LE(inputOffset + sampleOffset);
      }
      const sample = Math.max(-32_768, Math.min(32_767, Math.round(sum / 6)));
      output.writeInt16LE(sample, outputOffset);
      outputOffset += PCM_SAMPLE_BYTES;
    }
    return output;
  }

  finish() {
    if (this.carry.length !== 0) {
      this.carry = Buffer.alloc(0);
      throw new AudioSinkError("invalid_pcm_frame", "Decoded PCM ended on a partial frame.");
    }
  }
}

function validateSegment({
  segmentId,
  globalSequence,
  audienceGeneration,
  audienceRevision,
  userId,
  stream,
}) {
  if (typeof segmentId !== "string" || !SEGMENT_ID_PATTERN.test(segmentId)) {
    throw new AudioSinkError("invalid_segment", "Audio segment id is invalid.");
  }
  if (!Number.isSafeInteger(globalSequence) || globalSequence <= 0) {
    throw new AudioSinkError("invalid_segment", "Audio segment sequence is invalid.");
  }
  if (!Number.isSafeInteger(audienceGeneration) || audienceGeneration <= 0) {
    throw new AudioSinkError("invalid_segment", "Audio audience generation is invalid.");
  }
  if (!Number.isSafeInteger(audienceRevision) || audienceRevision <= 0) {
    throw new AudioSinkError("invalid_segment", "Audio audience revision is invalid.");
  }
  if (typeof userId !== "string" || !USER_ID_PATTERN.test(userId)) {
    throw new AudioSinkError("invalid_speaker", "Audio speaker id is invalid.");
  }
  if (!stream || typeof stream[Symbol.asyncIterator] !== "function") {
    throw new AudioSinkError("invalid_stream", "Audio stream is invalid.");
  }
}

function segmentMetadata({
  segmentId,
  globalSequence,
  audienceGeneration,
  audienceRevision,
  userId,
  displayName,
  character,
}) {
  return Object.freeze({
    segmentId,
    globalSequence,
    audienceGeneration,
    audienceRevision,
    speaker: Object.freeze({
      userId,
      displayName: safeDiscordDisplayName(displayName),
      character: safeCharacterMapping(character),
    }),
  });
}

function classifyFailure(error) {
  if (error instanceof AudioSinkError) {
    return error.code;
  }
  return "audio_pipeline_failed";
}

export class PcmJsonlAudioSink {
  constructor({
    emitter,
    decoderFactory = defaultDecoderFactory,
    chunkBytes = DEFAULT_CHUNK_BYTES,
    maxActiveStreams = DEFAULT_MAX_ACTIVE_STREAMS,
  }) {
    if (!emitter || typeof emitter.emit !== "function") {
      throw new TypeError("emitter must expose emit().");
    }
    if (typeof decoderFactory !== "function") {
      throw new TypeError("decoderFactory must be a function.");
    }
    if (
      !Number.isSafeInteger(chunkBytes) ||
      chunkBytes < 2 ||
      chunkBytes % 2 !== 0 ||
      Math.ceil(chunkBytes / 3) * 4 > MAX_BASE64_BYTES
    ) {
      throw new TypeError("chunkBytes must produce at most 16 KB of base64 PCM.");
    }
    if (!Number.isSafeInteger(maxActiveStreams) || maxActiveStreams < 1) {
      throw new TypeError("maxActiveStreams must be a positive integer.");
    }
    this.emitter = emitter;
    this.decoderFactory = decoderFactory;
    this.chunkBytes = chunkBytes;
    this.maxActiveStreams = maxActiveStreams;
    this.active = new Map();
    this.failure = null;
    this.emitter.onFailure?.(() => {
      this.#tripFailure("output_unavailable");
    });
  }

  isReady() {
    return this.failure === null && this.emitter.isReady?.() !== false;
  }

  async acceptOpusStream({
    segmentId,
    globalSequence,
    audienceGeneration,
    audienceRevision,
    userId,
    displayName = null,
    character = null,
    stream,
  }) {
    validateSegment({
      segmentId,
      globalSequence,
      audienceGeneration,
      audienceRevision,
      userId,
      stream,
    });
    if (!this.isReady()) {
      throw this.failure || new AudioSinkError("output_unavailable", "Audio output is unavailable.");
    }
    if (this.active.size >= this.maxActiveStreams) {
      throw this.#tripFailure("stream_limit");
    }
    if (this.active.has(segmentId)) {
      throw this.#tripFailure("duplicate_segment");
    }

    let decoder;
    try {
      decoder = this.decoderFactory();
    } catch {
      throw this.#tripFailure("decoder_unavailable");
    }
    if (!decoder || typeof decoder.decode !== "function" || typeof decoder.delete !== "function") {
      try {
        decoder?.delete?.();
      } catch {
        // A decoder that failed validation is never reused.
      }
      throw this.#tripFailure("decoder_unavailable");
    }

    const metadata = segmentMetadata({
      segmentId,
      globalSequence,
      audienceGeneration,
      audienceRevision,
      userId,
      displayName,
      character,
    });
    const state = {
      stream,
      decoder,
      converter: new Pcm48StereoTo16Mono(),
      pending: Buffer.alloc(0),
      chunkSequence: 0,
      pcmBytes: 0,
      started: false,
      ended: false,
    };
    this.active.set(segmentId, state);

    try {
      this.emitter.emit("audio.segment.started", {
        ...metadata,
        format: PCM_FORMAT,
      });
      state.started = true;

      for await (const opusPacket of stream) {
        if (!this.isReady()) {
          throw this.failure || new AudioSinkError("output_unavailable", "Audio output is unavailable.");
        }
        const packet = requireBinaryChunk(opusPacket, "Opus packet");
        let decoded;
        try {
          decoded = state.decoder.decode(packet);
        } catch {
          throw new AudioSinkError("decode_failed", "Opus decoding failed.");
        }
        const pcm = state.converter.push(decoded);
        this.#appendPcm(state, metadata, pcm);
      }

      state.converter.finish();
      this.#flushPcm(state, metadata);
      this.#emitEnded(state, metadata, "completed");
    } catch (error) {
      // The runtime destroys a live stream for entirely routine reasons - the
      // maximum segment length, and every consent pause, stop or channel change
      // - so a premature close is a deliberate mid-segment stop rather than a
      // pipeline fault, and must not latch the fail-closed flag. A premature
      // close seen while a failure is already latched is the cascade from
      // #tripFailure destroying the other speakers, and still reports failed.
      if (error?.code === "ERR_STREAM_PREMATURE_CLOSE" && !this.failure) {
        this.#flushPcm(state, metadata);
        this.#emitEnded(state, metadata, "completed");
        return;
      }
      // A decode failure is ONE speaker's corrupt packet - Discord
      // delivers those routinely - and it must cost that one segment,
      // not the sink. Latching here cascaded every other live speaker
      // into failure and took the raid's captions down three times in
      // twenty minutes on a real machine. Structural faults below still
      // trip the fail-closed latch, because for those continuing would
      // be lying.
      if (error instanceof AudioSinkError && error.code === "decode_failed"
          && !this.failure) {
        this.#emitEnded(state, metadata, "failed");
        return;
      }
      const failure = this.#tripFailure(classifyFailure(error));
      this.#emitEnded(state, metadata, "failed");
      throw failure;
    } finally {
      this.active.delete(segmentId);
      state.pending = Buffer.alloc(0);
      try {
        state.decoder.delete();
      } catch {
        throw this.#tripFailure("decoder_cleanup_failed");
      }
    }
  }

  #appendPcm(state, metadata, pcm) {
    if (pcm.length === 0) {
      return;
    }
    state.pending =
      state.pending.length === 0 ? Buffer.from(pcm) : Buffer.concat([state.pending, pcm]);
    while (state.pending.length >= this.chunkBytes) {
      const chunk = state.pending.subarray(0, this.chunkBytes);
      state.pending = Buffer.from(state.pending.subarray(this.chunkBytes));
      this.#emitChunk(state, metadata, chunk);
    }
  }

  #flushPcm(state, metadata) {
    if (state.pending.length > 0) {
      this.#emitChunk(state, metadata, state.pending);
      state.pending = Buffer.alloc(0);
    }
  }

  #emitChunk(state, metadata, pcm) {
    const pcmBase64 = pcm.toString("base64");
    if (Buffer.byteLength(pcmBase64, "ascii") > MAX_BASE64_BYTES) {
      throw new AudioSinkError("chunk_limit", "PCM chunk exceeded its safety limit.");
    }
    state.chunkSequence += 1;
    state.pcmBytes += pcm.length;
    this.emitter.emit("audio.segment.chunk", {
      segmentId: metadata.segmentId,
      globalSequence: metadata.globalSequence,
      audienceGeneration: metadata.audienceGeneration,
      audienceRevision: metadata.audienceRevision,
      chunkSequence: state.chunkSequence,
      pcmBase64,
    });
  }

  #emitEnded(state, metadata, status) {
    if (!state.started || state.ended || this.emitter.isReady?.() === false) {
      return;
    }
    try {
      this.emitter.emit("audio.segment.ended", {
        ...metadata,
        status,
        chunks: state.chunkSequence,
        pcmBytes: state.pcmBytes,
      });
      state.ended = true;
    } catch {
      state.ended = true;
      throw this.#tripFailure("output_unavailable");
    }
  }

  #tripFailure(code) {
    if (!this.failure) {
      this.failure = new AudioSinkError(code, "Audio capture stopped because its local pipeline failed.");
      for (const { stream } of this.active.values()) {
        if (typeof stream.destroy === "function" && !stream.destroyed) {
          stream.destroy();
        }
      }
    }
    return this.failure;
  }
}

export function validateAudioSink(audioSink) {
  if (
    !audioSink ||
    typeof audioSink.isReady !== "function" ||
    typeof audioSink.acceptOpusStream !== "function"
  ) {
    throw new TypeError("audioSink must expose isReady() and acceptOpusStream().");
  }
  return audioSink;
}
