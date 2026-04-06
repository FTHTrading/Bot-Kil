'use client';
import React, { useState, useEffect, useCallback } from 'react';
import {
  AreaChart, Area, LineChart, Line, XAxis, YAxis,
  Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine,
} from 'recharts';
import { GlassPanel } from '@/components/ui';
import { TrendingUp, TrendingDown, BarChart2, Calculator, Minus, Plus } from 'lucide-react';
import clsx from 'clsx';

const API = process.env.NEXT_PUBLIC_MCP_API_URL || 'http://localhost:8420';

// ─── Types ────────────────────────────────────────────────────────────────────

interface EquityPoint { date: string; balance: number; baseline?: number }

// Deterministic 30-day equity curve — Mar 7 → Apr 6, 2026
const MOCK_EQUITY: EquityPoint[] = [
  { date: 'Mar 7',  balance: 10_000, baseline: 10_000 },
  { date: 'Mar 8',  balance: 10_090, baseline: 10_000 },
  { date: 'Mar 9',  balance: 9_910,  baseline: 10_000 },
  { date: 'Mar 10', balance: 10_185, baseline: 10_000 },
  { date: 'Mar 11', balance: 10_420, baseline: 10_000 },
  { date: 'Mar 12', balance: 10_360, baseline: 10_000 },
  { date: 'Mar 13', balance: 10_580, baseline: 10_000 },
  { date: 'Mar 14', balance: 10_520, baseline: 10_000 },
  { date: 'Mar 15', balance: 10_740, baseline: 10_000 },
  { date: 'Mar 16', balance: 10_690, baseline: 10_000 },
  { date: 'Mar 17', balance: 10_915, baseline: 10_000 },
  { date: 'Mar 18', balance: 11_020, baseline: 10_000 },
  { date: 'Mar 19', balance: 10_870, baseline: 10_000 },
  { date: 'Mar 20', balance: 11_080, baseline: 10_000 },
  { date: 'Mar 21', balance: 11_240, baseline: 10_000 },
  { date: 'Mar 22', balance: 11_160, baseline: 10_000 },
  { date: 'Mar 23', balance: 11_345, baseline: 10_000 },
  { date: 'Mar 24', balance: 11_290, baseline: 10_000 },
  { date: 'Mar 25', balance: 11_460, baseline: 10_000 },
  { date: 'Mar 26', balance: 11_385, baseline: 10_000 },
  { date: 'Mar 27', balance: 11_560, baseline: 10_000 },
  { date: 'Mar 28', balance: 11_490, baseline: 10_000 },
  { date: 'Mar 29', balance: 11_650, baseline: 10_000 },
  { date: 'Mar 30', balance: 11_380, baseline: 10_000 },
  { date: 'Mar 31', balance: 11_715, baseline: 10_000 },
  { date: 'Apr 1',  balance: 11_840, baseline: 10_000 },
  { date: 'Apr 2',  balance: 11_690, baseline: 10_000 },
  { date: 'Apr 3',  balance: 11_870, baseline: 10_000 },
  { date: 'Apr 4',  balance: 12_080, baseline: 10_000 },
  { date: 'Apr 5',  balance: 11_960, baseline: 10_000 },
  { date: 'Apr 6',  balance: 12_240, baseline: 10_000 },
];

// ─── Custom Tooltip ───────────────────────────────────────────────────────────

function ChartTooltip({ active, payload, label }: { active?: boolean; payload?: any[]; label?: string }) {
  if (!active || !payload?.length) return null;
  const v = payload[0]?.value as number;
  const base = payload[1]?.value as number ?? 10_000;
  const delta = v - base;
  return (
    <div className="glass-panel px-3 py-2 text-xs shadow-xl">
      <div className="text-ink-400 mb-1 font-medium">{label}</div>
      <div className="font-mono font-bold text-ink-100">${v?.toLocaleString()}</div>
      {base && (
        <div className={clsx('font-mono text-[10px]', delta >= 0 ? 'text-edge-green' : 'text-edge-red')}>
          {delta >= 0 ? '+' : ''}{delta.toLocaleString()}
        </div>
      )}
    </div>
  );
}

// ─── Equity Curve Card ────────────────────────────────────────────────────────

