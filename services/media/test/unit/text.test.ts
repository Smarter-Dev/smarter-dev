import { describe, expect, it } from "vitest";

import { pixel, shadowedText, wrapTextWithSpacing } from "../../src/cards/text.js";
import { testMetrics } from "../helpers.js";

const metrics = testMetrics();

function wrap(text: string, maxWidth: number) {
  return wrapTextWithSpacing(metrics, text, "text_large", maxWidth);
}

describe("wrapTextWithSpacing", () => {
  it("keeps a short line on one line and never flags the last paragraph", () => {
    expect(wrap("hello world", 832)).toEqual([{ text: "hello world", isParagraphBreak: false }]);
  });

  it("flags the last line of every paragraph except the last", () => {
    const lines = wrap("one\ntwo\nthree", 832);
    expect(lines).toEqual([
      { text: "one", isParagraphBreak: true },
      { text: "two", isParagraphBreak: true },
      { text: "three", isParagraphBreak: false },
    ]);
  });

  it("turns a blank paragraph into an empty flagged line", () => {
    const lines = wrap("one\n\ntwo", 832);
    expect(lines[1]).toEqual({ text: "", isParagraphBreak: true });
  });

  it("wraps on words, marking only the paragraph's last line", () => {
    const lines = wrap("aaaa bbbb cccc dddd eeee", 100);
    expect(lines.length).toBeGreaterThan(1);
    expect(lines.every((line) => line.isParagraphBreak === false)).toBe(true);
  });

  it("emits a single word wider than the limit rather than looping forever", () => {
    const lines = wrap("supercalifragilisticexpialidocious", 20);
    expect(lines).toEqual([
      { text: "supercalifragilisticexpialidocious", isParagraphBreak: false },
    ]);
  });

  it("collapses runs of whitespace the way Python's str.split does", () => {
    expect(wrap("a    b", 832)).toEqual([{ text: "a b", isParagraphBreak: false }]);
  });
});

describe("shadowedText", () => {
  it("emits the shadow first, offset by one pixel, then the fill", () => {
    expect(shadowedText(64, 64, "TITLE", "title_medium", "#00E1FF")).toEqual([
      { op: "text", x: 65, y: 65, text: "TITLE", font: "title_medium", fill: "#000000" },
      { op: "text", x: 64, y: 64, text: "TITLE", font: "title_medium", fill: "#00E1FF" },
    ]);
  });
});

describe("pixel", () => {
  it("truncates toward zero, as Pillow's draw coordinates do", () => {
    expect(pixel(499.9)).toBe(499);
    expect(pixel(-0.5)).toBe(-0);
  });
});
