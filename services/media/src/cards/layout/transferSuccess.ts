/** Port of `create_transfer_success_embed`. */

import { thousands } from "../format.js";
import { shadowedText, textOp } from "../text.js";
import { COLORS, CONTENT_WIDTH, PADDING_HORIZONTAL, PADDING_TOP, TEXT_COLOR } from "../theme.js";
import type { CardLayout, DrawOp, TextMetrics } from "../types.js";
import type { TransferSuccessCardRequest } from "../requests.js";

const TITLE = "BYTES TRANSFERRED SUCCESSFULLY";

export function layoutTransferSuccessCard(
  request: TransferSuccessCardRequest,
  metrics: TextMetrics,
): CardLayout {
  const ops: DrawOp[] = [];
  let currentY = PADDING_TOP;

  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, TITLE, "title_medium", COLORS.success!));
  currentY += metrics.measure("title_medium", TITLE).lineHeight + 32;

  const rows: [string, string, string][] = [
    ["From:", request.giver_name, TEXT_COLOR],
    ["To:", request.receiver_name, TEXT_COLOR],
    ["Amount:", `${thousands(request.amount)} bytes`, "#00E1FF"],
  ];

  const reason = request.reason;
  if (reason !== undefined && reason !== null && reason !== "") {
    rows.push(["Reason:", reason, "#B0B0B0"]);
  }
  const newBalance = request.new_balance;
  if (newBalance !== undefined && newBalance !== null) {
    rows.push(["New Balance:", `${thousands(newBalance)} bytes`, "#11FF00"]);
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
    card: "transfer-success",
    background: "success-background.png",
    canvas: { width: 960, height: 540 },
    ops,
  };
}
