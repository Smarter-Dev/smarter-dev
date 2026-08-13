/** Request shapes. Every field name is the Python parameter or attribute name, verbatim. */

export interface SimpleCardRequest {
  title: string;
  description: string;
  embed_type?: string;
}

export interface ErrorCardRequest {
  message: string;
}

export interface SuccessCardRequest {
  title: string;
  description: string;
}

export interface InfoCardRequest {
  title: string;
  description: string;
}

export interface CooldownCardRequest {
  message: string;
  cooldown_end_timestamp?: number | null;
}

export interface LeaderboardEntry {
  rank: number;
  user_id: string;
  balance: number;
  streak_count: number;
}

export interface LeaderboardCardRequest {
  entries: LeaderboardEntry[];
  guild_name: string;
  user_display_names: Record<string, string>;
}

export interface Transaction {
  created_at?: string | null;
  giver_id: string;
  giver_username: string;
  receiver_id: string;
  receiver_username: string;
  amount: number;
  reason?: string | null;
}

export interface HistoryCardRequest {
  transactions: Transaction[];
  user_id: string;
}

export interface BytesConfig {
  daily_amount: number;
  starting_balance: number;
  max_transfer: number;
  transfer_cooldown_hours: number;
  streak_bonuses?: Record<string, number> | null;
}

export interface ConfigCardRequest {
  config: BytesConfig;
  guild_name: string;
}

export interface Squad {
  name: string;
  description?: string | null;
  member_count: number;
  max_members?: number | null;
  switch_cost: number;
  current_join_cost?: number | null;
  has_join_sale?: boolean;
  role_id?: string | null;
  is_default?: boolean;
  is_active?: boolean;
}

export interface SquadListCardRequest {
  squads: Squad[];
  guild_name: string;
  current_squad_id?: string | null;
  guild_roles?: Record<string, number> | null;
  has_active_campaign?: boolean;
}

export interface SquadMember {
  user_id: string;
  username?: string | null;
  joined_at?: string | null;
}

export interface UserMemberInfo {
  member_since?: string | null;
}

export interface SquadInfoCardRequest {
  squad: Squad;
  members: SquadMember[];
  user_member_info?: UserMemberInfo | null;
}

export interface SquadMembersCardRequest {
  squad: Squad;
  members: SquadMember[];
}

export interface SquadJoinSelectorCardRequest {
  user_balance: number;
  current_squad_name?: string | null;
  available_squads_count?: number;
}

export interface BalanceCardRequest {
  username: string;
  balance: number;
  streak_count?: number;
  last_daily?: string | null;
  total_received?: number;
  total_sent?: number;
}

export interface TransferSuccessCardRequest {
  giver_name: string;
  receiver_name: string;
  amount: number;
  reason?: string | null;
  new_balance?: number | null;
}
