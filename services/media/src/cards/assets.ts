/** Fonts and background plates, loaded once at boot. A missing asset stops the process. */

import { existsSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { GlobalFonts, loadImage } from "@napi-rs/canvas";
import type { Image } from "@napi-rs/canvas";

import { FONT_SPECS } from "./theme.js";

export const BACKGROUND_NAMES = [
  "background.png",
  "error-background.png",
  "success-background.png",
] as const;

export interface CardAssets {
  backgrounds: Map<string, Image>;
}

/** `services/media/assets`. Both `src/cards/` and `dist/cards/` sit two levels
 * below the service root, so one relative path serves tsx and the built image. */
export function defaultAssetsDir(): string {
  return fileURLToPath(new URL("../../assets/", import.meta.url));
}

export function registerFonts(assetsDir: string): void {
  const registered = new Set<string>();
  for (const spec of Object.values(FONT_SPECS)) {
    if (registered.has(spec.file)) {
      continue;
    }
    const path = join(assetsDir, "fonts", spec.file);
    if (!existsSync(path)) {
      throw new Error(`Missing font asset: ${path}`);
    }
    if (!GlobalFonts.registerFromPath(path, spec.family)) {
      throw new Error(`Could not register font ${spec.family} from ${path}`);
    }
    registered.add(spec.file);
  }
}

export async function loadCardAssets(assetsDir: string): Promise<CardAssets> {
  registerFonts(assetsDir);

  const backgrounds = new Map<string, Image>();
  for (const name of BACKGROUND_NAMES) {
    const path = join(assetsDir, "backgrounds", name);
    if (!existsSync(path)) {
      throw new Error(`Missing background asset: ${path}`);
    }
    backgrounds.set(name, await loadImage(path));
  }
  return { backgrounds };
}
