/** The branches the fourteen goldens do not reach. */

import { describe, expect, it } from "vitest";

import { layoutBalanceCard } from "../../src/cards/layout/balance.js";
import { layoutConfigCard } from "../../src/cards/layout/config.js";
import { cooldownDescription, layoutCooldownCard } from "../../src/cards/layout/cooldown.js";
import { layoutHistoryCard } from "../../src/cards/layout/history.js";
import { layoutLeaderboardCard } from "../../src/cards/layout/leaderboard.js";
import { layoutSimpleCard } from "../../src/cards/layout/simple.js";
import { layoutSquadInfoCard } from "../../src/cards/layout/squadInfo.js";
import { joinCost, layoutSquadListCard } from "../../src/cards/layout/squadList.js";
import { layoutSquadMembersCard } from "../../src/cards/layout/squadMembers.js";
import { layoutTransferSuccessCard } from "../../src/cards/layout/transferSuccess.js";
import type { CardLayout } from "../../src/cards/types.js";
import type { Squad, Transaction } from "../../src/cards/requests.js";
import { FIXED_NOW, testMetrics } from "../helpers.js";

const metrics = testMetrics();

function texts(layout: CardLayout): string[] {
  return layout.ops.filter((op) => op.op === "text").map((op) => op.text);
}

/** Text ops after the title's shadow-and-fill pair. */
function bodyTexts(layout: CardLayout): string[] {
  return texts(layout).slice(2);
}

const baseSquad: Squad = {
  name: "Nightshade",
  description: null,
  member_count: 3,
  max_members: null,
  switch_cost: 0,
  current_join_cost: null,
  has_join_sale: false,
  role_id: null,
  is_default: false,
  is_active: true,
};

describe("simple", () => {
  it("picks the plate and title colour from embed_type", () => {
    expect(layoutSimpleCard({ title: "T", description: "D", embed_type: "error" }, metrics).background)
      .toBe("error-background.png");
    expect(layoutSimpleCard({ title: "T", description: "D", embed_type: "success" }, metrics).background)
      .toBe("success-background.png");
    expect(layoutSimpleCard({ title: "T", description: "D", embed_type: "warning" }, metrics).ops[1])
      .toMatchObject({ fill: "#f59e0b" });
    expect(layoutSimpleCard({ title: "T", description: "D" }, metrics).ops[1])
      .toMatchObject({ fill: "#00E1FF" });
  });
});

describe("cooldown", () => {
  const message = "You are on transfer cooldown.";
  const nowSeconds = Math.trunc(FIXED_NOW.getTime() / 1000);

  it.each([
    [null, message],
    [nowSeconds - 60, "You can send bytes again now."],
    [nowSeconds + 7200, "You can send bytes again in 2 hours."],
    [nowSeconds + 3600, "You can send bytes again in 1 hour."],
    [nowSeconds + 120, "You can send bytes again in 2 minutes."],
    [nowSeconds + 61, "You can send bytes again in 1 minute."],
    [nowSeconds + 30, "You can send bytes again in 30 seconds."],
    [nowSeconds + 1, "You can send bytes again in 1 second."],
  ])("timestamp %s reads as %s", (timestamp, expected) => {
    expect(
      cooldownDescription({ message, cooldown_end_timestamp: timestamp }, FIXED_NOW),
    ).toBe(expected);
  });

  it("always uses the warning plate and title", () => {
    const layout = layoutCooldownCard({ message }, metrics, FIXED_NOW);
    expect(layout.background).toBe("background.png");
    expect(texts(layout)).toContain("TRANSFER COOLDOWN");
  });
});

describe("leaderboard", () => {
  it("delegates to a simple card when there are no entries", () => {
    const layout = layoutLeaderboardCard(
      { entries: [], guild_name: "g", user_display_names: {} },
      metrics,
    );
    expect(texts(layout)).toContain("No leaderboard data available yet!");
  });

  it("suppresses the streak column at zero and falls back on an unknown user", () => {
    const layout = layoutLeaderboardCard(
      {
        entries: [{ rank: 4, user_id: "123456789012345678", balance: 10, streak_count: 0 }],
        guild_name: "g",
        user_display_names: {},
      },
      metrics,
    );
    expect(bodyTexts(layout)).toEqual(["4", "User 12345678", "10"]);
  });

  it("truncates a display name longer than 18 characters", () => {
    const layout = layoutLeaderboardCard(
      {
        entries: [{ rank: 1, user_id: "u", balance: 1, streak_count: 0 }],
        guild_name: "g",
        user_display_names: { u: "abcdefghijklmnopqrstuvwxyz" },
      },
      metrics,
    );
    expect(texts(layout)).toContain("abcdefghijklmno...");
  });
});

