/** Bearer-token gate. Every route is protected unless it declares `public: true`. */

import { timingSafeEqual } from "node:crypto";
import type { FastifyReply, FastifyRequest } from "fastify";

import { UnauthorizedError } from "./errors.js";

const BEARER_PREFIX = "Bearer ";

export interface RouteAccessConfig {
  public?: boolean;
}

function presentedToken(header: string | undefined): Buffer {
  if (header === undefined || !header.startsWith(BEARER_PREFIX)) {
    return Buffer.alloc(0);
  }
  return Buffer.from(header.slice(BEARER_PREFIX.length), "utf8");
}

export function requireBearerToken(apiKey: string) {
  const expected = Buffer.from(apiKey, "utf8");

  return async function onRequest(request: FastifyRequest, _reply: FastifyReply): Promise<void> {
    const routeConfig = request.routeOptions.config as RouteAccessConfig | undefined;
    if (routeConfig?.public === true) {
      return;
    }

    const presented = presentedToken(request.headers.authorization);
    if (presented.length !== expected.length) {
      throw new UnauthorizedError();
    }
    if (!timingSafeEqual(presented, expected)) {
      throw new UnauthorizedError();
    }
  };
}
