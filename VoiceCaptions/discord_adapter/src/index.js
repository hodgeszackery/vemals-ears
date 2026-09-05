import { PcmJsonlAudioSink } from "./audio_sink.js";
import { createInterface } from "node:readline";
import { ConfigurationError, loadConfig } from "./config.js";
import { DiscordAdapterRuntime } from "./discord_runtime.js";
import { JsonlEmitter } from "./protocol.js";
import {
  QuickChatCommandQueue,
  readQuickChatLines,
} from "./quick_chat_input.js";
import { createProductionSessionManager } from "./session_manager.js";

const emitter = new JsonlEmitter();

let config;
try {
  config = loadConfig();
} catch (error) {
  const reason = error instanceof ConfigurationError ? error.code : "configuration_failed";
  emitter.emit("adapter.failed", { reason });
  process.stderr.write(`Vemal's Ears adapter stopped: ${reason}\n`);
  process.exitCode = 2;
}

let sessionManager;
if (config) {
  try {
    sessionManager = createProductionSessionManager(config);
  } catch {
    emitter.emit("adapter.failed", { reason: "consent_state_unavailable" });
    process.stderr.write("Vemal's Ears adapter stopped: consent_state_unavailable\n");
    process.exitCode = 2;
    config = null;
  }
}

if (config && sessionManager) {
  const audioSink = new PcmJsonlAudioSink({ emitter });
  const runtime = new DiscordAdapterRuntime({
    config,
    emitter,
    audioSink,
    sessionManager,
  });

  let stopping = false;
  const stop = async (reason) => {
    if (stopping) {
      return;
    }
    stopping = true;
    await runtime.shutdown(reason);
  };

  process.once("SIGINT", () => {
    void stop("sigint");
  });
  process.once("SIGTERM", () => {
    void stop("sigterm");
  });

  // The shutdown channel that works on Windows.
  //
  // The coordinator stops this process with terminate(), which on Windows
  // is TerminateProcess: no signal is delivered, so neither handler above
  // ever runs, so the consent ledger's close() - the only thing that
  // clears its dirty marker - never happened on the platform this is
  // actually deployed on. Every run started dirty, and consent
  // persistence was a no-op that quietly ate the file.
  //
  // Closing our stdin is something Node sees everywhere. The coordinator
  // closes it, waits, and only then falls back to terminate().
  if (process.stdin && typeof process.stdin.on === "function") {
    const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
    let closeReason = "stdin_closed";
    const failInput = (reason, exitCode) => {
      if (closeReason === "stdin_closed") {
        closeReason = reason;
        process.exitCode = exitCode;
      }
      input.close();
    };
    // A typed message that fails is one message that failed.
    //
    // Both of these used to close stdin, which shuts the adapter down -
    // leaving the voice channel, ending captions for everyone in it. One
    // person's line going wrong took the room's captions with it. Shino,
    // 2026-09-04, on the other direction: "ability to type when voice
    // goes down is a feature not a bug." It has to hold both ways round,
    // and it mattered more this way: voice is the thing people are in the
    // middle of when the message is sent.
    //
    // The queue has already reported the message as delivery_failed by
    // the time either of these runs, so the relay learns the truth and
    // can retry. Nothing is silently swallowed: the reason goes to
    // stderr, where the operator reads every other adapter fault.
    const commandQueue = new QuickChatCommandQueue({
      bind: (command) => runtime.bindQuickChat(command),
      deliver: (command) => runtime.postQuickChat(command),
      onResult: ({ requestId, status }) => {
        emitter.emit("quickchat.result", { requestId, status });
      },
      onError: () => {
        process.stderr.write("Vemal's Ears quick chat failed: quick_chat_failed\n");
      },
    });
    readQuickChatLines({
      input,
      queue: commandQueue,
      log: (reason) => {
        process.stderr.write("Vemal's Ears quick chat failed: " + reason + "\n");
      },
    });
    input.on("close", () => {
      void commandQueue.close().finally(() => stop(closeReason));
    });
    process.stdin.on("error", () => {
      failInput("stdin_error", 1);
    });
  }

  runtime.start().catch(async () => {
    process.stderr.write("Vemal's Ears adapter stopped: discord_start_failed\n");
    process.exitCode = 1;
    await stop("start_failed");
  });
}
