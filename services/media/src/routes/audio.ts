/** `POST /v1/audio/opus-ogg` — raw WAV in, raw Ogg out. */

import type { FastifyInstance } from "fastify";

import type { MediaConfig } from "../config.js";
import type { ServerDependencies } from "../dependencies.js";
import { InvalidRequestError, UnsupportedMediaTypeError } from "../errors.js";
import { AUDIO_QUERY_SCHEMA } from "../schemas/latex.js";
import { withTimeout } from "../withTimeout.js";

export const AUDIO_BODY_LIMIT_BYTES = 8 * 1024 * 1024;

export function registerAudioRoute(
  app: FastifyInstance,
  config: MediaConfig,
  deps: ServerDependencies,
): void {
  app.post<{ Querystring: { bitrate: string } }>(
    "/v1/audio/opus-ogg",
    { bodyLimit: AUDIO_BODY_LIMIT_BYTES, schema: { querystring: AUDIO_QUERY_SCHEMA } },
    async (request, reply) => {
      // Fastify's own JSON parser would happily accept `application/json` here,
      // so the route states the one type it takes rather than inferring it.
      const contentType = request.headers["content-type"] ?? "";
      if (contentType.split(";")[0]!.trim().toLowerCase() !== "audio/wav") {
        throw new UnsupportedMediaTypeError(`Expected Content-Type audio/wav, got ${contentType}`);
      }

      const wav = request.body;
      if (!Buffer.isBuffer(wav) || wav.byteLength === 0) {
        throw new InvalidRequestError("WAV body must not be empty");
      }

      const startedAt = Date.now();
      const ogg = await withTimeout(config.audioTimeoutMs, "Audio transcode", (signal) =>
        deps.transcodeWavToOpusOgg(wav, request.query.bitrate, signal),
      );

      return reply
        .header("Content-Type", "audio/ogg")
        .header("Content-Disposition", 'inline; filename="voice-message.ogg"')
        .header("X-Media-Render-Ms", String(Date.now() - startedAt))
        .send(ogg);
    },
  );
}
