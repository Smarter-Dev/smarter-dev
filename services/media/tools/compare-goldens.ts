/** Writes a diff PNG when the golden comparison fails, for eyeballing. */

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import pixelmatch from "pixelmatch";
import { PNG } from "pngjs";

export const OUTPUT_DIR = fileURLToPath(new URL("../test/output/", import.meta.url));

export function writeDiff(card: string, rendered: PNG, golden: PNG): string {
  mkdirSync(OUTPUT_DIR, { recursive: true });

  const diff = new PNG({ width: rendered.width, height: rendered.height });
  pixelmatch(rendered.data, golden.data, diff.data, rendered.width, rendered.height, {
    threshold: 0.15,
  });

  const path = join(OUTPUT_DIR, `${card}.diff.png`);
  writeFileSync(path, PNG.sync.write(diff));
  writeFileSync(join(OUTPUT_DIR, `${card}.rendered.png`), PNG.sync.write(rendered));
  return path;
}
