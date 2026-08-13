import { describe, expect, it } from "vitest";

import { loadConfig } from "../../src/config.js";

const VALID_KEY = "media-test-key-0123456789";

describe("loadConfig", () => {
  it("refuses to start without an API key", () => {
    expect(() => loadConfig({})).toThrow(/MEDIA_API_KEY/);
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
});
