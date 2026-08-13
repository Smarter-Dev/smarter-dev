/** Pure server factory: no `listen`, no process-level side effects. */

import Fastify from "fastify";
import type { FastifyInstance } from "fastify";

import { requireBearerToken } from "./auth.js";
import type { MediaConfig } from "./config.js";
import type { ServerDependencies } from "./dependencies.js";
import {
  InternalError,
  InvalidRequestError,
  MediaError,
  NotFoundError,
  PayloadTooLargeError,
  UnsupportedMediaTypeError,
} from "./errors.js";
import { registerAudioRoute } from "./routes/audio.js";
import { registerCardRoutes } from "./routes/cards.js";
import { registerHealthRoute } from "./routes/health.js";
import { registerLatexRoute } from "./routes/latex.js";

export const SERVICE_VERSION = "1.0.0";

function asMediaError(error: unknown): MediaError {
  if (error instanceof MediaError) {
    return error;
  }

  const fastifyError = error as { code?: string; validation?: unknown; message?: string };
  switch (fastifyError.code) {
    case "FST_ERR_CTP_BODY_TOO_LARGE":
    case "FST_ERR_CTP_INVALID_CONTENT_LENGTH":
      return new PayloadTooLargeError();
    case "FST_ERR_CTP_INVALID_MEDIA_TYPE":
    case "FST_ERR_CTP_EMPTY_TYPE":
      return new UnsupportedMediaTypeError(fastifyError.message ?? "Unsupported content type");
    case "FST_ERR_CTP_EMPTY_JSON_BODY":
    case "FST_ERR_CTP_INVALID_JSON_BODY":
      return new InvalidRequestError(fastifyError.message ?? "Malformed JSON body");
    default:
      break;
  }

  if (fastifyError.validation !== undefined) {
    return new InvalidRequestError(fastifyError.message ?? "Request failed validation", {
      validation: fastifyError.validation,
    });
  }

  return new InternalError(fastifyError.message ?? "Internal error");
}

export function buildServer(config: MediaConfig, deps: ServerDependencies): FastifyInstance {
  const app = Fastify({
    logger: { level: config.logLevel },
    bodyLimit: 256 * 1024,
    ajv: {
      // Fastify defaults to stripping unknown fields and coercing types. Both
      // hide caller mistakes, and §0.4 wants a schema violation to be a 400.
      customOptions: { removeAdditional: false, coerceTypes: false, useDefaults: true },
    },
  });

  // Without this, `Content-Type: text/plain` would be parsed as a string and
  // fail schema validation with a 400 where the contract calls for a 415.
  app.removeContentTypeParser("text/plain");

  app.addContentTypeParser(
    "audio/wav",
    { parseAs: "buffer", bodyLimit: 8 * 1024 * 1024 },
    (_request, body, done) => {
      done(null, body);
    },
  );

  app.addHook("onRequest", requireBearerToken(config.apiKey));

  app.setNotFoundHandler((request, reply) => {
    const error = new NotFoundError(`No route for ${request.method} ${request.url}`);
    reply.status(error.statusCode).type("application/json").send(error.toBody());
  });

  app.setErrorHandler((error, request, reply) => {
    const mediaError = asMediaError(error);
    if (mediaError.statusCode >= 500 && mediaError.code !== "not_ready") {
      request.log.error({ err: error }, "request failed");
    }
    reply.status(mediaError.statusCode).type("application/json").send(mediaError.toBody());
  });

  registerHealthRoute(app, deps, `${SERVICE_VERSION}-${config.imageTag}`);
  registerLatexRoute(app, config, deps);
  registerAudioRoute(app, config, deps);
  registerCardRoutes(app, config, deps);

  return app;
}
