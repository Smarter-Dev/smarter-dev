/** Rasterises a `CardLayout`. The only impure part of the card pipeline.
 *
 * Glyphs are placed one at a time using the Pillow advance table rather than
 * letting Skia lay out the run, so a string lands on exactly the pixels Pillow
 * put it on. Pillow's `ellipse` fills a box that is `2r + 1` pixels across, so
 * the equivalent arc radius is `r + 0.5`.
 */

import { createCanvas } from "@napi-rs/canvas";

import type { CardAssets } from "./assets.js";
import { FONT_SPECS } from "./theme.js";
import type { CardLayout, TextMetrics } from "./types.js";

export interface PaintedCard {
  png: Buffer;
  width: number;
  height: number;
}

export function paintCard(
  layout: CardLayout,
  assets: CardAssets,
  metrics: TextMetrics,
): PaintedCard {
  const background = assets.backgrounds.get(layout.background);
  if (background === undefined) {
    throw new Error(`Unknown background plate: ${layout.background}`);
  }

  const canvas = createCanvas(layout.canvas.width, layout.canvas.height);
  const context = canvas.getContext("2d");
  context.drawImage(background, 0, 0);
  context.textBaseline = "alphabetic";

  for (const op of layout.ops) {
    if (op.op === "ellipse") {
      context.fillStyle = op.fill;
      context.beginPath();
      context.arc(op.cx, op.cy, op.r + 0.5, 0, Math.PI * 2);
      context.fill();
      continue;
    }

    const spec = FONT_SPECS[op.font];
    context.font = `${spec.px}px "${spec.family}"`;
    context.fillStyle = op.fill;

    const baseline = op.y + metrics.ascent(op.font);
    const advances = metrics.advances(op.font, op.text);
    let pen = op.x;
    [...op.text].forEach((character, index) => {
      context.fillText(character, pen, baseline);
      pen += advances[index] ?? 0;
    });
  }

  return {
    png: canvas.toBuffer("image/png"),
    width: layout.canvas.width,
    height: layout.canvas.height,
  };
}
