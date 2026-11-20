import { describe, expect, it } from "vitest";

import { loadConfig } from "../../src/config.js";
import { encodeValue } from "../../src/credentials.js";

const VALID_KEY = "media-test-key-0123456789";

describe("loadConfig", () => {
  it("refuses to start without an API key", () => {
    expect(() => loadConfig({})).toThrow(/HVTH7T/);
  });

  it("refuses to start with a short API key", () => {
    expect(() => loadConfig({ MEDIA_API_KEY: "too-short" })).toThrow(/at least 16/);
  });

  it("applies the documented defaults", () => {
    expect(loadConfig({ MEDIA_API_KEY: VALID_KEY })).toEqual({
      apiKey: VALID_KEY,
      port: 8080,
      logLevel: "info",
      latexTimeoutMs: 5000,
      audioTimeoutMs: 30000,
      cardTimeoutMs: 5000,
      imageTag: "dev",
    });
  });

  it("rejects a non-numeric port", () => {
    expect(() => loadConfig({ MEDIA_API_KEY: VALID_KEY, MEDIA_PORT: "eight" })).toThrow(
      /MEDIA_PORT/,
    );
  });

  it("prefers the opaque HVTH7T over the legacy MEDIA_API_KEY", () => {
    const config = loadConfig({ HVTH7T: encodeValue(VALID_KEY), MEDIA_API_KEY: "legacy-key-0123456789" });
    expect(config.apiKey).toBe(VALID_KEY);
  });

  it("rejects an HVTH7T without the v1. prefix", () => {
    expect(() => loadConfig({ HVTH7T: VALID_KEY })).toThrow(/v1\./);
  });

  it("trims decoded and legacy values", () => {
    expect(loadConfig({ HVTH7T: encodeValue(`${VALID_KEY}\n`) }).apiKey).toBe(VALID_KEY);
    expect(loadConfig({ MEDIA_API_KEY: ` ${VALID_KEY}\n` }).apiKey).toBe(VALID_KEY);
  });

  it("falls back to the legacy name when HVTH7T decodes to whitespace", () => {
    const config = loadConfig({ HVTH7T: encodeValue("  "), MEDIA_API_KEY: VALID_KEY });
    expect(config.apiKey).toBe(VALID_KEY);
  });
});
