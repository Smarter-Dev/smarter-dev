import { afterAll, describe, expect, it } from "vitest";

import { RenderFailedError } from "../../src/errors.js";
import { renderLatex } from "../../src/latex/rasterize.js";
import { authHeaders, buildTestServer } from "../helpers.js";

const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47]);

const app = buildTestServer({ renderLatex: (source) => renderLatex(source) });

afterAll(async () => {
  await app.close();
});

async function post(payload: unknown, headers: Record<string, string> = authHeaders()) {
  return app.inject({ method: "POST", url: "/v1/latex", headers, payload: payload as never });
}

describe("POST /v1/latex", () => {
  it("requires a bearer token", async () => {
    const response = await post({ source: "E = mc^2" }, {});
    expect(response.statusCode).toBe(401);
  });

  it("renders valid TeX to a PNG with the pinned headers", async () => {
    const response = await post({ source: "E = mc^2" });
    expect(response.statusCode).toBe(200);
    expect(response.headers["content-type"]).toBe("image/png");
    expect(response.headers["content-disposition"]).toBe('inline; filename="latex.png"');
    expect(Number(response.headers["x-media-width"])).toBeGreaterThan(0);
    expect(Number(response.headers["x-media-height"])).toBeGreaterThan(0);
    expect(Number(response.headers["x-media-render-ms"])).toBeGreaterThanOrEqual(0);
    expect(response.rawPayload.subarray(0, 4)).toEqual(PNG_MAGIC);
  });

  it("renders MathJax's own error box for bad TeX, as the worker does today", async () => {
    const response = await post({ source: "\\frac{1}" });
    expect(response.statusCode).toBe(200);
    expect(response.rawPayload.subarray(0, 4)).toEqual(PNG_MAGIC);
  });

  it("reports a renderer rejection as a recoverable render failure", async () => {
    const failing = buildTestServer({
      renderLatex: async () => {
        throw new RenderFailedError("TeX parse error: Undefined control sequence \\foo");
      },
    });
    try {
      const response = await failing.inject({
        method: "POST",
        url: "/v1/latex",
        headers: authHeaders(),
        payload: { source: "\\foo" },
      });
      expect(response.statusCode).toBe(422);
      expect(response.json()).toEqual({
        error: {
          code: "render_failed",
          message: "TeX parse error: Undefined control sequence \\foo",
        },
      });
    } finally {
      await failing.close();
    }
  });

  it.each([
    ["a missing source", {}],
    ["an empty source", { source: "" }],
    ["a whitespace-only source", { source: "   \n  " }],
    ["a source over 1800 characters", { source: "x".repeat(1801) }],
    ["an unknown field", { source: "x", colour: "red" }],
  ])("rejects %s with invalid_request", async (_name, payload) => {
    const response = await post(payload);
    expect(response.statusCode).toBe(400);
    expect(response.json().error.code).toBe("invalid_request");
  });

  it("rejects a body over 16 KiB", async () => {
    const response = await app.inject({
      method: "POST",
      url: "/v1/latex",
      headers: { ...authHeaders(), "content-type": "application/json" },
      payload: JSON.stringify({ source: "x".repeat(20 * 1024) }),
    });
    expect(response.statusCode).toBe(413);
    expect(response.json().error.code).toBe("payload_too_large");
  });

  it("rejects a non-JSON content type", async () => {
    const response = await app.inject({
      method: "POST",
      url: "/v1/latex",
      headers: { ...authHeaders(), "content-type": "text/plain" },
      payload: "E = mc^2",
    });
    expect(response.statusCode).toBe(415);
    expect(response.json().error.code).toBe("unsupported_media_type");
  });

  it("turns a slow render into render_timeout", async () => {
    const slow = buildTestServer(
      {
        renderLatex: (_source, signal) =>
          new Promise((_resolve, reject) => {
            signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
          }),
      },
      { latexTimeoutMs: 25 },
    );
    try {
      const response = await slow.inject({
        method: "POST",
        url: "/v1/latex",
        headers: authHeaders(),
        payload: { source: "E = mc^2" },
      });
      expect(response.statusCode).toBe(504);
      expect(response.json().error.code).toBe("render_timeout");
    } finally {
      await slow.close();
    }
  });

  it("surfaces an oversized render as render_failed", async () => {
    const oversized = buildTestServer({
      renderLatex: async () => {
        throw new RenderFailedError("Rendered image exceeds 4096px (8000x40)");
      },
    });
    try {
      const response = await oversized.inject({
        method: "POST",
        url: "/v1/latex",
        headers: authHeaders(),
        payload: { source: "x" },
      });
      expect(response.statusCode).toBe(422);
      expect(response.json().error.message).toContain("4096px");
    } finally {
      await oversized.close();
    }
  });
});
