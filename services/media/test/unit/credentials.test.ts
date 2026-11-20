import { describe, expect, it } from "vitest";

import { decodeValue, encodeValue } from "../../src/credentials.js";

// Shared with smarter_dev/shared/credentials.py (tests/shared/test_credentials.py).
const HUNDRED = "abcdefghijklmnopqrstuvwxyz".repeat(4).slice(0, 100);
const VECTORS: Array<[string, string]> = [
  ["hello", "v1.-PEJJ30"],
  [
    HUNDRED,
    "v1.8fYGL3ffnLgeWOKWu7SXGM0HxWDKe99rYkhhqXx6esSxwJ1exa0o3_tIAHdkoi1LK_ib1ffado-_J6v18XOqbewvuyfC5Qq_UrICBwjGMhZEUxpOvbVm85RMj_Rruyn6ttJxJg",
  ],
];

describe("credentials", () => {
  it.each(VECTORS)("encodes %s to the fixed vector", (plain, encoded) => {
    expect(encodeValue(plain)).toBe(encoded);
    expect(decodeValue(encoded)).toBe(plain);
  });

  it("round-trips multi-byte text and spans keystream blocks", () => {
    const plain = "ключ-🔑-".repeat(20);
    expect(decodeValue(encodeValue(plain))).toBe(plain);
  });

  it("rejects values without the v1. prefix", () => {
    expect(() => decodeValue("plain-value")).toThrow(/v1\./);
  });

  it.each(["v1.a+b", "v1.a/b", "v1.a=b", "v1.abcd="])("rejects non-base64url body %s", (bad) => {
    expect(() => decodeValue(bad)).toThrow(/malformed/);
  });

  it("rejects a body whose length is 1 mod 4", () => {
    expect(() => decodeValue("v1.abcde")).toThrow(/malformed/);
  });

  it("rejects bytes that are not valid UTF-8", () => {
    // 0xff 0xfe 0xfd XORed with the keystream, then base64url: decodes to
    // bytes that are not UTF-8 once unmasked.
    const body = Buffer.from([0x01, 0x02, 0x03]).toString("base64url");
    expect(() => decodeValue(`v1.${body}`)).toThrow(/malformed/);
  });
});
