import { describe, expect, it } from "vitest";

import { FONT_KEYS } from "../../src/cards/types.js";
import type { FontKey } from "../../src/cards/types.js";
import { readFixture, testMetrics } from "../helpers.js";

interface PillowTextMetrics {
  fonts: Record<string, { file: string; px: number; ascent: number; descent: number }>;
  measurements: { font: FontKey; text: string; bbox: number[]; advance: number }[];
}

const groundTruth = readFixture<PillowTextMetrics>("pillow-text-metrics.json");
const metrics = testMetrics();

describe("measureText against Pillow", () => {
  it("knows every font key the layouts reference", () => {
    for (const key of FONT_KEYS) {
      expect(groundTruth.fonts[key], `missing ground truth for ${key}`).toBeDefined();
      expect(metrics.ascent(key)).toBe(groundTruth.fonts[key]!.ascent);
    }
  });

  it("reproduces every measured string exactly", () => {
    const mismatches: string[] = [];
    for (const measurement of groundTruth.measurements) {
      const measured = metrics.measure(measurement.font, measurement.text);
      if (JSON.stringify(measured.bbox) !== JSON.stringify(measurement.bbox)) {
        mismatches.push(
          `${measurement.font} ${JSON.stringify(measurement.text)}: ` +
            `${JSON.stringify(measured.bbox)} != ${JSON.stringify(measurement.bbox)}`,
        );
      }
      expect(Math.abs(measured.advance - measurement.advance)).toBeLessThanOrEqual(1);
    }
    expect(mismatches).toEqual([]);
    expect(groundTruth.measurements.length).toBeGreaterThan(100);
  });

  it("treats the empty string the way Pillow does", () => {
    const measured = metrics.measure("text_small", "");
    expect(measured.bbox).toEqual([0, 0, 0, 0]);
    expect(measured.width).toBe(0);
    expect(measured.lineHeight).toBe(0);
  });

  it("falls back to .notdef for codepoints the font does not cover", () => {
    const cjk = metrics.measure("text_small", "日");
    const privateUse = metrics.measure("text_small", "");
    expect(cjk.bbox).toEqual(privateUse.bbox);
    expect(cjk.width).toBeGreaterThan(0);
  });

  it("returns one advance per code point", () => {
    expect(metrics.advances("text_small", "abc")).toHaveLength(3);
    expect(metrics.advances("text_small", "\u{1f600}")).toHaveLength(1);
  });
});
