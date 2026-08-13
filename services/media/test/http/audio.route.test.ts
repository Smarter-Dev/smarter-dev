import { readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { afterAll, describe, expect, it } from "vitest";

import { probeFfmpegVersion, transcodeWavToOpusOgg } from "../../src/audio/transcode.js";
import { authHeaders, buildTestServer } from "../helpers.js";

const TEMP_PREFIX = "media-audio-";

function tempDirectories(): string[] {
  return readdirSync(tmpdir()).filter((entry) => entry.startsWith(TEMP_PREFIX));
}

/** 1 s of 24 kHz mono 16-bit sine, wrapped in a WAV container. */
function sineWav(seconds = 1, sampleRate = 24_000): Buffer {
  const frames = seconds * sampleRate;
  const pcm = Buffer.alloc(frames * 2);
  for (let index = 0; index < frames; index += 1) {
    pcm.writeInt16LE(Math.round(Math.sin((index / sampleRate) * 440 * 2 * Math.PI) * 12000), index * 2);
  }
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + pcm.byteLength, 4);
  header.write("WAVE", 8);
  header.write("fmt ", 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(sampleRate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(pcm.byteLength, 40);
  return Buffer.concat([header, pcm]);
}

const ffmpegAvailable = await probeFfmpegVersion().then(
  () => true,
  () => false,
);

const app = buildTestServer({ transcodeWavToOpusOgg });

afterAll(async () => {
  await app.close();
});

function post(payload: Buffer | string, contentType = "audio/wav", query = "") {
  return app.inject({
    method: "POST",
    url: `/v1/audio/opus-ogg${query}`,
    headers: { ...authHeaders(), "content-type": contentType },
    payload,
  });
}

describe("POST /v1/audio/opus-ogg", () => {
  it("requires a bearer token", async () => {
    const response = await app.inject({
      method: "POST",
      url: "/v1/audio/opus-ogg",
      headers: { "content-type": "audio/wav" },
      payload: sineWav(),
    });
    expect(response.statusCode).toBe(401);
  });

  it("rejects a JSON content type", async () => {
    const response = await post("{}", "application/json");
    expect(response.statusCode).toBe(415);
    expect(response.json().error.code).toBe("unsupported_media_type");
  });

  it("rejects an empty body", async () => {
    const response = await post(Buffer.alloc(0));
    expect(response.statusCode).toBe(400);
    expect(response.json().error.code).toBe("invalid_request");
  });

  it.each(["nope", "48kbps", "12345k"])("rejects bitrate=%s", async (bitrate) => {
    const response = await post(sineWav(), "audio/wav", `?bitrate=${bitrate}`);
    expect(response.statusCode).toBe(400);
    expect(response.json().error.code).toBe("invalid_request");
  });

  it("rejects a body over 8 MiB", async () => {
    const response = await post(Buffer.alloc(9 * 1024 * 1024, 1));
    expect(response.statusCode).toBe(413);
    expect(response.json().error.code).toBe("payload_too_large");
  });

  it("turns a slow transcode into render_timeout", async () => {
    const slow = buildTestServer(
      {
        transcodeWavToOpusOgg: (_wav, _bitrate, signal) =>
          new Promise((_resolve, reject) => {
            signal.addEventListener("abort", () => reject(new Error("killed")), { once: true });
          }),
      },
      { audioTimeoutMs: 25 },
    );
    try {
      const response = await slow.inject({
        method: "POST",
        url: "/v1/audio/opus-ogg",
        headers: { ...authHeaders(), "content-type": "audio/wav" },
        payload: sineWav(),
      });
      expect(response.statusCode).toBe(504);
      expect(response.json().error.code).toBe("render_timeout");
    } finally {
      await slow.close();
    }
  });

  it("removes its temp directory even when ffmpeg cannot run", async () => {
    const before = tempDirectories();
    await transcodeWavToOpusOgg(Buffer.from("not audio"), "48k", new AbortController().signal).catch(
      () => undefined,
    );
    expect(tempDirectories()).toEqual(before);
  });

  it.skipIf(!ffmpegAvailable)("transcodes a sine WAV to Ogg", async () => {
    const before = tempDirectories();
    const response = await post(sineWav(), "audio/wav", "?bitrate=48k");
    expect(response.statusCode).toBe(200);
    expect(response.headers["content-type"]).toBe("audio/ogg");
    expect(response.headers["content-disposition"]).toBe('inline; filename="voice-message.ogg"');
    expect(response.rawPayload.subarray(0, 4).toString("latin1")).toBe("OggS");
    expect(tempDirectories()).toEqual(before);
  });

  it.skipIf(!ffmpegAvailable)("reports garbage input as render_failed with ffmpeg's stderr", async () => {
    const response = await post(Buffer.from("this is not a wav file"));
    expect(response.statusCode).toBe(422);
    expect(response.json().error.code).toBe("render_failed");
    expect(response.json().error.message.length).toBeGreaterThan(0);
  });
});
