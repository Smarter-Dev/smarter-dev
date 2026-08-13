/** MathJax initialisation and TeX -> SVG, ported from `latex_renderer/worker.mjs`.
 *
 * The loader config and the wrapper string are load-bearing: `ui/safe` is the
 * only thing between arbitrary user TeX and the renderer, and `\color{white}`
 * plus the `aligned` environment are what make the output match today's images.
 */

import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

interface MathJaxAdaptor {
  innerHTML(node: unknown): string;
}

interface MathJaxHandle {
  tex2svgPromise(source: string, options: { display: boolean }): Promise<unknown>;
  startup: { adaptor: MathJaxAdaptor };
}

let initialisation: Promise<MathJaxHandle> | null = null;

export function initMathJax(): Promise<MathJaxHandle> {
  if (initialisation === null) {
    const mathjax = require("mathjax") as {
      init(config: unknown): Promise<MathJaxHandle>;
    };
    initialisation = mathjax.init({
      loader: { load: ["input/tex", "output/svg", "ui/safe"] },
    });
  }
  return initialisation;
}

export async function texToSvg(source: string): Promise<string> {
  const jax = await initMathJax();
  const node = await jax.tex2svgPromise(
    String.raw`\color{white} \begin{aligned} ${source} \end{aligned}`,
    { display: true },
  );
  return jax.startup.adaptor.innerHTML(node);
}
