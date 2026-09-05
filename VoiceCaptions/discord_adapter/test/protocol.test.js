import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { JsonlEmitter, ProtocolSafetyError } from "../src/protocol.js";

function memoryWriter() {
  return {
    chunks: [],
    write(chunk) {
      this.chunks.push(chunk);
    },
  };
}

test("JSONL events are ordered, parseable, and redact secret-shaped fields", () => {
  const writer = memoryWriter();
  const emitter = new JsonlEmitter({
    writer,
    clock: () => new Date("2026-08-29T21:00:00.000Z"),
  });

  emitter.emit("health.changed", {
    status: "ready",
    nested: { accessToken: "must-not-appear", guildId: "123" },
  });
  emitter.emit("session.started", { guildId: "123" });

  const events = writer.chunks.map((chunk) => JSON.parse(chunk));
  assert.equal(events[0].version, 1);
  assert.equal(events[0].sequence, 1);
  assert.equal(events[1].sequence, 2);
  assert.equal(events[0].payload.nested.accessToken, "[REDACTED]");
  assert.equal(writer.chunks.join("").includes("must-not-appear"), false);
  assert.ok(writer.chunks.every((chunk) => chunk.endsWith("\n")));
});

test("JSONL protocol refuses audio or other binary payloads", () => {
  const writer = memoryWriter();
  const emitter = new JsonlEmitter({ writer });

  assert.throws(
    () => emitter.emit("speaker.packet", { opus: Buffer.from([1, 2, 3]) }),
    ProtocolSafetyError,
  );
  assert.equal(writer.chunks.length, 0);
});

test("JSONL output queues while stdout is backpressured and drains in order", () => {
  class ControlledWriter extends EventEmitter {
    constructor() {
      super();
      this.acceptWrites = false;
      this.chunks = [];
    }

    write(chunk) {
      this.chunks.push(chunk);
      return this.acceptWrites;
    }
  }

  const writer = new ControlledWriter();
  const emitter = new JsonlEmitter({ writer, maxQueuedBytes: 4096 });
  emitter.emit("test.first");
  emitter.emit("test.second");
  emitter.emit("test.third");
  assert.equal(writer.chunks.length, 1);

  writer.acceptWrites = true;
  writer.emit("drain");
  assert.deepEqual(
    writer.chunks.map((chunk) => JSON.parse(chunk).type),
    ["test.first", "test.second", "test.third"],
  );
  assert.equal(emitter.queuedBytes, 0);
  assert.equal(emitter.isReady(), true);
});
