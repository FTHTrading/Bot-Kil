'use client';
import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Search, Target, GitMerge, BarChart, TrendingUp, BookOpen,
  Bot, RefreshCw, Filter, SlidersHorizontal, ChevronRight,
  ExternalLink, Flame, Clock, Star, ArrowUpRight, Zap, DollarSign,
} from 'lucide-react';
import {
  GlassPanel, Badge, SportPill, VerdictBadge, OddsChip,
  Skeleton, SkeletonRows, EmptyState,
} from '@/components/ui';
import clsx from 'clsx';

const API = process.env.NEXT_PUBLIC_MCP_API_URL || 'http://localhost:8420';

// ─── Types ────────────────────────────────────────────────────────────────────

interface Pick {
  id: string; sport: string; event: string; pick: string;
  odds: number; verdict: string; confidence: number;
  kelly_fraction: number; edge_pct: number;
  line?: number; book?: string; matchup?: string;
  analysis?: string;
}

interface Arb {
  id: string; event: string; sport: string;
  book_a: string; book_b: string;
  side_a: string; side_b: string;
  odds_a: number; odds_b: number;
  profit_pct: number; stake_100: number; created_at?: string;
}

interface LineShopItem {
  id: string; event: string; sport: string;
  team: string; best_odds: number; worst_odds: number;
  consensus: number; books: { name: string; odds: number }[];
}

interface SharpMove {
  id: string; event: string; sport: string;
  description: string; severity: string;
  line_move: number; pct_bets: number; pct_money: number;
  created_at?: string;
}

interface Bet {
  id: string; event: string; side: string;
  odds: number; stake: number; pnl?: number;
  status: 'won' | 'lost' | 'push' | 'pending';
  created_at?: string; book?: string;
}

interface PerfStat {
  sport: string; bets: number; wins: number;
  units: number; roi: number;
}

// ─── Tab definition ───────────────────────────────────────────────────────────

type TabId = 'picks' | 'arb' | 'lineshop' | 'steam' | 'performance' | 'betlog' | 'ai';

interface TabDef {
  id: TabId; label: string; icon: React.ElementType;
  description: string; badge?: string;
}

const TABS: TabDef[] = [
  { id: 'picks',       icon: Target,      label: 'Picks',        description: "Today's AI value picks"     },
  { id: 'arb',         icon: GitMerge,    label: 'Arb',          description: 'Arbitrage opportunities'     },
  { id: 'lineshop',    icon: BarChart,    label: 'Line Shop',     description: 'Best odds across books'      },
  { id: 'steam',       icon: Flame,       label: 'Steam',         description: 'Sharp line movement alerts'  },
  { id: 'performance', icon: TrendingUp,  label: 'Performance',   description: 'Historical performance lab'  },
  { id: 'betlog',      icon: BookOpen,    label: 'Bet Log',       description: 'Full wagering history'       },
  { id: 'ai',          icon: Bot,         label: 'AI Chat',       description: 'Intelligence assistant'      },
];

// ─── Mock data ────────────────────────────────────────────────────────────────

