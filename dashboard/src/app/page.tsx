'use client';

import { useState, useEffect, useCallback } from 'react';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';
import {
  TrendingUp, DollarSign, Target, Zap, RefreshCw,
  AlertTriangle, CheckCircle, Activity, BarChart2,
  ArrowUpRight, ArrowDownRight, Clock, Loader2
} from 'lucide-react';
import clsx from 'clsx';

const API = process.env.NEXT_PUBLIC_MCP_API_URL || 'http://localhost:8420';

// ─── Types ─────────────────────────────────────────────────────────────────────

interface Bankroll {
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

interface Pick {
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

interface ArbOpportunity {
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

interface Bet {
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

// ─── Data Fetching Hook ────────────────────────────────────────────────────────

function useApi<T>(endpoint: string, initialValue: T, intervalMs = 0) {
  const [data, setData] = useState<T>(initialValue);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetch_ = useCallback(async () => {
    try {
      const res = await fetch(`${API}${endpoint}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed');
    } finally {
      setLoading(false);
    }
  }, [endpoint]);

  useEffect(() => {
    fetch_();
    if (intervalMs > 0) {
      const id = setInterval(fetch_, intervalMs);
      return () => clearInterval(id);
    }
  }, [fetch_, intervalMs]);

  return { data, loading, error, refetch: fetch_ };
}

// ─── Micro components ──────────────────────────────────────────────────────────

function Trend({ positive }: { positive: boolean }) {
  return positive
    ? <ArrowUpRight className="w-3.5 h-3.5 text-edge-green inline" />
    : <ArrowDownRight className="w-3.5 h-3.5 text-edge-red inline" />;
}

function SportPill({ sport }: { sport: string }) {
  const key = sport.toLowerCase()
    .replace(/americanfootball_|basketball_|baseball_|icehockey_/, '');
  const map: Record<string, string> = {
    nfl: 'badge-gold', nba: 'badge-red', mlb: 'badge-blue', nhl: 'badge-cyan',
  };
  return <span className={clsx('badge uppercase font-bold tracking-wide', map[key] ?? 'badge-ink')}>{key}</span>;
}

function VerdictBadge({ verdict }: { verdict: string }) {
  const v = (verdict ?? '').toUpperCase();
  if (v.includes('STRONG') || v.includes('EXCELLENT')) return <span className="badge-green">{verdict}</span>;
  if (v.includes('GOOD')) return <span className="badge-blue">{verdict}</span>;
  if (v.includes('MARGINAL')) return <span className="badge-gold">{verdict}</span>;
  return <span className="badge-red">{verdict}</span>;
}

function OddsChip({ odds }: { odds: number }) {
  return (
    <span className={clsx('font-mono font-semibold text-sm', odds > 0 ? 'text-edge-green' : 'text-ink-200')}>
      {odds > 0 ? `+${odds}` : odds}
    </span>
  );
}

function BetResult({ result }: { result?: string }) {
  if (result === 'win')  return <span className="badge-green">WIN</span>;
  if (result === 'loss') return <span className="badge-red">LOSS</span>;
  if (result === 'push') return <span className="badge-gold">PUSH</span>;
  return <span className="text-ink-500 text-xs italic">Open</span>;
}

function EmptyState({ msg }: { msg: string }) {
  return (
    <div className="py-10 flex flex-col items-center gap-2 text-ink-500">
      <Activity className="w-8 h-8 opacity-30" />
      <p className="text-sm text-center max-w-xs">{msg}</p>
    </div>
  );
}

function Spinner() {
  return (
    <div className="py-8 flex items-center justify-center">
      <Loader2 className="w-5 h-5 animate-spin text-edge-green/60" />
    </div>
  );
}

function SectionTitle({ icon: Icon, iconCls, title, children }: {
  icon: React.ElementType; iconCls: string; title: string; children?: React.ReactNode;
}) {
  return (
    <div className="section-title">
      <div className={clsx('section-title-icon', iconCls)}>
        <Icon className="w-4 h-4" />
      </div>
      <span className="section-title-text">{title}</span>
      {children}
    </div>
  );
}

// ─── Live dot ─────────────────────────────────────────────────────────────────

function LiveDot() {
  const [status, setStatus] = useState<'live' | 'connecting' | 'offline'>('connecting');
  useEffect(() => {
    let ws: WebSocket;
    try {
      ws = new WebSocket('ws://localhost:8420/ws/live');
      ws.onopen  = () => setStatus('live');
      ws.onclose = () => setStatus('offline');
      ws.onerror = () => setStatus('offline');
    } catch { setStatus('offline'); }
    return () => ws?.close();
  }, []);
  const cls =
    status === 'live'       ? { dot: 'bg-edge-green live-dot', text: 'text-edge-green' } :
    status === 'connecting' ? { dot: 'bg-edge-gold animate-pulse', text: 'text-edge-gold' } :
                              { dot: 'bg-edge-red', text: 'text-edge-red' };
  return (
    <div className="flex items-center gap-1.5">
      <span className={clsx('w-1.5 h-1.5 rounded-full', cls.dot)} />
      <span className={clsx('text-xs font-semibold tracking-wider uppercase', cls.text)}>{status}</span>
    </div>
  );
}

// ─── Bankroll Ticker ──────────────────────────────────────────────────────────

function BankrollTicker() {
  const { data: bk, loading } = useApi<Bankroll>('/bankroll', {
    current_bankroll: 10000, starting_bankroll: 10000, total_profit: 0,
    roi_pct: 0, win_rate: 0, total_bets: 0, open_bets: 0,
    max_drawdown: 0, clv_avg: 0, high_water_mark: 10000, daily_pnl: 0,
  }, 30000);

  const roi   = bk.roi_pct  ?? 0;
  const pnl   = bk.total_profit ?? 0;
  const daily = bk.daily_pnl ?? 0;
  const wr    = (bk.win_rate ?? 0) * 100;
  const clv   = bk.clv_avg ?? 0;

  const items = [
    { label: 'BANKROLL', value: `$${(bk.current_bankroll ?? 0).toLocaleString('en-US', { maximumFractionDigits: 0 })}`, sub: `HWM $${(bk.high_water_mark ?? 0).toLocaleString('en-US', { maximumFractionDigits: 0 })}`, up: (bk.current_bankroll ?? 0) >= (bk.starting_bankroll ?? 0), color: (bk.current_bankroll ?? 0) >= (bk.starting_bankroll ?? 0) ? 'text-edge-green' : 'text-edge-red' },
    { label: 'TOTAL P&L', value: `${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(0)}`, sub: `ROI ${roi >= 0 ? '+' : ''}${roi.toFixed(1)}%`, up: pnl >= 0, color: pnl >= 0 ? 'text-edge-green' : 'text-edge-red' },
    { label: 'WIN RATE', value: `${wr.toFixed(1)}%`, sub: `${bk.total_bets ?? 0} bets total`, up: wr >= 53, color: wr >= 53 ? 'text-edge-green' : 'text-edge-gold' },
    { label: 'TODAY P&L', value: `${daily >= 0 ? '+' : ''}$${Math.abs(daily).toFixed(0)}`, sub: `${bk.open_bets ?? 0} open`, up: daily >= 0, color: daily >= 0 ? 'text-edge-green' : 'text-edge-red' },
    { label: 'CLV AVG', value: `${clv >= 0 ? '+' : ''}${clv.toFixed(2)}%`, sub: `Max DD: ${(bk.max_drawdown ?? 0).toFixed(1)}%`, up: clv >= 0, color: clv >= 0 ? 'text-edge-green' : 'text-edge-red' },
  ];

  return (
    <div className="grid grid-cols-5 gap-2">
      {items.map(({ label, value, sub, up, color }) => (
        <div key={label} className="stat-card">
          <div className="stat-label">{label}</div>
          <div className={clsx('stat-value mt-0.5', color)}>
            {loading
              ? <div className="skeleton rounded w-20 h-6" />
              : <span className="flex items-center gap-0.5"><span className="font-mono">{value}</span><Trend positive={up} /></span>
            }
          </div>
          <div className="stat-sub">{loading ? <div className="skeleton rounded w-14 h-3 mt-1" /> : sub}</div>
        </div>
      ))}
    </div>
  );
}

// ─── Equity Curve ─────────────────────────────────────────────────────────────

function EquityCurve() {
  const { data, loading } = useApi<{ history: Array<{ date: string; bankroll: number; roi_pct: number }> }>(
    '/bankroll/history', { history: [] }, 120000
  );
  const chartData = (data.history?.length > 0 ? data.history : [{ date: 'Day 1', bankroll: 10000, roi_pct: 0 }])
    .map(s => ({ date: s.date?.slice(5) ?? s.date, bankroll: s.bankroll, roi: s.roi_pct }));
  const isProfit = chartData.length > 1 && chartData[chartData.length - 1].bankroll >= chartData[0].bankroll;
  return (
    <div className="card h-full">
      <SectionTitle icon={TrendingUp} iconCls="bg-edge-green/10 text-edge-green" title="Equity Curve">
        <span className={clsx('badge ml-auto', isProfit ? 'badge-green' : 'badge-red')}>
          {isProfit ? 'Profitable' : 'Drawdown'}
        </span>
      </SectionTitle>
      {loading
        ? <div className="skeleton rounded-xl w-full h-[160px]" />
        : (
          <ResponsiveContainer width="100%" height={160}>
            <AreaChart data={chartData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
              <defs>
                <linearGradient id="bkGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#00e87a" stopOpacity={0.22} />
                  <stop offset="95%" stopColor="#00e87a" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.04)" />
              <XAxis dataKey="date" stroke="rgba(255,255,255,0.04)" tick={{ fontSize: 9, fill: '#4a6580' }} />
              <YAxis stroke="rgba(255,255,255,0.04)" tick={{ fontSize: 9, fill: '#4a6580' }}
                tickFormatter={v => `$${(v / 1000).toFixed(0)}k`} />
              <Tooltip
                contentStyle={{
                  background: 'rgba(6, 14, 28, 0.92)',
                  backdropFilter: 'blur(16px)',
                  border: '1px solid rgba(255,255,255,0.09)',
                  borderRadius: 10, fontSize: 11,
                  boxShadow: '0 8px 24px rgba(0,0,0,0.6)',
                }}
                labelStyle={{ color: '#8faac0' }}
                formatter={(v: number) => [`$${v.toLocaleString()}`, 'Bankroll']}
              />
              <Area type="monotone" dataKey="bankroll" stroke="#00e87a" fill="url(#bkGrad)"
                strokeWidth={1.5} dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        )
      }
    </div>
  );
}

// ─── Kelly Calculator ─────────────────────────────────────────────────────────

function KellyCalc() {
  const [prob,     setProb]     = useState('55');
  const [odds,     setOdds]     = useState('-110');
  const [bankroll, setBankroll] = useState('10000');
  const [result,   setResult]   = useState<Record<string, number | string> | null>(null);
  const [busy,     setBusy]     = useState(false);

  const calculate = async () => {
    setBusy(true);
    try {
      const res = await fetch(`${API}/kelly`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ our_prob: parseFloat(prob) / 100, american_odds: parseInt(odds), bankroll: parseFloat(bankroll) }),
      });
      setResult(await res.json());
    } catch { setResult({ error: 'offline' }); }
    finally   { setBusy(false); }
  };

  return (
    <div className="card h-full flex flex-col gap-3">
      <SectionTitle icon={Target} iconCls="bg-edge-gold/10 text-edge-gold" title="Kelly Calculator" />
      <div className="grid grid-cols-3 gap-2">
        {[
          { label: 'Win Prob %', val: prob,     set: setProb,     ph: '55' },
          { label: 'Odds (US)',  val: odds,     set: setOdds,     ph: '-110' },
          { label: 'Bankroll',   val: bankroll, set: setBankroll, ph: '10000' },
        ].map(({ label, val, set, ph }) => (
          <div key={label}>
            <label className="stat-label block mb-1">{label}</label>
            <input type="number" value={val} placeholder={ph}
              onChange={e => set(e.target.value)} className="input-field py-1.5 text-sm" />
          </div>
        ))}
      </div>
      <button onClick={calculate} disabled={busy} className="btn-primary">
        {busy ? <><Loader2 className="w-3.5 h-3.5 animate-spin inline mr-1.5" />Calculating…</> : 'Calculate Kelly Stake'}
      </button>
      {result && !('error' in result) && (
        <div className="grid grid-cols-2 gap-2">
          {[
            { label: 'Stake',   val: `$${Number(result.recommended_stake).toFixed(2)}`,  cls: 'text-edge-green' },
            { label: 'Edge',    val: `+${Number(result.edge_pct).toFixed(2)}%`,           cls: 'text-edge-gold' },
            { label: 'Kelly %', val: `${Number(result.kelly_pct).toFixed(3)}%`,           cls: 'text-edge-blue' },
            { label: 'Verdict', val: String(result.verdict),                              cls: 'text-ink-100' },
          ].map(({ label, val, cls }) => (
            <div key={label} className="card-sm">
              <div className="stat-label">{label}</div>
              <div className={clsx('font-mono font-semibold text-base mt-0.5', cls)}>{val}</div>
            </div>
          ))}
        </div>
      )}
      {result?.error && <p className="text-edge-red text-xs">Server offline — check terminal</p>}
    </div>
  );
}

// ─── Today's Picks ────────────────────────────────────────────────────────────

function TodaysPicks() {
  const { data, loading, error, refetch } = useApi<{
    top_picks: Pick[]; total_picks: number; sports_covered: string[]; run_at?: string;
  }>('/picks/today', { top_picks: [], total_picks: 0, sports_covered: [] }, 300000);

  return (
    <>
      <SectionTitle icon={Zap} iconCls="bg-edge-gold/10 text-edge-gold" title="Today's Intelligence">
        <span className="badge-blue ml-1">{data.total_picks} picks</span>
        {data.sports_covered?.map(s => <SportPill key={s} sport={s} />)}
        <button onClick={refetch} className="ml-auto text-ink-500 hover:text-ink-200 transition-colors p-1">
          <RefreshCw className="w-3.5 h-3.5" />
        </button>
      </SectionTitle>
      {loading && <Spinner />}
      {error && (
        <div className="flex items-center gap-2 text-edge-gold text-sm py-4">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          MCP server offline — run <code className="text-edge-green bg-ink-800 px-1.5 py-0.5 rounded text-xs ml-1">python mcp/server.py</code>
        </div>
      )}
      {!loading && !error && !(data.top_picks?.length > 0) && (
        <EmptyState msg="No value edges found. Add your Odds API key or run: python workflows/daily_picks.py" />
      )}
      {!loading && data.top_picks?.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th className="w-6">#</th>
              <th>Sport</th>
              <th>Event &amp; Pick</th>
              <th className="text-right">Odds</th>
              <th className="text-right">Edge</th>
              <th className="text-right">EV</th>
              <th className="text-right">Stake</th>
              <th>Verdict</th>
            </tr>
          </thead>
          <tbody>
            {data.top_picks.slice(0, 15).map((p, i) => (
              <tr key={i}>
                <td className="text-ink-500 font-mono text-xs">{i + 1}</td>
                <td><SportPill sport={p.sport} /></td>
                <td>
                  <div className="text-ink-100 font-medium leading-snug">{p.event}</div>
                  <div className="text-[11px] mt-0.5 text-ink-400">
                    <span className="text-ink-200 font-semibold">{p.pick}</span>
                    <span className="mx-1 text-ink-600">·</span>
                    <span>{p.market}</span>
                    <span className="mx-1 text-ink-600">·</span>
                    <span className="text-ink-500 uppercase">{p.book}</span>
                  </div>
                </td>
                <td className="text-right"><OddsChip odds={p.american_odds} /></td>
                <td className="text-right font-mono text-edge-green font-semibold">+{p.edge_pct?.toFixed(2)}%</td>
                <td className="text-right font-mono text-edge-green/70">+{p.ev_pct?.toFixed(2)}%</td>
                <td className="text-right">
                  <span className="font-mono text-edge-gold font-semibold">${p.recommended_stake?.toFixed(0)}</span>
                  <div className="text-ink-500 text-[10px] font-mono">{p.kelly_pct?.toFixed(2)}% K</div>
                </td>
                <td><VerdictBadge verdict={p.verdict} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data.run_at && (
        <div className="flex items-center gap-1 text-ink-600 text-xs mt-3">
          <Clock className="w-3 h-3" /> Last run: {new Date(data.run_at).toLocaleTimeString()}
        </div>
      )}
    </>
  );
}

// ─── Arbitrage Panel ──────────────────────────────────────────────────────────

function ArbPanel() {
  const { data: resp, loading } = useApi<{ bets: ArbOpportunity[] }>(
    '/bets?market=arb&limit=20', { bets: [] }, 60000
  );
  const arbs = resp?.bets ?? [];
  return (
    <>
      <SectionTitle icon={BarChart2} iconCls="bg-edge-blue/10 text-edge-blue" title="Arbitrage">
        <span className="badge-green ml-1">Guaranteed Profit</span>
      </SectionTitle>
      {loading && <Spinner />}
      {!loading && arbs.length === 0 && (
        <EmptyState msg="No arbs live. Run: python workflows/arbitrage_scan.py" />
      )}
      <div className="space-y-2">
        {arbs.slice(0, 8).map((arb, i) => (
          <div key={i} className="card-sm border border-edge-green/15 hover:border-edge-green/35 transition-colors">
            <div className="flex items-start justify-between gap-2 mb-1">
              <span className="text-sm font-semibold text-ink-100 leading-tight">{arb.event}</span>
              {arb.profit_pct    != null && <span className="badge-green font-bold shrink-0">+{arb.profit_pct.toFixed(2)}%</span>}
              {arb.potential_edge_pct != null && <span className="badge-blue font-bold shrink-0">+{arb.potential_edge_pct.toFixed(2)}%</span>}
            </div>
            {arb.leg_a && arb.leg_b && (
              <div className="text-[11px] text-ink-400 space-y-0.5 font-mono">
                <div className="flex gap-2">
                  <span className="text-ink-500 w-3">A</span>
                  <span>{arb.leg_a.side}</span>
                  <span className="text-ink-200">@ {arb.leg_a.odds.toFixed(3)}</span>
                  <span className="text-ink-500">{arb.leg_a.book}</span>
                  <span className="text-edge-gold ml-auto">${arb.leg_a.stake?.toFixed(0)}</span>
                </div>
                <div className="flex gap-2">
                  <span className="text-ink-500 w-3">B</span>
                  <span>{arb.leg_b.side}</span>
                  <span className="text-ink-200">@ {arb.leg_b.odds.toFixed(3)}</span>
                  <span className="text-ink-500">{arb.leg_b.book}</span>
                  <span className="text-edge-gold ml-auto">${arb.leg_b.stake?.toFixed(0)}</span>
                </div>
                {arb.guaranteed_profit != null && (
                  <div className="text-edge-green font-semibold pt-1">✓ Guaranteed +${arb.guaranteed_profit.toFixed(2)}</div>
                )}
              </div>
            )}
            {arb.action && <div className="text-edge-cyan text-xs mt-1">{arb.action}</div>}
          </div>
        ))}
      </div>
    </>
  );
}

// ─── Bet Log ──────────────────────────────────────────────────────────────────

function BetLog() {
  const { data: resp } = useApi<{ bets: Bet[] }>('/bets?limit=20', { bets: [] }, 60000);
  const bets = resp?.bets ?? [];
  return (
    <>
      <SectionTitle icon={CheckCircle} iconCls="bg-edge-purple/10 text-edge-purple" title="Bet Log">
        <span className="badge-ink ml-1">{bets.length} bets</span>
      </SectionTitle>
      {bets.length === 0 && <EmptyState msg="No bets tracked yet. Picks auto-log here once placed." />}
      {bets.length > 0 && (
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Sport</th>
                <th>Event</th>
                <th>Pick / Market</th>
                <th className="text-right">Odds</th>
                <th className="text-right">Stake</th>
                <th className="text-right">Result</th>
                <th className="text-right">P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {bets.map(bet => (
                <tr key={bet.id}>
                  <td><SportPill sport={bet.sport} /></td>
                  <td className="text-ink-200 max-w-[160px] truncate">{bet.event}</td>
                  <td>
                    <span className="text-ink-100 font-medium">{bet.pick}</span>
                    <span className="text-ink-500 ml-1 text-[10px]">{bet.market}</span>
                  </td>
                  <td className="text-right"><OddsChip odds={bet.american_odds} /></td>
                  <td className="text-right font-mono text-ink-200">${bet.stake?.toFixed(0)}</td>
                  <td className="text-right"><BetResult result={bet.result} /></td>
                  <td className={clsx('text-right font-mono font-semibold',
                    bet.pnl == null ? 'text-ink-600' : bet.pnl >= 0 ? 'text-edge-green' : 'text-edge-red'
                  )}>
                    {bet.pnl != null ? `${bet.pnl >= 0 ? '+' : ''}$${bet.pnl.toFixed(0)}` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

// ─── Quick Actions ────────────────────────────────────────────────────────────

function QuickActions() {
  return (
    <div className="card">
      <SectionTitle icon={Activity} iconCls="bg-edge-blue/10 text-edge-blue" title="Quick Actions" />
      <div className="space-y-2 mb-4">
        {[
          { label: 'Run Daily Picks',   cmd: 'python workflows/daily_picks.py',    color: 'text-edge-green' },
          { label: 'Start Arb Scanner', cmd: 'python workflows/arbitrage_scan.py', color: 'text-edge-blue' },
          { label: 'Live Monitor',      cmd: 'python workflows/live_monitor.py',   color: 'text-edge-gold' },
          { label: 'MCP API Server',    cmd: 'python mcp/server.py',               color: 'text-edge-purple' },
        ].map(({ label, cmd, color }) => (
          <div key={cmd}
            className="flex items-center justify-between py-2 px-3 rounded-xl transition-all duration-200"
            style={{
              background: 'rgba(6, 14, 28, 0.5)',
              border: '1px solid rgba(255,255,255,0.06)',
            }}
            onMouseEnter={e => (e.currentTarget.style.borderColor = 'rgba(255,255,255,0.1)')}
            onMouseLeave={e => (e.currentTarget.style.borderColor = 'rgba(255,255,255,0.06)')}
          >
            <span className="text-sm text-ink-200">{label}</span>
            <code className={clsx('text-xs font-mono bg-ink-900 px-2 py-0.5 rounded', color)}>{cmd}</code>
          </div>
        ))}
      </div>
      <div className="divider pt-4">
        <p className="stat-label mb-2">API Endpoints</p>
        {[
          { method: 'GET', path: '/picks/today',      color: 'text-edge-green' },
          { method: 'GET', path: '/bankroll',         color: 'text-edge-blue' },
          { method: 'GET', path: '/bankroll/history', color: 'text-edge-blue' },
          { method: 'WS',  path: '/ws/live',          color: 'text-edge-gold' },
        ].map(({ method, path, color }) => (
          <div key={path} className="flex gap-2 text-[11px] font-mono py-0.5">
            <span className="text-ink-500 w-8">{method}</span>
            <span className={color}>localhost:8420{path}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── Dashboard ────────────────────────────────────────────────────────────────

type Tab = 'picks' | 'arb' | 'betlog';
const TABS: [Tab, string][] = [['picks', "Today's Picks"], ['arb', 'Arbitrage'], ['betlog', 'Bet Log']];

export default function Dashboard() {
  const [tab, setTab] = useState<Tab>('picks');
  const [now, setNow] = useState('');
  useEffect(() => {
    setNow(new Date().toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric' }));
  }, []);

  return (
    <div className="min-h-screen flex flex-col relative">

      {/* ── Ambient glow orbs (fixed behind everything) ── */}
      <div className="fixed inset-0 overflow-hidden pointer-events-none" style={{ zIndex: 0 }}>
        <div className="absolute -top-48 -left-48 w-[800px] h-[800px] rounded-full"
          style={{ background: 'radial-gradient(circle, rgba(0,232,122,0.055) 0%, transparent 60%)' }} />
        <div className="absolute top-[30%] -right-48 w-[650px] h-[650px] rounded-full"
          style={{ background: 'radial-gradient(circle, rgba(245,158,11,0.045) 0%, transparent 60%)' }} />
        <div className="absolute bottom-0 left-[20%] w-[600px] h-[600px] rounded-full"
          style={{ background: 'radial-gradient(circle, rgba(59,130,246,0.035) 0%, transparent 60%)' }} />
      </div>

      {/* ── Top Nav (frosted glass) ── */}
      <header className="sticky top-0 z-50"
        style={{
          background: 'rgba(2, 9, 18, 0.78)',
          backdropFilter: 'blur(24px) saturate(180%)',
          WebkitBackdropFilter: 'blur(24px) saturate(180%)',
          borderBottom: '1px solid rgba(255,255,255,0.07)',
        }}>
        <div className="max-w-screen-2xl mx-auto px-5 py-3 flex items-center gap-4">
          <div className="flex items-baseline gap-1.5 select-none">
            <span className="text-lg font-black tracking-tight text-edge-green"
              style={{ textShadow: '0 0 24px rgba(0,232,122,0.35)' }}>KALISHI</span>
            <span className="text-lg font-black tracking-tight text-ink-200">EDGE</span>
            <span className="ml-1 badge-green text-[9px] font-bold tracking-widest py-[1px]">AI</span>
          </div>
          <div className="flex-1" />
          <span className="text-[11px] text-ink-500 hidden sm:block font-medium">{now}</span>
          <LiveDot />
        </div>
        <div className="h-px" style={{ background: 'linear-gradient(90deg,transparent,rgba(0,232,122,0.25),transparent)' }} />
      </header>

      {/* ── Body ── */}
      <main className="relative flex-1 max-w-screen-2xl mx-auto w-full px-5 py-6 space-y-4" style={{ zIndex: 1 }}>

        {/* Bankroll stat row */}
        <BankrollTicker />

        {/* Equity curve + Kelly row */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2"><EquityCurve /></div>
          <KellyCalc />
        </div>

        {/* Tabbed main + sidebar */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2 card">
            <div className="tab-bar -mx-4 px-4">
              {TABS.map(([id, label]) => (
                <button key={id} className={clsx('tab-item', tab === id && 'active')} onClick={() => setTab(id)}>
                  {label}
                </button>
              ))}
            </div>
            {tab === 'picks'  && <TodaysPicks />}
            {tab === 'arb'    && <ArbPanel />}
            {tab === 'betlog' && <BetLog />}
          </div>
          <QuickActions />
        </div>
      </main>

      <footer className="relative text-center text-[10px] py-5"
        style={{ color: 'rgba(255,255,255,0.18)', borderTop: '1px solid rgba(255,255,255,0.05)', zIndex: 1 }}>
        Kalishi Edge — Personal use only · Probabilistic models · Manage your bankroll responsibly
      </footer>
    </div>
  );
}
