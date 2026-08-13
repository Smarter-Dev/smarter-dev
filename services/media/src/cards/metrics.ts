/** Text measurement that reproduces Pillow exactly.
 *
 * Skia (the rasteriser behind `@napi-rs/canvas`) and FreeType (the rasteriser
 * behind Pillow) disagree about glyph advances by fractions of a pixel, and the
 * card layouts right-align, centre and wrap against those numbers. So the
 * service does not measure text at render time at all: it carries a table of
 * per-glyph metrics exported straight out of Pillow and composes string extents
 * from it.
 *
 * Each glyph entry is `[advance, inkLeft, inkRight, inkTop, inkBottom]`, all in
 * whole pixels, all relative to the draw origin Pillow calls the ascender line.
 * `inkLeft` is `min(0, bearingX)` and `inkRight` is `max(advance, bearingX +
 * bitmapWidth)` — precisely what `font.getbbox(character)` reports for a single
 * character. A blank glyph has `inkTop === inkBottom` and contributes no ink.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import type { FontKey, TextMeasurement, TextMetrics } from "./types.js";
import { FONT_KEYS } from "./types.js";

type GlyphEntry = [number, number, number, number, number];

interface FontTable {
  file: string;
  px: number;
  ascent: number;
  descent: number;
  notdef: GlyphEntry;
  glyphs: Record<string, GlyphEntry>;
}

interface GlyphMetricsFile {
  fonts: Record<string, FontTable>;
}

export const GLYPH_METRICS_FILE = "pillow-glyph-metrics.json";

export function loadTextMetrics(assetsDir: string): TextMetrics {
  const path = join(assetsDir, "fonts", GLYPH_METRICS_FILE);
  const parsed = JSON.parse(readFileSync(path, "utf8")) as GlyphMetricsFile;

  for (const key of FONT_KEYS) {
    if (parsed.fonts[key] === undefined) {
      throw new Error(`${GLYPH_METRICS_FILE} is missing font "${key}"`);
    }
  }

  return new GlyphTableMetrics(parsed.fonts as Record<FontKey, FontTable>);
}

class GlyphTableMetrics implements TextMetrics {
  private readonly fonts: Record<FontKey, FontTable>;

  constructor(fonts: Record<FontKey, FontTable>) {
    this.fonts = fonts;
  }

  ascent(font: FontKey): number {
    return this.fonts[font].ascent;
  }

  advances(font: FontKey, text: string): number[] {
    const table = this.fonts[font];
    return [...text].map((character) => this.glyph(table, character)[0]);
  }

  measure(font: FontKey, text: string): TextMeasurement {
    const table = this.fonts[font];
    if (text.length === 0) {
      return { bbox: [0, 0, 0, 0], width: 0, lineHeight: 0, advance: 0, ascent: table.ascent };
    }

    let pen = 0;
    let left = 0;
    let right = 0;
    let top: number | null = null;
    let bottom: number | null = null;

    for (const character of text) {
      const glyph = this.glyph(table, character);
      left = Math.min(left, pen + glyph[1]);
      right = Math.max(right, pen + glyph[2]);
      pen += glyph[0];
      if (glyph[3] !== glyph[4]) {
        top = top === null ? glyph[3] : Math.min(top, glyph[3]);
        bottom = bottom === null ? glyph[4] : Math.max(bottom, glyph[4]);
      }
    }

    if (top === null || bottom === null) {
      top = table.ascent;
      bottom = table.ascent;
    }

    const bbox: [number, number, number, number] = [left, top, Math.max(pen, right), bottom];
    return {
      bbox,
      width: bbox[2] - bbox[0],
      lineHeight: bbox[3],
      advance: pen,
      ascent: table.ascent,
    };
  }

  private glyph(table: FontTable, character: string): GlyphEntry {
    return table.glyphs[String(character.codePointAt(0))] ?? table.notdef;
  }
}