describe("history", () => {
  const transaction = (overrides: Partial<Transaction>): Transaction => ({
    created_at: "2026-08-01T10:00:00",
    giver_id: "me",
    giver_username: "me",
    receiver_id: "you",
    receiver_username: "you",
    amount: 10,
    reason: null,
    ...overrides,
  });

  function cell(overrides: Partial<Transaction>): string[] {
    return bodyTexts(
      layoutHistoryCard({ transactions: [transaction(overrides)], user_id: "me" }, metrics),
    );
  }

  it("delegates to a simple card when there is nothing to show", () => {
    const layout = layoutHistoryCard({ transactions: [], user_id: "me" }, metrics);
    expect(texts(layout)).toContain("No transactions found.");
  });

  it.each([
    [{}, "> you"],
    [{ receiver_id: "SYSTEM", reason: "Squad join fee: Nightshade" }, "- Joined Nightshade"],
    [{ receiver_id: "SYSTEM", reason: "some other charge" }, "- System Charge"],
    [{ giver_id: "them", giver_username: "them", receiver_id: "me" }, "< them"],
    [{ giver_id: "SYSTEM", receiver_id: "me", reason: "New member welcome bonus" }, "+ Welcome Bonus"],
    [
      { giver_id: "SYSTEM", receiver_id: "me", reason: "Daily reward (Day 27, 4x multiplier)" },
      "+ Daily (4x)",
    ],
    [
      { giver_id: "SYSTEM", receiver_id: "me", reason: "Daily reward (Day 5, 1x multiplier)" },
      "+ Daily (Day 5)",
    ],
    [{ giver_id: "SYSTEM", receiver_id: "me", reason: "Daily reward (Day 3)" }, "+ Daily Reward"],
    [{ giver_id: "SYSTEM", receiver_id: "me", reason: "bounty" }, "+ System Reward"],
  ])("%j renders %s", (overrides, expected) => {
    expect(cell(overrides as Partial<Transaction>)).toContain(expected);
  });

  it.each([
    [null, ""],
    ["", ""],
    ["not-a-date", "N/A"],
    ["2026-08-01T10:00:00Z", "08-01"],
    ["2026-12-25", "12-25"],
  ])("created_at %s renders the date cell as %s", (createdAt, expected) => {
    expect(cell({ created_at: createdAt })[0]).toBe(expected);
  });

  it("truncates a counterparty longer than 35 characters", () => {
    const long = "x".repeat(40);
    expect(cell({ receiver_username: long })).toContain(`> ${"x".repeat(32)}...`);
  });

  it("colours outgoing amounts red and incoming green", () => {
    const sent = layoutHistoryCard(
      { transactions: [transaction({})], user_id: "me" },
      metrics,
    ).ops.at(-1);
    const received = layoutHistoryCard(
      { transactions: [transaction({ giver_id: "them", receiver_id: "me" })], user_id: "me" },
      metrics,
    ).ops.at(-1);
    expect(sent).toMatchObject({ fill: "#FF6B6B", text: "-10" });
    expect(received).toMatchObject({ fill: "#11FF00", text: "+10" });
  });
});

describe("config", () => {
  const config = {
    daily_amount: 25,
    starting_balance: 100,
    max_transfer: 5000,
    transfer_cooldown_hours: 0,
    streak_bonuses: null,
  };

  it("says 'No cooldown' at zero hours and omits an absent bonus table", () => {
    const rendered = texts(layoutConfigCard({ config, guild_name: "g" }, metrics));
    expect(rendered).toContain("No cooldown");
    expect(rendered).not.toContain("Streak Bonuses:");
  });

  it("sorts streak bonus keys numerically, not lexically", () => {
    const rendered = texts(
      layoutConfigCard(
        { config: { ...config, streak_bonuses: { "30": 8, "7": 2, "14": 4 } }, guild_name: "g" },
        metrics,
      ),
    );
    expect(rendered).toContain("  7 days: 2x • 14 days: 4x • 30 days: 8x");
  });
});

describe("squad-list", () => {
  it("delegates to a simple card when there are no squads", () => {
    const layout = layoutSquadListCard({ squads: [], guild_name: "g" }, metrics);
    expect(texts(layout)).toContain("No squads have been created yet!");
  });

  it.each([
    [{ is_default: true }, "Default", "#f59e0b"],
    [{ switch_cost: 0 }, "Free", "#11FF00"],
    [{ switch_cost: 500 }, "500 bytes", "#11FF00"],
    [{ switch_cost: 500, current_join_cost: 350, has_join_sale: true }, "350 bytes (Sale)", "#FF6B35"],
  ])("%j renders the cost cell as %s", (overrides, text, color) => {
    expect(joinCost({ ...baseSquad, ...overrides })).toEqual({ text, color });
  });

  it("suppresses the member count for the default squad", () => {
    const layout = layoutSquadListCard(
      { squads: [{ ...baseSquad, is_default: true, member_count: 204 }], guild_name: "g" },
      metrics,
    );
    expect(texts(layout)).not.toContain("204");
  });

  it("drops squads past the tenth without a truncation note", () => {
    const squads = Array.from({ length: 14 }, (_unused, index) => ({
      ...baseSquad,
      name: `Squad ${index}`,
    }));
    const rendered = texts(layoutSquadListCard({ squads, guild_name: "g" }, metrics));
    expect(rendered).toContain("Squad 9");
    expect(rendered).not.toContain("Squad 10");
    expect(rendered.some((text) => text.includes("more"))).toBe(false);
  });

  it("uses white when the role colour is absent or zero", () => {
    const layout = layoutSquadListCard(
      {
        squads: [{ ...baseSquad, role_id: "1" }],
        guild_name: "g",
        guild_roles: { "1": 0 },
      },
      metrics,
    );
    expect(layout.ops.find((op) => op.op === "ellipse")).toMatchObject({ fill: "#FFFFFF" });
  });

  it("pins the campaign warning to height - 105", () => {
    const layout = layoutSquadListCard(
      { squads: [baseSquad], guild_name: "g", has_active_campaign: true },
      metrics,
    );
    expect(layout.ops.at(-1)).toMatchObject({ y: 435, text: "Campaign active - Switching disabled" });
  });
});

