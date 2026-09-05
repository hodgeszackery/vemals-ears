import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import test from "node:test";

import OpusScript from "opusscript";

import {
  Pcm48StereoTo16Mono,
  PcmJsonlAudioSink,
  safeDiscordDisplayName,
} from "../src/audio_sink.js";
import { JsonlEmitter } from "../src/protocol.js";

class MemoryWriter extends EventEmitter {
  constructor({ acceptWrites = true } = {}) {
    super();
    this.acceptWrites = acceptWrites;
    this.lines = [];
  }

  write(line) {
    this.lines.push(line);
    return this.acceptWrites;
  }

  events() {
    return this.lines.map((line) => JSON.parse(line));
  }
}

function stereo48kForMonoSamples(samples) {
  const pcm = Buffer.alloc(samples.length * 12);
  let offset = 0;
  for (const sample of samples) {
    for (let index = 0; index < 6; index += 1) {
      pcm.writeInt16LE(sample, offset);
      offset += 2;
    }
  }
  return pcm;
}

function constantStereo48k(outputSamples, value) {
  return stereo48kForMonoSamples(Array.from({ length: outputSamples }, () => value));
}

function fakeDecoderFactory({ decode, deleted }) {
  return () => ({
    decode,
    delete() {
      deleted.count += 1;
    },
  });
}

function segment(overrides = {}) {
  return {
    segmentId: "123.456.1.1",
    globalSequence: 1,
    audienceGeneration: 7,
    audienceRevision: 11,
    userId: "456",
    displayName: "  Mira\n\u202eMage  ",
    character: {
      characterName: "Mira",
      realm: "Moon Guard",
      wowClass: "priest",
      role: "healer",
    },
    ...overrides,
  };
}

function audioEvents(writer, type) {
  return writer.events().filter((event) => event.type === type);
}

test("downmix and 48 kHz to 16 kHz conversion stays correct across buffer splits", () => {
  const converter = new Pcm48StereoTo16Mono();
  const input = stereo48kForMonoSamples([1200, -2400, 32700]);
  const first = converter.push(input.subarray(0, 17));
  const second = converter.push(input.subarray(17));
  converter.finish();

  const output = Buffer.concat([first, second]);
  assert.equal(output.length, 6);
  assert.deepEqual(
    [output.readInt16LE(0), output.readInt16LE(2), output.readInt16LE(4)],
    [1200, -2400, 32700],
  );
});

test("default pinned Opus decoder emits 16 kHz mono PCM without disk work", async () => {
  const writer = new MemoryWriter();
  const sink = new PcmJsonlAudioSink({ emitter: new JsonlEmitter({ writer }) });
  const stream = new PassThrough({ objectMode: true });
  const encoder = new OpusScript(48_000, 2, OpusScript.Application.AUDIO);
  const sourcePcm = constantStereo48k(320, 4000);
  const opusPacket = encoder.encode(sourcePcm, 960);

  const task = sink.acceptOpusStream({ ...segment(), stream });
  stream.end(opusPacket);
  await task;
  encoder.delete();

  const chunks = audioEvents(writer, "audio.segment.chunk");
  assert.equal(chunks.length, 1);
  assert.equal(Buffer.from(chunks[0].payload.pcmBase64, "base64").length, 640);
  assert.equal(audioEvents(writer, "audio.segment.ended")[0].payload.pcmBytes, 640);
  for (const event of writer.events().filter(({ type }) => type.startsWith("audio.segment."))) {
    assert.equal(event.payload.audienceGeneration, 7);
    assert.equal(event.payload.audienceRevision, 11);
  }
  assert.equal(sink.active.size, 0);
});

