/** One route per entry in the card registry. */

import type { FastifyInstance } from "fastify";

import type { MediaConfig } from "../config.js";
import { CARD_ROUTES } from "../cards/registry.js";
import type { ServerDependencies } from "../dependencies.js";
import { withTimeout } from "../withTimeout.js";

export const CARD_BODY_LIMIT_BYTES = 256 * 1024;

export function registerCardRoutes(
  app: FastifyInstance,
  config: MediaConfig,
  deps: ServerDependencies,
): void {
  for (const route of CARD_ROUTES) {
    app.post(
      route.path,
      { bodyLimit: CARD_BODY_LIMIT_BYTES, schema: { body: route.schema } },
      async (request, reply) => {
        const startedAt = Date.now();
        const painted = await withTimeout(config.cardTimeoutMs, `Card render (${route.name})`, () => {
          const layout = route.layout(request.body as never, deps.metrics, deps.now());
          return Promise.resolve(deps.paintCard(layout));
        });

        return reply
          .header("Content-Type", "image/png")
          .header("Content-Disposition", `inline; filename="${route.filename}"`)
          .header("X-Media-Render-Ms", String(Date.now() - startedAt))
          .header("X-Media-Width", String(painted.width))
          .header("X-Media-Height", String(painted.height))
          .send(painted.png);
      },
    );
  }
}
