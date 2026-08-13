/** `GET /health` — no auth, drives the container HEALTHCHECK and the k8s probes. */

import type { FastifyInstance } from "fastify";

import { NotReadyError } from "../errors.js";
import type { ServerDependencies } from "../dependencies.js";

export function registerHealthRoute(
  app: FastifyInstance,
  deps: ServerDependencies,
  version: string,
): void {
  app.get("/health", { config: { public: true } }, async () => {
    if (!deps.latexReady()) {
      throw new NotReadyError();
    }
    return {
      status: "ok",
      version,
      renderers: {
        latex: "ready",
        cards: "ready",
        ffmpeg: deps.ffmpegVersion,
      },
    };
  });
}
