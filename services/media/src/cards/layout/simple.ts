/** Port of `create_simple_embed`, plus the three wrappers that delegate to it. */

import { shadowedText } from "../text.js";
import { wrapTextWithSpacing } from "../text.js";
import { COLORS, CONTENT_WIDTH, PADDING_HORIZONTAL, PADDING_TOP, TEXT_COLOR, backgroundFor } from "../theme.js";
import type { CardLayout, DrawOp, FontKey, TextMetrics } from "../types.js";
import type {
  ErrorCardRequest,
  InfoCardRequest,
  SimpleCardRequest,
  SuccessCardRequest,
} from "../requests.js";

interface Block {
  text: string;
  font: FontKey;
  fill: string;
  paragraphSpacing: number;
  wrappedSpacing: number;
}

function drawBlocks(metrics: TextMetrics, blocks: Block[], gapBetweenBlocks: number): DrawOp[] {
  const ops: DrawOp[] = [];
  let currentY = PADDING_TOP;

  blocks.forEach((block, index) => {
    if (index > 0) {
      currentY += gapBetweenBlocks;
    }
    const lines = wrapTextWithSpacing(metrics, block.text, block.font, CONTENT_WIDTH);
    for (const line of lines) {
      if (line.text !== "") {
        ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, line.text, block.font, block.fill));
      }
      const lineHeight =
        line.text !== ""
          ? metrics.measure(block.font, line.text).lineHeight
          : metrics.measure(block.font, "A").lineHeight;
      currentY += lineHeight + (line.isParagraphBreak ? block.paragraphSpacing : block.wrappedSpacing);
    }
  });

  return ops;
}

export function layoutSimpleCard(
  request: SimpleCardRequest,
  metrics: TextMetrics,
  card = "simple",
): CardLayout {
  const embedType = request.embed_type ?? "default";
  const titleColor = COLORS[embedType] ?? COLORS.default!;

  const ops = drawBlocks(
    metrics,
    [
      {
        text: request.title,
        font: "title_medium",
        fill: titleColor,
        paragraphSpacing: 16,
        wrappedSpacing: 4,
      },
      {
        text: request.description,
        font: "text_large",
        fill: TEXT_COLOR,
        paragraphSpacing: 12,
        wrappedSpacing: 2,
      },
    ],
    32,
  );

  return {
    card,
    background: backgroundFor(embedType),
    canvas: { width: 960, height: 540 },
    ops,
  };
}

export function layoutErrorCard(request: ErrorCardRequest, metrics: TextMetrics): CardLayout {
  return layoutSimpleCard(
    { title: "ERROR", description: request.message, embed_type: "error" },
    metrics,
    "error",
  );
}

export function layoutSuccessCard(request: SuccessCardRequest, metrics: TextMetrics): CardLayout {
  return layoutSimpleCard(
    { title: request.title, description: request.description, embed_type: "success" },
    metrics,
    "success",
  );
}

export function layoutInfoCard(request: InfoCardRequest, metrics: TextMetrics): CardLayout {
  return layoutSimpleCard(
    { title: request.title, description: request.description, embed_type: "info" },
    metrics,
    "info",
  );
}

/** Several cards fall back to a simple embed when their collection is empty. */
export function layoutSimpleFallback(
  card: string,
  title: string,
  description: string,
  embedType: string,
  metrics: TextMetrics,
): CardLayout {
  return layoutSimpleCard({ title, description, embed_type: embedType }, metrics, card);
}
