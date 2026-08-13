/** JSON Schema per card body. Field names are the Python parameter names verbatim.
 *
 * Dead parameters (`guild_name` on leaderboard, `current_squad_id` on
 * squad-list, `current_squad_name` / `available_squads_count` on
 * squad-join-selector) are declared so callers can keep passing them; the
 * layouts ignore them exactly as the Python does.
 */

const nullableString = { type: ["string", "null"] } as const;
const nullableInteger = { type: ["integer", "null"] } as const;

const leaderboardEntry = {
  type: "object",
  additionalProperties: false,
  required: ["rank", "user_id", "balance", "streak_count"],
  properties: {
    rank: { type: "integer" },
    user_id: { type: "string" },
    balance: { type: "integer" },
    streak_count: { type: "integer" },
  },
} as const;

const transaction = {
  type: "object",
  additionalProperties: false,
  required: ["giver_id", "giver_username", "receiver_id", "receiver_username", "amount"],
  properties: {
    created_at: nullableString,
    giver_id: { type: "string" },
    giver_username: { type: "string" },
    receiver_id: { type: "string" },
    receiver_username: { type: "string" },
    amount: { type: "integer" },
    reason: nullableString,
  },
} as const;

const bytesConfig = {
  type: "object",
  additionalProperties: false,
  required: ["daily_amount", "starting_balance", "max_transfer", "transfer_cooldown_hours"],
  properties: {
    daily_amount: { type: "integer" },
    starting_balance: { type: "integer" },
    max_transfer: { type: "integer" },
    transfer_cooldown_hours: { type: "integer" },
    streak_bonuses: {
      type: ["object", "null"],
      additionalProperties: { type: "number" },
    },
  },
} as const;

const squad = {
  type: "object",
  additionalProperties: false,
  required: ["name", "member_count", "switch_cost"],
  properties: {
    name: { type: "string" },
    description: nullableString,
    member_count: { type: "integer" },
    max_members: nullableInteger,
    switch_cost: { type: "integer" },
    current_join_cost: nullableInteger,
    has_join_sale: { type: "boolean", default: false },
    role_id: nullableString,
    is_default: { type: "boolean", default: false },
    is_active: { type: "boolean", default: true },
  },
} as const;

const squadMember = {
  type: "object",
  additionalProperties: false,
  required: ["user_id"],
  properties: {
    user_id: { type: "string" },
    username: nullableString,
    joined_at: nullableString,
  },
} as const;

const userMemberInfo = {
  type: ["object", "null"],
  additionalProperties: false,
  properties: {
    member_since: nullableString,
  },
} as const;

export const CARD_SCHEMAS = {
  simple: {
    type: "object",
    additionalProperties: false,
    required: ["title", "description"],
    properties: {
      title: { type: "string" },
      description: { type: "string" },
      embed_type: {
        type: "string",
        enum: ["default", "error", "success", "warning", "info"],
        default: "default",
      },
    },
  },
  error: {
    type: "object",
    additionalProperties: false,
    required: ["message"],
    properties: { message: { type: "string" } },
  },
  success: {
    type: "object",
    additionalProperties: false,
    required: ["title", "description"],
    properties: { title: { type: "string" }, description: { type: "string" } },
  },
  info: {
    type: "object",
    additionalProperties: false,
    required: ["title", "description"],
    properties: { title: { type: "string" }, description: { type: "string" } },
  },
  cooldown: {
    type: "object",
    additionalProperties: false,
    required: ["message"],
    properties: {
      message: { type: "string" },
      cooldown_end_timestamp: nullableInteger,
    },
  },
  leaderboard: {
    type: "object",
    additionalProperties: false,
    required: ["entries", "guild_name", "user_display_names"],
    properties: {
      entries: { type: "array", items: leaderboardEntry, maxItems: 100 },
      guild_name: { type: "string" },
      user_display_names: { type: "object", additionalProperties: { type: "string" } },
    },
  },
  history: {
    type: "object",
    additionalProperties: false,
    required: ["transactions", "user_id"],
    properties: {
      transactions: { type: "array", items: transaction, maxItems: 100 },
      user_id: { type: "string" },
    },
  },
  config: {
    type: "object",
    additionalProperties: false,
    required: ["config", "guild_name"],
    properties: { config: bytesConfig, guild_name: { type: "string" } },
  },
  "squad-list": {
    type: "object",
    additionalProperties: false,
    required: ["squads", "guild_name"],
    properties: {
      squads: { type: "array", items: squad, maxItems: 100 },
      guild_name: { type: "string" },
      current_squad_id: nullableString,
      guild_roles: { type: ["object", "null"], additionalProperties: { type: "integer" } },
      has_active_campaign: { type: "boolean", default: false },
    },
  },
  "squad-info": {
    type: "object",
    additionalProperties: false,
    required: ["squad", "members"],
    properties: {
      squad,
      members: { type: "array", items: squadMember, maxItems: 500 },
      user_member_info: userMemberInfo,
    },
  },
  "squad-members": {
    type: "object",
    additionalProperties: false,
    required: ["squad", "members"],
    properties: {
      squad,
      members: { type: "array", items: squadMember, maxItems: 500 },
    },
  },
  "squad-join-selector": {
    type: "object",
    additionalProperties: false,
    required: ["user_balance"],
    properties: {
      user_balance: { type: "integer" },
      current_squad_name: nullableString,
      available_squads_count: { type: "integer", default: 0 },
    },
  },
  balance: {
    type: "object",
    additionalProperties: false,
    required: ["username", "balance"],
    properties: {
      username: { type: "string" },
      balance: { type: "integer" },
      streak_count: { type: "integer", default: 0 },
      last_daily: nullableString,
      total_received: { type: "integer", default: 0 },
      total_sent: { type: "integer", default: 0 },
    },
  },
  "transfer-success": {
    type: "object",
    additionalProperties: false,
    required: ["giver_name", "receiver_name", "amount"],
    properties: {
      giver_name: { type: "string" },
      receiver_name: { type: "string" },
      amount: { type: "integer" },
      reason: nullableString,
      new_balance: nullableInteger,
    },
  },
} as const;

export type CardName = keyof typeof CARD_SCHEMAS;
