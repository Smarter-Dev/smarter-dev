/** Everything `buildServer` needs from the outside world, so tests can stub it. */

import type { CardLayout, TextMetrics } from "./cards/types.js";
import type { RenderedImage } from "./latex/rasterize.js";

export interface PaintedCard {
  png: Buffer;
  width: number;
  height: number;
}

export interface ServerDependencies {
  metrics: TextMetrics;
  latexReady(): boolean;
  ffmpegVersion: string;
  now(): Date;
  renderLatex(source: string, signal: AbortSignal): Promise<RenderedImage>;
  transcodeWavToOpusOgg(wav: Buffer, bitrate: string, signal: AbortSignal): Promise<Buffer>;
  paintCard(layout: CardLayout): PaintedCard;
}