// ─── Today's picks — Apr 6 2026 ─────────────────────────────────────────────
// NBA: late regular season (Warriors @ Celtics, OKC @ Minnesota, Nuggets @ Lakers)
// MLB: Week 2 (Yankees @ Baltimore, Dodgers @ SF, Cubs @ STL, Astros @ TEX)
// NHL: late regular season (Leafs @ Ottawa, Bruins @ Tampa)
const MOCK_PICKS: Pick[] = [
  { id:'p1', sport:'basketball_nba',       event:'Warriors @ Celtics',          pick:'Celtics -4.5',       odds:-112, verdict:'EXCELLENT EDGE', confidence:88, kelly_fraction:0.09, edge_pct:9.2, book:'DraftKings',  matchup:'GSW vs BOS', analysis:'Celtics 18-4 at home this month. Warriors missing Curry (questionable). Market line opened -3, steamed to -4.5 on sharp action.' },
  { id:'p2', sport:'basketball_nba',       event:'OKC Thunder @ Minnesota',     pick:'Thunder -7',         odds:-110, verdict:'EXCELLENT EDGE', confidence:81, kelly_fraction:0.08, edge_pct:8.1, book:'FanDuel',     matchup:'OKC vs MIN', analysis:'OKC #1 seed protecting home-court seeding. Minnesota eliminated from top-4 race, minimal incentive. SZN-best 9-game ATS run for Thunder on road.' },
  { id:'p3', sport:'baseball_mlb',         event:'Dodgers @ Giants',            pick:'Dodgers ML',         odds:-155, verdict:'EXCELLENT EDGE', confidence:79, kelly_fraction:0.07, edge_pct:7.4, book:'BetMGM',      matchup:'LAD vs SF',  analysis:'Ohtani healthy. Yamamoto vs. Logan Webb — model likes LAD run differential +1.8 lifetime. Giants 2-8 at Oracle Park vs LHP this early season.' },
  { id:'p4', sport:'basketball_nba',       event:'Nuggets @ Lakers',            pick:'Nuggets -3',         odds:-108, verdict:'GOOD EDGE',      confidence:72, kelly_fraction:0.06, edge_pct:6.1, book:'Caesars',     matchup:'DEN vs LAL', analysis:'Jokic triple-double machine on 3-day rest. Lakers 4th-back-to-back in 7 days. Denver +7.4 net rating as road favorite this season.' },
  { id:'p5', sport:'baseball_mlb',         event:'Yankees @ Baltimore',         pick:'Under 8.5',          odds:-115, verdict:'GOOD EDGE',      confidence:69, kelly_fraction:0.05, edge_pct:5.8, book:'PointsBet',   matchup:'NYY vs BAL', analysis:'Cole vs. Grayson Rodriguez — two elite starters with sub-3.20 xFIP. April at Camden Yards historically low-scoring (7.1 avg runs/game last 3 yrs).' },
  { id:'p6', sport:'baseball_mlb',         event:'Astros @ Rangers',            pick:'Astros -1.5 RL',     odds:+122, verdict:'GOOD EDGE',      confidence:66, kelly_fraction:0.04, edge_pct:5.2, book:'DraftKings',  matchup:'HOU vs TEX', analysis:'Astros rotation depth vs Rangers bullpen vulnerability. Model projects HOU winning by 2+ in 61% of sims. +EV on RL at plus-odds.' },
  { id:'p7', sport:'icehockey_nhl',        event:'Maple Leafs @ Ottawa',        pick:'Leafs ML',           odds:-140, verdict:'GOOD EDGE',      confidence:67, kelly_fraction:0.04, edge_pct:4.9, book:'FanDuel',     matchup:'TOR vs OTT', analysis:'Toronto chasing 2nd Wild Card, Ottawa eliminated. Mathews returning from lower-body. Leafs 7-2 vs Atlantic division opponents this month.' },
  { id:'p8', sport:'baseball_mlb',         event:'Cubs @ Cardinals',            pick:'Over 8.5',           odds:-108, verdict:'MARGINAL',       confidence:58, kelly_fraction:0.02, edge_pct:2.8, book:'BetMGM',      matchup:'CHC vs STL', analysis:'Both bullpens taxed — Cardinals used 5 relievers yesterday. Busch Stadium April weather favorable (72°, minimal wind). Historical O/U: 8.9 in last 10 meetings.' },
];

// ─── Arb opportunities — Apr 6 2026 ─────────────────────────────────────────
const MOCK_ARB: Arb[] = [
  { id:'a1', event:'Nuggets @ Lakers',   sport:'basketball_nba', book_a:'DraftKings', book_b:'Caesars',  side_a:'Nuggets -2.5', side_b:'Lakers +4',    odds_a:-108, odds_b:+118, profit_pct:1.94, stake_100:91.8 },
  { id:'a2', event:'Dodgers @ Giants',   sport:'baseball_mlb',   book_a:'FanDuel',    book_b:'BetMGM',   side_a:'Dodgers ML',   side_b:'Giants ML',    odds_a:-145, odds_b:+165, profit_pct:0.87, stake_100:94.2 },
  { id:'a3', event:'Leafs @ Ottawa',     sport:'icehockey_nhl',  book_a:'PointsBet',  book_b:'Caesars',  side_a:'Leafs -0.5 PL', side_b:'Ottawa +1.5 PL', odds_a:-112, odds_b:+130, profit_pct:1.12, stake_100:93.1 },
];

