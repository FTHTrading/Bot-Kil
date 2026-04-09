// ─── Domain types shared across all components ────────────────────────────────

export interface Bankroll {
  current_bankroll: number;
  starting_bankroll: number;
  total_profit: number;
  roi_pct: number;
  win_rate: number;
  total_bets: number;
  open_bets: number;
  max_drawdown: number;
  clv_avg: number;
  high_water_mark: number;
  daily_pnl: number;
}

export interface Pick {
  sport: string;
  event: string;
  pick: string;
  market: string;
  american_odds: number;
  decimal_odds: number;
  our_prob: number;
  implied_prob: number;
  edge_pct: number;
  ev_pct: number;
  recommended_stake: number;
  kelly_pct: number;
  verdict: string;
  book: string;
}

export interface ArbOpportunity {
  type: string;
  event: string;
  sport: string;
  profit_pct?: number;
  potential_edge_pct?: number;
  guaranteed_profit?: number;
  leg_a?: { side: string; odds: number; book: string; stake: number };
  leg_b?: { side: string; odds: number; book: string; stake: number };
  action?: string;
}

export interface Bet {
  id: string;
  sport: string;
  event: string;
  pick: string;
  market: string;
  american_odds: number;
  stake: number;
  result?: string;
  pnl?: number;
  placed_at: string;
}

export interface SharpMove {
  event: string;
  market: string;
  from_odds: number;
  to_odds: number;
  delta: number;
  book: string;
  sharp: boolean;
  sport: string;
  age_mins: number;
}

export interface LineShopMarket {
  event: string;
  sport: string;
  commence: string;
  books: Record<string, { h2h_home?: number; h2h_away?: number }>;
}

export interface PerfStats {
  bets: number;
  settled: number;
  wins: number;
  losses: number;
  win_rate: number;
  roi_pct: number;
  profit: number;
  wagered: number;
  clv_avg: number;
  edge_avg: number;
}

export interface Performance {
  total_bets: number;
  settled: number;
  wins: number;
  losses: number;
  win_rate: number;
  roi_pct: number;
  total_wagered: number;
  total_profit: number;
  clv_avg: number;
  edge_avg: number;
  sharpe: number;
  by_sport: Record<string, PerfStats>;
  by_market: Record<string, PerfStats>;
  by_agent: Record<string, PerfStats>;
  by_edge_bucket: Record<string, PerfStats>;
  periods: Record<string, PerfStats>;
}

export interface Middle {
  event: string;
  sport: string;
  leg_a: { side: string; odds: number; book: string; stake: number };
  leg_b: { side: string; odds: number; book: string; stake: number };
  window: number;
  max_win: number;
  guaranteed_loss: number;
  ev_pct: number;
}

// ─── Live Autonomous Agent types ──────────────────────────────────────────────

export interface LiveStatus {
  agent_alive: boolean;
  date: string | null;
  daily_spend_usd: number;
  cooldowns_sec: Record<string, number>;
  last_session_ts: string | null;
  last_file_age_s: number | null;
  reopen_mode: boolean;
}

export interface LiveBet {
  source: 'kalshi_order' | 'ledger';
  order_id?: string;
  ticker?: string;
  side?: string;
  entry_price_cents?: number;
  edge_pct?: number;
  asset?: string;
  market_type?: string;
  entry_ts?: string;
  clv?: number | null;
  close_price_cents?: number | null;
  status?: string;
  amount?: number;
  ts?: number;
}

export interface LiveSession {
  session: number;
  timestamp: string;
  bets_placed: number;
  tool_calls: number;
  provider: string;
  dry_run: boolean;
  bets: Array<{
    status: string;
    ticker: string;
    side: string;
    contracts: number;
    yes_price: number;
    reasoning: string;
  }>;
  summary: string;
}

export interface SteamAlert {
  event: string;
  sport: string;
  market: string;
  alert_type: string;
  conviction: string;
  move_direction?: string;
  move_amount?: number;
  public_pct?: number;
  book?: string;
  detected_at?: string;
}

export interface ConsensusItem {
  event: string;
  sport: string;
  market: string;
  outcome: string;
  grade: string;
  action: string;
  confidence: string;
  edge_pct: number;
  ev_pct: number;
  notes: string[];
}

export type ConnectionStatus = 'live' | 'connecting' | 'offline';

export type WorkflowStatus = 'idle' | 'running' | 'success' | 'error';

export interface WorkflowDef {
  id: string;
  label: string;
  cmd: string;
  description: string;
  color: string;
  accent: string;
}

export interface ApiEndpoint {
  method: 'GET' | 'POST' | 'WS';
  path: string;
  description: string;
  category: string;
}

// Detail drawer can be opened for any of these object types
export type DrawerPayload =
  | { type: 'pick'; data: Pick }
  | { type: 'arb'; data: ArbOpportunity }
  | { type: 'bet'; data: Bet }
  | { type: 'metric'; label: string; value: string; detail: Record<string, string | number> }
  | { type: 'workflow'; workflow: WorkflowDef };
