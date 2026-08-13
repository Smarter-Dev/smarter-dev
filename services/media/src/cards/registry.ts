/** One entry per card endpoint: URL segment, body schema, filename, layout function. */

import { CARD_SCHEMAS } from "../schemas/cards.js";
import type { CardName } from "../schemas/cards.js";
import { layoutBalanceCard } from "./layout/balance.js";
import { layoutConfigCard } from "./layout/config.js";
import { layoutCooldownCard } from "./layout/cooldown.js";
import { layoutHistoryCard } from "./layout/history.js";
import { layoutLeaderboardCard } from "./layout/leaderboard.js";
import {
  layoutErrorCard,
  layoutInfoCard,
  layoutSimpleCard,
  layoutSuccessCard,
} from "./layout/simple.js";
import { layoutSquadInfoCard } from "./layout/squadInfo.js";
import { layoutSquadJoinSelectorCard } from "./layout/squadJoinSelector.js";
import { layoutSquadListCard } from "./layout/squadList.js";
import { layoutSquadMembersCard } from "./layout/squadMembers.js";
import { layoutTransferSuccessCard } from "./layout/transferSuccess.js";
import type { CardLayout, TextMetrics } from "./types.js";

export interface CardRoute {
  name: CardName;
  path: string;
  filename: string;
  schema: unknown;
  layout: (body: never, metrics: TextMetrics, now: Date) => CardLayout;
}

function ignoringClock<T>(
  layout: (body: T, metrics: TextMetrics) => CardLayout,
): (body: T, metrics: TextMetrics, now: Date) => CardLayout {
  return (body, metrics) => layout(body, metrics);
}

interface CardRouteSeed {
  name: CardName;
  filename: string;
  layout: CardRoute["layout"];
}

const CARD_ROUTE_SEEDS: CardRouteSeed[] = [
  { name: "simple", filename: "embed.png", layout: ignoringClock(layoutSimpleCard) },
  { name: "error", filename: "embed.png", layout: ignoringClock(layoutErrorCard) },
  { name: "success", filename: "embed.png", layout: ignoringClock(layoutSuccessCard) },
  { name: "info", filename: "embed.png", layout: ignoringClock(layoutInfoCard) },
  { name: "cooldown", filename: "embed.png", layout: layoutCooldownCard },
  { name: "leaderboard", filename: "embed.png", layout: ignoringClock(layoutLeaderboardCard) },
  { name: "history", filename: "embed.png", layout: ignoringClock(layoutHistoryCard) },
  { name: "config", filename: "embed.png", layout: ignoringClock(layoutConfigCard) },
  { name: "squad-list", filename: "embed.png", layout: ignoringClock(layoutSquadListCard) },
  { name: "squad-info", filename: "embed.png", layout: layoutSquadInfoCard },
  { name: "squad-members", filename: "embed.png", layout: ignoringClock(layoutSquadMembersCard) },
  {
    name: "squad-join-selector",
    filename: "embed.png",
    layout: ignoringClock(layoutSquadJoinSelectorCard),
  },
  { name: "balance", filename: "balance.png", layout: ignoringClock(layoutBalanceCard) },
  {
    name: "transfer-success",
    filename: "transfer_success.png",
    layout: ignoringClock(layoutTransferSuccessCard),
  },
];

export const CARD_ROUTES: CardRoute[] = CARD_ROUTE_SEEDS.map((seed) => ({
  ...seed,
  path: `/v1/cards/${seed.name}`,
  schema: CARD_SCHEMAS[seed.name],
}));
