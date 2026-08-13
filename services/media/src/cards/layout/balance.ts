/** Port of `create_balance_embed`. */

import { thousands } from "../format.js";
import { shadowedText, textOp } from "../text.js";
import { COLORS, CONTENT_WIDTH, PADDING_HORIZONTAL, PADDING_TOP, TEXT_COLOR } from "../theme.js";
import type { CardLayout, DrawOp, TextMetrics } from "../types.js";
import type { BalanceCardRequest } from "../requests.js";

const TITLE = "BYTES BALANCE";

export function layoutBalanceCard(
  request: BalanceCardRequest,
  metrics: TextMetrics,
): CardLayout {
  const ops: DrawOp[] = [];
  let currentY = PADDING_TOP;

  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, TITLE, "title_medium", COLORS.info!));
  currentY += metrics.measure("title_medium", TITLE).lineHeight + 32;

  const subtitle = `Account overview for ${request.username}`;
  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, subtitle, "text_medium", TEXT_COLOR));
  currentY += metrics.measure("text_medium", subtitle).lineHeight + 32;

  const streakCount = request.streak_count ?? 0;
  const totalReceived = request.total_received ?? 0;
  const totalSent = request.total_sent ?? 0;
  const lastDaily = request.last_daily;

  const rows: [string, string, string][] = [
    ["Balance:", `${thousands(request.balance)} bytes`, "#00E1FF"],
  ];
  if (streakCount > 0) {
    rows.push(["Streak:", `${streakCount} days`, "#FF6B35"]);
  }
  if (lastDaily !== undefined && lastDaily !== null && lastDaily !== "") {
    rows.push(["Last Daily:", lastDaily, "#B0B0B0"]);
  }
  if (totalReceived > 0) {
    rows.push(["Total Received:", thousands(totalReceived), "#11FF00"]);
  }
  if (totalSent > 0) {
    rows.push(["Total Sent:", thousands(totalSent), "#FF9999"]);
  }
  if (totalReceived > 0 || totalSent > 0) {
    const netChange = totalReceived - totalSent;
    rows.push([
      "Net Change:",
      `${netChange >= 0 ? "+" : ""}${thousands(netChange)}`,
      netChange >= 0 ? "#11FF00" : "#FF6B6B",
    ]);
  }

  for (const [label, value, color] of rows) {
    ops.push(textOp(PADDING_HORIZONTAL, currentY, label, "text_small", TEXT_COLOR));
    const valueWidth = metrics.measure("text_small", value).width;
    ops.push(
      textOp(PADDING_HORIZONTAL + CONTENT_WIDTH - valueWidth, currentY, value, "text_small", color),
    );
    currentY += metrics.measure("text_small", label).lineHeight + 12;
  }

  return {
    card: "balance",
    background: "background.png",
    canvas: { width: 960, height: 540 },
    ops,
  };
}
