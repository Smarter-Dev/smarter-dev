/** Port of `create_history_embed`, including every reason-parsing branch. */

import { formatMonthDay, parseNaiveTimestamp, thousands } from "../format.js";
import { shadowedText, textOp } from "../text.js";
import { COLORS, CONTENT_WIDTH, PADDING_HORIZONTAL, PADDING_TOP, TEXT_COLOR } from "../theme.js";
import type { CardLayout, DrawOp, TextMetrics } from "../types.js";
import type { HistoryCardRequest, Transaction } from "../requests.js";
import { layoutSimpleFallback } from "./simple.js";

const TITLE = "TRANSACTION HISTORY";
const ROW_SPACING = 26;
const USER_COLUMN_OFFSET = 120;
const DAILY_REASON = /Day (\d+)(?:, (\d+)x multiplier)?/;

function dateCell(transaction: Transaction): string {
  const createdAt = transaction.created_at;
  if (createdAt === undefined || createdAt === null || createdAt === "") {
    return "";
  }
  const parsed = parseNaiveTimestamp(createdAt);
  return parsed === null ? "N/A" : formatMonthDay(parsed);
}

interface Counterparty {
  typeIndicator: string;
  otherUser: string;
}

function sentCounterparty(transaction: Transaction): Counterparty {
  if (transaction.receiver_id !== "SYSTEM") {
    return { typeIndicator: ">", otherUser: transaction.receiver_username };
  }
  const reason = transaction.reason ?? "";
  if (reason.startsWith("Squad join fee:")) {
    return { typeIndicator: "-", otherUser: `Joined ${reason.replace("Squad join fee: ", "")}` };
  }
  return { typeIndicator: "-", otherUser: "System Charge" };
}

function receivedCounterparty(transaction: Transaction): Counterparty {
  if (transaction.giver_id !== "SYSTEM") {
    return { typeIndicator: "<", otherUser: transaction.giver_username };
  }

  const reason = transaction.reason ?? "";
  if (reason.trim() === "New member welcome bonus") {
    return { typeIndicator: "+", otherUser: "Welcome Bonus" };
  }
  if (reason.startsWith("Daily reward")) {
    if (!reason.includes("multiplier)")) {
      return { typeIndicator: "+", otherUser: "Daily Reward" };
    }
    const match = DAILY_REASON.exec(reason);
    if (match === null) {
      return { typeIndicator: "+", otherUser: "Daily Reward" };
    }
    const [, day, multiplier] = match;
    if (multiplier !== undefined && multiplier !== "1") {
      return { typeIndicator: "+", otherUser: `Daily (${multiplier}x)` };
    }
    return { typeIndicator: "+", otherUser: `Daily (Day ${day})` };
  }
  return { typeIndicator: "+", otherUser: "System Reward" };
}

export function layoutHistoryCard(
  request: HistoryCardRequest,
  metrics: TextMetrics,
): CardLayout {
  if (request.transactions.length === 0) {
    return layoutSimpleFallback("history", TITLE, "No transactions found.", "info", metrics);
  }

  const ops: DrawOp[] = [];
  let currentY = PADDING_TOP;

  ops.push(...shadowedText(PADDING_HORIZONTAL, currentY, TITLE, "title_medium", COLORS.info!));
  currentY += metrics.measure("title_medium", TITLE).lineHeight + 64;

  request.transactions.forEach((transaction, index) => {
    const isSender = transaction.giver_id === request.user_id;
    const amountText = isSender
      ? `-${thousands(transaction.amount)}`
      : `+${thousands(transaction.amount)}`;
    const counterparty = isSender
      ? sentCounterparty(transaction)
      : receivedCounterparty(transaction);

    let otherUser = counterparty.otherUser;
    if (otherUser.length > 35) {
      otherUser = `${otherUser.slice(0, 32)}...`;
    }

    const rowY = currentY + index * ROW_SPACING;

    ops.push(textOp(PADDING_HORIZONTAL, rowY, dateCell(transaction), "text_small", TEXT_COLOR));

    ops.push(
      textOp(
        PADDING_HORIZONTAL + USER_COLUMN_OFFSET,
        rowY,
        `${counterparty.typeIndicator} ${otherUser}`,
        "text_small",
        TEXT_COLOR,
      ),
    );

    const amountWidth = metrics.measure("text_small", amountText).width;
    const amountX = PADDING_HORIZONTAL + CONTENT_WIDTH - amountWidth;
    ops.push(textOp(amountX, rowY, amountText, "text_small", isSender ? "#FF6B6B" : "#11FF00"));
  });

  return {
    card: "history",
    background: "background.png",
    canvas: { width: 960, height: 540 },
    ops,
  };
}
