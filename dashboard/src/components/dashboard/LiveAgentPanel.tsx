'use client';
import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Bot, Zap, Clock, TrendingUp, AlertTriangle,
  CheckCircle2, XCircle, DollarSign, BarChart3, RefreshCw,
} from 'lucide-react';
import { GlassPanel, Badge } from '@/components/ui';
import clsx from 'clsx';
import type { LiveStatus, LiveBet, LiveSession } from '@/lib/types';

const API = process.env.NEXT_PUBLIC_MCP_API_URL || 'http://localhost:8420';

// ─── Helpers ─────────────────────────────────────────────────────────────────

function fmtAge(isoTs: string | null): string {
  if (!isoTs) return '—';
  const diff = Date.now() - new Date(isoTs).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60)   return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function fmtCooldown(secs: number): string {
  if (secs <= 0) return 'Ready';
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function fmtTicker(ticker: string): string {
  // KXBTCD-26APR0907-T71299.99 → BTC $71,300 Apr-9 07h
  return ticker.replace('KXBTCD-', 'BTC ').replace('KXETH', 'ETH ').replace('KXXRP', 'XRP ')
    .replace('KXSOL', 'SOL ').replace('KXDOGE', 'DOGE ').replace(/26APR/, 'Apr-')
    .replace(/-T/, ' $').replace('.99', '').replace(/0*(\d)h$/, '$1h');
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function AgentStatusBadge({ status }: { status: LiveStatus }) {
  return (
    <div className={clsx(
      'flex items-center gap-2 px-3 py-1.5 rounded-full border text-xs font-semibold',
      status.agent_alive
        ? 'bg-edge-green/10 border-edge-green/30 text-edge-green'
        : 'bg-red-500/10 border-red-500/30 text-red-400',
    )}>
      <span className={clsx(
        'w-2 h-2 rounded-full',
        status.agent_alive ? 'bg-edge-green animate-pulse' : 'bg-red-400',
      )} />
      {status.agent_alive ? 'Agent Online' : 'Agent Offline'}
    </div>
  );
}

function StatBox({ icon: Icon, label, value, accent }: {
  icon: React.ElementType; label: string; value: string; accent?: string;
}) {
  return (
    <div className="flex flex-col gap-1 p-3 rounded-lg bg-ink-900/60 border border-ink-800">
      <div className="flex items-center gap-1.5 text-ink-500">
        <Icon className="w-3.5 h-3.5" />
        <span className="text-[10px] font-semibold uppercase tracking-widest">{label}</span>
      </div>
      <p className={clsx('text-lg font-bold font-mono tracking-tight', accent || 'text-ink-100')}>
        {value}
      </p>
    </div>
  );
}

function BetRow({ bet, index }: { bet: LiveBet; index: number }) {
  const isOpen   = bet.status === 'open';
  const isClosed = bet.status === 'closed';
  const sideUp   = (bet.side || '').toUpperCase();

  return (
    <motion.div
      initial={{ opacity: 0, x: -12 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay: index * 0.04 }}
      className="flex items-center gap-3 py-2.5 border-b border-ink-800/60 last:border-0"
    >
      {/* Side badge */}
      <span className={clsx(
        'text-[10px] font-bold px-2 py-0.5 rounded border',
        sideUp === 'YES'
          ? 'text-edge-green bg-edge-green/10 border-edge-green/30'
          : 'text-edge-red   bg-red-500/10   border-red-500/30',
      )}>
        {sideUp || '?'}
      </span>

      {/* Ticker */}
      <div className="flex-1 min-w-0">
        <p className="text-xs font-mono text-ink-200 truncate">
          {bet.ticker ? fmtTicker(bet.ticker) : 'Unknown'}
        </p>
        {bet.entry_ts && (
          <p className="text-[10px] text-ink-500">{fmtAge(bet.entry_ts)}</p>
        )}
      </div>

      {/* Price */}
      {bet.entry_price_cents !== undefined && (
        <span className="text-xs font-mono text-ink-300">
          {bet.entry_price_cents}¢
        </span>
      )}

      {/* Edge */}
      {bet.edge_pct !== undefined && (
        <span className={clsx(
          'text-xs font-bold font-mono',
          bet.edge_pct > 0 ? 'text-edge-green' : 'text-edge-red',
        )}>
          {bet.edge_pct > 0 ? '+' : ''}{bet.edge_pct.toFixed(1)}%
        </span>
      )}

      {/* Status dot */}
      <div className={clsx(
        'w-2 h-2 rounded-full flex-shrink-0',
        isOpen   ? 'bg-edge-gold animate-pulse' :
        isClosed ? 'bg-ink-600' :
                   'bg-edge-blue',
      )} title={bet.status} />
    </motion.div>
  );
}

function SessionRow({ sess, index }: { sess: LiveSession; index: number }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05 }}
      className="border-b border-ink-800/60 last:border-0"
    >
      <button
        className="w-full flex items-center gap-3 py-2.5 text-left group"
        onClick={() => setExpanded(e => !e)}
      >
        <span className={clsx(
          'text-[10px] font-bold px-1.5 py-0.5 rounded border',
          sess.bets_placed > 0
            ? 'text-edge-green bg-edge-green/10 border-edge-green/20'
            : 'text-ink-500 bg-ink-800 border-ink-700',
        )}>
          S{sess.session ?? '?'}
        </span>

        <div className="flex-1 min-w-0">
          <p className="text-xs text-ink-300 truncate">
            {sess.bets_placed > 0
              ? `${sess.bets_placed} bet${sess.bets_placed !== 1 ? 's' : ''} placed`
              : 'No bets'
            } · {sess.tool_calls} tool calls
          </p>
          <p className="text-[10px] text-ink-600">{fmtAge(sess.timestamp)}</p>
        </div>

        {sess.dry_run && (
          <span className="text-[9px] text-edge-gold bg-edge-gold/10 border border-edge-gold/20 px-1.5 py-0.5 rounded">
            DRY
          </span>
        )}
      </button>

      <AnimatePresence>
        {expanded && sess.summary && (
          <motion.p
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="text-[11px] text-ink-500 pb-2.5 leading-relaxed overflow-hidden"
          >
            {sess.summary}
          </motion.p>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

// ─── Main Component ───────────────────────────────────────────────────────────

export default function LiveAgentPanel() {
  const [status,   setStatus]   = useState<LiveStatus | null>(null);
  const [bets,     setBets]     = useState<LiveBet[]>([]);
  const [sessions, setSessions] = useState<LiveSession[]>([]);
  const [loading,  setLoading]  = useState(true);
  const [lastFetch, setLastFetch] = useState<Date | null>(null);
  const [tab,      setTab]      = useState<'bets' | 'sessions'>('bets');

  const load = async () => {
    try {
      const [sRes, bRes, ssRes] = await Promise.all([
        fetch(`${API}/live/status`),
        fetch(`${API}/live/bets?limit=30`),
        fetch(`${API}/live/sessions?limit=12`),
      ]);
      if (sRes.ok)  setStatus(await sRes.json());
      if (bRes.ok)  { const d = await bRes.json(); setBets(d.bets || []); }
      if (ssRes.ok) { const d = await ssRes.json(); setSessions(d.sessions || []); }
      setLastFetch(new Date());
    } catch {
      // silently fail
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    const handler = () => load();
    window.addEventListener('kalishi:refresh', handler);
    return () => { clearInterval(t); window.removeEventListener('kalishi:refresh', handler); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // total bets placed today (kalshi orders)
  const betsToday = bets.filter(b => b.source === 'kalshi_order').length;
  const openBets  = bets.filter(b => b.status === 'open').length;
  const sessionsWithBets = sessions.filter(s => s.bets_placed > 0).length;

  return (
    <GlassPanel className="flex flex-col gap-0 overflow-hidden" padding="none">
      {/* Header */}
      <div className="flex items-center justify-between px-4 pt-4 pb-3 border-b border-ink-800">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-edge-blue/20 to-edge-cyan/10 border border-edge-blue/20 grid place-items-center">
            <Bot className="w-4 h-4 text-edge-blue" />
          </div>
          <div>
            <h3 className="text-sm font-bold text-ink-100">Live Agent</h3>
            <p className="text-[10px] text-ink-500">
              {lastFetch ? `Updated ${fmtAge(lastFetch.toISOString())}` : 'Loading…'}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {status && <AgentStatusBadge status={status} />}
          <button
            onClick={load}
            className="p-1.5 rounded-lg hover:bg-ink-800 text-ink-500 hover:text-ink-200 transition-colors"
            title="Refresh"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-2 p-4">
        <StatBox
          icon={DollarSign}
          label="Daily Spend"
          value={status ? `$${status.daily_spend_usd.toFixed(2)}` : '—'}
          accent="text-edge-gold"
        />
        <StatBox
          icon={Zap}
          label="Bets Today"
          value={betsToday > 0 ? String(betsToday) : String(openBets)}
          accent={betsToday > 0 ? 'text-edge-green' : 'text-ink-400'}
        />
        <StatBox
          icon={BarChart3}
          label="Sessions"
          value={sessionsWithBets > 0 ? `${sessionsWithBets}/${sessions.length}` : String(sessions.length)}
          accent="text-edge-blue"
        />
      </div>

      {/* Cooldowns */}
      {status && Object.keys(status.cooldowns_sec).length > 0 && (
        <div className="px-4 pb-3 flex flex-wrap gap-2">
          {Object.entries(status.cooldowns_sec).map(([asset, secs]) => (
            <div key={asset} className={clsx(
              'flex items-center gap-1.5 text-[10px] font-medium px-2 py-1 rounded-full border',
              secs > 0
                ? 'text-edge-gold bg-edge-gold/10 border-edge-gold/20'
                : 'text-edge-green bg-edge-green/10 border-edge-green/20',
            )}>
              <Clock className="w-3 h-3" />
              {asset}: {fmtCooldown(secs)}
            </div>
          ))}
        </div>
      )}

      {/* Tabs */}
      <div className="flex border-b border-ink-800 px-4 gap-4">
        {(['bets', 'sessions'] as const).map(t => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={clsx(
              'text-xs font-semibold py-2.5 border-b-2 -mb-px transition-colors',
              tab === t
                ? 'border-edge-blue text-edge-blue'
                : 'border-transparent text-ink-500 hover:text-ink-300',
            )}
          >
            {t === 'bets' ? `Bets (${bets.length})` : `Sessions (${sessions.length})`}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="overflow-y-auto flex-1 px-4 py-2" style={{ maxHeight: 340 }}>
        {loading ? (
          <div className="flex flex-col gap-2 py-4">
            {[1,2,3].map(i => (
              <div key={i} className="skeleton h-8 rounded" />
            ))}
          </div>
        ) : tab === 'bets' ? (
          bets.length === 0 ? (
            <p className="text-ink-600 text-xs py-6 text-center">No bets recorded yet</p>
          ) : (
            bets.map((bet, i) => <BetRow key={bet.order_id || `${bet.ticker}-${i}`} bet={bet} index={i} />)
          )
        ) : (
          sessions.length === 0 ? (
            <p className="text-ink-600 text-xs py-6 text-center">No sessions recorded yet</p>
          ) : (
            sessions.map((sess, i) => <SessionRow key={`${sess.timestamp}-${i}`} sess={sess} index={i} />)
          )
        )}
      </div>
    </GlassPanel>
  );
}
