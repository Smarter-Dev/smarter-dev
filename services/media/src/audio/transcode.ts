/** WAV -> Ogg/Opus, running the same ffmpeg invocation the bot runs today. */

import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { RenderFailedError } from "../errors.js";

const STDERR_TAIL_BYTES = 2048;

export class FfmpegMissingError extends Error {}

export function probeFfmpegVersion(): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn("ffmpeg", ["-version"]);
    let stdout = "";
    child.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
    });
    child.on("error", (cause) => {
      reject(new FfmpegMissingError(`ffmpeg is not available: ${cause.message}`));
    });
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new FfmpegMissingError(`ffmpeg -version exited with ${code}`));
        return;
      }
      const match = /^ffmpeg version (\S+)/.exec(stdout);
      resolve(match?.[1] ?? "unknown");
    });
  });
}

function runFfmpeg(args: string[], signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn("ffmpeg", args);
    let stderr = "";

    const onAbort = () => child.kill("SIGKILL");
    signal.addEventListener("abort", onAbort, { once: true });

    child.stderr.on("data", (chunk: Buffer) => {
      stderr = (stderr + chunk.toString("utf8")).slice(-STDERR_TAIL_BYTES);
    });
    child.on("error", (cause) => {
      signal.removeEventListener("abort", onAbort);
      reject(new RenderFailedError(`ffmpeg could not be started: ${cause.message}`));
    });
    child.on("close", (code) => {
      signal.removeEventListener("abort", onAbort);
      if (code === 0) {
        resolve();
        return;
      }
      reject(new RenderFailedError(stderr.trim() || `ffmpeg exited with ${code}`));
    });
  });
}

export async function transcodeWavToOpusOgg(
  wav: Buffer,
  bitrate: string,
  signal: AbortSignal,
): Promise<Buffer> {
  const directory = await mkdtemp(join(tmpdir(), "media-audio-"));
  try {
    const input = join(directory, "in.wav");
    const output = join(directory, "out.ogg");
    await writeFile(input, wav);
    await runFfmpeg(
      ["-y", "-i", input, "-c:a", "libopus", "-b:a", bitrate, "-vbr", "on", output],
      signal,
    );
    return await readFile(output);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}
