/** Port of `create_squad_list_embed`.
 *
 * Bug-for-bug: squads past index 10 are dropped with no truncation note, and
 * `guild_name` / `current_squad_id` are accepted and never drawn. */

import { thousands } from "../format.js";
import { shadowedText, textOp } from "../text.js";
import {
  CANVAS_HEIGHT,
  COLORS,
  CONTENT_WIDTH,
  PADDING_HORIZONTAL,
  PADDING_TOP,
  TEXT_COLOR,
} from "../theme.js";
import type { CardLayout, DrawOp, TextMetrics } from "../types.js";
import type { Squad, SquadListCardRequest } from "../requests.js";
import { layoutSimpleFallback } from "./simple.js";

const TITLE = "AVAILABLE SQUADS";
const ROW_SPACING = 28;
const MEMBERS_COLUMN_START = CONTENT_WIDTH * 0.6;
const MEMBERS_COLUMN_WIDTH = 100;
const MAX_ROWS = 10;
const CIRCLE_RADIUS = 6;

export function joinCost(squad: Squad): { text: string; color: string } {
  if (squad.is_default === true) {
    return { text: "Default", color: "#f59e0b" };
  }
  if (squad.switch_cost > 0) {
    const cost =
      squad.current_join_cost === undefined || squad.current_join_cost === null
        ? squad.switch_cost
        : squad.current_join_cost;
    if (squad.has_join_sale === true) {
      return { text: `${thousands(cost)} bytes (Sale)`, color: "#FF6B35" };
    }
    return { text: `${thousands(cost)} bytes`, color: "#11FF00" };
  }
  return { text: "Free", color: "#11FF00" };
}

function roleColor(squad: Squad, guildRoles: Record<string, number> | null | undefined): string {
  if (guildRoles === undefined || guildRoles === null) {
    return "#FFFFFF";
  }
  const roleId = squad.role_id;
  if (roleId === undefined || roleId === null) {
    return "#FFFFFF";
  }
  const colorInt = guildRoles[roleId];
  if (colorInt === undefined || colorInt === 0) {
    return "#FFFFFF";
  }
  return `#${colorInt.toString(16).toUpperCase().padStart(6, "0")}`;
}

export function layoutSquadListCard(
  request: SquadListCardRequest,
  metrics: TextMetrics,
): CardLayout {
  if (request.squads.length === 0) {
    return layoutSimpleFallback(
      "squad-list",
      TITLE,
      "No squads have been created yet!",
      "info",
      metrics,
    );
  }

  const ops: DrawOp[] = [];
  let currentY = PADDING_TOP;

  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, TITLE, "title_medium", COLORS.info!));
  currentY += metrics.measure("title_medium", TITLE).lineHeight + 64;

  ops.push(textOp(PADDING_HORIZONTAL, currentY, "Squad Name", "text_medium", TEXT_COLOR));

  const membersHeader = "Members";
  const membersHeaderWidth = metrics.measure("text_medium", membersHeader).width;
  const membersX =
    PADDING_HORIZONTAL + MEMBERS_COLUMN_START + MEMBERS_COLUMN_WIDTH - membersHeaderWidth;
  ops.push(textOp(membersX, currentY, membersHeader, "text_medium", TEXT_COLOR));

  const costHeader = "Join Cost";
  const costHeaderWidth = metrics.measure("text_medium", costHeader).width;
  ops.push(
    textOp(
      PADDING_HORIZONTAL + CONTENT_WIDTH - costHeaderWidth,
      currentY,
      costHeader,
      "text_medium",
      TEXT_COLOR,
    ),
  );

  currentY += metrics.measure("text_medium", "A").lineHeight + 12;

  request.squads.slice(0, MAX_ROWS).forEach((squad, index) => {
    const rowY = currentY + index * ROW_SPACING;

    ops.push({
      op: "ellipse",
      cx: PADDING_HORIZONTAL + 8,
      cy: rowY + 12,
      r: CIRCLE_RADIUS,
      fill: roleColor(squad, request.guild_roles),
    });

    let nameText = squad.name;
    if (nameText.length > 22) {
      nameText = `${nameText.slice(0, 19)}...`;
    }
    ops.push(textOp(PADDING_HORIZONTAL + 24, rowY, nameText, "text_small", TEXT_COLOR));

    if (squad.is_default !== true) {
      let memberText = `${squad.member_count}`;
      if (squad.max_members !== undefined && squad.max_members !== null && squad.max_members !== 0) {
        memberText += `/${squad.max_members}`;
      }
      const memberWidth = metrics.measure("text_small", memberText).width;
      const memberX =
        PADDING_HORIZONTAL + MEMBERS_COLUMN_START + MEMBERS_COLUMN_WIDTH - memberWidth;
      ops.push(textOp(memberX, rowY, memberText, "text_small", "#00E1FF"));
    }

    const cost = joinCost(squad);
    const costWidth = metrics.measure("text_small", cost.text).width;
    ops.push(
      textOp(PADDING_HORIZONTAL + CONTENT_WIDTH - costWidth, rowY, cost.text, "text_small", cost.color),
    );
  });

  if (request.has_active_campaign === true) {
    ops.push(
      textOp(
        PADDING_HORIZONTAL,
        CANVAS_HEIGHT - 105,
        "Campaign active - Switching disabled",
        "text_small",
        "#f59e0b",
      ),
    );
  }

  return {
    card: "squad-list",
    background: "background.png",
    canvas: { width: 960, height: 540 },
    ops,
  };
}