// ─── Steam alerts — Apr 6 2026 ───────────────────────────────────────────────
const MOCK_STEAM: SharpMove[] = [
  { id:'s1', event:'Warriors @ Celtics',  sport:'basketball_nba', description:'Celtics spread steamed -3.5 → -4.5 in 40 min. Sharp accounts hammering at open. 74% of money on Celtics vs 51% of tickets.',          severity:'HIGH',   line_move:1,  pct_bets:51, pct_money:74, created_at:'2026-04-06T12:14:00Z' },
  { id:'s2', event:'Dodgers @ Giants',    sport:'baseball_mlb',   description:'Dodgers ML moved -140 → -155 with 68% of money on LAD. Correlated action on Dodgers -1.5 RL simultaneously at FanDuel and BetMGM.',     severity:'HIGH',   line_move:15, pct_bets:62, pct_money:68, created_at:'2026-04-06T11:52:00Z' },
  { id:'s3', event:'Yankees @ Baltimore', sport:'baseball_mlb',   description:'Under 8.5 ticked from -108 to -118 at three books simultaneously. Classic sharp under move — big money on cold April pitchers duel.',    severity:'MEDIUM', line_move:10, pct_bets:44, pct_money:61, created_at:'2026-04-06T13:05:00Z' },
  { id:'s4', event:'OKC @ Minnesota',     sport:'basketball_nba', description:'Reverse line movement: 62% of tickets on Minnesota but money pushing Thunder from -5.5 to -7. Textbook sharp vs public split.',          severity:'MEDIUM', line_move:1,  pct_bets:38, pct_money:64, created_at:'2026-04-06T10:30:00Z' },
  { id:'s5', event:'Astros @ Rangers',    sport:'baseball_mlb',   description:'RL line moved from +105 to +122 on Astros. Sharp money targeting plus-odds RL as Houston bullpen rest advantage becomes clear.',           severity:'LOW',    line_move:17, pct_bets:39, pct_money:55, created_at:'2026-04-06T09:45:00Z' },
];

// ─── Season-to-date performance by sport ─────────────────────────────────────
const MOCK_PERF: PerfStat[] = [
  { sport:'NBA', bets:68, wins:43, units:9.8,  roi:14.4 },
  { sport:'MLB', bets:52, wins:31, units:7.1,  roi:13.7 },
  { sport:'NHL', bets:28, wins:17, units:3.2,  roi:11.4 },
  { sport:'NFL', bets:18, wins:11, units:2.1,  roi:11.7 },
  { sport:'NCAAB',bets:6, wins: 4, units:0.2,  roi: 3.3 },
];

