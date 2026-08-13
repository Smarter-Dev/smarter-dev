import { describe, expect, it } from "vitest";

import { RenderTimeoutError } from "../../src/errors.js";
import { withTimeout } from "../../src/withTimeout.js";

describe("withTimeout", () => {
  it("returns the value when the work finishes in time", async () => {
    await expect(withTimeout(1000, "work", async () => "done")).resolves.toBe("done");
  });

  it("aborts the signal and reports render_timeout", async () => {
    const error = await withTimeout(
      10,
      "slow work",
      (signal) =>
        new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
        }),
    ).catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(RenderTimeoutError);
    expect((error as RenderTimeoutError).message).toBe("slow work exceeded 10ms");
  });

  it("reports render_timeout for uninterruptible work that overran", async () => {
    const error = await withTimeout(1, "blocking work", async () => {
      const deadline = Date.now() + 30;
      while (Date.now() < deadline) {
        // Synchronous native work cannot honour the signal.
      }
      return "late";
    }).catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(RenderTimeoutError);
  });

  it("passes through a failure that is not a timeout", async () => {
    await expect(
      withTimeout(1000, "work", async () => {
        throw new Error("boom");
      }),
    ).rejects.toThrow("boom");
  });
});
