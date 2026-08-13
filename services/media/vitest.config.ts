import { defineConfig } from "vitest/config";

// The card fixtures were exported with the clock pinned to UTC, and `cooldown`
// compares a real unix timestamp, so the suite has to read the same zone.
process.env.TZ = "UTC";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    environment: "node",
    env: { TZ: "UTC" },
    testTimeout: 30_000,
    hookTimeout: 60_000,
  },
});
