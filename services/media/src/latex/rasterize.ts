/** SVG -> PNG, a line-for-line port of `latex_renderer/worker.mjs`. */

import sharp from "sharp";

import { RenderFailedError } from "../errors.js";
import { texToSvg } from "./mathjax.js";

export const MAX_DIMENSION = 4096;
export const MAX_OUTPUT_BYTES = 8 * 1024 * 1024;

export interface RenderedImage {
  png: Buffer;
  width: number;
  height: number;
}

export async function svgToPng(svg: string): Promise<RenderedImage> {
  const image = sharp(Buffer.from(svg));
  const sourceHeight = (await image.metadata()).height || 16;
  const outputHeight = Math.min(Math.ceil(sourceHeight * 2), MAX_DIMENSION);

  const png = await image
    .resize({ height: outputHeight, withoutEnlargement: false })
    .png({ effort: 2 })
    .toBuffer();

  const rendered = await sharp(png).metadata();
  const width = rendered.width ?? 0;
  const height = rendered.height ?? 0;

  if (width > MAX_DIMENSION || height > MAX_DIMENSION) {
    throw new RenderFailedError(`Rendered image exceeds ${MAX_DIMENSION}px (${width}x${height})`);
  }
  if (png.byteLength > MAX_OUTPUT_BYTES) {
    throw new RenderFailedError(
      `Rendered image exceeds ${MAX_OUTPUT_BYTES} bytes (${png.byteLength})`,
    );
  }

  return { png, width, height };
}

export async function renderLatex(source: string): Promise<RenderedImage> {
  let svg: string;
  try {
    svg = await texToSvg(source);
  } catch (cause) {
    throw new RenderFailedError(cause instanceof Error ? cause.message : String(cause));
  }
  return svgToPng(svg);
}
