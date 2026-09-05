import assert from "node:assert/strict";
import { resolve } from "node:path";
import test from "node:test";

import { ConfigurationError, loadConfig, publicConfigSummary } from "../src/config.js";

const VALID_ENV = Object.freeze({
  DISCORD_TOKEN: `not-a-real-token.${"x".repeat(40)}`,
  DISCORD_APPLICATION_ID: "123456789012345678",
  DISCORD_GUILD_ID: "223456789012345678",
});

test("config validates values while keeping the token non-enumerable", () => {
  const config = loadConfig({ ...VALID_ENV });

  assert.equal(config.token, VALID_ENV.DISCORD_TOKEN);
  assert.equal(config.applicationId, VALID_ENV.DISCORD_APPLICATION_ID);
  assert.equal(config.consentStatePath, null);
  // 500 is a latency floor on every caption, so the default is pinned here:
  // a drift back to the old 750 would silently slow every caption by a
  // quarter second with nothing else failing.
  assert.equal(config.silenceMs, 500);
  assert.equal(config.maxSegmentMs, 30_000);
  assert.equal(JSON.stringify(config).includes(VALID_ENV.DISCORD_TOKEN), false);
  assert.deepEqual(publicConfigSummary(config), {
    applicationId: VALID_ENV.DISCORD_APPLICATION_ID,
    guildId: VALID_ENV.DISCORD_GUILD_ID,
    silenceMs: 500,
    connectTimeoutMs: 15_000,
    maxSegmentMs: 30_000,
  });
});

test("config accepts only an absolute dedicated consent state file", () => {
  const statePath = resolve("state", "consent.json");
  const config = loadConfig({
    ...VALID_ENV,
    CAPTIONS_CONSENT_STATE_PATH: statePath,
  });

  assert.equal(config.consentStatePath, statePath);
  assert.equal(JSON.stringify(publicConfigSummary(config)).includes(statePath), false);
  for (const invalid of ["state/consent.json", "bad\0path", 42]) {
    assert.throws(
      () => loadConfig({ ...VALID_ENV, CAPTIONS_CONSENT_STATE_PATH: invalid }),
      (error) =>
        error instanceof ConfigurationError &&
        error.code === "invalid_captions_consent_state_path" &&
        !error.message.includes(String(invalid)),
    );
  }
});

test("config rejects missing credentials without echoing a supplied value", () => {
  const supplied = "short-sensitive-value";
  assert.throws(
    () => loadConfig({ ...VALID_ENV, DISCORD_TOKEN: supplied }),
    (error) => {
      assert.ok(error instanceof ConfigurationError);
      assert.equal(error.code, "invalid_discord_token");
      assert.equal(error.message.includes(supplied), false);
      return true;
    },
  );
});

test("config rejects unsafe receive timing", () => {
  assert.throws(
    () => loadConfig({ ...VALID_ENV, CAPTIONS_MAX_SEGMENT_MS: "999999" }),
    (error) => error instanceof ConfigurationError && error.code === "invalid_captions_max_segment_ms",
  );
});

test("config keeps CAPTIONS_SILENCE_MS as an operator override of the 500 default", () => {
  // The 500 default is a floor on caption latency, not a ceiling on what an
  // operator may want; the old 750 must stay reachable through the env var,
  // and the 250..5000 bounds must still be enforced on it. Without this a
  // hard-coded 500 would pass the default assertion and silently drop the
  // override.
  assert.equal(loadConfig({ ...VALID_ENV, CAPTIONS_SILENCE_MS: "750" }).silenceMs, 750);
  assert.throws(
    () => loadConfig({ ...VALID_ENV, CAPTIONS_SILENCE_MS: "249" }),
    (error) => error instanceof ConfigurationError && error.code === "invalid_captions_silence_ms",
  );
});

test("config cannot exceed the local STT 30 second segment boundary", () => {
  assert.equal(
    loadConfig({ ...VALID_ENV, CAPTIONS_MAX_SEGMENT_MS: "30000" }).maxSegmentMs,
    30_000,
  );
  assert.throws(
    () => loadConfig({ ...VALID_ENV, CAPTIONS_MAX_SEGMENT_MS: "30001" }),
    (error) => error instanceof ConfigurationError && error.code === "invalid_captions_max_segment_ms",
  );
});
