import { describe, expect, it } from "vitest";

import {
  InternalError,
  InvalidRequestError,
  MediaError,
  NotFoundError,
  NotReadyError,
  PayloadTooLargeError,
  RenderFailedError,
  RenderTimeoutError,
  UnauthorizedError,
  UnsupportedMediaTypeError,
} from "../../src/errors.js";

const cases: [MediaError, string, number][] = [
  [new UnauthorizedError(), "unauthorized", 401],
  [new NotFoundError(), "not_found", 404],
  [new InvalidRequestError("bad"), "invalid_request", 400],
  [new UnsupportedMediaTypeError("nope"), "unsupported_media_type", 415],
  [new PayloadTooLargeError(), "payload_too_large", 413],
  [new RenderFailedError("boom"), "render_failed", 422],
  [new RenderTimeoutError("slow"), "render_timeout", 504],
  [new InternalError(), "internal_error", 500],
  [new NotReadyError(), "not_ready", 503],
];

describe("MediaError serialisation", () => {
  it.each(cases)("%s serialises to the pinned envelope", (error, code, statusCode) => {
    expect(error.code).toBe(code);
    expect(error.statusCode).toBe(statusCode);
    expect(error.toBody()).toEqual({ error: { code, message: error.message } });
  });

  it("includes detail only when one was supplied", () => {
    const withDetail = new RenderFailedError("boom", { card: "leaderboard" });
    expect(withDetail.toBody()).toEqual({
      error: { code: "render_failed", message: "boom", detail: { card: "leaderboard" } },
    });
    expect(new RenderFailedError("boom").toBody().error).not.toHaveProperty("detail");
  });
});
