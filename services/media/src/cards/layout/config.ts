/** Port of `create_config_embed`. */

import { thousands } from "../format.js";
import { shadowedText } from "../text.js";
import { COLORS, CONTENT_WIDTH, PADDING_HORIZONTAL, PADDING_TOP, TEXT_COLOR } from "../theme.js";
import type { CardLayout, DrawOp, TextMetrics } from "../types.js";
import type { ConfigCardRequest } from "../requests.js";

const TITLE = "Bytes Info";
const ROW_SPACING = 28;
const VALUE_RIGHT_MARGIN = 20;

export function layoutConfigCard(
  request: ConfigCardRequest,
  metrics: TextMetrics,
): CardLayout {
  const config = request.config;
  const ops: DrawOp[] = [];
  let currentY = PADDING_TOP;

  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, TITLE, "title_medium", COLORS.info!));
  currentY += metrics.measure("title_medium", TITLE).lineHeight + 32;

  const subtitle = `Settings for ${request.guild_name}`;
  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, subtitle, "text_medium", TEXT_COLOR));
  currentY += metrics.measure("text_medium", subtitle).lineHeight + 32;

  const cooldownText =
    config.transfer_cooldown_hours === 0
      ? "No cooldown"
      : `${config.transfer_cooldown_hours} hours`;
  const items: [string, string][] = [
    ["Daily Activity Reward:", `${thousands(config.daily_amount)} bytes`],
    ["New Member Balance:", `${thousands(config.starting_balance)} bytes`],
    ["Max Transfer:", `${thousands(config.max_transfer)} bytes`],
    ["Transfer Cooldown:", cooldownText],
  ];

  const columnWidth = Math.floor(CONTENT_WIDTH / 2);
  const secondColumnStart = PADDING_HORIZONTAL + columnWidth;

  items.forEach(([label, value], index) => {
    const rowY = currentY + index * ROW_SPACING;
    ops.push(...shadowedText(PADDING_HORIZONTAL, rowY, label, "text_small", TEXT_COLOR));
    const valueWidth = metrics.measure("text_small", value).width;
    const valueX = secondColumnStart + columnWidth - valueWidth - VALUE_RIGHT_MARGIN;
    ops.push(...shadowedText(valueX, rowY, value, "text_small", TEXT_COLOR));
  });

  currentY += items.length * ROW_SPACING + 20;

  const bonuses = request.config.streak_bonuses;
  if (bonuses !== undefined && bonuses !== null && Object.keys(bonuses).length > 0) {
    const header = "Streak Bonuses:";
    ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, header, "text_small", TEXT_COLOR));
    currentY += metrics.measure("text_small", header).lineHeight + 8;

    const bonusLine = `  ${Object.entries(bonuses)
      .sort(([left], [right]) => Number(left) - Number(right))
      .map(([days, multiplier]) => `${days} days: ${multiplier}x`)
      .join(" • ")}`;
    ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, bonusLine, "text_small", TEXT_COLOR));
  }

  return {
    card: "config",
    background: "background.png",
    canvas: { width: 960, height: 540 },
    ops,
  };
}
