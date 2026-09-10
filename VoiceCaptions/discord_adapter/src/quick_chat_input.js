const REQUEST_ID = /^[A-Za-z0-9_-]{16,128}$/;
const CONTROL = /[\p{Cc}\p{Cf}\p{Cs}]/u;
export const QUICK_CHAT_QUEUE_LIMIT = 8;
export const QUICK_CHAT_MAX_QUEUE_AGE_MS = 7_000;
const WOW_CLASSES = new Set([
  "",
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

function exactKeys(value, expected) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const keys = Object.keys(value).sort();
  return keys.length === expected.length &&
    keys.every((key, index) => key === expected[index]);
}

function text(value, maximum) {
  if (typeof value !== "string" || CONTROL.test(value)) {
    throw new Error("invalid_quick_chat_command");
  }
  const clean = value.normalize("NFKC").trim();
  if (!clean || [...clean].length > maximum || CONTROL.test(clean)) {
    throw new Error("invalid_quick_chat_command");
  }
  return clean;
}

export function parseQuickChatCommand(line) {
  if (typeof line !== "string" || Buffer.byteLength(line, "utf8") > 2048) {
    throw new Error("invalid_quick_chat_command");
  }
  let value;
  try {
    value = JSON.parse(line);
  } catch {
    throw new Error("invalid_quick_chat_command");
  }
  const expected = ["requestId", "speaker", "text", "type", "version", "wowClass"];
  if (!exactKeys(value, expected) || value.version !== 1 || value.type !== "quick_chat.command") {
    throw new Error("invalid_quick_chat_command");
  }
  if (typeof value.requestId !== "string" || !REQUEST_ID.test(value.requestId)) {
    throw new Error("invalid_quick_chat_command");
  }
  if (typeof value.wowClass !== "string" || !WOW_CLASSES.has(value.wowClass)) {
    throw new Error("invalid_quick_chat_command");
  }
  return {
    requestId: value.requestId,
    speaker: text(value.speaker, 32),
    wowClass: value.wowClass,
    text: text(value.text, 320),
  };
}

export function readQuickChatLines({ input, queue, log = () => {} }) {
  // Reading the pipe, and what a bad line costs.
  //
  // A line that will not parse is dropped and the next one is read. It
  // used to close stdin, which is the adapter's shutdown channel: one
  // malformed typed message ended the voice session and everybody's
  // captions with it. A newline-delimited pipe does not desynchronise on
  // a bad line. Invalid input cannot enter the compatibility queue, whose
  // terminal has no Discord sink, so there was nothing for that shutdown to
  // protect.
  input.on("line", (line) => {
    let command;
    try {
      command = parseQuickChatCommand(line);
    } catch {
      log("stdin_protocol_error");
      return;
    }
    queue.enqueue(command);
  });
  return queue;
}


export class QuickChatCommandQueue {
  constructor({
    bind,
    deliver,
    onResult,
    onError = () => {},
    limit = QUICK_CHAT_QUEUE_LIMIT,
    maxAgeMs = QUICK_CHAT_MAX_QUEUE_AGE_MS,
    clock = () => Date.now(),
  }) {
    if (typeof bind !== "function" || typeof deliver !== "function") {
      throw new TypeError("bind and deliver must be functions");
    }
    if (typeof onResult !== "function" || typeof onError !== "function") {
      throw new TypeError("onResult and onError must be functions");
    }
    if (!Number.isSafeInteger(limit) || limit < 1) {
      throw new TypeError("limit must be a positive integer");
    }
    if (!Number.isFinite(maxAgeMs) || maxAgeMs <= 0) {
      throw new TypeError("maxAgeMs must be positive");
    }
    if (typeof clock !== "function") {
      throw new TypeError("clock must be a function");
    }
    this.bind = bind;
    this.deliver = deliver;
    this.onResult = onResult;
    this.onError = onError;
    this.limit = limit;
    this.maxAgeMs = maxAgeMs;
    this.clock = clock;
    this.pending = [];
    this.running = false;
    this.accepting = true;
    this.idleWaiters = [];
  }

  get size() {
    return this.pending.length + (this.running ? 1 : 0);
  }

  enqueue(command) {
    const requestId = command?.requestId;
    // Check capacity before binding. An overflow command is reported using
    // only its ID and its speaker/text are never retained by this queue.
    if (!this.accepting || this.size >= this.limit) {
      this.#report(requestId, "delivery_failed");
      return false;
    }

    const acceptedAt = this.clock();
    let bound;
    try {
      bound = this.bind(command);
    } catch (error) {
      this.#report(requestId, "delivery_failed");
      this.#fail(error);
      return false;
    }
    this.pending.push({ requestId, bound, acceptedAt });
    void this.#drain();
    return true;
  }

  close() {
    this.accepting = false;
    return this.whenIdle();
  }

  whenIdle() {
    if (this.size === 0) {
      return Promise.resolve();
    }
    return new Promise((resolve) => this.idleWaiters.push(resolve));
  }

  async #drain() {
    if (this.running) {
      return;
    }
    this.running = true;
    try {
      while (this.pending.length > 0) {
        const item = this.pending.shift();
        if (this.clock() - item.acceptedAt >= this.maxAgeMs) {
          this.#report(item.requestId, "delivery_failed");
          continue;
        }
        let status = "delivery_failed";
        try {
          status = await this.deliver(item.bound);
        } catch (error) {
          this.#fail(error);
        }
        this.#report(item.requestId, status);
      }
    } finally {
      this.running = false;
      const waiters = this.idleWaiters.splice(0);
      for (const resolve of waiters) {
        resolve();
      }
    }
  }

  #report(requestId, status) {
    try {
      this.onResult({ requestId, status });
    } catch (error) {
      this.#fail(error);
    }
  }

  #fail(error) {
    try {
      this.onError(error);
    } catch {
      // The queue still fails the individual command closed.
    }
  }
}
