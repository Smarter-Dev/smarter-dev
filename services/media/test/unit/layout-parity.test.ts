/** The parity tests that matter: every layout must equal the Pillow draw-op trace. */

import { describe, expect, it } from "vitest";

import { CARD_ROUTES } from "../../src/cards/registry.js";
import type { CardLayout, DrawOp } from "../../src/cards/types.js";
import { CARD_NAMES, FIXED_NOW, cardLayoutFixture, cardRequest, testMetrics } from "../helpers.js";
import type { CardName } from "../helpers.js";

const metrics = testMetrics();

/** Python writes colour literals inconsistently (`#00E1FF` next to `#f59e0b`);
 * the trace preserves them verbatim and parity compares case-insensitively. */
function normalise(layout: CardLayout): CardLayout {
  return {
    ...layout,
    ops: layout.ops.map((op: DrawOp) => ({ ...op, fill: op.fill.toUpperCase() })),
  };
}

export function layoutFor(card: CardName): CardLayout {
  const route = CARD_ROUTES.find((candidate) => candidate.name === card);
  if (route === undefined) {
    throw new Error(`No registry entry for card ${card}`);
  }
  return route.layout(cardRequest(card) as never, metrics, FIXED_NOW);
}

describe("layout parity with Pillow", () => {
  it("registers exactly the fourteen documented cards", () => {
    expect(CARD_ROUTES.map((route) => route.name).sort()).toEqual([...CARD_NAMES].sort());
  });

  it.each(CARD_NAMES)("%s matches its captured draw-op trace", (card) => {
    expect(normalise(layoutFor(card))).toEqual(normalise(cardLayoutFixture(card)));
  });
});
