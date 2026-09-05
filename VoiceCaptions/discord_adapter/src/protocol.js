const EVENT_TYPE_PATTERN = /^[a-z][a-z0-9]*(?:\.[a-z0-9]+)*$/;
const SECRET_KEY_PATTERN = /(authorization|credential|password|secret|token)/i;
const DEFAULT_MAX_QUEUED_BYTES = 256 * 1024;

export class ProtocolSafetyError extends Error {
  constructor(message) {
    super(message);
    this.name = "ProtocolSafetyError";
  }
}

export class ProtocolOutputError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "ProtocolOutputError";
    this.code = code;
  }
}

function sanitize(value, seen) {
  if (value === null || ["string", "number", "boolean"].includes(typeof value)) {
    return value;
  }
  if (typeof value === "bigint") {
    return value.toString();
  }
  if (typeof value === "undefined") {
    return null;
  }
  if (typeof value === "function" || typeof value === "symbol") {
    throw new ProtocolSafetyError("Unsupported value in JSONL payload.");
  }
  if (Buffer.isBuffer(value) || ArrayBuffer.isView(value) || value instanceof ArrayBuffer) {
    throw new ProtocolSafetyError("Binary data is forbidden in JSONL payloads.");
  }
  if (seen.has(value)) {
    throw new ProtocolSafetyError("Circular data is forbidden in JSONL payloads.");
  }
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      return value.map((item) => sanitize(item, seen));
    }
    const result = {};
    for (const [key, child] of Object.entries(value)) {
      result[key] = SECRET_KEY_PATTERN.test(key) ? "[REDACTED]" : sanitize(child, seen);
    }
    return result;
  } finally {
    seen.delete(value);
  }
}

export class JsonlEmitter {
  constructor({
    writer = process.stdout,
    clock = () => new Date(),
    maxQueuedBytes = DEFAULT_MAX_QUEUED_BYTES,
  } = {}) {
    if (!writer || typeof writer.write !== "function") {
      throw new TypeError("writer must expose write(text).");
    }
    if (!Number.isSafeInteger(maxQueuedBytes) || maxQueuedBytes < 1024) {
      throw new TypeError("maxQueuedBytes must be an integer of at least 1024 bytes.");
    }
    this.writer = writer;
    this.clock = clock;
    this.maxQueuedBytes = maxQueuedBytes;
    this.sequence = 0;
    this.blocked = false;
    this.queue = [];
    this.queuedBytes = 0;
    this.failure = null;
    this.failureListeners = new Set();

    if (typeof this.writer.on === "function") {
      this.writer.on("drain", () => this.#flush());
      this.writer.on("error", () => {
        this.#fail("writer_error", "JSONL output is unavailable.");
      });
    }
  }

  emit(type, payload = {}) {
    if (this.failure) {
      throw this.failure;
    }
    if (!EVENT_TYPE_PATTERN.test(type)) {
      throw new ProtocolSafetyError("Invalid JSONL event type.");
    }
    const event = {
      version: 1,
      sequence: ++this.sequence,
      time: this.clock().toISOString(),
      type,
      payload: sanitize(payload, new Set()),
    };
    const line = `${JSON.stringify(event)}\n`;
    this.#writeOrQueue(line);
    return event;
  }

  isReady() {
    return this.failure === null;
  }

  onFailure(listener) {
    if (typeof listener !== "function") {
      throw new TypeError("listener must be a function.");
    }
    this.failureListeners.add(listener);
    if (this.failure) {
      listener(this.failure);
    }
    return () => this.failureListeners.delete(listener);
  }

  #writeOrQueue(line) {
    if (this.blocked || this.queue.length > 0) {
      this.#enqueue(line);
      return;
    }
    try {
      if (this.writer.write(line) === false) {
        this.blocked = true;
      }
    } catch {
      throw this.#fail("writer_error", "JSONL output is unavailable.");
    }
  }

  #enqueue(line) {
    const bytes = Buffer.byteLength(line, "utf8");
    if (this.queuedBytes + bytes > this.maxQueuedBytes) {
      throw this.#fail("backpressure_limit", "JSONL output queue reached its safety limit.");
    }
    this.queue.push({ line, bytes });
    this.queuedBytes += bytes;
  }

  #flush() {
    if (this.failure) {
      return;
    }
    this.blocked = false;
    while (this.queue.length > 0) {
      const next = this.queue.shift();
      this.queuedBytes -= next.bytes;
      try {
        if (this.writer.write(next.line) === false) {
          this.blocked = true;
          return;
        }
      } catch {
        this.#fail("writer_error", "JSONL output is unavailable.");
        return;
      }
    }
  }

  #fail(code, message) {
    if (!this.failure) {
      this.failure = new ProtocolOutputError(code, message);
      this.queue.length = 0;
      this.queuedBytes = 0;
      for (const listener of this.failureListeners) {
        try {
          listener(this.failure);
        } catch {
          // Failure listeners are advisory; output remains failed closed.
        }
      }
    }
    return this.failure;
  }
}
