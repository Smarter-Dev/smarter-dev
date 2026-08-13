import { afterAll, describe, expect, it } from "vitest";

import { TEST_API_KEY, buildTestServer } from "../helpers.js";

const app = buildTestServer();

afterAll(async () => {
  await app.close();
});

const UNAUTHORIZED_BODY = {
  error: { code: "unauthorized", message: "Missing or invalid bearer token" },
};

describe("bearer token gate", () => {
  it("rejects a request with no Authorization header", async () => {
    const response = await app.inject({ method: "POST", url: "/v1/cards/error" });
    expect(response.statusCode).toBe(401);
    expect(response.json()).toEqual(UNAUTHORIZED_BODY);
  });

  it.each([
    ["wrong scheme", "Basic dXNlcjpwYXNz"],
    ["empty token", "Bearer "],
    ["right length, wrong bytes", `Bearer ${"x".repeat(TEST_API_KEY.length)}`],
    ["wrong length", "Bearer short"],
    ["token without scheme", TEST_API_KEY],
  ])("rejects %s with a byte-identical response", async (_name, authorization) => {
    const response = await app.inject({
      method: "POST",
      url: "/v1/cards/error",
      headers: { authorization },
      payload: { message: "hi" },
    });
    expect(response.statusCode).toBe(401);
    expect(response.json()).toEqual(UNAUTHORIZED_BODY);
  });

  it("accepts the configured token", async () => {
    const response = await app.inject({
      method: "POST",
      url: "/v1/cards/error",
      headers: { authorization: `Bearer ${TEST_API_KEY}` },
      payload: { message: "hi" },
    });
    expect(response.statusCode).toBe(200);
  });

  it("leaves /health open", async () => {
    const response = await app.inject({ method: "GET", url: "/health" });
    expect(response.statusCode).toBe(200);
  });

  it("guards unknown paths too, and answers 404 once authenticated", async () => {
    const anonymous = await app.inject({ method: "GET", url: "/v1/nope" });
    expect(anonymous.statusCode).toBe(401);

    const authenticated = await app.inject({
      method: "GET",
      url: "/v1/nope",
      headers: { authorization: `Bearer ${TEST_API_KEY}` },
    });
    expect(authenticated.statusCode).toBe(404);
    expect(authenticated.json().error.code).toBe("not_found");
  });
});
