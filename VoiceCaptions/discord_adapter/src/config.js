import { isAbsolute, normalize } from "node:path";

const SNOWFLAKE_PATTERN = /^\d{17,20}$/;
const MAX_STATE_PATH_LENGTH = 4_096;

export class ConfigurationError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "ConfigurationError";
    this.code = code;
  }
}

function requiredSecret(env, name) {
  const value = env[name]?.trim();
  if (!value || value.length < 30 || /\s/.test(value)) {
    throw new ConfigurationError(
      `invalid_${name.toLowerCase()}`,
      `${name} is missing or invalid. Configure it outside the repository.`,
    );
  }
  return value;
}

function snowflake(env, name, { required }) {
  const value = env[name]?.trim();
  if (!value && !required) {
    return null;
  }
  if (!value || !SNOWFLAKE_PATTERN.test(value)) {
    throw new ConfigurationError(
      `invalid_${name.toLowerCase()}`,
      `${name} must be a Discord snowflake.`,
    );
  }
  return value;
}

function boundedInteger(env, name, fallback, minimum, maximum) {
  const raw = env[name]?.trim();
  if (!raw) {
    return fallback;
  }
  if (!/^\d+$/.test(raw)) {
    throw new ConfigurationError(`invalid_${name.toLowerCase()}`, `${name} must be an integer.`);
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new ConfigurationError(
      `invalid_${name.toLowerCase()}`,
      `${name} must be between ${minimum} and ${maximum}.`,
    );
  }
  return value;
}

// Where the consent ledger persists. Absent means memory-only, which is what
// the tests use - but production must never reach that by accident, so the
// companion always supplies it and createProductionSessionManager treats a
// missing key as a programming error rather than a default.
function optionalJoinBaseUrl(env) {
  const raw = env.CAPTIONS_JOIN_BASE_URL;
  if (typeof raw !== "string" || !raw.trim()) {
    return "";
  }
  const value = raw.trim();
  if (
    value.length > 512
    || /\p{C}/u.test(value)
    || !/^https:\/\/[^\s]+$/.test(value)
  ) {
    throw new ConfigurationError(
      "invalid_captions_join_base_url",
      "CAPTIONS_JOIN_BASE_URL must be an https address.",
    );
  }
  return value;
}

function optionalStatePath(env, name) {
  const raw = env[name];
  if (raw === undefined || raw === null || raw === "") {
    return null;
  }
  if (typeof raw !== "string") {
    throw new ConfigurationError(
      `invalid_${name.toLowerCase()}`,
      `${name} must be an absolute file path.`,
    );
  }
  const value = raw.trim();
  if (
    !value ||
    value.length > MAX_STATE_PATH_LENGTH ||
    /\p{C}/u.test(value) ||
    !isAbsolute(value)
  ) {
    throw new ConfigurationError(
      `invalid_${name.toLowerCase()}`,
      `${name} must be an absolute file path.`,
    );
  }
  return normalize(value);
}

export function loadConfig(env = process.env) {
  const token = requiredSecret(env, "DISCORD_TOKEN");
  const config = {
    applicationId: snowflake(env, "DISCORD_APPLICATION_ID", { required: true }),
    guildId: snowflake(env, "DISCORD_GUILD_ID", { required: false }),
    consentStatePath: optionalStatePath(env, "CAPTIONS_CONSENT_STATE_PATH"),
    mappingStatePath: optionalStatePath(env, "CAPTIONS_MAPPING_STATE_PATH"),
    // No caption can exist until this much silence follows the last word,
    // so it is a floor on every caption's latency. 500 ms still bridges the
    // pauses inside a normal sentence while taking a quarter second off
    // every caption compared with the old 750. The env var stays for
    // operators who want the old window; the bounds are unchanged.
    silenceMs: boundedInteger(env, "CAPTIONS_SILENCE_MS", 500, 250, 5_000),
    connectTimeoutMs: boundedInteger(
      env,
      "CAPTIONS_CONNECT_TIMEOUT_MS",
      15_000,
      5_000,
      60_000,
    ),
    maxSegmentMs: boundedInteger(env, "CAPTIONS_MAX_SEGMENT_MS", 30_000, 5_000, 30_000),
    // Where a pairing invitation points. Absent is not fatal: /vem
    // setup then says so plainly instead of handing out a broken link.
    joinBaseUrl: optionalJoinBaseUrl(env),
  };

  // Keep the credential usable by the runtime but absent from serialization,
  // object spreads, and routine diagnostics.
  Object.defineProperty(config, "token", {
    configurable: false,
    enumerable: false,
    value: token,
    writable: false,
  });
  return Object.freeze(config);
}

export function publicConfigSummary(config) {
  return Object.freeze({
    applicationId: config.applicationId,
    guildId: config.guildId,
    silenceMs: config.silenceMs,
    connectTimeoutMs: config.connectTimeoutMs,
    maxSegmentMs: config.maxSegmentMs,
  });
}
