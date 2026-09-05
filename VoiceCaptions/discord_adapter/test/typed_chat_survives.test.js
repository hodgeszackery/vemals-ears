// Typing must never be able to end the voice session.
//
// Shino, 2026-09-05: "there is a bug where when a message is sent via the
// 'Message Group' button VE disconnects from discord after showing the
// text." The caption is published by the relay, which is why the text
// appeared; the Discord copy goes down a private pipe into this adapter,
// and every failure on that pipe closed stdin. stdin is the adapter's
// shutdown channel. So one typed message that went wrong made the bot
// leave the voice channel and ended captions for the whole room.
//
// His rule for the other direction, 2026-09-04: "ability to type when
// voice goes down is a feature not a bug." These pin the converse, which
// matters more - voice is what people are in the middle of.

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { createInterface } from "node:readline";
import { readFile } from "node:fs/promises";
import { PassThrough } from "node:stream";
import test from "node:test";

import { DiscordAdapterRuntime } from "../src/discord_runtime.js";
import {
  QuickChatCommandQueue,
  readQuickChatLines,
} from "../src/quick_chat_input.js";
import { JsonlEmitter } from "../src/protocol.js";

const GUILD = "123456789012345678";
const CONTROL = "323456789012345678";
const REQUEST = "device_request_1234567890";
const REQUEST_TWO = "device_request_1234567891";

function commandLine(requestId, text = "pull in five") {
  return JSON.stringify({
    version: 1,
    type: "quick_chat.command",
    requestId,
    speaker: "Vemal",
    wowClass: "paladin",
    text,
  });
}

/** The stdin half of index.js, with a stream we control. */
function pipeUnderTest({ deliver, bind = (command) => command }) {
  const stdin = new PassThrough();
  const input = createInterface({ input: stdin, crlfDelay: Infinity });
  const results = [];
  const logged = [];
  let closed = false;
  input.on("close", () => {
    closed = true;
  });
  const queue = new QuickChatCommandQueue({
    bind,
    deliver,
    onResult: (result) => results.push(result),
    onError: () => logged.push("quick_chat_failed"),
  });
  readQuickChatLines({ input, queue, log: (reason) => logged.push(reason) });
  return {
    stdin,
    queue,
    results,
    logged,
    isClosed: () => closed,
    send: (line) => stdin.write(line + "\n"),
    settle: async () => {
      await new Promise((resolve) => setImmediate(resolve));
      await queue.whenIdle();
    },
  };
}

test("a delivery that throws does not close the shutdown channel", async () => {
  const pipe = pipeUnderTest({
    deliver: async () => {
      throw new Error("discord exploded");
    },
  });

  pipe.send(commandLine(REQUEST));
  await pipe.settle();

  assert.equal(pipe.isClosed(), false, "stdin closing shuts the adapter down");
  assert.deepEqual(pipe.results, [
    { requestId: REQUEST, status: "delivery_failed" },
  ]);
  assert.ok(pipe.logged.includes("quick_chat_failed"), "the reason is reported");
});

test("a message after a failed one is still delivered", async () => {
  // The point of not shutting down: the channel keeps working.
  let calls = 0;
  const pipe = pipeUnderTest({
    deliver: async () => {
      calls += 1;
      if (calls === 1) {
        throw new Error("discord exploded");
      }
      return "delivered";
    },
  });

  pipe.send(commandLine(REQUEST));
  await pipe.settle();
  pipe.send(commandLine(REQUEST_TWO));
  await pipe.settle();

  assert.deepEqual(pipe.results, [
    { requestId: REQUEST, status: "delivery_failed" },
    { requestId: REQUEST_TWO, status: "delivered" },
  ]);
  assert.equal(pipe.isClosed(), false);
});

test("an unparseable line is dropped, not fatal", async () => {
  const pipe = pipeUnderTest({ deliver: async () => "delivered" });

  pipe.send("{not json at all");
  await pipe.settle();
  pipe.send(commandLine(REQUEST));
  await pipe.settle();

  assert.equal(pipe.isClosed(), false, "one bad line must not end the session");
  assert.ok(pipe.logged.includes("stdin_protocol_error"));
  assert.deepEqual(pipe.results, [
    { requestId: REQUEST, status: "delivered" },
  ]);
});

