import { describe, expect, it } from "vitest";

import { buildTestServer } from "../helpers.js";

describe("GET /health", () => {
  it("answers 503 while MathJax is still initialising", async () => {
    const app = buildTestServer({ latexReady: () => false });
    try {
      const response = await app.inject({ method: "GET", url: "/health" });
      expect(response.statusCode).toBe(503);
      expect(response.json()).toEqual({
        error: { code: "not_ready", message: "MathJax is still initialising" },
      });
    } finally {
      await app.close();
    }
  });

  it("reports the version and every renderer once ready", async () => {
    const app = buildTestServer();
    try {
      const response = await app.inject({ method: "GET", url: "/health" });
      expect(response.statusCode).toBe(200);
      expect(response.json()).toEqual({
        status: "ok",
        version: "1.0.0-test",
        renderers: { latex: "ready", cards: "ready", ffmpeg: "7.1.1" },
      });
    } finally {
      await app.close();
    }
  });
});