// ─── Bet log — March 30 – April 6 2026 ───────────────────────────────────────
const MOCK_BETS: Bet[] = [
  // Today (Apr 6) — pending
  { id:'b20', event:'Warriors @ Celtics',      side:'Celtics -4.5',           odds:-112, stake:275, status:'pending', created_at:'2026-04-06T17:30:00Z', book:'DraftKings' },
  { id:'b19', event:'Dodgers @ Giants',        side:'Dodgers ML',             odds:-155, stake:225, status:'pending', created_at:'2026-04-06T17:25:00Z', book:'BetMGM' },
  { id:'b18', event:'OKC Thunder @ Minnesota', side:'Thunder -7',             odds:-110, stake:195, status:'pending', created_at:'2026-04-06T14:00:00Z', book:'FanDuel' },
  // Apr 5 — settled
  { id:'b17', event:'Warriors @ Kings',        side:'Warriors -3.5',          odds:-112, stake:190, pnl:170, status:'won',  created_at:'2026-04-05T22:00:00Z', book:'DraftKings' },
  { id:'b16', event:'Heat @ Celtics',          side:'Under 214',              odds:-110, stake:165, pnl:-165, status:'lost', created_at:'2026-04-05T19:30:00Z', book:'FanDuel' },
  { id:'b15', event:'Dodgers @ Padres',        side:'Dodgers ML',             odds:-138, stake:230, pnl:167, status:'won',  created_at:'2026-04-05T21:10:00Z', book:'BetMGM' },
  { id:'b14', event:'Cubs @ Reds',             side:'Over 8.5',               odds:-108, stake:155, pnl:144, status:'won',  created_at:'2026-04-05T19:05:00Z', book:'Caesars' },
  // Apr 4
  { id:'b13', event:'Nuggets @ Clippers',      side:'Under 220.5',            odds:-112, stake:175, pnl:156, status:'won',  created_at:'2026-04-04T22:00:00Z', book:'DraftKings' },
  { id:'b12', event:'Nets @ Raptors',          side:'Raptors ML',             odds:+118, stake:175, pnl:207, status:'won',  created_at:'2026-04-04T19:30:00Z', book:'FanDuel' },
  { id:'b11', event:'Yankees @ Orioles',       side:'Orioles +1.5 RL',        odds:-122, stake:120, pnl: 98, status:'won',  created_at:'2026-04-04T19:05:00Z', book:'PointsBet' },
  // Apr 3
  { id:'b10', event:'Thunder @ Wolves',        side:'Thunder -7.5',           odds:-110, stake:195, pnl:177, status:'won',  created_at:'2026-04-03T20:00:00Z', book:'BetMGM' },
  { id:'b09', event:'Suns @ Mavericks',        side:'Suns ML',                odds:+135, stake:110, pnl:149, status:'won',  created_at:'2026-04-03T20:30:00Z', book:'Caesars' },
  { id:'b08', event:'Astros @ Rangers',        side:'Over 9.5',               odds:-110, stake:150, pnl:-150, status:'lost', created_at:'2026-04-03T20:05:00Z', book:'DraftKings' },
  // Apr 2
  { id:'b07', event:'Knicks @ Sixers',         side:'Knicks -3',              odds:-112, stake:220, pnl:-220, status:'lost', created_at:'2026-04-02T19:30:00Z', book:'FanDuel' },
  { id:'b06', event:'Cubs @ Cardinals',        side:'Under 8',                odds:-110, stake:110, pnl:100, status:'won',  created_at:'2026-04-02T20:15:00Z', book:'BetMGM' },
  // Apr 1
  { id:'b05', event:'Heat @ Bucks',            side:'Heat +4.5',              odds:-115, stake:165, pnl:-165, status:'lost', created_at:'2026-04-01T20:30:00Z', book:'DraftKings' },
  { id:'b04', event:'Braves @ Mets',           side:'Braves ML',              odds:-128, stake:215, pnl:168, status:'won',  created_at:'2026-04-01T19:10:00Z', book:'FanDuel' },
  { id:'b03', event:'Lightning vs Senators',   side:'Lightning ML',           odds:-115, stake:155, pnl:135, status:'won',  created_at:'2026-04-01T19:30:00Z', book:'Caesars' },
  // Mar 31
  { id:'b02', event:'Suns @ Mavericks',        side:'Over 228.5',             odds:-110, stake:165, pnl:150, status:'won',  created_at:'2026-03-31T20:30:00Z', book:'DraftKings' },
  { id:'b01', event:'Dodgers @ Giants',        side:'Dodgers ML',             odds:-142, stake:225, pnl:158, status:'won',  created_at:'2026-03-31T22:10:00Z', book:'BetMGM' },
];

// ─── Picks Table ──────────────────────────────────────────────────────────────

