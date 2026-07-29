import { createInterface } from "node:readline";

import mathjax from "mathjax";
import sharp from "sharp";

const MAX_SOURCE_CHARS = 1_800;
const MAX_DIMENSION = 4_096;
const MAX_OUTPUT_BYTES = 8 * 1024 * 1024;

const jax = await mathjax.init({
  loader: {
    load: ["input/tex", "output/svg", "ui/safe"],
  },
});

process.stdout.write(`${JSON.stringify({ ready: true })}\n`);

async function render(source) {
  if (typeof source !== "string" || source.length === 0) {
    throw new Error("LaTeX source must not be empty");
  }
  if (source.length > MAX_SOURCE_CHARS) {
    throw new Error(`LaTeX source exceeds ${MAX_SOURCE_CHARS} characters`);
  }

  const svg = jax.startup.adaptor.innerHTML(
    await jax.tex2svgPromise(
      String.raw`\color{white} \begin{aligned} ${source} \end{aligned}`,
      { display: true },
    ),
  );

  const image = sharp(Buffer.from(svg));
  const metadata = await image.metadata();
  const sourceHeight = metadata.height || 16;
  const outputHeight = Math.min(Math.ceil(sourceHeight * 2), MAX_DIMENSION);
  const png = await image
    .resize({ height: outputHeight, withoutEnlargement: false })
    .png({ effort: 2 })
    .toBuffer();
  const outputMetadata = await sharp(png).metadata();

  if (
    (outputMetadata.width || 0) > MAX_DIMENSION ||
    (outputMetadata.height || 0) > MAX_DIMENSION
  ) {
    throw new Error(`Rendered image exceeds ${MAX_DIMENSION}px`);
  }
  if (png.length > MAX_OUTPUT_BYTES) {
    throw new Error(`Rendered image exceeds ${MAX_OUTPUT_BYTES} bytes`);
  }
  return png;
}

const lines = createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

for await (const line of lines) {
  let id = null;
  try {
    const request = JSON.parse(line);
    id = request.id;
    const png = await render(request.source);
    process.stdout.write(
      `${JSON.stringify({ id, png: png.toString("base64") })}\n`,
    );
  } catch (error) {
    process.stdout.write(
      `${JSON.stringify({
        id,
        error: error instanceof Error ? error.message : String(error),
      })}\n`,
    );
  }
}
