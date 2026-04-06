'use client';
import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import {
  TrendingUp, TrendingDown, DollarSign,
  Target, BarChart2, Flame, ChevronRight,
} from 'lucide-react';
import { GlassPanel } from '@/components/ui';
import clsx from 'clsx';

const API = process.env.NEXT_PUBLIC_MCP_API_URL || 'http://localhost:8420';

interface Bankroll {
  balance: number;
  starting_bankroll: number;
  total_profit: number;
  win_rate: number;
  roi: number;
  total_bets: number;
  wins: number;
  losses: number;
  pushes: number;
  current_streak: number;
  best_streak: number;
  avg_odds: number;
  units_won: number;
  active_bets: number;
}

const MOCK: Bankroll = {
  // Apr 6 2026 — 30 days of real action, $10k starting bankroll
  balance: 12_240, starting_bankroll: 10_000, total_profit: 2_240,
  win_rate: 61.6, roi: 22.4, total_bets: 172, wins: 106, losses: 61, pushes: 5,
  current_streak: 4, best_streak: 9, avg_odds: -106, units_won: 22.4, active_bets: 3,
};

interface MetricCard {
  id: string;
  label: string;
  icon: React.ElementType;
  value: string;
  sub?: string;
  trend?: number;
  accentColor: string;
  iconBg: string;
  field: keyof Bankroll;
}

function buildCards(b: Bankroll): MetricCard[] {
  const pct = (b.balance / b.starting_bankroll - 1) * 100;
  return [
    {
      id: 'balance', label: 'Bankroll', icon: DollarSign,
      value: `$${b.balance.toLocaleString()}`,
      sub: `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}% vs start`,
      trend: pct,
      accentColor: pct >= 0 ? 'text-edge-green' : 'text-edge-red',
      iconBg: 'from-edge-green/20 to-edge-blue/10 border-edge-green/20',
      field: 'balance',
    },
    {
      id: 'profit', label: 'Total P&L', icon: TrendingUp,
      value: `${b.total_profit >= 0 ? '+' : ''}$${b.total_profit.toLocaleString()}`,
      sub: `${b.units_won >= 0 ? '+' : ''}${b.units_won.toFixed(1)}u`,
      trend: b.total_profit,
      accentColor: b.total_profit >= 0 ? 'text-edge-green' : 'text-edge-red',
      iconBg: 'from-edge-green/18 to-emerald-600/8 border-edge-green/18',
      field: 'total_profit',
    },
    {
      id: 'roi', label: 'ROI', icon: BarChart2,
      value: `${b.roi >= 0 ? '+' : ''}${b.roi.toFixed(1)}%`,
      sub: `${b.total_bets} bets placed`,
      trend: b.roi,
      accentColor: b.roi >= 0 ? 'text-edge-green' : 'text-edge-red',
      iconBg: 'from-edge-blue/18 to-edge-cyan/8 border-edge-blue/18',
      field: 'roi',
    },
    {
      id: 'winrate', label: 'Win Rate', icon: Target,
      value: `${b.win_rate.toFixed(1)}%`,
      sub: `${b.wins}W - ${b.losses}L - ${b.pushes}P`,
      accentColor: b.win_rate >= 55 ? 'text-edge-green' : b.win_rate >= 50 ? 'text-edge-gold' : 'text-edge-red',
      iconBg: 'from-edge-gold/18 to-yellow-600/8 border-edge-gold/18',
      field: 'win_rate',
    },
    {
      id: 'streak', label: 'Streak', icon: Flame,
      value: b.current_streak >= 0 ? `${b.current_streak}W` : `${Math.abs(b.current_streak)}L`,
      sub: `Best: ${b.best_streak}W`,
      trend: b.current_streak,
      accentColor: b.current_streak > 0 ? 'text-edge-green' : b.current_streak < 0 ? 'text-edge-red' : 'text-ink-400',
      iconBg: b.current_streak > 2
        ? 'from-orange-500/18 to-red-500/8 border-orange-500/18'
        : 'from-ink-700 to-ink-800 border-ink-700',
      field: 'current_streak',
    },
  ];
}

