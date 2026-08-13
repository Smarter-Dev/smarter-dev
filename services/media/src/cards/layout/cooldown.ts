/** Port of `create_cooldown_embed`. */

import type { CardLayout, TextMetrics } from "../types.js";
import type { CooldownCardRequest } from "../requests.js";
import { layoutSimpleCard } from "./simple.js";

function plural(count: number, unit: string): string {
  return `${count} ${unit}${count !== 1 ? "s" : ""}`;
}

export function cooldownDescription(request: CooldownCardRequest, now: Date): string {
  const endTimestamp = request.cooldown_end_timestamp;
  if (endTimestamp === undefined || endTimestamp === null || endTimestamp === 0) {
    return request.message;
  }

  const currentTime = Math.trunc(now.getTime() / 1000);
  const remaining = endTimestamp - currentTime;
  if (remaining <= 0) {
    return "You can send bytes again now.";
  }

  if (remaining >= 3600) {
    return `You can send bytes again in ${plural(Math.floor(remaining / 3600), "hour")}.`;
  }
  if (remaining >= 60) {
    return `You can send bytes again in ${plural(Math.floor(remaining / 60), "minute")}.`;
  }
  return `You can send bytes again in ${plural(remaining, "second")}.`;
}

export function layoutCooldownCard(
  request: CooldownCardRequest,
  metrics: TextMetrics,
  now: Date,
): CardLayout {
  return layoutSimpleCard(
    {
      title: "TRANSFER COOLDOWN",
      description: cooldownDescription(request, now),
      embed_type: "warning",
    },
    metrics,
    "cooldown",
  );
}