describe("squad-info", () => {
  it("reports the default squad's join cost as N/A and an inactive status", () => {
    const rendered = texts(
      layoutSquadInfoCard(
        { squad: { ...baseSquad, is_default: true, is_active: false }, members: [] },
        metrics,
        FIXED_NOW,
      ),
    );
    expect(rendered).toContain("N/A (default)");
    expect(rendered).toContain("Inactive");
  });

  it("counts membership in whole days, singular at one", () => {
    const rendered = texts(
      layoutSquadInfoCard(
        {
          squad: baseSquad,
          members: [],
          user_member_info: { member_since: "2026-08-05T12:00:00" },
        },
        metrics,
        FIXED_NOW,
      ),
    );
    expect(rendered).toContain("Member for 1 day");
  });

  it("omits the membership block when member_since is missing", () => {
    const rendered = texts(
      layoutSquadInfoCard({ squad: baseSquad, members: [], user_member_info: {} }, metrics, FIXED_NOW),
    );
    expect(rendered).not.toContain("Your Membership:");
  });
});

describe("squad-members", () => {
  const member = (index: number) => ({
    user_id: `1234567890${index}`,
    username: `member-${index}`,
    joined_at: "2026-07-01T00:00:00",
  });

  it("delegates to a simple card when the squad is empty", () => {
    const rendered = texts(
      layoutSquadMembersCard({ squad: baseSquad, members: [] }, metrics),
    );
    expect(rendered).toContain("This squad has no members.");
  });

  it("draws at most fifteen rows and then a truncation note", () => {
    const members = Array.from({ length: 17 }, (_unused, index) => member(index));
    const rendered = texts(layoutSquadMembersCard({ squad: baseSquad, members }, metrics));
    expect(rendered).toContain("15.");
    expect(rendered).not.toContain("16.");
    expect(rendered).toContain("... and 2 more members");
  });

  it("falls back to a user-id label and skips an unparseable join date", () => {
    const rendered = texts(
      layoutSquadMembersCard(
        {
          squad: baseSquad,
          members: [{ user_id: "123456789012", username: null, joined_at: "nope" }],
        },
        metrics,
      ),
    );
    expect(rendered).toContain("User 12345678");
    expect(rendered).not.toContain("nope");
  });

  it("uses the singular subtitle for one member", () => {
    const rendered = texts(
      layoutSquadMembersCard({ squad: baseSquad, members: [member(0)] }, metrics),
    );
    expect(rendered).toContain("1 member");
  });
});

describe("balance", () => {
  it("shows only the balance row when everything optional is absent", () => {
    const rendered = bodyTexts(layoutBalanceCard({ username: "nyx", balance: 10 }, metrics));
    expect(rendered).toEqual([
      "Account overview for nyx",
      "Account overview for nyx",
      "Balance:",
      "10 bytes",
    ]);
  });

  it("adds every optional row and computes the net change", () => {
    const rendered = texts(
      layoutBalanceCard(
        {
          username: "nyx",
          balance: 10,
          streak_count: 3,
          last_daily: "2026-08-06",
          total_received: 10,
          total_sent: 40,
        },
        metrics,
      ),
    );
    expect(rendered).toContain("Streak:");
    expect(rendered).toContain("Last Daily:");
    expect(rendered).toContain("Total Received:");
    expect(rendered).toContain("Total Sent:");
    expect(rendered).toContain("-30");
  });

  it("prefixes a non-negative net change with +", () => {
    const layout = layoutBalanceCard(
      { username: "nyx", balance: 10, total_received: 40, total_sent: 10 },
      metrics,
    );
    expect(layout.ops.at(-1)).toMatchObject({ text: "+30", fill: "#11FF00" });
  });
});

describe("transfer-success", () => {
  it("omits the optional rows and uses the success plate", () => {
    const layout = layoutTransferSuccessCard(
      { giver_name: "a", receiver_name: "b", amount: 1 },
      metrics,
    );
    expect(layout.background).toBe("success-background.png");
    expect(texts(layout)).not.toContain("Reason:");
    expect(texts(layout)).not.toContain("New Balance:");
  });

  it("renders a zero new balance, which is not the same as an absent one", () => {
    const rendered = texts(
      layoutTransferSuccessCard(
        { giver_name: "a", receiver_name: "b", amount: 1, new_balance: 0 },
        metrics,
      ),
    );
    expect(rendered).toContain("New Balance:");
    expect(rendered).toContain("0 bytes");
  });
});