export function EquityCurveCard() {
  const [equity, setEquity] = useState<EquityPoint[]>(MOCK_EQUITY);
  const [loading, setLoading] = useState(true);
  const [range, setRange] = useState<7 | 14 | 30>(30);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${API}/performance/equity`, { signal: AbortSignal.timeout(5000) });
      const d = await r.json();
      if (Array.isArray(d) && d.length > 0) setEquity(d);
    } catch { /* keep mock */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const data = equity.slice(-range);
  const first = data[0]?.balance ?? 0;
  const last  = data[data.length - 1]?.balance ?? 0;
  const pct   = first > 0 ? ((last - first) / first) * 100 : 0;
  const isPos = pct >= 0;

  const min = Math.min(...data.map(d => d.balance));
  const max = Math.max(...data.map(d => d.balance));

  return (
    <GlassPanel padding="none" className="p-5">
      {/* Header */}
      <div className="flex items-start justify-between mb-4">
        <div>
          <div className="flex items-center gap-2 mb-0.5">
            <div className="w-6 h-6 grid place-items-center rounded-md bg-edge-green/15 border border-edge-green/20">
              <TrendingUp className="w-3.5 h-3.5 text-edge-green" />
            </div>
            <span className="text-xs font-semibold text-ink-300 uppercase tracking-widest">Equity Curve</span>
          </div>
          <div className="flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-ink-100">${last.toLocaleString()}</span>
            <span className={clsx('text-sm font-mono font-semibold', isPos ? 'text-edge-green' : 'text-edge-red')}>
              {isPos ? '+' : ''}{pct.toFixed(2)}%
            </span>
          </div>
        </div>

        {/* Range selector */}
        <div className="flex items-center gap-0.5 p-0.5 rounded-lg bg-ink-850 border border-ink-800">
          {([7, 14, 30] as const).map(r => (
            <button
              key={r}
              onClick={() => setRange(r)}
              className={clsx(
                'px-2.5 py-1 rounded-md text-[10px] font-semibold tracking-wide transition-all duration-150',
                range === r
                  ? 'bg-edge-green/15 text-edge-green border border-edge-green/25'
                  : 'text-ink-500 hover:text-ink-300',
              )}
            >
              {r}d
            </button>
          ))}
        </div>
      </div>

      {/* Chart */}
      {loading ? (
        <div className="h-36 skeleton rounded-lg" />
      ) : (
        <ResponsiveContainer width="100%" height={144}>
          <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%"   stopColor={isPos ? '#00E87A' : '#ff4d6d'} stopOpacity={0.22} />
                <stop offset="100%" stopColor={isPos ? '#00E87A' : '#ff4d6d'} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid
              strokeDasharray="3 3"
              stroke="rgba(255,255,255,0.04)"
              horizontal vertical={false}
            />
            <XAxis
              dataKey="date" hide tick={{ fill: '#6b7280', fontSize: 9 }}
              axisLine={false} tickLine={false} interval="preserveStartEnd"
            />
            <YAxis
              domain={[Math.floor(min * 0.995), Math.ceil(max * 1.005)]}
              hide tick={{ fill: '#6b7280', fontSize: 9 }}
              axisLine={false} tickLine={false}
              tickFormatter={(v) => `$${Math.round(v/1000)}k`}
            />
            <Tooltip content={<ChartTooltip />} />
            <ReferenceLine y={10_000} stroke="rgba(255,255,255,0.08)" strokeDasharray="4 4" />
            <Area
              type="monotone"
              dataKey="balance"
              stroke={isPos ? '#00E87A' : '#ff4d6d'}
              strokeWidth={2}
              fill="url(#equityGrad)"
              dot={false}
              activeDot={{ r: 4, strokeWidth: 0, fill: isPos ? '#00E87A' : '#ff4d6d' }}
            />
          </AreaChart>
        </ResponsiveContainer>
      )}

      {/* Footer stats */}
      <div className="mt-3 grid grid-cols-3 gap-2 pt-3 border-t border-ink-800">
        {[
          { label: 'Period High', value: `$${max.toLocaleString()}`, color: 'text-edge-green' },
          { label: 'Period Low',  value: `$${min.toLocaleString()}`,  color: 'text-edge-red' },
          { label: 'Baseline',   value: '$10,000',                    color: 'text-ink-400' },
        ].map(({ label, value, color }) => (
          <div key={label}>
            <div className="text-[9px] text-ink-600 uppercase tracking-wider">{label}</div>
            <div className={clsx('font-mono text-xs font-semibold', color)}>{value}</div>
          </div>
        ))}
      </div>
    </GlassPanel>
  );
}

// ─── Kelly Calculator Card ────────────────────────────────────────────────────

export function KellyCalcCard() {
  const [odds, setOdds]       = useState<string>('-110');
  const [winProb, setWinProb] = useState<string>('55');
  const [bankroll, setBankroll] = useState<string>('10000');

  const o = parseFloat(odds);
  const w = parseFloat(winProb) / 100;
  const b = parseFloat(bankroll);

  const decOdds = o > 0 ? o / 100 + 1 : 100 / Math.abs(o) + 1;
  const payout  = decOdds - 1;
  const kelly   = isNaN(w) || isNaN(payout) ? 0 : Math.max(0, w - (1 - w) / payout);
  const halfK   = kelly / 2;

  const suggestedAmt = isNaN(b) ? 0 : halfK * b;
  const impliedProb  = o > 0 ? 100 / (o + 100) : Math.abs(o) / (Math.abs(o) + 100);
  const edge         = isNaN(w) ? 0 : w - impliedProb;

  const verdict =
    edge >= 0.08 ? { label: 'STRONG EDGE', color: 'text-edge-green', bg: 'bg-edge-green/10' }
    : edge >= 0.04 ? { label: 'GOOD EDGE',   color: 'text-edge-blue',  bg: 'bg-edge-blue/10' }
    : edge >= 0.01 ? { label: 'MARGINAL',    color: 'text-edge-gold',  bg: 'bg-edge-gold/10' }
    : { label: 'NO EDGE', color: 'text-edge-red', bg: 'bg-edge-red/10' };

  return (
    <GlassPanel padding="none" className="p-5 h-full">
      {/* Header */}
      <div className="flex items-center gap-2 mb-4">
        <div className="w-6 h-6 grid place-items-center rounded-md bg-edge-blue/15 border border-edge-blue/20">
          <Calculator className="w-3.5 h-3.5 text-edge-blue" />
        </div>
        <span className="text-xs font-semibold text-ink-300 uppercase tracking-widest">Kelly Calculator</span>
      </div>

      {/* Inputs */}
      <div className="space-y-2.5 mb-4">
        {[
          { label: 'American Odds', key: 'odds', value: odds, onChange: setOdds, placeholder: '-110' },
          { label: 'Win Prob %',    key: 'wp',   value: winProb, onChange: setWinProb, placeholder: '55' },
          { label: 'Bankroll $',   key: 'br',   value: bankroll, onChange: setBankroll, placeholder: '10000' },
        ].map(({ label, key, value, onChange, placeholder }) => (
          <div key={key}>
            <label className="block text-[9px] font-semibold text-ink-500 uppercase tracking-widest mb-1">
              {label}
            </label>
            <input
              type="number"
              value={value}
              onChange={e => onChange(e.target.value)}
              placeholder={placeholder}
              className="input-field w-full font-mono text-sm"
            />
          </div>
        ))}
      </div>

      {/* Result */}
      <div className={clsx('rounded-xl p-3 border', verdict.bg,
        edge >= 0.08 ? 'border-edge-green/20' :
        edge >= 0.04 ? 'border-edge-blue/20' :
        edge >= 0.01 ? 'border-edge-gold/20' : 'border-edge-red/20',
      )}>
        <div className="grid grid-cols-2 gap-2 text-xs">
          {[
            { l: 'Full Kelly', v: `${(kelly * 100).toFixed(2)}%`, c: 'text-ink-200' },
            { l: 'Half Kelly', v: `${(halfK * 100).toFixed(2)}%`, c: 'text-edge-blue' },
            { l: 'Bet Size',   v: `$${suggestedAmt.toFixed(0)}`,  c: 'text-edge-green font-bold' },
            { l: 'Edge',       v: `${(edge * 100).toFixed(2)}%`,  c: edge >= 0 ? 'text-edge-green' : 'text-edge-red' },
          ].map(({ l, v, c }) => (
            <div key={l}>
              <div className="text-[9px] text-ink-500 uppercase tracking-wider">{l}</div>
              <div className={clsx('font-mono font-semibold mt-0.5', c)}>{v}</div>
            </div>
          ))}
        </div>
        <div className={clsx('mt-2 text-center text-[10px] font-bold tracking-widest uppercase', verdict.color)}>
          {verdict.label}
        </div>
      </div>
    </GlassPanel>
  );
}
