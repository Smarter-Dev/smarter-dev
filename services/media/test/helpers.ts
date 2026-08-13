/** Shared test scaffolding: fixture loading and a server wired to stub renderers. */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { FastifyInstance } from "fastify";

import { defaultAssetsDir } from "../src/cards/assets.js";
import { loadTextMetrics } from "../src/cards/metrics.js";
import type { CardLayout, TextMetrics } from "../src/cards/types.js";
import type { MediaConfig } from "../src/config.js";
import type { PaintedCard, ServerDependencies } from "../src/dependencies.js";
import type { RenderedImage } from "../src/latex/rasterize.js";
import { buildServer } from "../src/server.js";

export const FIXTURES_DIR = fileURLToPath(new URL("./fixtures/", import.meta.url));

export const TEST_API_KEY = "media-test-key-0123456789";

/** Matches the `FIXED_NOW` the Pillow exporter froze the clock to. */
export const FIXED_NOW = new Date(2026, 7, 6, 12, 0, 0);

export const CARD_NAMES = [
  "simple",
  "error",
  "success",
  "info",
  "cooldown",
  "leaderboard",
  "history",
  "config",
  "squad-list",
  "squad-info",
  "squad-members",
  "squad-join-selector",
  "balance",
  "transfer-success",
] as const;

export type CardName = (typeof CARD_NAMES)[number];

export function readFixture<T>(...segments: string[]): T {
  return JSON.parse(readFileSync(join(FIXTURES_DIR, ...segments), "utf8")) as T;
}

export function readGolden(card: CardName): Buffer {
  return readFileSync(join(FIXTURES_DIR, "goldens", `${card}.png`));
}

export function cardRequest(card: CardName): Record<string, unknown> {
  return readFixture("cards", `${card}.json`);
}

export function cardLayoutFixture(card: CardName): CardLayout {
  return readFixture("layouts", `${card}.json`);
}

let cachedMetrics: TextMetrics | null = null;

export function testMetrics(): TextMetrics {
  if (cachedMetrics === null) {
    cachedMetrics = loadTextMetrics(defaultAssetsDir());
  }
  return cachedMetrics;
}

export function testConfig(overrides: Partial<MediaConfig> = {}): MediaConfig {
  return {
    apiKey: TEST_API_KEY,
    port: 0,
    logLevel: "silent",
    latexTimeoutMs: 5000,
    audioTimeoutMs: 30000,
    cardTimeoutMs: 5000,
    imageTag: "test",
    ...overrides,
  };
}

const TRANSPARENT_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

export function stubDependencies(
  overrides: Partial<ServerDependencies> = {},
): ServerDependencies {
  return {
    metrics: testMetrics(),
    latexReady: () => true,
    ffmpegVersion: "7.1.1",
    now: () => FIXED_NOW,
    renderLatex: async (): Promise<RenderedImage> => ({
      png: TRANSPARENT_PNG,
      width: 512,
      height: 96,
    }),
    transcodeWavToOpusOgg: async (): Promise<Buffer> => Buffer.from("OggS-stub"),
    paintCard: (layout: CardLayout): PaintedCard => ({
      png: TRANSPARENT_PNG,
      width: layout.canvas.width,
      height: layout.canvas.height,
    }),
    ...overrides,
  };
}

export function buildTestServer(
  depsOverrides: Partial<ServerDependencies> = {},
  configOverrides: Partial<MediaConfig> = {},
): FastifyInstance {
  return buildServer(testConfig(configOverrides), stubDependencies(depsOverrides));
}

export function authHeaders(): Record<string, string> {
  return { authorization: `Bearer ${TEST_API_KEY}` };
}
