/** Entrypoint: fail fast on anything missing, then listen. */

import { loadCardAssets, defaultAssetsDir } from "./cards/assets.js";
import { loadTextMetrics } from "./cards/metrics.js";
import { paintCard } from "./cards/paint.js";
import { loadConfig } from "./config.js";
import type { ServerDependencies } from "./dependencies.js";
import { probeFfmpegVersion, transcodeWavToOpusOgg } from "./audio/transcode.js";
import { initMathJax } from "./latex/mathjax.js";
import { renderLatex } from "./latex/rasterize.js";
import { buildServer } from "./server.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const assetsDir = defaultAssetsDir();

  // A media pod without ffmpeg or without its fonts is broken, not degraded.
  const ffmpegVersion = await probeFfmpegVersion();
  const metrics = loadTextMetrics(assetsDir);
  const assets = await loadCardAssets(assetsDir);

  let latexReady = false;
  const mathjaxReady = initMathJax().then(
    () => {
      latexReady = true;
    },
    (cause: unknown) => {
      // eslint-disable-next-line no-console
      console.error("MathJax failed to initialise", cause);
      process.exit(1);
    },
  );

  const deps: ServerDependencies = {
    metrics,
    latexReady: () => latexReady,
    ffmpegVersion,
    now: () => new Date(),
    renderLatex: (source) => renderLatex(source),
    transcodeWavToOpusOgg,
    paintCard: (layout) => paintCard(layout, assets, metrics),
  };

  const app = buildServer(config, deps);
  await app.listen({ port: config.port, host: "0.0.0.0" });
  void mathjaxReady;
}

main().catch((cause: unknown) => {
  // eslint-disable-next-line no-console
  console.error(cause);
  process.exit(1);
});
