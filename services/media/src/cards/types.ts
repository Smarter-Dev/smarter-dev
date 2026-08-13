/** The layout intermediate representation shared by every card. */

export const FONT_KEYS = [
  "title_large",
  "title_medium",
  "title_small",
  "text_large",
  "text_medium",
  "text_small",
  "text_tiny",
] as const;

export type FontKey = (typeof FONT_KEYS)[number];

export interface TextOp {
  op: "text";
  x: number;
  y: number;
  text: string;
  font: FontKey;
  fill: string;
}

export interface EllipseOp {
  op: "ellipse";
  cx: number;
  cy: number;
  r: number;
  fill: string;
}

export type DrawOp = TextOp | EllipseOp;

export interface CardLayout {
  card: string;
  background: string;
  canvas: { width: number; height: number };
  ops: DrawOp[];
}

export interface TextMeasurement {
  /** Pillow's `font.getbbox(text)`: [left, top, right, bottom] from the ascender line. */
  bbox: [number, number, number, number];
  /** `bbox[2] - bbox[0]` — what the Python layout code calls "width". */
  width: number;
  /** `bbox[3]` — what the Python layout code calls "line height". */
  lineHeight: number;
  /** Sum of the glyph advances, ignoring ink overhang. */
  advance: number;
  /** Pillow's `font.getmetrics()[0]`; the distance from the draw origin to the baseline. */
  ascent: number;
}

export interface TextMetrics {
  measure(font: FontKey, text: string): TextMeasurement;
  ascent(font: FontKey): number;
  /** Sum of glyph advances, used to place glyphs one at a time when painting. */
  advances(font: FontKey, text: string): number[];
}