function PicksPane({
  search, onSelect,
}: { search: string; onSelect: (p: Pick) => void }) {
  const [picks, setPicks]   = useState<Pick[]>(MOCK_PICKS);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    try {
      const r = await fetch(`${API}/picks/today`, { signal: AbortSignal.timeout(5000) });
      const d = await r.json();
      if (Array.isArray(d) && d.length > 0) setPicks(d);
    } catch { /* keep mock */ }
    finally { setLoading(false); setRefreshing(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const filtered = useMemo(() =>
    picks.filter(p =>
      !search || p.event.toLowerCase().includes(search.toLowerCase())
        || p.pick.toLowerCase().includes(search.toLowerCase())
    ), [picks, search]);

  if (loading) return <SkeletonRows n={4} />;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-[10px] text-ink-500 pb-1">
        <span>{filtered.length} picks today</span>
        <button
          className="flex items-center gap-1 hover:text-ink-300 transition-colors"
          onClick={() => load(true)}
        >
          <RefreshCw className={clsx('w-3 h-3', refreshing && 'animate-spin')} />
          Refresh
        </button>
      </div>
      {filtered.length === 0 && <EmptyState msg="No picks match your search." icon={Target} />}
      {filtered.map((pick, i) => (
        <motion.div
          key={pick.id}
          initial={{ opacity: 0, x: -8 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: i * 0.04 }}
        >
          <div
            className="group flex items-start gap-4 p-4 rounded-xl bg-ink-900/50 border border-ink-800
              hover:border-ink-700 hover:bg-ink-850/70 cursor-pointer transition-all duration-150"
            onClick={() => onSelect(pick)}
          >
            <SportPill sport={pick.sport} />
            <div className="flex-1 min-w-0">
              <div className="flex items-start gap-2 flex-wrap">
                <span className="text-sm font-semibold text-ink-100 leading-tight">{pick.pick}</span>
                <VerdictBadge verdict={pick.verdict} />
              </div>
              <div className="text-[11px] text-ink-500 mt-0.5 truncate">{pick.event}</div>
              {pick.book && (
                <div className="text-[10px] text-ink-600 mt-0.5">{pick.book}</div>
              )}
            </div>
            <div className="text-right shrink-0 space-y-1">
              <OddsChip odds={pick.odds} />
              <div className="text-[10px] text-ink-500">{(pick.confidence).toFixed(0)}% conf</div>
              <div className={clsx('text-[10px] font-semibold', pick.edge_pct >= 5 ? 'text-edge-green' : 'text-edge-gold')}>
                +{pick.edge_pct.toFixed(1)}% edge
              </div>
            </div>
            <ChevronRight className="w-4 h-4 text-ink-700 group-hover:text-ink-400 transition-colors mt-0.5 shrink-0" />
          </div>
        </motion.div>
      ))}
    </div>
  );
}

// ─── Arb Pane ─────────────────────────────────────────────────────────────────

function ArbPane({ search, onSelect }: { search: string; onSelect: (a: Arb) => void }) {
  const [arbs, setArbs]   = useState<Arb[]>(MOCK_ARB);
  const [loading, setLoading] = useState(true);
  const [runningAuto, setRunningAuto] = useState(false);

  useEffect(() => {
    fetch(`${API}/arb/live`, { signal: AbortSignal.timeout(5000) })
      .then(r => r.json())
      .then(d => { if (Array.isArray(d) && d.length) setArbs(d); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const filtered = useMemo(() =>
    arbs.filter(a => !search || a.event.toLowerCase().includes(search.toLowerCase())),
    [arbs, search]);

  const runAutoArb = useCallback(async () => {
    setRunningAuto(true);
    try {
      await fetch(`${API}/arb/auto`, { method: 'POST', signal: AbortSignal.timeout(10000) });
    } catch {}
    finally { setRunningAuto(false); }
  }, []);

  if (loading) return <SkeletonRows n={3} />;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-ink-500">{filtered.length} live opportunities</span>
        <button
          onClick={runAutoArb}
          disabled={runningAuto}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold
            bg-edge-green/10 border border-edge-green/25 text-edge-green
            hover:bg-edge-green/15 disabled:opacity-50 transition-all"
        >
          <Zap className="w-3 h-3" />
          {runningAuto ? 'Scanning…' : 'Auto-Scan'}
        </button>
      </div>
      {filtered.length === 0 && <EmptyState msg="No arb opportunities found right now." icon={GitMerge} />}
      {filtered.map(arb => (
        <div
          key={arb.id}
          className="group p-4 rounded-xl bg-edge-green/5 border border-edge-green/15
            hover:border-edge-green/30 cursor-pointer transition-all"
          onClick={() => onSelect(arb)}
        >
          <div className="flex items-start justify-between gap-4">
            <div className="flex-1">
              <div className="flex items-center gap-2 mb-1">
                <SportPill sport={arb.sport} />
                <Badge v="green" className="font-mono">+{arb.profit_pct.toFixed(2)}%</Badge>
              </div>
              <p className="text-sm font-semibold text-ink-100">{arb.event}</p>
              <div className="mt-2 space-y-1 text-xs text-ink-400">
                <div className="flex items-center gap-2">
                  <span className="w-20 text-ink-500">{arb.book_a}</span>
                  <span className="font-medium text-ink-200">{arb.side_a}</span>
                  <OddsChip odds={arb.odds_a} />
                </div>
                <div className="flex items-center gap-2">
                  <span className="w-20 text-ink-500">{arb.book_b}</span>
                  <span className="font-medium text-ink-200">{arb.side_b}</span>
                  <OddsChip odds={arb.odds_b} />
                </div>
              </div>
            </div>
            <div className="text-right shrink-0">
              <div className="text-lg font-bold text-edge-green font-mono">+{arb.profit_pct.toFixed(2)}%</div>
              <div className="text-[10px] text-ink-500">${arb.stake_100.toFixed(0)} / $100</div>
              <ChevronRight className="w-4 h-4 text-ink-600 group-hover:text-ink-400 ml-auto mt-1 transition-colors" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Steam / Sharp Moves Pane ─────────────────────────────────────────────────

function SteamPane({ search }: { search: string }) {
  const [moves, setMoves] = useState<SharpMove[]>(MOCK_STEAM);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API}/sharp/moves`, { signal: AbortSignal.timeout(5000) })
      .then(r => r.json())
      .then(d => { if (Array.isArray(d) && d.length) setMoves(d); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const filtered = useMemo(() =>
    moves.filter(m => !search || m.event.toLowerCase().includes(search.toLowerCase())),
    [moves, search]);

  if (loading) return <SkeletonRows n={3} />;

  return (
    <div className="space-y-2">
      {filtered.length === 0 && <EmptyState msg="No sharp moves detected recently." icon={Flame} />}
      {filtered.map(move => (
        <div
          key={move.id}
          className="flex items-center gap-4 p-4 rounded-xl bg-orange-500/5 border border-orange-500/15 hover:border-orange-500/25 transition-all"
        >
          <div className={clsx(
            'shrink-0 px-2 py-0.5 rounded text-[9px] font-bold uppercase tracking-widest',
            move.severity === 'HIGH' ? 'bg-edge-red/15 text-edge-red border border-edge-red/25' : 'bg-edge-gold/15 text-edge-gold border border-edge-gold/25',
          )}>
            {move.severity}
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-ink-200">{move.description}</p>
            <p className="text-[10px] text-ink-500 mt-0.5">{move.event}</p>
          </div>
          <div className="text-right shrink-0 space-y-0.5">
            <div className="text-xs font-mono font-semibold text-orange-400">{move.line_move > 0 ? '+' : ''}{move.line_move} pts</div>
            <div className="text-[9px] text-ink-500">{move.pct_money}% money</div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Performance Pane ─────────────────────────────────────────────────────────

function PerformancePane() {
  const [stats, setStats] = useState<PerfStat[]>(MOCK_PERF);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API}/performance/breakdown`, { signal: AbortSignal.timeout(5000) })
      .then(r => r.json())
      .then(d => { if (Array.isArray(d) && d.length) setStats(d); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <SkeletonRows n={4} />;

  return (
    <div className="space-y-1.5">
      <div className="grid grid-cols-5 gap-2 text-[9px] font-semibold text-ink-500 uppercase tracking-widest px-1 pb-1 border-b border-ink-800">
        <div>Sport</div>
        <div className="text-right">Bets</div>
        <div className="text-right">W/L</div>
        <div className="text-right">Units</div>
        <div className="text-right">ROI</div>
      </div>
      {stats.map(s => (
        <div key={s.sport} className="grid grid-cols-5 gap-2 items-center py-2.5 px-1 rounded-lg hover:bg-ink-850 transition-colors">
          <div className="text-xs font-semibold text-ink-200">{s.sport}</div>
          <div className="text-xs font-mono text-right text-ink-400">{s.bets}</div>
          <div className="text-xs font-mono text-right text-ink-400">{s.wins}-{s.bets - s.wins}</div>
          <div className={clsx('text-xs font-mono text-right font-semibold', s.units >= 0 ? 'text-edge-green' : 'text-edge-red')}>
            {s.units >= 0 ? '+' : ''}{s.units.toFixed(1)}u
          </div>
          <div className={clsx('text-xs font-mono text-right font-semibold', s.roi >= 0 ? 'text-edge-green' : 'text-edge-red')}>
            {s.roi >= 0 ? '+' : ''}{s.roi.toFixed(1)}%
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Bet Log Pane ─────────────────────────────────────────────────────────────

const STATUS_STYLES = {
  won:     'bg-edge-green/15 text-edge-green border-edge-green/25',
  lost:    'bg-edge-red/15   text-edge-red   border-edge-red/25',
  push:    'bg-ink-700       text-ink-400    border-ink-600',
  pending: 'bg-edge-gold/15  text-edge-gold  border-edge-gold/25',
};

function BetLogPane({ search, onSelect }: { search: string; onSelect: (b: Bet) => void }) {
  const [bets, setBets]   = useState<Bet[]>(MOCK_BETS);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API}/bets`, { signal: AbortSignal.timeout(5000) })
      .then(r => r.json())
      .then(d => { if (Array.isArray(d) && d.length) setBets(d); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const filtered = useMemo(() =>
    bets.filter(b => !search || b.event.toLowerCase().includes(search.toLowerCase())),
    [bets, search]);

  if (loading) return <SkeletonRows n={4} />;

  return (
    <div className="space-y-1.5">
      {filtered.length === 0 && <EmptyState msg="No bets found." icon={BookOpen} />}
      {filtered.map(bet => (
        <div
          key={bet.id}
          className="group flex items-center gap-4 py-3 px-4 rounded-xl hover:bg-ink-850 cursor-pointer transition-all border border-transparent hover:border-ink-800"
          onClick={() => onSelect(bet)}
        >
          <span className={clsx('shrink-0 px-2 py-0.5 rounded border text-[9px] font-bold uppercase tracking-widest', STATUS_STYLES[bet.status])}>
            {bet.status}
          </span>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-ink-200 truncate">{bet.side}</p>
            <p className="text-[10px] text-ink-500 truncate">{bet.event} · {bet.book}</p>
          </div>
          <div className="text-right shrink-0">
            <div className="text-xs font-mono text-ink-300">${bet.stake}</div>
            {bet.pnl !== undefined && bet.status !== 'pending' && (
              <div className={clsx('text-xs font-mono font-bold', bet.pnl >= 0 ? 'text-edge-green' : 'text-edge-red')}>
                {bet.pnl >= 0 ? '+' : ''}${bet.pnl}
              </div>
            )}
          </div>
          <ChevronRight className="w-3.5 h-3.5 text-ink-700 group-hover:text-ink-400 transition-colors shrink-0" />
        </div>
      ))}
    </div>
  );
}

// ─── Line Shop Pane (stub) ────────────────────────────────────────────────────

function LineShopPane({ search }: { search: string }) {
  return (
    <EmptyState msg="Line shop compares odds across DraftKings, FanDuel, BetMGM, Caesars. Connect your API to populate." icon={BarChart} />
  );
}

// ─── AI Chat Pane ─────────────────────────────────────────────────────────────

function AIChatPane() {
  const [msgs, setMsgs]   = useState<Array<{ role: 'user' | 'ai'; content: string }>>([
    { role: 'ai', content: "I'm your betting intelligence assistant. Ask me about any pick, market, or strategy." },
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const endRef = React.useRef<HTMLDivElement>(null);

  const send = async () => {
    if (!input.trim()) return;
    const q = input.trim();
    setInput('');
    setMsgs(m => [...m, { role: 'user', content: q }]);
    setLoading(true);
    try {
      const r = await fetch(`${API}/ai/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: q }),
        signal: AbortSignal.timeout(30000),
      });
      const d = await r.json();
      setMsgs(m => [...m, { role: 'ai', content: d.response || d.message || 'No response received.' }]);
    } catch (e: any) {
      setMsgs(m => [...m, { role: 'ai', content: `Error: ${e?.message ?? 'Connection failed'}` }]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [msgs]);

  return (
    <div className="flex flex-col h-full min-h-[400px]">
      <div className="flex-1 overflow-y-auto space-y-3 pr-1 min-h-0 max-h-[500px]">
        {msgs.map((m, i) => (
          <div key={i} className={clsx('flex', m.role === 'user' ? 'justify-end' : 'justify-start')}>
            <div className={clsx(
              'max-w-[80%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed',
              m.role === 'user'
                ? 'bg-edge-blue/15 border border-edge-blue/25 text-ink-200 rounded-br-sm'
                : 'bg-ink-850 border border-ink-800 text-ink-300 rounded-bl-sm',
            )}>
              {m.content}
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex justify-start">
            <div className="bg-ink-850 border border-ink-800 rounded-2xl rounded-bl-sm px-4 py-3">
              <div className="flex gap-1">
                {[0,1,2].map(i => (
                  <span key={i} className="w-1.5 h-1.5 bg-edge-blue/50 rounded-full animate-bounce" style={{ animationDelay: `${i*0.15}s` }} />
                ))}
              </div>
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>
      <div className="mt-3 flex gap-2">
        <input
          type="text"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send()}
          placeholder="Ask about any pick or market…"
          className="input-field flex-1 text-sm"
        />
        <button
          onClick={send}
          disabled={!input.trim() || loading}
          className="px-4 py-2 rounded-xl text-sm font-semibold bg-edge-blue/20 border border-edge-blue/30
            text-edge-blue hover:bg-edge-blue/25 disabled:opacity-40 transition-all"
        >
          Send
        </button>
      </div>
    </div>
  );
}

// ─── IntelligenceTabs (main export) ──────────────────────────────────────────

interface Props {
  onPickSelect?: (p: Pick) => void;
  onArbSelect?:  (a: Arb)  => void;
  onBetSelect?:  (b: Bet)  => void;
}

export default function IntelligenceTabs({ onPickSelect, onArbSelect, onBetSelect }: Props) {
  const [active, setActive] = useState<TabId>('picks');
  const [search, setSearch] = useState('');

  const currentTab = TABS.find(t => t.id === active)!;

  return (
    <GlassPanel padding="none" className="flex flex-col">
      {/* Tab bar */}
      <div className="flex items-center gap-0 border-b border-ink-800 px-2 pt-1 overflow-x-auto">
        {TABS.map(tab => (
          <button
            key={tab.id}
            onClick={() => { setActive(tab.id); setSearch(''); }}
            className={clsx(
              'flex items-center gap-1.5 px-3 py-2.5 text-xs font-medium transition-all duration-150 whitespace-nowrap',
              'border-b-2 -mb-px',
              active === tab.id
                ? 'border-edge-green text-edge-green'
                : 'border-transparent text-ink-500 hover:text-ink-300',
            )}
          >
            <tab.icon className="w-3.5 h-3.5" />
            {tab.label}
          </button>
        ))}
      </div>

      {/* Search / filter bar */}
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-ink-800">
        <div className="flex items-center gap-2 flex-1 bg-ink-850 border border-ink-800 rounded-lg px-2.5 py-1.5">
          <Search className="w-3 h-3 text-ink-600 shrink-0" />
          <input
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder={`Search ${currentTab.label.toLowerCase()}…`}
            className="text-xs text-ink-300 bg-transparent outline-none placeholder:text-ink-600 flex-1 w-full"
          />
        </div>
        <div className="text-[10px] text-ink-600 hidden sm:block">
          {currentTab.description}
        </div>
      </div>

      {/* Pane content */}
      <div className="flex-1 p-4 overflow-y-auto min-h-0 max-h-[520px]">
        <AnimatePresence mode="wait">
          <motion.div
            key={active}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.2 }}
          >
            {active === 'picks'       && <PicksPane       search={search} onSelect={onPickSelect ?? (() => {})} />}
            {active === 'arb'         && <ArbPane         search={search} onSelect={onArbSelect  ?? (() => {})} />}
            {active === 'lineshop'    && <LineShopPane    search={search} />}
            {active === 'steam'       && <SteamPane       search={search} />}
            {active === 'performance' && <PerformancePane />}
            {active === 'betlog'      && <BetLogPane      search={search} onSelect={onBetSelect ?? (() => {})} />}
            {active === 'ai'          && <AIChatPane />}
          </motion.div>
        </AnimatePresence>
      </div>
    </GlassPanel>
  );
}
