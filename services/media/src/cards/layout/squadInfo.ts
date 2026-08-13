/** Port of `create_squad_info_embed`. */

import { naiveEpochMs, nowNaiveEpochMs, thousands } from "../format.js";
import { shadowedText, textOp, wrapTextWithSpacing } from "../text.js";
import { COLORS, CONTENT_WIDTH, PADDING_HORIZONTAL, PADDING_TOP, TEXT_COLOR } from "../theme.js";
import type { CardLayout, DrawOp, TextMetrics } from "../types.js";
import type { Squad, SquadInfoCardRequest } from "../requests.js";

const ROW_SPACING = 28;
const VALUE_SPACING = 40;
const DAY_MS = 86_400_000;

function joinCostDisplay(squad: Squad): string {
  if (squad.is_default === true) {
    return "N/A (default)";
  }
  const cost =
    squad.current_join_cost === undefined || squad.current_join_cost === null
      ? squad.switch_cost
      : squad.current_join_cost;
  return squad.has_join_sale === true
    ? `${thousands(cost)} bytes (Sale)`
    : `${thousands(cost)} bytes`;
}

export function layoutSquadInfoCard(
  request: SquadInfoCardRequest,
  metrics: TextMetrics,
  now: Date,
): CardLayout {
  const squad = request.squad;
  const ops: DrawOp[] = [];
  let currentY = PADDING_TOP;

  const title = squad.name.toUpperCase();
  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, title, "title_medium", COLORS.info!));
  currentY += metrics.measure("title_medium", title).lineHeight + 32;

  const description = squad.description;
  if (description !== undefined && description !== null && description !== "") {
    const lines = wrapTextWithSpacing(metrics, description, "text_medium", CONTENT_WIDTH);
    for (const line of lines) {
      if (line.text !== "") {
        ops.push(
          ...shadowedText(PADDING_HORIZONTAL, currentY, line.text, "text_medium", TEXT_COLOR),
        );
      }
      const lineHeight =
        line.text !== ""
          ? metrics.measure("text_medium", line.text).lineHeight
          : metrics.measure("text_medium", "A").lineHeight;
      currentY += lineHeight + (line.isParagraphBreak ? 12 : 2);
    }
    currentY += 48;
  }

  const maxMembers = squad.max_members;
  const memberValue =
    maxMembers === undefined || maxMembers === null || maxMembers === 0
      ? `${request.members.length}`
      : `${request.members.length}/${maxMembers}`;

  const stats: [string, string][] = [
    ["Members", memberValue],
    ["Join Cost", joinCostDisplay(squad)],
    ["Status", squad.is_active === false ? "Inactive" : "Active"],
  ];

  const maxLabelWidth = stats.reduce(
    (widest, [label]) => Math.max(widest, metrics.measure("text_small", label).width),
    0,
  );
  const valueX = PADDING_HORIZONTAL + maxLabelWidth + VALUE_SPACING;

  stats.forEach(([label, value], index) => {
    const statsY = currentY + index * ROW_SPACING;
    ops.push(textOp(PADDING_HORIZONTAL, statsY, label, "text_small", TEXT_COLOR));
    ops.push(textOp(valueX, statsY, value, "text_small", "#00E1FF"));
  });

  currentY += stats.length * ROW_SPACING + 16;

  const memberSince = request.user_member_info?.member_since;
  if (memberSince !== undefined && memberSince !== null && memberSince !== "") {
    const memberSinceMs = naiveEpochMs(memberSince);
    if (memberSinceMs !== null) {
      const header = "Your Membership:";
      ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, header, "text_small", TEXT_COLOR));
      currentY += metrics.measure("text_small", header).lineHeight + 8;

      const days = Math.floor((nowNaiveEpochMs(now) - memberSinceMs) / DAY_MS);
      const memberInfo = `Member for ${days} day${days !== 1 ? "s" : ""}`;
      ops.push(textOp(PADDING_HORIZONTAL + 20, currentY, memberInfo, "text_small", "#11FF00"));
      currentY += metrics.measure("text_small", memberInfo).lineHeight + 16;
    }
  }

  return {
    card: "squad-info",
    background: "background.png",
    canvas: { width: 960, height: 540 },
    ops,
  };
}
