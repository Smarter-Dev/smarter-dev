/** `POST /v1/latex` — JSON in, raw PNG out. */

import type { FastifyInstance } from "fastify";

import type { MediaConfig } from "../config.js";
import type { ServerDependencies } from "../dependencies.js";
import { InvalidRequestError } from "../errors.js";
import { LATEX_BODY_SCHEMA } from "../schemas/latex.js";
import { withTimeout } from "../withTimeout.js";

const BODY_LIMIT_BYTES = 16 * 1024;

export function registerLatexRoute(
  app: FastifyInstance,
  config: MediaConfig,
  deps: ServerDependencies,
): void {
  app.post<{ Body: { source: string } }>(
    "/v1/latex",
    { bodyLimit: BODY_LIMIT_BYTES, schema: { body: LATEX_BODY_SCHEMA } },
    async (request, reply) => {
      const source = request.body.source;
      if (source.trim() === "") {
        throw new InvalidRequestError("LaTeX source must not be empty");
      }

      const startedAt = Date.now();
      const rendered = await withTimeout(config.latexTimeoutMs, "LaTeX render", (signal) =>
        deps.renderLatex(source, signal),
      );

      return reply
        .header("Content-Type", "image/png")
        .header("Content-Disposition", 'inline; filename="latex.png"')
        .header("X-Media-Render-Ms", String(Date.now() - startedAt))
        .header("X-Media-Width", String(rendered.width))
        .header("X-Media-Height", String(rendered.height))
        .send(rendered.png);
    },
  );
}
