/** Port of `create_squad_members_embed`.
 *
 * Bug-for-bug: with more than about 12 members the last rows and the truncation
 * note are drawn below y=540 and get clipped. */

import { formatShortDate, parseNaiveTimestamp } from "../format.js";
import { shadowedText, textOp } from "../text.js";
import { COLORS, CONTENT_WIDTH, PADDING_HORIZONTAL, PADDING_TOP, TEXT_COLOR } from "../theme.js";
import type { CardLayout, DrawOp, TextMetrics } from "../types.js";
import type { SquadMembersCardRequest } from "../requests.js";
import { layoutSimpleFallback } from "./simple.js";

const ROW_SPACING = 26;
const MAX_ROWS = 15;

export function layoutSquadMembersCard(
  request: SquadMembersCardRequest,
  metrics: TextMetrics,
): CardLayout {
  const title = request.squad.name.toUpperCase();

  if (request.members.length === 0) {
    return layoutSimpleFallback(
      "squad-members",
      title,
      "This squad has no members.",
      "info",
      metrics,
    );
  }

  const ops: DrawOp[] = [];
  let currentY = PADDING_TOP;

  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, title, "title_medium", COLORS.info!));
  currentY += metrics.measure("title_medium", title).lineHeight + 32;

  const memberCount = request.members.length;
  const subtitle = `${memberCount} member${memberCount !== 1 ? "s" : ""}`;
  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, subtitle, "text_medium", TEXT_COLOR));
  currentY += metrics.measure("text_medium", subtitle).lineHeight + 32;

  request.members.slice(0, MAX_ROWS).forEach((member, index) => {
    const username = member.username;
    let memberName =
      username === undefined || username === null || username === ""
        ? `User ${member.user_id.slice(0, 8)}`
        : username;
    if (memberName.length > 20) {
      memberName = `${memberName.slice(0, 17)}...`;
    }

    const rowY = currentY + index * ROW_SPACING;

    ops.push(textOp(PADDING_HORIZONTAL, rowY, `${index + 1}.`, "text_small", TEXT_COLOR));
    ops.push(textOp(PADDING_HORIZONTAL + 40, rowY, memberName, "text_small", TEXT_COLOR));

    const joinedAt = member.joined_at;
    if (joinedAt !== undefined && joinedAt !== null && joinedAt !== "") {
      const parsed = parseNaiveTimestamp(joinedAt);
      if (parsed !== null) {
        const joinText = formatShortDate(parsed);
        const joinWidth = metrics.measure("text_small", joinText).width;
        ops.push(
          textOp(
            PADDING_HORIZONTAL + CONTENT_WIDTH - joinWidth,
            rowY,
            joinText,
            "text_small",
            "#00E1FF",
          ),
        );
      }
    }
  });

  if (memberCount > MAX_ROWS) {
    const truncateY = currentY + MAX_ROWS * ROW_SPACING + 10;
    ops.push(
      textOp(
        PADDING_HORIZONTAL,
        truncateY,
        `... and ${memberCount - MAX_ROWS} more members`,
        "text_small",
        "#888888",
      ),
    );
  }

  return {
    card: "squad-members",
    background: "background.png",
    canvas: { width: 960, height: 540 },
    ops,
  };
}