test("a binding that throws fails the message and nothing else", async () => {
  const pipe = pipeUnderTest({
    bind: () => {
      throw new Error("no session state");
    },
    deliver: async () => "delivered",
  });

  pipe.send(commandLine(REQUEST));
  await pipe.settle();

  assert.equal(pipe.isClosed(), false);
  assert.deepEqual(pipe.results, [
    { requestId: REQUEST, status: "delivery_failed" },
  ]);
});

// ---------------------------------------------------------------------
// The runtime half: postQuickChat must not hand the queue a rejection.
// ---------------------------------------------------------------------

class ExplodingSessions extends EventEmitter {
  constructor() {
    super();
    this.captureHealthy = true;
    this.calls = 0;
  }

  getSessionBinding() {
    return { generation: 1, controlChannelId: CONTROL };
  }

  matchesSessionBinding() {
    this.calls += 1;
    // Healthy for bind, then broken while the post is in flight - the
    // shape a gateway reconnect actually has.
    if (this.calls > 1) {
      throw new Error("session state is gone");
    }
    return true;
  }
}

function runtimeWithBrokenSessions() {
  const client = new EventEmitter();
  client.guilds = { cache: new Map(), fetch: async () => null };
  client.application = { commands: { set: async () => {} } };
  client.user = { id: "523456789012345678" };
  return new DiscordAdapterRuntime({
    config: {
      token: "unused-test-token",
      applicationId: "623456789012345678",
      guildId: GUILD,
      silenceMs: 750,
      connectTimeoutMs: 5_000,
      maxSegmentMs: 30_000,
    },
    emitter: new JsonlEmitter({ writer: { write: () => true } }),
    audioSink: { isReady: () => true, acceptOpusStream: async () => {} },
    sessionManager: new ExplodingSessions(),
    client,
  });
}

test("the adapter's own wiring never fails the input on a typed message", async () => {
  // The harness above builds the same wiring index.js builds, so it cannot
  // notice index.js putting failInput back. This reads the real file.
  // failInput closes stdin, and closing stdin is how this process is asked
  // to shut down - so it must not appear on either quick-chat path.
  const source = await readFile(
    new URL("../src/index.js", import.meta.url),
    "utf8",
  );
  assert.match(source, /readQuickChatLines\(\{/, "the shared reader is used");
  const wiring = source.slice(
    source.indexOf("new QuickChatCommandQueue"),
    source.indexOf('input.on("close"'),
  );
  assert.ok(wiring.length > 0, "the quick chat wiring was not found");
  assert.doesNotMatch(
    wiring,
    /failInput/,
    "a failed typed message must not close the shutdown channel",
  );
  // failInput still belongs on a genuinely broken pipe.
  assert.match(source, /process\.stdin\.on\("error"[\s\S]{0,80}failInput/);
});

test("postQuickChat resolves to delivery_failed instead of rejecting", async () => {
  const runtime = runtimeWithBrokenSessions();
  const status = await runtime.postQuickChat({
    requestId: REQUEST,
    speaker: "Vemal",
    wowClass: "paladin",
    text: "pull in five",
  });
  assert.equal(status, "delivery_failed");
});

test("a retry of a failed request does not throw the same rejection again", async () => {
  // The relay retries one request ID three times. A rejected promise left
  // in the delivery cache threw on every retry, so a single bad message
  // shut the adapter down repeatedly.
  const runtime = runtimeWithBrokenSessions();
  const command = {
    requestId: REQUEST,
    speaker: "Vemal",
    wowClass: "paladin",
    text: "pull in five",
  };
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const status = await runtime.postQuickChat(command);
    assert.ok(
      ["delivery_failed", "no_active_session"].includes(status),
      "attempt " + attempt + " returned " + status,
    );
  }
});
