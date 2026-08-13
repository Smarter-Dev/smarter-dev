import { afterAll, describe, expect, it } from "vitest";

import { CARD_ROUTES } from "../../src/cards/registry.js";
import { CARD_NAMES, authHeaders, buildTestServer, cardRequest } from "../helpers.js";

const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47]);

const app = buildTestServer();

afterAll(async () => {
  await app.close();
});

function post(card: string, payload: unknown) {
  return app.inject({
    method: "POST",
    url: `/v1/cards/${card}`,
    headers: authHeaders(),
    payload: payload as never,
  });
}

const EXPECTED_FILENAMES: Record<string, string> = {
  balance: "balance.png",
  "transfer-success": "transfer_success.png",
};

describe("POST /v1/cards/*", () => {
  it.each(CARD_NAMES)("%s renders a PNG with the pinned headers", async (card) => {
    const response = await post(card, cardRequest(card));
    expect(response.statusCode).toBe(200);
    expect(response.headers["content-type"]).toBe("image/png");
    expect(response.headers["content-disposition"]).toBe(
      `inline; filename="${EXPECTED_FILENAMES[card] ?? "embed.png"}"`,
    );
    expect(response.headers["x-media-width"]).toBe("960");
    expect(response.headers["x-media-height"]).toBe("540");
    expect(Number(response.headers["x-media-render-ms"])).toBeGreaterThanOrEqual(0);
    expect(response.rawPayload.subarray(0, 4)).toEqual(PNG_MAGIC);
  });

  it.each(CARD_NAMES)("%s requires a bearer token", async (card) => {
    const response = await app.inject({
      method: "POST",
      url: `/v1/cards/${card}`,
      payload: cardRequest(card) as never,
    });
    expect(response.statusCode).toBe(401);
  });

  it("exposes exactly the fourteen documented paths", () => {
    expect(CARD_ROUTES.map((route) => route.path).sort()).toEqual(
      [...CARD_NAMES].map((card) => `/v1/cards/${card}`).sort(),
    );
  });

  it("rejects an unknown field", async () => {
    const response = await post("error", { message: "hi", severity: "high" });
    expect(response.statusCode).toBe(400);
    expect(response.json().error.code).toBe("invalid_request");
    expect(response.json().error.detail).toBeDefined();
  });

  it("rejects a missing required field", async () => {
    const response = await post("balance", { username: "nyx" });
    expect(response.statusCode).toBe(400);
    expect(response.json().error.code).toBe("invalid_request");
  });

  it("rejects a wrongly typed field", async () => {
    const response = await post("balance", { username: "nyx", balance: "lots" });
    expect(response.statusCode).toBe(400);
  });

  it("rejects an entries array over maxItems", async () => {
    const entries = Array.from({ length: 101 }, (_unused, index) => ({
      rank: index + 1,
      user_id: `${index}`,
      balance: 1,
      streak_count: 0,
    }));
    const response = await post("leaderboard", {
      entries,
      guild_name: "g",
      user_display_names: {},
    });
    expect(response.statusCode).toBe(400);
  });

  it("rejects an embed_type outside the enum", async () => {
    const response = await post("simple", { title: "t", description: "d", embed_type: "chartreuse" });
    expect(response.statusCode).toBe(400);
  });

  it("applies schema defaults for omitted optional fields", async () => {
    const response = await post("balance", { username: "nyx", balance: 10 });
    expect(response.statusCode).toBe(200);
  });

  it("rejects a body over 256 KiB", async () => {
    const response = await app.inject({
      method: "POST",
      url: "/v1/cards/error",
      headers: { ...authHeaders(), "content-type": "application/json" },
      payload: JSON.stringify({ message: "x".repeat(300 * 1024) }),
    });
    expect(response.statusCode).toBe(413);
  });

  it("turns a slow paint into render_timeout", async () => {
    const slow = buildTestServer(
      {
        paintCard: () => {
          const deadline = Date.now() + 60;
          while (Date.now() < deadline) {
            // Busy-wait so the timeout has already fired by the time we resolve.
          }
          return { png: Buffer.alloc(0), width: 960, height: 540 };
        },
      },
      { cardTimeoutMs: 1 },
    );
    try {
      const response = await slow.inject({
        method: "POST",
        url: "/v1/cards/error",
        headers: authHeaders(),
        payload: { message: "hi" },
      });
      expect(response.statusCode).toBe(504);
      expect(response.json().error.code).toBe("render_timeout");
    } finally {
      await slow.close();
    }
  });

  it("reports a canvas failure as internal_error", async () => {
    const broken = buildTestServer({
      paintCard: () => {
        throw new Error("canvas exploded");
      },
    });
    try {
      const response = await broken.inject({
        method: "POST",
        url: "/v1/cards/error",
        headers: authHeaders(),
        payload: { message: "hi" },
      });
      expect(response.statusCode).toBe(500);
      expect(response.json().error.code).toBe("internal_error");
    } finally {
      await broken.close();
    }
  });

  it("returns the pinned envelope for an unknown card", async () => {
    const response = await post("does-not-exist", {});
    expect(response.statusCode).toBe(404);
    expect(response.json()).toEqual({
      error: { code: "not_found", message: "No route for POST /v1/cards/does-not-exist" },
    });
  });
});
