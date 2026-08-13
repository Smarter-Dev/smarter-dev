/** Port of `create_squad_join_selector_embed`.
 *
 * Bug-for-bug: the paragraph-break flag from the wrapper is discarded, so every
 * subtitle line uses tight spacing. `current_squad_name` and
 * `available_squads_count` are accepted and never drawn. */

import { thousands } from "../format.js";
import { shadowedText, wrapTextWithSpacing } from "../text.js";
import { COLORS, CONTENT_WIDTH, PADDING_HORIZONTAL, PADDING_TOP, TEXT_COLOR } from "../theme.js";
import type { CardLayout, DrawOp, TextMetrics } from "../types.js";
import type { SquadJoinSelectorCardRequest } from "../requests.js";

const TITLE = "SELECT A SQUAD TO JOIN";
const SUBTITLE = "Choose a squad from the menu below.";

export function layoutSquadJoinSelectorCard(
  request: SquadJoinSelectorCardRequest,
  metrics: TextMetrics,
): CardLayout {
  const ops: DrawOp[] = [];
  let currentY = PADDING_TOP;

  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, TITLE, "title_medium", COLORS.info!));
  currentY += metrics.measure("title_medium", TITLE).lineHeight + 32;

  for (const line of wrapTextWithSpacing(metrics, SUBTITLE, "text_medium", CONTENT_WIDTH)) {
    if (line.text !== "") {
      ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, line.text, "text_medium", TEXT_COLOR));
    }
    const lineHeight =
      line.text !== ""
        ? metrics.measure("text_medium", line.text).lineHeight
        : metrics.measure("text_medium", "A").lineHeight;
    currentY += lineHeight + 2;
  }

  currentY += 20;

  const balanceText = `Your Balance: ${thousands(request.user_balance)} bytes`;
  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, balanceText, "text_small", "#00E1FF"));

  return {
    card: "squad-join-selector",
    background: "background.png",
    canvas: { width: 960, height: 540 },
    ops,
  };
}
