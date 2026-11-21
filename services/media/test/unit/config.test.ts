import { describe, expect, it } from "vitest";

import { loadConfig } from "../../src/config.js";
import { encodeValue } from "../../src/credentials.js";

const VALID_KEY = "media-test-key-0123456789";
const PROD = { ENVIRONMENT: "production" };
const DEV = { ENVIRONMENT: "development" };

describe("loadConfig", () => {
  it("refuses to start without an API key", () => {
    expect(() => loadConfig({ ...PROD })).toThrow(/HVTH7T/);
  });

  it("refuses to start with a short API key", () => {
    expect(() => loadConfig({ ...PROD, HVTH7T: encodeValue("too-short") })).toThrow(/at least 16/);
  });

  it("applies the documented defaults", () => {
    expect(loadConfig({ ...PROD, HVTH7T: encodeValue(VALID_KEY) })).toEqual({
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
    expect(() =>
      loadConfig({ ...PROD, HVTH7T: encodeValue(VALID_KEY), MEDIA_PORT: "eight" }),
    ).toThrow(/MEDIA_PORT/);
  });

  it.each([
    ["production", { ENVIRONMENT: "production" }],
    ["unset", {}],
    ["a typo", { ENVIRONMENT: "prod" }],
  ])("ignores MEDIA_API_KEY and a plain HVTH7T when ENVIRONMENT is %s", (_label, env) => {
    expect(() => loadConfig({ ...env, MEDIA_API_KEY: VALID_KEY })).toThrow(/HVTH7T must be set/);
    expect(() => loadConfig({ ...env, HVTH7T: VALID_KEY })).toThrow(/HVTH7T must be set/);
  });

  it.each([
    ["no v1. prefix", VALID_KEY],
    ["bad base64 length", "v1.abcde"],
    ["= padding", "v1.abcd="],
    ["invalid UTF-8", `v1.${Buffer.from([0xff, 0xfe, 0xfd]).toString("base64url")}`],
  ])("never falls back to MEDIA_API_KEY in production when HVTH7T is invalid (%s)", (_label, bad) => {
    expect(() => loadConfig({ ...PROD, HVTH7T: bad, MEDIA_API_KEY: VALID_KEY })).toThrow(
      /HVTH7T must be set/,
    );
  });

  it("trims decoded values", () => {
    expect(loadConfig({ ...PROD, HVTH7T: encodeValue(`${VALID_KEY}\n`) }).apiKey).toBe(VALID_KEY);
  });

  it("rejects a value that decodes to whitespace in production", () => {
    expect(() => loadConfig({ ...PROD, HVTH7T: encodeValue("  ") })).toThrow(/HVTH7T must be set/);
  });

  describe("in development", () => {
    it("accepts an encoded HVTH7T", () => {
      expect(loadConfig({ ...DEV, HVTH7T: encodeValue(VALID_KEY) }).apiKey).toBe(VALID_KEY);
    });

    it("accepts a plain (non-v1.) value in HVTH7T", () => {
      expect(loadConfig({ ...DEV, HVTH7T: ` ${VALID_KEY}\n` }).apiKey).toBe(VALID_KEY);
    });

    it("accepts the plain MEDIA_API_KEY name", () => {
      expect(loadConfig({ ...DEV, MEDIA_API_KEY: ` ${VALID_KEY}\n` }).apiKey).toBe(VALID_KEY);
    });

    it("prefers a valid encoded HVTH7T over MEDIA_API_KEY", () => {
      const config = loadConfig({
        ...DEV,
        HVTH7T: encodeValue(VALID_KEY),
        MEDIA_API_KEY: "other-key-0123456789",
      });
      expect(config.apiKey).toBe(VALID_KEY);
    });

    it("is also on for test and testing", () => {
      for (const ENVIRONMENT of ["test", "TESTING"]) {
        expect(loadConfig({ ENVIRONMENT, MEDIA_API_KEY: VALID_KEY }).apiKey).toBe(VALID_KEY);
      }
    });
  });
});
