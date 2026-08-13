/** Port of `create_leaderboard_embed`. */

import { thousands } from "../format.js";
import { shadowedText, textOp } from "../text.js";
import { COLORS, CONTENT_WIDTH, PADDING_HORIZONTAL, PADDING_TOP, TEXT_COLOR } from "../theme.js";
import type { CardLayout, DrawOp, TextMetrics } from "../types.js";
import type { LeaderboardCardRequest } from "../requests.js";
import { layoutSimpleFallback } from "./simple.js";

const TITLE = "BYTES LEADERBOARD";
const ROW_SPACING = 26;
const RANK_COLUMN_WIDTH = 60;
const STREAK_COLUMN_RIGHT = 400;

const RANK_COLORS: Record<number, string> = {
  1: "#FFD700",
  2: "#C0C0C0",
  3: "#CD7F32",
};

export function layoutLeaderboardCard(
  request: LeaderboardCardRequest,
  metrics: TextMetrics,
): CardLayout {
  if (request.entries.length === 0) {
    return layoutSimpleFallback(
      "leaderboard",
      TITLE,
      "No leaderboard data available yet!",
      "info",
      metrics,
    );
  }

  const ops: DrawOp[] = [];
  let currentY = PADDING_TOP;

  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, TITLE, "title_medium", COLORS.info!));
  currentY += metrics.measure("title_medium", TITLE).lineHeight + 64;

  request.entries.forEach((entry, index) => {
    const fallbackName = `User ${entry.user_id.slice(0, 8)}`;
    let displayName = request.user_display_names[entry.user_id] ?? fallbackName;
    if (displayName.length > 18) {
      displayName = `${displayName.slice(0, 15)}...`;
    }

    const rowY = currentY + index * ROW_SPACING;

    const rankText = String(entry.rank);
    const rankColor = RANK_COLORS[entry.rank] ?? TEXT_COLOR;
    const rankWidth = metrics.measure("text_small", rankText).width;
    const rankX = PADDING_HORIZONTAL + Math.floor((RANK_COLUMN_WIDTH - rankWidth) / 2);
    ops.push(textOp(rankX, rowY, rankText, "text_small", rankColor));

    ops.push(
      textOp(PADDING_HORIZONTAL + RANK_COLUMN_WIDTH, rowY, displayName, "text_small", TEXT_COLOR),
    );

    if (entry.streak_count > 0) {
      const streakText = `${entry.streak_count} days`;
      const streakWidth = metrics.measure("text_small", streakText).width;
      const streakX = PADDING_HORIZONTAL + STREAK_COLUMN_RIGHT - streakWidth;
      ops.push(textOp(streakX, rowY, streakText, "text_small", "#11FF00"));
    }

    const balanceText = thousands(entry.balance);
    const balanceWidth = metrics.measure("text_small", balanceText).width;
    const balanceX = PADDING_HORIZONTAL + CONTENT_WIDTH - balanceWidth;
    ops.push(textOp(balanceX, rowY, balanceText, "text_small", "#00E1FF"));
  });

  return {
    card: "leaderboard",
    background: "background.png",
    canvas: { width: 960, height: 540 },
    ops,
  };
}
