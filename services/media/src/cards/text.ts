/** Word wrapping and shadowed text, ported from `_wrap_text_with_spacing` and
 * `_draw_text_with_shadow`. */

import { SHADOW_COLOR } from "./theme.js";
import type { DrawOp, FontKey, TextMetrics, TextOp } from "./types.js";

export interface WrappedLine {
  text: string;
  isParagraphBreak: boolean;
}

export function wrapTextWithSpacing(
  metrics: TextMetrics,
  text: string,
  font: FontKey,
  maxWidth: number,
): WrappedLine[] {
  const paragraphs = text.split("\n");
  const lines: WrappedLine[] = [];

  paragraphs.forEach((paragraph, paragraphIndex) => {
    if (paragraph.trim() === "") {
      lines.push({ text: "", isParagraphBreak: true });
      return;
    }

    const words = paragraph.split(/\s+/).filter((word) => word !== "");
    const paragraphLines: string[] = [];
    let currentLine = "";

    for (const word of words) {
      const candidate = currentLine === "" ? word : `${currentLine} ${word}`;
      if (metrics.measure(font, candidate).width <= maxWidth) {
        currentLine = candidate;
      } else {
        if (currentLine !== "") {
          paragraphLines.push(currentLine);
        }
        currentLine = word;
      }
    }
    if (currentLine !== "") {
      paragraphLines.push(currentLine);
    }

    const isLastParagraph = paragraphIndex === paragraphs.length - 1;
    paragraphLines.forEach((line, lineIndex) => {
      const isLastLineOfParagraph = lineIndex === paragraphLines.length - 1;
      lines.push({ text: line, isParagraphBreak: isLastLineOfParagraph && !isLastParagraph });
    });
  });

  return lines;
}

/** Pillow truncates draw coordinates toward zero; the layouts must do the same. */
export function pixel(value: number): number {
  return Math.trunc(value);
}

export function textOp(
  x: number,
  y: number,
  text: string,
  font: FontKey,
  fill: string,
): TextOp {
  return { op: "text", x: pixel(x), y: pixel(y), text, font, fill };
}

/** Shadow first, then the fill — literally what `_draw_text_with_shadow` does. */
export function shadowedText(
  x: number,
  y: number,
  text: string,
  font: FontKey,
  fill: string,
): DrawOp[] {
  return [
    textOp(x + 1, y + 1, text, font, SHADOW_COLOR),
    textOp(x, y, text, font, fill),
  ];
}
