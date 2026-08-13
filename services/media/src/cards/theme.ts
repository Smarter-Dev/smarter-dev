/** Colours, padding and font specs, copied verbatim from `EmbedImageGenerator`. */

import type { FontKey } from "./types.js";

export const COLORS: Record<string, string> = {
  default: "#00E1FF",
  error: "#FF0004",
  success: "#11FF00",
  warning: "#f59e0b",
  info: "#3b82f6",
};

export const TEXT_COLOR = "#FFFFFF";
export const SHADOW_COLOR = "#000000";

export const PADDING_TOP = 64;
export const PADDING_HORIZONTAL = 64;
/** Defined in the Python source and never used to crop; kept so the port is complete. */
export const PADDING_BOTTOM = 32;

export const CANVAS_WIDTH = 960;
export const CANVAS_HEIGHT = 540;
export const CONTENT_WIDTH = CANVAS_WIDTH - PADDING_HORIZONTAL * 2;

export const BACKGROUND_FILES: Record<string, string> = {
  error: "error-background.png",
  success: "success-background.png",
  default: "background.png",
  warning: "background.png",
  info: "background.png",
};

export const DEFAULT_BACKGROUND = "background.png";

export function backgroundFor(embedType: string): string {
  return BACKGROUND_FILES[embedType] ?? DEFAULT_BACKGROUND;
}

export interface FontSpec {
  family: string;
  px: number;
  file: string;
}

export const FONT_SPECS: Record<FontKey, FontSpec> = {
  title_large: { family: "Bruno Ace SC", px: 60, file: "BrunoAceSC-Regular.ttf" },
  title_medium: { family: "Bruno Ace SC", px: 48, file: "BrunoAceSC-Regular.ttf" },
  title_small: { family: "Bruno Ace SC", px: 36, file: "BrunoAceSC-Regular.ttf" },
  text_large: { family: "Anta", px: 32, file: "Anta-Regular.ttf" },
  text_medium: { family: "Anta", px: 28, file: "Anta-Regular.ttf" },
  text_small: { family: "Anta", px: 24, file: "Anta-Regular.ttf" },
  text_tiny: { family: "Anta", px: 20, file: "Anta-Regular.ttf" },
};
