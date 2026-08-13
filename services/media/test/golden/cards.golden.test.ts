/** Three-tier pixel comparison against the Pillow goldens.
 *
 * 1. Dimensions — exactly 960x540 RGBA. Hard fail.
 * 2. Structural — both images box-downsampled to 120x68, zero differing cells.
 *    Washes out glyph antialiasing and catches any displacement, wrong colour,
 *    missing element or wrong background plate.
 * 3. Full resolution — under 3% differing pixels. FreeType and Skia never agree
 *    on glyph edge pixels; the port currently sits under 0.25%.
 */

import { beforeAll, describe, expect, it } from "vitest";

import pixelmatch from "pixelmatch";
import { PNG } from "pngjs";

import { defaultAssetsDir, loadCardAssets } from "../../src/cards/assets.js";
import type { CardAssets } from "../../src/cards/assets.js";
import { paintCard } from "../../src/cards/paint.js";
import { writeDiff } from "../../tools/compare-goldens.js";
import { CARD_NAMES, cardLayoutFixture, readGolden, testMetrics } from "../helpers.js";
import type { CardName } from "../helpers.js";

const WIDTH = 960;
const HEIGHT = 540;
const STRUCTURAL_WIDTH = 120;
const STRUCTURAL_HEIGHT = 68;
const STRUCTURAL_BLOCK = 8;
const FULL_RESOLUTION_BUDGET = 0.03;

const metrics = testMetrics();
let assets: CardAssets;

beforeAll(async () => {
  assets = await loadCardAssets(defaultAssetsDir());
});

function boxDownsample(source: PNG): PNG {
  const out = new PNG({ width: STRUCTURAL_WIDTH, height: STRUCTURAL_HEIGHT });
  for (let y = 0; y < STRUCTURAL_HEIGHT; y += 1) {
    for (let x = 0; x < STRUCTURAL_WIDTH; x += 1) {
      const totals = [0, 0, 0, 0];
      let samples = 0;
      for (let sy = y * STRUCTURAL_BLOCK; sy < Math.min((y + 1) * STRUCTURAL_BLOCK, HEIGHT); sy += 1) {
        for (let sx = x * STRUCTURAL_BLOCK; sx < Math.min((x + 1) * STRUCTURAL_BLOCK, WIDTH); sx += 1) {
          const offset = (sy * WIDTH + sx) * 4;
          for (let channel = 0; channel < 4; channel += 1) {
            totals[channel]! += source.data[offset + channel]!;
          }
          samples += 1;
        }
      }
      const offset = (y * STRUCTURAL_WIDTH + x) * 4;
      for (let channel = 0; channel < 4; channel += 1) {
        out.data[offset + channel] = Math.round(totals[channel]! / samples);
      }
    }
  }
  return out;
}

function paint(card: CardName): PNG {
  const painted = paintCard(cardLayoutFixture(card), assets, metrics);
  return PNG.sync.read(painted.png);
}

describe("card rasterisation against the goldens", () => {
  it.each(CARD_NAMES)("%s", (card) => {
    const rendered = paint(card);
    const golden = PNG.sync.read(readGolden(card));

    expect([rendered.width, rendered.height]).toEqual([WIDTH, HEIGHT]);
    expect([golden.width, golden.height]).toEqual([WIDTH, HEIGHT]);
    expect(rendered.data.length).toBe(WIDTH * HEIGHT * 4);

    const structural = pixelmatch(
      boxDownsample(rendered).data,
      boxDownsample(golden).data,
      null,
      STRUCTURAL_WIDTH,
      STRUCTURAL_HEIGHT,
      { threshold: 0.15 },
    );

    const differing = pixelmatch(rendered.data, golden.data, null, WIDTH, HEIGHT, {
      threshold: 0.15,
    });
    const ratio = differing / (WIDTH * HEIGHT);

    if (structural > 0 || ratio > FULL_RESOLUTION_BUDGET) {
      writeDiff(card, rendered, golden);
    }

    expect(structural, `${card}: structural cells differ`).toBe(0);
    expect(ratio, `${card}: ${(ratio * 100).toFixed(3)}% of pixels differ`).toBeLessThanOrEqual(
      FULL_RESOLUTION_BUDGET,
    );
  });
});