test("JSONL audio chunks and complete lines remain below 16 KB", async () => {
  const writer = new MemoryWriter();
  const deleted = { count: 0 };
  const sink = new PcmJsonlAudioSink({
    emitter: new JsonlEmitter({ writer }),
    decoderFactory: fakeDecoderFactory({
      deleted,
      decode: () => constantStereo48k(960, 700),
    }),
  });
  const stream = new PassThrough({ objectMode: true });
  const task = sink.acceptOpusStream({ ...segment(), stream });
  for (let index = 0; index < 6; index += 1) {
    stream.write(Buffer.from([index]));
  }
  stream.end();
  await task;

  const chunks = audioEvents(writer, "audio.segment.chunk");
  assert.ok(chunks.length >= 2);
  for (const event of chunks) {
    assert.ok(Buffer.byteLength(event.payload.pcmBase64, "ascii") <= 16 * 1024);
    assert.ok(Buffer.from(event.payload.pcmBase64, "base64").length <= 8 * 1024);
  }
  for (const line of writer.lines.filter((line) => line.includes('"audio.segment.chunk"'))) {
    assert.ok(Buffer.byteLength(line, "utf8") <= 16 * 1024);
  }
  assert.equal(deleted.count, 1);
});

test("simultaneous speakers keep independent decoders, metadata, and chunks", async () => {
  const writer = new MemoryWriter();
  const deleted = { count: 0 };
  const sink = new PcmJsonlAudioSink({
    emitter: new JsonlEmitter({ writer }),
    chunkBytes: 640,
    decoderFactory: fakeDecoderFactory({
      deleted,
      decode: (packet) => constantStereo48k(320, packet[0] * 100),
    }),
  });
  const firstStream = new PassThrough({ objectMode: true });
  const secondStream = new PassThrough({ objectMode: true });
  const first = sink.acceptOpusStream({ ...segment(), stream: firstStream });
  const second = sink.acceptOpusStream({
    ...segment({
      segmentId: "123.789.1.2",
      globalSequence: 2,
      userId: "789",
      displayName: "Drex",
      character: {
        characterName: "Drex",
        realm: null,
        wowClass: "warrior",
        role: "tank",
      },
    }),
    stream: secondStream,
  });

  firstStream.write(Buffer.from([11]));
  secondStream.write(Buffer.from([22]));
  secondStream.end();
  firstStream.end();
  await Promise.all([first, second]);

  const starts = audioEvents(writer, "audio.segment.started");
  assert.equal(starts.length, 2);
  assert.equal(starts[0].payload.segmentId, "123.456.1.1");
  assert.equal(starts[0].payload.globalSequence, 1);
  assert.equal(starts[0].payload.speaker.userId, "456");
  assert.equal(starts[0].payload.speaker.displayName, "Mira Mage");
  assert.equal(starts[0].payload.speaker.character.role, "healer");
  assert.equal(starts[1].payload.speaker.character.wowClass, "warrior");
  const chunks = audioEvents(writer, "audio.segment.chunk");
  assert.deepEqual(new Set(chunks.map((event) => event.payload.segmentId)), new Set([
    "123.456.1.1",
    "123.789.1.2",
  ]));
  const ends = audioEvents(writer, "audio.segment.ended");
  assert.equal(ends.length, 2);
  assert.ok(ends.every((event) => event.payload.status === "completed"));
  assert.deepEqual(new Set(ends.map((event) => event.payload.speaker.userId)), new Set([
    "456",
    "789",
  ]));
  assert.equal(deleted.count, 2);
  assert.equal(sink.active.size, 0);
});

test("a stream destroyed mid-segment completes and leaves capture usable", async () => {
  const writer = new MemoryWriter();
  const deleted = { count: 0 };
  const stream = new PassThrough({ objectMode: true });
  const sink = new PcmJsonlAudioSink({
    emitter: new JsonlEmitter({ writer }),
    decoderFactory: fakeDecoderFactory({
      deleted,
      // Destroying from inside decode reproduces the runtime cutting a live
      // stream off mid-segment without waiting on iteration timing.
      decode: () => {
        stream.destroy();
        return constantStereo48k(320, 500);
      },
    }),
  });
  const task = sink.acceptOpusStream({ ...segment(), stream });
  stream.write(Buffer.from([1]));
  await task;

  const ends = audioEvents(writer, "audio.segment.ended");
  assert.equal(ends.length, 1);
  assert.equal(ends[0].payload.status, "completed");
  assert.equal(ends[0].payload.pcmBytes, 640);
  assert.equal(audioEvents(writer, "audio.segment.chunk").length, 1);
  assert.equal(sink.isReady(), true);
  assert.equal(sink.active.size, 0);
  assert.equal(deleted.count, 1);
});

