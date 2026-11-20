/**
 * Opaque, encoded credentials. Obfuscation, not encryption.
 *
 * Must stay byte for byte identical to smarter_dev/shared/credentials.py:
 * value = "v1." + base64url_nopad(utf8(plain) XOR keystream), where the
 * keystream is sha256(MASK || uint32_be(i)) for i = 0, 1, 2, ... concatenated.
 */
import { createHash } from "node:crypto";

export const PREFIX = "v1.";
const MASK = Buffer.from("f57d64f1e6341340da83b07d00003b1cb3478f4083d21eda", "hex");

function xorKeystream(data: Buffer): Buffer {
  const out = Buffer.alloc(data.length);
  let offset = 0;
  for (let counter = 0; offset < data.length; counter += 1) {
    const index = Buffer.alloc(4);
    index.writeUInt32BE(counter);
    const block = createHash("sha256").update(MASK).update(index).digest();
    for (let i = 0; i < block.length && offset < data.length; i += 1, offset += 1) {
      out[offset] = data[offset]! ^ block[i]!;
    }
  }
  return out;
}

export function encodeValue(value: string): string {
  return PREFIX + xorKeystream(Buffer.from(value, "utf8")).toString("base64url");
}

export function decodeValue(encoded: string): string {
  if (!encoded.startsWith(PREFIX)) {
    throw new Error("encoded credential must start with 'v1.'");
  }
  const body = encoded.slice(PREFIX.length);
  if (!/^[A-Za-z0-9_-]*$/.test(body)) {
    throw new Error("encoded credential is malformed");
  }
  return xorKeystream(Buffer.from(body, "base64url")).toString("utf8");
}