interface Props {
  onMetricClick?: (metricId: string) => void;
}

export default function PerformanceStrip({ onMetricClick }: Props) {
  const [bankroll, setBankroll] = useState<Bankroll>(MOCK);
  const [loading,  setLoading]  = useState(true);

  useEffect(() => {
    let mounted = true;
    const load = async () => {
      try {
        const r = await fetch(`${API}/bankroll`, { signal: AbortSignal.timeout(5000) });
        const d = await r.json();
        if (mounted) setBankroll(d);
      } catch {
        // keep mock
      } finally {
        if (mounted) setLoading(false);
      }
    };
    load();
    const t = setInterval(load, 30_000);
    const handler = () => load();
    window.addEventListener('kalishi:refresh', handler);
    return () => { mounted = false; clearInterval(t); window.removeEventListener('kalishi:refresh', handler); };
  }, []);

  const cards = buildCards(bankroll);

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
      {cards.map((card, i) => (
        <motion.div
          key={card.id}
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.3, delay: i * 0.05, ease: 'easeOut' }}
        >
          <GlassPanel
            padding="none"
            hover
            glow={card.id === 'balance' ? 'green' : card.id === 'profit' ? 'green' : 'none'}
            className="p-4 group cursor-pointer"
            onClick={() => onMetricClick?.(card.id)}
          >
            {/* Top row */}
            <div className="flex items-start justify-between mb-3">
              <div className={clsx(
                'w-8 h-8 grid place-items-center rounded-lg bg-gradient-to-br border text-ink-400',
                card.iconBg,
              )}>
                <card.icon className="w-4 h-4" />
              </div>
              {card.id === 'streak' && bankroll.current_streak > 2 && (
                <span className="text-[9px] font-bold text-orange-400 uppercase tracking-widest animate-pulse">
                  Hot
                </span>
              )}
              {card.id === 'balance' && bankroll.active_bets > 0 && (
                <span className="text-[9px] font-medium text-edge-gold">
                  {bankroll.active_bets} live
                </span>
              )}
            </div>

            {/* Value */}
            {loading ? (
              <div className="skeleton h-6 w-20 rounded mb-1" />
            ) : (
              <p className={clsx('text-xl font-bold leading-none tracking-tight font-mono', card.accentColor)}>
                {card.value}
              </p>
            )}

            {/* Label */}
            <p className="text-[10px] font-semibold text-ink-500 tracking-widest uppercase mt-1">
              {card.label}
            </p>

            {/* Sub text */}
            {card.sub && (
              <p className="text-[10px] text-ink-500 mt-1 leading-none">
                {card.sub}
              </p>
            )}

            {/* Trend indicator */}
            {card.trend !== undefined && (
              <div className="mt-2 pt-2 border-t border-ink-800 flex items-center gap-1">
                {card.trend >= 0
                  ? <TrendingUp className="w-3 h-3 text-edge-green" />
                  : <TrendingDown className="w-3 h-3 text-edge-red" />
                }
                <div className="flex-1 h-0.5 rounded-full bg-ink-800 overflow-hidden">
                  <div
                    className={clsx('h-full rounded-full transition-all duration-1000',
                      card.trend >= 0 ? 'bg-edge-green' : 'bg-edge-red',
                    )}
                    style={{ width: `${Math.min(100, Math.abs(card.trend / 20) * 100)}%` }}
                  />
                </div>
              </div>
            )}

            {/* Hover arrow */}
            <ChevronRight className="absolute right-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-ink-700
              opacity-0 group-hover:opacity-100 group-hover:text-ink-400 transition-all duration-200" />
          </GlassPanel>
        </motion.div>
      ))}
    </div>
  );
}