test("one speaker's decode failure costs one segment, never the raid", async () => {
  // The old policy - latch the sink and destroy every concurrent stream
  // on a single decode error - was pinned here by name. It took the
  // raid's captions down three times in twenty minutes on a real
  // machine, from one speaker's corrupt packet, which Discord delivers
  // routinely. The failing SEGMENT still reports failed and never leaks
  // the decoder's message; everyone else keeps captioning.
  const writer = new MemoryWriter();
  const deleted = { count: 0 };
  const sink = new PcmJsonlAudioSink({
    emitter: new JsonlEmitter({ writer }),
    decoderFactory: fakeDecoderFactory({
      deleted,
      decode(packet) {
        if (packet[0] === 99) {
          throw new Error("private decoder detail");
        }
        return constantStereo48k(320, 100);
      },
    }),
  });
  const firstStream = new PassThrough({ objectMode: true });
  const secondStream = new PassThrough({ objectMode: true });
  const first = sink.acceptOpusStream({ ...segment(), stream: firstStream });
  const second = sink.acceptOpusStream({
    ...segment({ segmentId: "123.789.1.2", globalSequence: 2, userId: "789" }),
    stream: secondStream,
  });

  firstStream.write(Buffer.from([99]));
  await first;

  assert.equal(sink.isReady(), true, "the sink must survive one bad packet");

  secondStream.write(Buffer.from([1]));
  secondStream.end();
  await second;

  const events = writer.lines.map((line) => JSON.parse(line));
  const ended = events.filter((event) => event.type === "audio.segment.ended");
  assert.deepEqual(
    ended.map((event) => event.payload.status).sort(),
    ["completed", "failed"],
    "the bad segment fails alone; the concurrent one completes",
  );
  assert.equal(sink.active.size, 0);
  assert.equal(deleted.count, 2, "both decoders still cleaned up");
  assert.equal(writer.lines.join("").includes("private decoder detail"), false);
});

test("a decode failure after the sink is already tripped still cascades", async () => {
  // The scoping is exactly one case wide. Once a STRUCTURAL fault has
  // latched the sink, a decode error must not sneak a segment through
  // the fail-closed state.
  const writer = new MemoryWriter();
  const sink = new PcmJsonlAudioSink({
    emitter: new JsonlEmitter({ writer }),
    decoderFactory: fakeDecoderFactory({
      deleted: { count: 0 },
      decode() {
        throw new Error("boom");
      },
    }),
  });
  const stream = new PassThrough({ objectMode: true });
  const task = sink.acceptOpusStream({ ...segment(), stream });
  sink.failure = Object.assign(new Error("Audio output is unavailable."), {
    code: "output_unavailable",
  });
  stream.write(Buffer.from([1]));
  await assert.rejects(task, (error) => error.code === "output_unavailable");
  assert.equal(sink.isReady(), false);
});

test("bounded stdout backpressure fails closed without retaining audio", async () => {
  const writer = new MemoryWriter({ acceptWrites: false });
  const emitter = new JsonlEmitter({ writer, maxQueuedBytes: 1024 });
  const deleted = { count: 0 };
  const sink = new PcmJsonlAudioSink({
    emitter,
    chunkBytes: 512,
    decoderFactory: fakeDecoderFactory({
      deleted,
      decode: () => constantStereo48k(960, 300),
    }),
  });
  const stream = new PassThrough({ objectMode: true });
  const task = sink.acceptOpusStream({ ...segment(), stream });
  stream.write(Buffer.from([1]));
  stream.write(Buffer.from([2]));

  await assert.rejects(task, (error) => error.code === "output_unavailable");
  assert.equal(stream.destroyed, true);
  assert.equal(emitter.isReady(), false);
  assert.equal(sink.isReady(), false);
  assert.equal(sink.active.size, 0);
  assert.equal(deleted.count, 1);
});

test("display names remove control and bidi characters and have a bounded fallback", () => {
  assert.equal(safeDiscordDisplayName("\u0000  Mira\n\u202eMage  "), "Mira Mage");
  assert.equal(safeDiscordDisplayName("\n\t"), "Discord speaker");
  assert.equal(Array.from(safeDiscordDisplayName("x".repeat(100))).length, 64);
});
