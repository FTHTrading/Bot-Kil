"""
KALISHI EDGE - Autonomous Betting Agent
========================================
The brain drives itself. It receives tools and decides what to bet.

Flow:
  1. Brain calls get_balance() to know its budget
  2. Brain calls scan_markets(category) to see what's available
  3. Brain calls get_market(ticker) to inspect a specific contract
  4. Brain calls analyze_edge(ticker, side, our_prob) to get EV math
  5. Brain calls place_bet(ticker, side, contracts, yes_price) to execute

The brain (Llama 3.3 70B via NVIDIA NIM, or local qwen2.5 via Ollama) handles
ALL reasoning. This file just gives it the tool wrappers and runs the loop.

Usage:
  python -m agents.autonomous                  # dry run, one pass
  python -m agents.autonomous --live           # LIVE MONEY - careful
  python -m agents.autonomous --live --loop    # runs every 15 min
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
# Fix Windows charmap encoding errors on console — replace unencodable chars
import sys as _sys
for _h in logging.root.handlers:
    if hasattr(_h, "stream") and hasattr(_h.stream, "reconfigure"):
        try:
            _h.stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
# Also patch stderr/stdout for any direct print() calls
try:
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
log = logging.getLogger("autonomous")

# ── Session risk controls ─────────────────────────────────────────────────────
ALLOWED_ASSETS = {"BTC", "ETH"}   # SOL / DOGE / XRP bets suspended

# Stake / spend limits
_SESSION_BUDGET           = 3.00  # max USD to risk per agent session
_DAILY_STOP_LOSS          = 6.00  # halt if cumulative daily spend reaches $6
_DAILY_STOP_WIN           = 8.00  # halt if daily realized P&L reaches +$8

# Cooldown (per-asset lockout after any fill)
_COOLDOWN_MINUTES         = 45    # minutes

# Settlement buffer
_SETTLEMENT_BUFFER_SECS   = 180   # block daily entry if < 3 min to close
_INTRADAY_MIN_MINUTES_REM = 2     # block intraday entry if < 2 min remaining

# Market quality
_MIN_OPEN_INTEREST        = 50    # reject daily market if OI below this
_MAX_SPREAD_CENTS         = 15    # reject daily market if spread > 15c

# Volatility regime  (realized vol per 5-minute bar; BTC normal ≈ 0.0022)
_VOL_FLOOR = {"BTC": 0.0008, "ETH": 0.0010}  # below = too quiet, no signal
_VOL_CEIL  = {"BTC": 0.0060, "ETH": 0.0075}  # above = crash/spike mode

# Mutable session/daily state
_session_spent:          float            = 0.0   # reset each run_agent() call
_session_bets:           list[dict]       = []    # [{asset, strike, side}]
_daily_spend:            float            = 0.0   # gross stake out (cost, not realized loss)
_daily_pnl:              float            = 0.0   # net settled P&L from Kalshi; refreshed per session
_asset_cooldown:         dict[str, float] = {}    # asset -> unix ts when cooldown expires

# Reopen-mode state (armed by warm_start.py at ~4:55 AM ET)
# During the first 10-15 min after session open, apply stricter thresholds.
_reopen_mode:            bool             = False
_reopen_mode_expires:    float            = 0.0   # unix ts when reopen window ends
_reopen_no_trade_until:  float            = 0.0   # unix ts of hard no-trade period
_REOPEN_EDGE_MULTIPLIER  = 1.25              # min_edge * 1.25 during reopen window

# State persistence: survives restarts within the same calendar day
import json as _json
from pathlib import Path as _Path
_STATE_FILE = _Path("logs/daily_state.json")


def _load_daily_state() -> tuple[float, dict, bool, float, float]:
    """Load persisted daily state for today.
    Returns (daily_spend, cooldowns, reopen_mode, reopen_expires, no_trade_until)."""
    try:
        from datetime import date as _date
        if _STATE_FILE.exists():
            _d = _json.loads(_STATE_FILE.read_text())
            if _d.get("date") == _date.today().isoformat():
                return (
                    float(_d.get("daily_spend",           0.0)),
                    dict(_d.get("cooldowns",              {})),
                    bool(_d.get("reopen_mode",            False)),
                    float(_d.get("reopen_mode_expires",   0.0)),
                    float(_d.get("reopen_no_trade_until", 0.0)),
                )
    except Exception:
        pass
    return 0.0, {}, False, 0.0, 0.0


def _save_daily_state() -> None:
    """Persist daily_spend, cooldowns, and reopen-mode fields across restarts."""
    try:
        from datetime import date as _date
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(_json.dumps({
            "date":                   _date.today().isoformat(),
            "daily_spend":            _daily_spend,
            "cooldowns":              _asset_cooldown,
            "reopen_mode":            _reopen_mode,
            "reopen_mode_expires":    _reopen_mode_expires,
            "reopen_no_trade_until":  _reopen_no_trade_until,
        }))
    except Exception:
        pass

# ── Tool definitions (OpenAI function-calling format) ─────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_balance",
            "description": "Get current Kalshi account balance in dollars.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_markets",
            "description": (
                "Scan Kalshi for liquid open markets. Returns markets with real prices and open interest. "
                "Use series='KXNBA' for NBA championship futures (millions of contracts OI), "
                "'KXMLB' for MLB, 'KXBTC' for Bitcoin price markets, 'KXETH' for Ethereum, "
                "or series='' to scan all categories. Always scan KXNBA and KXBTC first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "series": {
                        "type": "string",
                        "description": "Series ticker: 'KXNBA', 'KXMLB', 'KXBTC', 'KXETH', or '' for all",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market",
            "description": "Get full details of a specific Kalshi market by ticker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Kalshi market ticker, e.g. KXNBA-25APR09-T200"}
                },
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_edge",
            "description": (
                "Calculate Expected Value and Kelly stake for a potential bet. "
                "Returns edge_pct, ev_pct, kelly_fraction, recommended_contracts, "
                "spend_usd, potential_profit. Call this before place_bet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker":   {"type": "string",  "description": "Kalshi market ticker"},
                    "side":     {"type": "string",  "description": "'yes' or 'no'"},
                    "our_prob": {"type": "number",  "description": "Your estimated win probability 0-1"},
                    "bankroll": {"type": "number",  "description": "Available bankroll in dollars"},
                },
                "required": ["ticker", "side", "our_prob", "bankroll"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_bet",
            "description": (
                "Place a real bet on Kalshi. Only call this when you have "
                "positive EV (edge > 5%) and sufficient liquidity. "
                "Returns order_id and confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker":     {"type": "string",  "description": "Kalshi market ticker"},
                    "side":       {"type": "string",  "description": "'yes' or 'no'"},
                    "contracts":  {"type": "integer", "description": "Number of contracts (1-5); max 2 for 15-min intraday markets"},
                    "yes_price":  {"type": "integer", "description": "Yes-side price in cents (10-90)"},
                    "reasoning":  {"type": "string",  "description": "Your 1-sentence reason: include model prob vs market price"},
                },
                "required": ["ticker", "side", "contracts", "yes_price", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_positions",
            "description": "Get all current open positions on Kalshi.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_crypto_snapshot",
            "description": (
                "CRYPTO INTELLIGENCE TOOL — call this FIRST for any crypto betting session. "
                "Returns: (1) live BTC/ETH spot prices, (2) short-term momentum signals "
                "(mom_5m, mom_15m, trend, realized_vol), (3) pre-computed model picks from "
                "the log-normal diffusion engine — every open Kalshi KXBTC/KXETH market with "
                "its model probability, edge_pct, implied vs model prob, and Kelly stake. "
                "Edge > 8% = strong value. Use model_picks directly to decide what to bet."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_deep_analysis",
            "description": (
                "DEEP QUANTITATIVE VALIDATION — run on any pick before placing >2 contracts. "
                "Runs 5 models simultaneously: "
                "(1) Log-normal diffusion at 3 vol scenarios (base/realized/±20%), "
                "(2) Monte Carlo 10,000 GBM price paths via Box-Muller transform, "
                "(3) RSI from recent candle data (>70=overbought, <30=oversold), "
                "(4) Bollinger Bands %B position (>0.8=overbought, <0.2=oversold), "
                "(5) KalshiNet neural inference (13-feature residual MLP, falls back to math). "
                "Returns: grade (A+/A/B/C/D/F), action (BET_NOW/LEAN/FADE/WAIT), "
                "all model probs, edge_pct, and full Kelly breakdown with formula. "
                "Use this to VALIDATE strong picks from get_crypto_snapshot before betting big."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Kalshi ticker e.g. KXBTCD-26APR09-T85000",
                    },
                    "side": {
                        "type": "string",
                        "description": "'yes' or 'no' — which side you plan to bet",
                    },
                },
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_intraday_picks",
            "description": (
                "Scan Kalshi 15-MINUTE DIRECTIONAL markets (KXBTC15M and KXETH15M only — "
                "SOL/DOGE/XRP are disabled). These resolve YES if asset price at 15-min close >= "
                "opening price — pure UP/DOWN bets. Uses KalshiNet neural inference (13 "
                "features: gap, momentum 1m/3m/5m/15m, realized vol, time, trend, asset "
                "embedding) with math blend fallback. "
                "Returns picks with: minutes_remaining, floor_strike, current_price, "
                "gap_pct, model_prob_pct, edge_pct, momentum_trend, used_neural, verdict. "
                "New market every 15 min — call this for high-frequency short-term edge."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

# ── Tool implementations ──────────────────────────────────────────────────────

async def _tool_get_balance() -> dict:
    from data.feeds.kalshi import get_balance
    try:
        bal = await get_balance()
        return bal
    except Exception as e:
        return {"error": str(e)}


async def _tool_scan_markets(series: str = "") -> list[dict]:
    from data.feeds.kalshi import get_active_markets, normalize_kalshi_market
    try:
        raw = await get_active_markets(series_ticker=series, max_pages=2)
        out = []
        for m in raw:
            n = normalize_kalshi_market(m)
            yes_p = round((n.get("yes_prob") or 0) * 100)
            oi = n.get("open_interest", 0) or 0
            # Skip illiquid markets (0¢ prices or zero open interest)
            if oi == 0 or yes_p < 5 or yes_p > 95:
                continue
            out.append({
                "ticker":        n.get("ticker", ""),
                "title":         n.get("title", ""),
                "yes_price_c":   yes_p,
                "no_price_c":    100 - yes_p,
                "open_interest": oi,
                "close_time":    n.get("close_time", ""),
            })
            if len(out) >= 40:
                break
        return out
    except Exception as e:
        return [{"error": str(e)}]


async def _tool_get_market(ticker: str) -> dict:
    from data.feeds.kalshi import get_market, normalize_kalshi_market
    try:
        raw = await get_market(ticker)
        if not raw:
            return {"error": f"market {ticker} not found"}
        return normalize_kalshi_market(raw)
    except Exception as e:
        return {"error": str(e)}


async def _tool_analyze_edge(ticker: str, side: str, our_prob: float, bankroll: float) -> dict:
    from data.feeds.kalshi import get_market, normalize_kalshi_market
    from engine.kelly import calculate_kelly
    from engine.ev import calculate_ev
    try:
        raw = await get_market(ticker)
        if not raw:
            return {"error": f"market {ticker} not found"}
        m = normalize_kalshi_market(raw)
        yes_prob = m.get("yes_prob") or 0
        yes_price_c = round(yes_prob * 100)
        no_price_c  = 100 - yes_price_c

        if side == "yes":
            market_prob = yes_prob
            price_cents = yes_price_c
            decimal_odds = 1.0 / yes_prob if yes_prob > 0 else 1.0
        else:
            market_prob = 1 - yes_prob
            price_cents = no_price_c
            decimal_odds = 1.0 / (1 - yes_prob) if yes_prob < 1 else 1.0

        ev = calculate_ev(our_prob, decimal_odds)
        kelly = calculate_kelly(our_prob, decimal_odds, bankroll, kelly_multiplier=0.25, min_edge=0.04)

        contracts = max(1, min(5, int(kelly.bet_amount / (price_cents / 100))))
        spend = round(contracts * price_cents / 100, 2)
        profit = round(contracts * (1 - price_cents / 100), 2)

        return {
            "ticker":                ticker,
            "side":                  side,
            "market_prob":           round(market_prob, 3),
            "our_prob":              round(our_prob, 3),
            "edge_pct":              round(ev.edge * 100, 2),
            "ev_pct":                round(ev.ev_pct, 2),
            "kelly_fraction":        round(kelly.fraction, 4),
            "recommended_contracts": contracts,
            "price_cents":           price_cents,
            "spend_usd":             spend,
            "potential_profit":      profit,
            "verdict":               ev.confidence,
        }
    except Exception as e:
        return {"error": str(e)}


async def _fetch_daily_realized_pnl() -> float:
    """
    Fetch today's settled positions from Kalshi and compute realized P&L.
    Returns net USD (positive = profit, negative = loss).
    Non-blocking: returns 0.0 on any error so gates are not disrupted.
    """
    try:
        from data.feeds.kalshi import get_settlements
        from datetime import date
        settlements = await get_settlements()
        today_str   = date.today().isoformat()
        pnl = 0.0
        for s in (settlements or []):
            ts = str(s.get("settled_time") or s.get("created_time") or "")
            if today_str not in ts:
                continue
            revenue = float(s.get("revenue", 0) or 0)
            fees    = float(s.get("fees",    0) or 0)
            pnl    += revenue - fees
        return round(pnl, 2)
    except Exception:
        return 0.0


async def _tool_place_bet(
    ticker: str,
    side: str,
    contracts: int,
    yes_price: int,
    reasoning: str,
    edge_pct: float = 0.0,  # ignored — edge computed server-side
    dry_run: bool = True,
) -> dict:
    import re as _re
    from datetime import timezone
    global _session_spent, _session_bets, _daily_spend, _daily_pnl, _asset_cooldown
    global _reopen_mode, _reopen_mode_expires, _reopen_no_trade_until
    _entry_meta: dict = {}  # populated through gate checks for post-trade attribution
    # ── Server-side edge verification (LLM cannot bypass) ─────────────────────
    is_intraday = ("15M" in ticker.upper() or "15MIN" in ticker.upper()) and "-T" not in ticker.upper()
    min_edge = 10.0 if is_intraday else 8.0
    # Cap contracts to 2 for all market types
    contracts = min(contracts, 2)

    # ── Hard block: 15-min TARGET PRICE markets are lottery tickets ───────────
    # These are KXBTC15M-T69964.89 style tickers. The daily diffusion model
    # applied to a 12-minute window creates false edge. Block them completely.
    _is_15m_target = ("15M" in ticker.upper() or "15MIN" in ticker.upper()) and "-T" in ticker.upper()
    if _is_15m_target:
        return {
            "status": "REJECTED",
            "reason": (
                f"{ticker} is a 15-min target-price market. These are blocked: "
                "the daily diffusion model has no edge on 12-min windows. "
                "Only bet directional 15-min markets (no -T suffix) or daily/hourly markets."
            ),
        }

    # ── Asset restriction: BTC and ETH only (SOL/DOGE/XRP suspended) ─────────
    _tu = ticker.upper()
    _bet_asset = "ETH" if "ETH" in _tu else "BTC"
    if not any(a in _tu for a in ALLOWED_ASSETS):
        return {
            "status": "REJECTED",
            "reason": (
                f"Asset not in approved list {ALLOWED_ASSETS}. "
                "Only BTC and ETH are approved — SOL/DOGE/XRP bets are suspended."
            ),
        }

    # ── Asset cooldown ────────────────────────────────────────────────────────
    import time as _time
    _now_ts = _time.time()
    _cd_exp = _asset_cooldown.get(_bet_asset, 0.0)
    if _now_ts < _cd_exp:
        _cd_left = round((_cd_exp - _now_ts) / 60, 1)
        return {
            "status": "REJECTED",
            "reason": (
                f"Cooldown: {_bet_asset} locked for {_cd_left:.0f} more min "
                f"({_COOLDOWN_MINUTES}-min cooldown after last fill)."
            ),
        }

    # ── Daily stop-loss / stop-win ────────────────────────────────────────────
    # NOTE: _daily_spend = gross cost (stake out), NOT realized loss.
    # $6 stop-loss means: no more than $6 in total stake deployed today.
    # _daily_pnl is net settled revenue; stop-win triggers on confirmed profit.
    if _daily_spend >= _DAILY_STOP_LOSS:
        return {
            "status": "REJECTED",
            "reason": (
                f"Daily stop-loss: ${_daily_spend:.2f} staked today "
                f">= ${_DAILY_STOP_LOSS:.2f} gross-stake limit. No more bets today."
            ),
        }
    if _daily_pnl >= _DAILY_STOP_WIN:
        return {
            "status": "REJECTED",
            "reason": (
                f"Daily stop-win: +${_daily_pnl:.2f} realized today "
                f">= ${_DAILY_STOP_WIN:.2f} target. Banking the gain — done for today."
            ),
        }

    # ── Reopen-mode gate ──────────────────────────────────────────────────────
    # Warm-start.py arms this during the first 10-15 min after session open.
    # Auto-expires when reopen_mode_expires timestamp passes.
    import time as _time_rm
    _now_rm = _time_rm.time()
    if _reopen_mode and _now_rm > _reopen_mode_expires:
        # Expired — disarm
        _reopen_mode = False
    if _reopen_mode:
        if _now_rm < _reopen_no_trade_until:
            _no_trade_left = round((_reopen_no_trade_until - _now_rm) / 60, 1)
            return {
                "status": "REJECTED",
                "reason": (
                    f"Reopen mode: {_no_trade_left:.0f} min remaining in no-trade window. "
                    "Waiting for orderbooks to stabilize after session open."
                ),
            }
        # Apply stricter edge threshold within the reopen window
        min_edge = round(min_edge * _REOPEN_EDGE_MULTIPLIER, 1)

    # ── Session budget gate ───────────────────────────────────────────────────
    _est_cost = round(
        contracts * (yes_price / 100 if side.lower() == "yes" else (100 - yes_price) / 100), 2
    )
    if _session_spent + _est_cost > _SESSION_BUDGET:
        return {
            "status": "REJECTED",
            "reason": (
                f"Session budget cap: ${_session_spent:.2f} already spent, "
                f"this bet ~${_est_cost:.2f} would exceed ${_SESSION_BUDGET:.2f} limit. "
                "Session risk ceiling reached."
            ),
        }

    # ── Adjacent-strike guard ─────────────────────────────────────────────────
    _m_st = _re.search(r"-T([\d.]+)$", ticker)
    _new_strike = float(_m_st.group(1)) if _m_st else None
    if _new_strike is not None:
        for _prev in _session_bets:
            if _prev["asset"] == _bet_asset and _prev["strike"] > 0:
                _dist = abs(_new_strike - _prev["strike"]) / _prev["strike"] * 100
                if _dist < 1.5:
                    return {
                        "status": "REJECTED",
                        "reason": (
                            f"Adjacent-strike guard: {_bet_asset} strike ${_new_strike:,.2f} is "
                            f"{_dist:.1f}% from session bet at ${_prev['strike']:,.2f}. "
                            "Do not stack adjacent strikes — pick one level."
                        ),
                    }

    # ── Portfolio guard: concentration / exposure limits ──────────────────────
    try:
        from engine.portfolio_guard import PortfolioGuard as _PG
        _asset_pg = ("BTC"  if "BTC"  in ticker.upper() else
                     "ETH"  if "ETH"  in ticker.upper() else
                     "SOL"  if "SOL"  in ticker.upper() else
                     "DOGE" if "DOGE" in ticker.upper() else
                     "XRP"  if "XRP"  in ticker.upper() else "BTC")
        _positions_pg = await _tool_get_positions()
        _pg = _PG().check({"asset": _asset_pg}, _positions_pg)
        if not _pg.allowed:
            return {"status": "REJECTED", "reason": f"Portfolio guard: {_pg.reason}"}
    except Exception as _pg_err:
        log.debug("[place_bet] portfolio guard error (non-blocking): %s", _pg_err)

    if is_intraday:
        # Intraday directional markets have no strike price in ticker.
        # Gate: verify ticker appears in neural_edge_picks output with edge >= 10%.
        try:
            from data.feeds.kalshi_intraday import get_intraday_markets
            from data.feeds.btc_momentum    import get_momentum_signals
            from engine.neural_ev           import neural_edge_picks
            _mkts, _mom = await asyncio.gather(get_intraday_markets(), get_momentum_signals())
            _picks = neural_edge_picks(_mkts, _mom, bankroll=500.0, min_edge=0.0)
            _match = next((p for p in _picks if p.get("ticker") == ticker and
                           p.get("side", "").lower() == side.lower()), None)
            _edge = _match.get("edge_pct", 0.0) if _match else -999.0
            # ── Settlement buffer (intraday) ──────────────────────────────────
            _min_rem = float((_match or {}).get("minutes_remaining", 99) or 99)
            if _min_rem < _INTRADAY_MIN_MINUTES_REM:
                return {
                    "status": "REJECTED",
                    "reason": (
                        f"Settlement buffer: {_min_rem:.0f} min remaining — "
                        f"no entries within {_INTRADAY_MIN_MINUTES_REM} min of close."
                    ),
                }
            # ── Vol regime (intraday) ─────────────────────────────────────────
            _ms_in = (_mom or {}).get(_bet_asset.lower()) or (_mom or {}).get(_bet_asset) or {}
            _rv_in = float(_ms_in.get("realized_vol", 0) or 0)
            if _rv_in > 0:
                _vf_in = _VOL_FLOOR.get(_bet_asset, 0.0008)
                _vc_in = _VOL_CEIL.get(_bet_asset,  0.0060)
                if _rv_in < _vf_in:
                    return {"status": "REJECTED", "reason": (
                        f"Vol regime: {_bet_asset} rv={_rv_in:.6f} < floor {_vf_in:.6f}. "
                        "Too quiet — no directional signal.")}
                if _rv_in > _vc_in:
                    return {"status": "REJECTED", "reason": (
                        f"Vol regime: {_bet_asset} rv={_rv_in:.6f} > ceiling {_vc_in:.6f}. "
                        "Crash/spike mode — model invalid.")}
            _entry_meta.update({
                "edge_pct":         _edge,
                "trend":            _ms_in.get("trend", "unknown"),
                "realized_vol":     _rv_in,
                "minutes_to_close": _min_rem,
                "market_type":      "intraday",
            })
            log.info("INTRADAY EDGE CHECK: %s %s | neural_edge=%.1f%% (need %.1f%%)",
                     ticker, side, _edge, min_edge)
            if _edge < min_edge:
                return {
                    "status": "REJECTED",
                    "reason": (
                        f"Intraday neural edge {_edge:.1f}% < {min_edge:.0f}% minimum. "
                        f"{'Market not in picks list.' if _match is None else 'Edge too low.'} "
                        f"Only bet intraday picks from get_intraday_picks() with edge >= 10%."
                    ),
                }
        except Exception as _ie:
            log.warning("Intraday edge gate failed (%s) -- blocking bet to be safe", _ie)
            return {
                "status": "REJECTED",
                "reason": f"Could not verify intraday edge ({_ie}). Bet blocked for safety.",
            }
    else:
        # Daily/hourly markets: use log-normal diffusion edge check
        try:
            from data.feeds.btc_price import get_crypto_prices
            from engine.crypto_ev     import _diffusion_prob_above, _DAILY_VOL as _CVOL
            prices = await get_crypto_prices()
            tu     = ticker.upper()
            asset  = ("BTC" if "BTC" in tu else "ETH" if "ETH" in tu else
                      "SOL" if "SOL" in tu else "DOGE" if "DOGE" in tu else
                      "XRP" if "XRP"  in tu else "BTC")
            spot   = prices.get(asset.lower(), 0.0) or prices.get(asset, 0.0)
            m_thr  = _re.search(r"-T([\d.]+)$", ticker)
            strike = float(m_thr.group(1)) if m_thr else None
            if spot > 0 and strike is not None:
                dv    = _CVOL.get(asset, 0.038)
                hours = 4.0
                raw_m = {}
                try:
                    raw_m  = await _tool_get_market(ticker)
                    ct_str = raw_m.get("close_time", "")
                    if ct_str:
                        ct    = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                        hours = max(0.01, (ct - datetime.now(timezone.utc)).total_seconds() / 3600)
                except Exception:
                    pass
                # ── Settlement buffer (daily) ─────────────────────────────────
                if hours * 3600 < _SETTLEMENT_BUFFER_SECS:
                    return {
                        "status": "REJECTED",
                        "reason": (
                            f"Settlement buffer: {hours*60:.1f} min remaining "
                            f"< {_SETTLEMENT_BUFFER_SECS//60} min minimum. "
                            "Too close to settlement — do not enter."
                        ),
                    }
                # ── Market quality (daily) ────────────────────────────────────
                _raw_oi = int(raw_m.get("open_interest", 0) or 0)
                # Fetch live orderbook for accurate bid/ask.
                # The market object (/markets/{ticker}) does not reliably include
                # yes_bid/yes_ask — those fields are often absent, which would make
                # the spread silently compute as 0. Use the orderbook endpoint instead.
                _yes_bid, _yes_ask, _spread_c = yes_price, yes_price, 0
                try:
                    from data.feeds.kalshi import get_market_orderbook as _get_ob
                    _ob = (await _get_ob(ticker)) or {}
                    # Kalshi orderbook: yes = [[price, size]...] best bids (desc)
                    #                   no  = [[price, size]...] best no-bids (desc)
                    # yes_ask = 100 - best_no_bid (lowest price to sell YES)
                    _yb_list = _ob.get("yes", [])
                    _nb_list = _ob.get("no",  [])
                    if _yb_list:
                        _yes_bid = int(_yb_list[0][0])
                    if _nb_list:
                        _yes_ask = 100 - int(_nb_list[0][0])
                    _spread_c = max(0, _yes_ask - _yes_bid)
                except Exception:
                    # Fall back to market object fields if orderbook unavailable
                    _yes_bid  = int(raw_m.get("yes_bid",  yes_price) or yes_price)
                    _yes_ask  = int(raw_m.get("yes_ask",  yes_price) or yes_price)
                    _spread_c = abs(_yes_ask - _yes_bid)
                if 0 < _raw_oi < _MIN_OPEN_INTEREST:
                    return {
                        "status": "REJECTED",
                        "reason": (
                            f"Market quality: OI={_raw_oi} < minimum {_MIN_OPEN_INTEREST}. "
                            "Illiquid market — skip."
                        ),
                    }
                if _spread_c > _MAX_SPREAD_CENTS:
                    return {
                        "status": "REJECTED",
                        "reason": (
                            f"Market quality: spread={_spread_c}c > max {_MAX_SPREAD_CENTS}c "
                            f"(best_bid={_yes_bid}c, best_ask={_yes_ask}c). "
                            "Wide spread eats edge — skip."
                        ),
                    }
                # ── Vol regime (daily) ────────────────────────────────────────
                try:
                    from data.feeds.btc_momentum import get_momentum_signals as _gms_d
                    _mom_d_raw = await _gms_d()
                    _ms_d = (_mom_d_raw or {}).get(_bet_asset.lower()) or \
                            (_mom_d_raw or {}).get(_bet_asset) or {}
                    _rv_d = float(_ms_d.get("realized_vol", 0) or 0)
                    if _rv_d > 0:
                        _vfd = _VOL_FLOOR.get(_bet_asset, 0.0008)
                        _vcd = _VOL_CEIL.get(_bet_asset,  0.0060)
                        if _rv_d < _vfd:
                            return {"status": "REJECTED", "reason": (
                                f"Vol regime: {_bet_asset} rv={_rv_d:.6f} < floor {_vfd:.6f}. "
                                "Too quiet — no directional signal.")}
                        if _rv_d > _vcd:
                            return {"status": "REJECTED", "reason": (
                                f"Vol regime: {_bet_asset} rv={_rv_d:.6f} > ceiling {_vcd:.6f}. "
                                "Crash/spike mode — model invalid.")}
                    _entry_meta.update({
                        "trend":          _ms_d.get("trend", "unknown"),
                        "realized_vol":   _rv_d,
                        "oi":             _raw_oi,
                        "spread_cents":   _spread_c,
                        "minutes_to_close": round(hours * 60, 1),
                        "market_type":    "daily",
                    })
                except Exception:
                    pass
                model_p   = _diffusion_prob_above(spot, strike, hours, dv)
                model_prob = model_p if side.lower() == "yes" else (1.0 - model_p)
                mkt_prob   = (yes_price / 100.0) if side.lower() == "yes" else ((100 - yes_price) / 100.0)
                computed_edge = (model_prob - mkt_prob) * 100.0
                _entry_meta["edge_pct"]   = round(computed_edge, 2)
                _entry_meta["model_prob"] = round(model_prob * 100, 1)
                _entry_meta["mkt_prob"]   = round(mkt_prob   * 100, 1)
                log.info("DAILY EDGE CHECK: %s %s | spot=%.2f strike=%.2f hrs=%.2f | model=%.1f%% mkt=%.1f%% edge=%+.1f%%",
                         ticker, side, spot, strike, hours, model_prob * 100, mkt_prob * 100, computed_edge)
                if computed_edge < min_edge:
                    return {
                        "status": "REJECTED",
                        "reason": (
                            f"Server edge {computed_edge:.1f}% < {min_edge:.0f}% minimum. "
                            f"model={model_prob*100:.1f}% mkt={mkt_prob*100:.1f}%. "
                            f"Find a market with stronger mispricing."
                        ),
                    }
        except Exception as _eg:
            log.warning("Daily edge gate failed (%s) -- blocking bet to be safe", _eg)
            return {
                "status": "REJECTED",
                "reason": f"Could not verify daily edge ({_eg}). Bet blocked for safety.",
            }
    # Enforce minimum tradeable price — Kalshi rejects orders below ~10¢
    if yes_price < 10 or yes_price > 90:
        return {
            "status": "REJECTED",
            "reason": f"yes_price {yes_price}¢ is outside the 10-90¢ tradeable range. "
                      f"Pick a market where the price is between 10 and 90 cents.",
        }

    if dry_run:
        cost = round(contracts * (yes_price / 100 if side == "yes" else (100 - yes_price) / 100), 2)
        _session_spent += cost
        _daily_spend   += cost
        if _new_strike is not None:
            _session_bets.append({"asset": _bet_asset, "strike": _new_strike, "side": side})
        _asset_cooldown[_bet_asset] = _time.time() + _COOLDOWN_MINUTES * 60
        _save_daily_state()
        return {
            "status":     "DRY_RUN",
            "ticker":     ticker,
            "side":       side,
            "contracts":  contracts,
            "yes_price":  yes_price,
            "cost_usd":   cost,
            "reasoning":  reasoning,
            "entry_meta": _entry_meta,
            "message":    "Dry run - no real money placed. Pass --live to enable.",
        }

    from data.feeds.kalshi import place_order
    try:
        resp = await place_order(
            ticker=ticker, side=side, count=contracts, yes_price=yes_price
        )
        if "error" in resp:
            return {"status": "ERROR", "error": resp["error"]}
        order = resp.get("order", {})
        _fill_cost = round(contracts * (yes_price / 100 if side == "yes" else (100 - yes_price) / 100), 2)
        log.info("AUTONOMOUS BET PLACED: %s %s x%d @ %dc  reason=%s",
                 ticker, side.upper(), contracts, yes_price, reasoning)
        _session_spent += _fill_cost
        _daily_spend   += _fill_cost
        if _new_strike is not None:
            _session_bets.append({"asset": _bet_asset, "strike": _new_strike, "side": side})
        _asset_cooldown[_bet_asset] = _time.time() + _COOLDOWN_MINUTES * 60
        _save_daily_state()
        _bet_result = {
            "status":     "PLACED",
            "order_id":   order.get("order_id", ""),
            "ticker":     ticker,
            "side":       side,
            "contracts":  contracts,
            "yes_price":  yes_price,
            "reasoning":  reasoning,
            "entry_meta": _entry_meta,
        }
        # ── Record CLV entry for edge validation ────────────────────────
        try:
            from engine.clv_tracker import CLVTracker as _CLVTracker
            _CLVTracker().record_entry(
                order_id=_bet_result["order_id"],
                ticker=ticker, side=side,
                entry_price_cents=yes_price,
                edge_pct=_entry_meta.get("edge_pct", edge_pct),
                asset=_bet_asset,
                market_type=_entry_meta.get("market_type", "daily"),
            )
        except Exception as _clv_err:
            log.debug("[place_bet] CLV record failed (non-blocking): %s", _clv_err)
        return _bet_result
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}


async def _tool_get_positions() -> list[dict]:
    from data.feeds.kalshi import get_portfolio
    try:
        return await get_portfolio()
    except Exception as e:
        return [{"error": str(e)}]


async def _tool_get_crypto_snapshot() -> dict:
    """
    Unified crypto intelligence snapshot:
      - live spot prices (BTC, ETH)
      - short-term momentum (5m, 15m, realized vol, trend)
      - pre-computed edge picks from log-normal diffusion model
    Applies momentum bias: trending asset volatility bumped ±20% so the
    model aligns direction with market microstructure.
    """
    import asyncio
    from data.feeds.btc_price import get_crypto_prices
    from data.feeds.btc_momentum import get_momentum_signals
    from data.feeds.kalshi_crypto import get_crypto_markets
    from engine.crypto_ev import price_edge_picks, _DAILY_VOL

    try:
        prices_task   = get_crypto_prices()
        momentum_task = get_momentum_signals()
        markets_task  = get_crypto_markets(min_hours=0.25, max_hours=72.0)
        prices, momentum, markets = await asyncio.gather(
            prices_task, momentum_task, markets_task, return_exceptions=True
        )
        if isinstance(prices,   Exception): prices   = {"btc": 0.0, "eth": 0.0}
        if isinstance(momentum, Exception): momentum = {}
        if isinstance(markets,  Exception): markets  = []

        # Momentum bias: adjust per-asset vol so the diffusion model tilts
        # in the direction the market is already moving.
        # Uptrend → slightly lower vol (price more likely to stay above) for
        # ABOVE strikes; downtrend → slightly higher vol spreads probability.
        adjusted_prices = dict(prices)
        momentum_summary = {}
        for asset, sig in momentum.items():
            if not sig:
                continue
            key = asset.lower()
            mom5 = sig.get("mom_5m", 0.0) or 0.0
            mom15 = sig.get("mom_15m", 0.0) or 0.0
            trend = sig.get("trend", "flat")
            rv    = sig.get("realized_vol", 0.0) or 0.0
            cur   = sig.get("current", 0.0) or 0.0
            momentum_summary[asset] = {
                "current_price": round(cur, 2),
                "mom_5m_pct":    round(mom5 * 100, 3),
                "mom_15m_pct":   round(mom15 * 100, 3),
                "trend":         trend,
                "realized_vol_per_5m": round(rv, 6),
                "annualized_vol_est":  round(rv * (288 ** 0.5) * 100, 1),  # % annualized
            }
            # Override spot price with live candle anchor if available
            if cur > 0 and key in ("btc", "eth"):
                adjusted_prices[key] = cur

        # Run the diffusion model with live-anchored prices.
        # bankroll=500 so the Kelly stake filter (>$1) doesn't kill all picks
        # on a small account — we scale the output down to real bankroll later.
        picks = price_edge_picks(
            markets, adjusted_prices, bankroll=500.0, min_edge=0.04
        )

        # Filter and enrich picks for the brain
        actionable = []
        for p in picks:
            meta = p.get("crypto_meta", {})
            ticker = p.get("market", "")
            asset_key = meta.get("asset", "").lower()
            mom_sig = momentum_summary.get(meta.get("asset", ""), {})
            side = meta.get("side", "YES")
            yes_price_cents = int(round(float(p.get("decimal_odds", 1) and
                (meta.get("market_prob", 50)))))
            # yes_price_cents is ALWAYS the YES-side market price (what Kalshi calls yes_ask).
            # For YES bets: the brain pays yes_price_cents per contract.
            # For NO bets:  the brain passes yes_price_cents to place_bet(yes_price=...) and
            #               actually pays (100 - yes_price_cents) per NO contract.
            yes_price_cents = int(round(meta.get("market_prob", 50)))

            # Skip bets below minimum tradeable price
            if yes_price_cents < 10 or yes_price_cents > 90:
                continue

            actionable.append({
                "ticker":          ticker,
                "title":           p.get("event", ""),
                "asset":           meta.get("asset", ""),
                "threshold":       meta.get("threshold", 0),
                "current_price":   meta.get("current_price", adjusted_prices.get(asset_key, 0)),
                "hours_to_close":  meta.get("hours_to_close", 0),
                "side":            side,
                "yes_price_cents": yes_price_cents,
                "model_prob_pct":  meta.get("model_prob", 0),
                "market_prob_pct": meta.get("market_prob", 0),
                "edge_pct":        p.get("edge_pct", 0),
                "ev_pct":          p.get("ev_pct", 0),
                "verdict":         p.get("verdict", ""),
                "momentum_trend":  mom_sig.get("trend", "unknown"),
                "mom_5m_pct":      mom_sig.get("mom_5m_pct", 0),
                "kelly_stake_usd": round(p.get("recommended_stake", 0), 3),
            })

        # Sort by edge descending
        actionable.sort(key=lambda x: x["edge_pct"], reverse=True)

        return {
            "spot_prices":      {k: round(v, 2) for k, v in adjusted_prices.items()},
            "momentum":         momentum_summary,
            "model_picks":      actionable[:12],
            "total_markets":    len(markets),
            "strong_picks":     [x for x in actionable if x["edge_pct"] >= 8],
            "timestamp":        datetime.now().isoformat(),
        }
    except Exception as e:
        return {"error": str(e), "spot_prices": {}, "model_picks": [], "strong_picks":[]}


async def _tool_run_deep_analysis(ticker: str, side: str = "yes") -> dict:
    """
    Deep quantitative validation using 5 models simultaneously.
    Models: log-normal diffusion × 3 vol, Monte Carlo 10k GBM, RSI,
            Bollinger Bands, KalshiNet neural (math fallback if no .pt file).
    Returns consensus grade A+–F, action BET_NOW/LEAN/FADE/WAIT, full Kelly.
    """
    import math
    import random
    import re
    from datetime import timezone
    from data.feeds.btc_price    import get_crypto_prices
    from data.feeds.btc_momentum import get_momentum_signals
    from engine.crypto_ev        import _diffusion_prob_above, _DAILY_VOL as CRYPTO_VOL

    try:
        # ── 1. Parallel fetch ──────────────────────────────────────────────────
        market_data, prices, momentum = await asyncio.gather(
            _tool_get_market(ticker),
            get_crypto_prices(),
            get_momentum_signals(),
            return_exceptions=True,
        )
        if isinstance(market_data, Exception):
            return {"error": str(market_data), "ticker": ticker}
        if isinstance(prices, Exception):
            prices = {}
        if isinstance(momentum, Exception):
            momentum = {}
        if market_data.get("error"):
            return market_data

        # ── 2. Market basics ───────────────────────────────────────────────────
        yes_prob = float(market_data.get("yes_prob") or 0.50)
        yes_ask  = round(yes_prob * 100)
        no_ask   = 100 - yes_ask
        price    = yes_ask if side.lower() == "yes" else no_ask

        # ── 3. Asset detection ─────────────────────────────────────────────────
        tu = ticker.upper()
        asset = ("BTC" if "BTC" in tu else "ETH" if "ETH" in tu else
                 "SOL" if "SOL" in tu else "DOGE" if "DOGE" in tu else
                 "XRP" if "XRP"  in tu else "BTC")

        sig     = momentum.get(asset, {})
        current = sig.get("current", 0.0) or prices.get(asset.lower(), 0.0)
        closes  = sig.get("closes") or []

        # ── 4. Threshold + hours ───────────────────────────────────────────────
        m_thresh  = re.search(r"-T([\d.]+)$", ticker)
        threshold = float(m_thresh.group(1)) if m_thresh else None

        hours = 4.0
        ct_str = market_data.get("close_time", "")
        if ct_str:
            try:
                ct    = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                hours = max(0.05, (ct - datetime.now(timezone.utc)).total_seconds() / 3600)
            except Exception:
                pass

        is_15min          = "15M" in tu
        minutes_remaining = hours * 60

        # ── 5. Volatility ──────────────────────────────────────────────────────
        dv_base = CRYPTO_VOL.get(asset, 0.038)
        rv      = sig.get("realized_vol", 0.0) or 0.0
        dv_real = max(rv * (288 ** 0.5), dv_base * 0.5) if rv > 0.00005 else dv_base

        # ── 6. RSI ─────────────────────────────────────────────────────────────
        rsi = None
        rsi_interp = "N/A"
        if len(closes) >= 4:
            ch     = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains  = [max(c, 0.0) for c in ch]
            losses = [abs(min(c, 0.0)) for c in ch]
            ag = sum(gains)  / len(gains)
            al = sum(losses) / len(losses)
            rsi = round(100.0 - (100.0 / (1.0 + ag / al)), 1) if al > 0 else 100.0
            rsi_interp = ("OVERBOUGHT (RSI>70) — bearish bias" if rsi > 70 else
                          "OVERSOLD (RSI<30) — bullish bias"    if rsi < 30 else "NEUTRAL")

        # ── 7. Bollinger Bands ─────────────────────────────────────────────────
        bollinger = None
        if len(closes) >= 4 and current > 0:
            n   = min(len(closes), 8)
            sma = sum(closes[-n:]) / n
            std = (sum((x - sma) ** 2 for x in closes[-n:]) / n) ** 0.5
            upp = sma + 2 * std
            low = sma - 2 * std
            pb  = (current - low) / (upp - low) if (upp - low) > 0 else 0.5
            bollinger = {
                "sma":   round(sma, 2),
                "upper": round(upp, 2),
                "lower": round(low, 2),
                "pct_b": round(pb,  3),
                "interp": ("OVERBOUGHT (%B>0.8) — bearish signal" if pb > 0.8 else
                           "OVERSOLD (%B<0.2) — bullish signal"   if pb < 0.2 else "NEUTRAL"),
            }

        # ── 8. Log-normal diffusion × 3 vol scenarios ─────────────────────────
        diffusion = None
        if threshold and threshold > 0 and current > 0 and hours > 0:
            pb = _diffusion_prob_above(current, threshold, hours, dv_base)
            pr = _diffusion_prob_above(current, threshold, hours, dv_real)
            ph = _diffusion_prob_above(current, threshold, hours, dv_base * 1.20)
            pl = _diffusion_prob_above(current, threshold, hours, dv_base * 0.80)
            diffusion = {
                "prob_base_vol":     round(pb * 100, 1),
                "prob_realized_vol": round(pr * 100, 1),
                "prob_high_vol":     round(ph * 100, 1),
                "prob_low_vol":      round(pl * 100, 1),
                "prob_avg":          round((pb + pr + ph + pl) / 4 * 100, 1),
                "formula": f"d=[ln(S/{threshold})+(−0.5σ²t)]/(σ√t)  P=Φ(d)",
            }

        # ── 9. Monte Carlo (10k paths, Box-Muller GBM) ────────────────────────
        mc_prob = None
        if threshold and threshold > 0 and current > 0 and hours > 0:
            t      = hours / 24.0
            sigma  = dv_real * math.sqrt(t)
            drift  = -0.5 * (dv_real ** 2) * t
            above  = sum(
                1 for _ in range(10_000)
                if current * math.exp(
                    drift + sigma * math.sqrt(-2.0 * math.log(max(random.random(), 1e-15)))
                    * math.cos(2.0 * math.pi * random.random())
                ) > threshold
            )
            mc_prob = round(above / 100, 1)   # percent

        # ── 10. Intraday blend (≤ 30-min markets only) ────────────────────────
        intraday_blend = None
        if threshold and current > 0 and minutes_remaining <= 30:
            from engine.intraday_ev import (
                _position_prob, _momentum_prob, _blend_prob,
                _DAILY_VOL as INTR_VOL,
            )
            dv2   = INTR_VOL.get(asset, 0.038)
            m5    = sig.get("mom_5m",  0.0)
            m15   = sig.get("mom_15m", 0.0)
            trend = sig.get("trend",   "flat")
            pp    = _position_prob(current, threshold, minutes_remaining, dv2)
            pm    = _momentum_prob(m5, m15, trend, minutes_remaining)
            bl    = _blend_prob(pp, pm, minutes_remaining)
            tf    = min(minutes_remaining, 15) / 15.0
            wm    = round((0.25 + (1.0 - tf) * 0.50) * 100)
            intraday_blend = {
                "position_prob": round(pp * 100, 1),
                "momentum_prob": round(pm * 100, 1),
                "blend_prob":    round(bl * 100, 1),
                "weights":       f"{100-wm}% position / {wm}% momentum",
            }

        # ── 11. KalshiNet neural inference ────────────────────────────────────
        neural_result = None
        try:
            from engine.neural_ev import _neural_prob
            nn_prob, used_nn = _neural_prob(sig, threshold or current, minutes_remaining, asset)
            if nn_prob is not None:
                neural_result = {
                    "prob":        round(nn_prob * 100, 1),
                    "used_neural": used_nn,
                    "model":       ("KalshiNet 13-feat residual MLP" if used_nn
                                   else "math blend fallback"),
                    "note":        "on-domain" if is_15min else "experimental (trained on 15-min data)",
                }
        except Exception:
            pass

        # ── 12. Aggregate + consensus ─────────────────────────────────────────
        model_probs = []
        if diffusion:          model_probs.append(diffusion["prob_avg"]   / 100)
        if mc_prob is not None: model_probs.append(mc_prob                / 100)
        if intraday_blend:     model_probs.append(intraday_blend["blend_prob"] / 100)
        if neural_result:       model_probs.append(neural_result["prob"]   / 100)
        avg_p = sum(model_probs) / len(model_probs) if model_probs else yes_prob

        edge = (avg_p - yes_ask / 100) if side.lower() == "yes" else ((1.0 - avg_p) - no_ask / 100)
        ea = abs(edge)
        nm = len(model_probs)
        grade  = ("A+" if ea >= 0.12 and nm >= 3 else "A" if ea >= 0.10 and nm >= 2 else
                  "B"  if ea >= 0.08 and nm >= 2 else "C" if ea >= 0.06 else
                  "D"  if ea >= 0.04              else "F")
        action = ("BET_NOW" if edge > 0.08 else "LEAN" if edge > 0.05 else
                  "FADE"    if edge < 0.00 else "WAIT")

        # ── 12b. Regime + weighted ensemble + calibration + abstain ──────────
        _regime_info   = {}
        _ensemble_info = {}
        _calib_info    = {}
        _abstain_info  = {"abstained": False, "reason": None, "detail": "enrichment not run"}
        try:
            from engine.regime      import classify_regime
            from engine.ensemble    import WeightedEnsemble
            from engine.calibration import CalibrationStore
            from engine.abstain     import should_abstain

            _regime = classify_regime(sig, hours, asset)
            _regime_info = _regime.to_dict()

            _mprobs: dict[str, float] = {}
            if diffusion:            _mprobs["diffusion"]   = diffusion["prob_avg"] / 100
            if mc_prob is not None:  _mprobs["monte_carlo"] = mc_prob / 100
            if neural_result:        _mprobs["neural"]      = neural_result["prob"] / 100
            if bollinger:
                _pb = bollinger["pct_b"]
                _mprobs["technical"] = (1 - _pb) if side.lower() == "yes" else _pb

            _ens = WeightedEnsemble().run(_mprobs, _regime)
            _ensemble_info = _ens.to_dict()
            avg_p = _ens.weighted_prob   # replace naive average

            _cal_store = CalibrationStore()
            _cal_p, _cal_meta = _cal_store.calibrate(
                avg_p, asset, _regime.vol_regime, _regime.ttc_bucket, _regime.trend
            )
            _calib_info = _cal_meta
            _cal_edge = (_cal_p - yes_ask / 100) if side.lower() == "yes" else ((1 - _cal_p) - no_ask / 100)

            _pick_a = {"asset": asset, "ttc": hours, "yes_price": yes_ask / 100}
            _abstain, _ab_reason, _ab_detail = should_abstain(
                pick=_pick_a, regime=_regime, ensemble=_ens, calibrated_edge_pct=_cal_edge,
            )
            _abstain_info = {
                "abstained": _abstain,
                "reason":    _ab_reason.value if _ab_reason else None,
                "detail":    _ab_detail,
            }
            # Upgrade edge + grade + action with calibrated values
            edge   = _cal_edge
            ea     = abs(edge)
            nm     = _ens.n_models
            grade  = ("A+" if ea >= 0.12 and nm >= 3 else "A" if ea >= 0.10 and nm >= 2 else
                      "B"  if ea >= 0.08 and nm >= 2 else "C" if ea >= 0.06 else
                      "D"  if ea >= 0.04              else "F")
            action = ("ABSTAIN" if _abstain else
                      "BET_NOW" if edge > 0.08 else
                      "LEAN"    if edge > 0.05 else
                      "FADE"    if edge < 0.00 else "WAIT")

        except Exception as _enrichment_err:
            log.debug("[run_deep] regime/ensemble enrichment failed: %s", _enrichment_err)

        # ── 13. Kelly breakdown ────────────────────────────────────────────────
        pd   = max(0.01, min(0.99, price / 100))
        b    = (1.0 / pd) - 1.0
        p_w  = avg_p if side.lower() == "yes" else (1.0 - avg_p)
        kf   = max(0.0, (b * p_w - (1.0 - p_w)) / b)
        fk   = kf * 0.25
        kelly = {
            "formula":     "f★=(b×p−q)/b  [b=(1/price)−1,  p=win_prob,  q=1−p]",
            "b":           round(b,   3),
            "p_win":       round(p_w, 3),
            "full_kelly":  round(kf,  4),
            "qtr_kelly":   round(fk,  4),
            "stake_usd":   round(fk * 500, 2),
            "rec_contracts": max(1, min(5, int(fk * 500 / max(pd * 100, 1)))),
            "ev_pct":      round(edge * 100, 2),
        }

        return {
            "ticker":          ticker,
            "asset":           asset,
            "side":            side,
            "current_price":   round(current, 2),
            "threshold":       threshold,
            "hours_to_close":  round(hours, 2),
            "yes_price_cents": yes_ask,
            "model_prob_avg":  round(avg_p * 100, 1),
            "edge_pct":        round(edge * 100, 2),
            "grade":           grade,
            "action":          action,
            "n_models":        nm,
            "rsi":             rsi,
            "rsi_interp":      rsi_interp,
            "bollinger":       bollinger,
            "diffusion":       diffusion,
            "monte_carlo_pct": mc_prob,
            "intraday_blend":  intraday_blend,
            "neural":          neural_result,
            "kelly":           kelly,
            "regime":          _regime_info,
            "ensemble":        _ensemble_info,
            "calibration":     _calib_info,
            "abstain":         _abstain_info,
        }

    except Exception as e:
        return {"error": str(e), "ticker": ticker}


async def _tool_get_intraday_picks() -> dict:
    """
    Scan Kalshi 15-minute directional markets using KalshiNet + math fallback.
    Returns BTC/ETH UP or DOWN picks for the current 15-min window (SOL/DOGE/XRP disabled).
    """
    try:
        from data.feeds.kalshi_intraday import get_intraday_markets
        from data.feeds.btc_momentum    import get_momentum_signals
        from engine.neural_ev           import neural_edge_picks

        markets, momentum = await asyncio.gather(
            get_intraday_markets(),
            get_momentum_signals(),
            return_exceptions=True,
        )
        if isinstance(markets,  Exception): markets  = []
        if isinstance(momentum, Exception): momentum = {}

        if not markets:
            return {
                "picks":   [],
                "message": "No 15-min directional markets open right now. "
                           "Try again near the quarter-hour mark.",
            }

        # Restrict to approved assets only (BTC and ETH)
        markets = [m for m in markets if any(a in m.get("asset", "").upper() for a in ALLOWED_ASSETS)]
        picks = neural_edge_picks(markets, momentum, bankroll=500.0, min_edge=0.10)

        formatted = []
        for p in picks:
            meta    = p.get("intraday_meta", {})
            side    = p.get("side", "yes").lower()
            implied = p.get("implied_prob", 50)
            # place_bet() always takes the YES price:
            # YES side: paying yes_ask → pass yes_ask ≈ implied
            # NO  side: paying no_ask  → yes_price ≈ 100 - implied
            yp = int(round(implied)) if side == "yes" else int(round(100 - implied))
            formatted.append({
                "ticker":            p["market"],
                "asset":             meta.get("asset", ""),
                "side":              side,
                "yes_price_cents":   yp,
                "floor_strike":      meta.get("floor_strike", 0),
                "current_price":     meta.get("current_price", 0),
                "gap_pct":           meta.get("gap_pct", 0),
                "minutes_remaining": p.get("minutes_remaining", 0),
                "model_prob_pct":    p.get("our_prob", 0),
                "market_prob_pct":   implied,
                "edge_pct":          p.get("edge_pct", 0),
                "ev_pct":            p.get("ev_pct", 0),
                "momentum_trend":    meta.get("trend", "flat"),
                "mom_5m_pct":        meta.get("mom_5m_pct", 0),
                "used_neural":       meta.get("used_neural", False),
                "verdict":           p.get("verdict", ""),
                "confidence":        meta.get("confidence", 0),
            })

        return {
            "picks":          formatted[:10],
            "total_markets":  len(markets),
            "strong_picks":   [p for p in formatted if p["edge_pct"] >= 8],
            "assets_scanned": list({m["asset"] for m in markets}),
            "timestamp":      datetime.now().isoformat(),
            "note": (
                "Resolve YES if asset price at 15-min close >= opening price. "
                "yes_price_cents = pass directly to place_bet(yes_price=...)."
            ),
        }

    except Exception as e:
        return {"error": str(e), "picks": []}


# ── Tool dispatcher ───────────────────────────────────────────────────────────

async def dispatch_tool(name: str, args: dict, dry_run: bool) -> Any:
    if name == "get_balance":
        return await _tool_get_balance()
    if name == "scan_markets":
        return await _tool_scan_markets(args.get("series", ""))
    if name == "get_market":
        return await _tool_get_market(args["ticker"])
    if name == "analyze_edge":
        return await _tool_analyze_edge(
            args["ticker"], args["side"], float(args["our_prob"]), float(args["bankroll"])
        )
    if name == "place_bet":
        return await _tool_place_bet(
            args["ticker"], args["side"], int(args["contracts"]),
            int(args.get("yes_price") or args.get("yes_price_cents", 50)),
            args.get("reasoning", ""),
            dry_run=dry_run,
        )
    if name == "get_positions":
        return await _tool_get_positions()
    if name == "get_crypto_snapshot":
        return await _tool_get_crypto_snapshot()
    if name == "run_deep_analysis":
        return await _tool_run_deep_analysis(args.get("ticker", ""), args.get("side", "yes"))
    if name == "get_intraday_picks":
        return await _tool_get_intraday_picks()
    return {"error": f"unknown tool: {name}"}


# ── Agent loop ────────────────────────────────────────────────────────────────

AGENT_SYSTEM = """You are KALISHI CRYPTO — a self-directed prediction market agent with a full quantitative 
edge stack: log-normal diffusion, Monte Carlo simulation, RSI, Bollinger Bands, and KalshiNet 
neural inference. You run your models, identify positive-EV bets, and place them.

════════════════════════════════════════════════════════════
  FORMULA REFERENCE  (use these to verify and reason)
════════════════════════════════════════════════════════════
• Kelly:   f★ = (b×p − q) / b      b=(1/price)−1,  p=win_prob,  q=1−p
           Use ¼-Kelly: stake = f★ × 0.25 × bankroll
           → f★=0.20, bankroll=$50 → stake=$2.50, buy 2-3 contracts at 10-40¢

• EV:      EV% = p×(1/price − 1) − (1−p)   → positive = profitable long-run

• Log-normal P(above K):
           d = [ln(S/K) + (−0.5σ²t)] / (σ√t)    P = Φ(d)
           drift = −0.5σ²t  (Ito correction, keeps model risk-neutral)
           σ = daily_vol × √(t_days)
           BTC daily vol = 3.8%, ETH = 4.2%,  hour ≈ daily/√24

• Monte Carlo: GBM via Box-Muller
           z = √(−2·ln u₁) × cos(2π·u₂)        u₁,u₂ ~ Uniform(0,1)
           S_T = S₀ × exp(drift + σ·z)          P(above K) = fraction > K

• RSI:     RS = avg_gain / avg_loss  (14 periods)
           RSI = 100 − 100/(1+RS)
           RSI > 70 = overbought (lean bearish/NO), RSI < 30 = oversold (lean bullish/YES)

• Bollinger %B = (price − lower) / (upper − lower)
           bands  = SMA₂₀ ± 2σ
           %B > 0.8 = overbought (lean NO), %B < 0.2 = oversold (lean YES)
           %B hitting band + momentum reversal = strong signal

════════════════════════════════════════════════════════════
  WORKFLOW — follow this every session
════════════════════════════════════════════════════════════
STEP 1 — INTELLIGENCE SWEEP (always do all three)
   get_balance()           → know your bankroll
   get_crypto_snapshot()   → live BTC/ETH prices + momentum + pre-scored picks
   get_intraday_picks()    → 15-min BTC/ETH directional markets only

STEP 2 — VALIDATE TOP PICKS (for any pick with edge ≥ 8% or before >2 contracts)
   run_deep_analysis(ticker, side) → gets grade A+–F, Monte Carlo, RSI, Bollinger,
                                     neural prob, full Kelly formula breakdown
   • Grade A+/A  = BET_NOW         • Grade B       = LEAN (1-2 contracts max)
   • Grade C/D   = WAIT            • Grade F       = SKIP

STEP 3 — EXECUTE
   analyze_edge(ticker, side, model_prob/100, bankroll)   → final EV check
   place_bet(ticker, side, contracts, yes_price_cents, reasoning)

════════════════════════════════════════════════════════════
  MARKET TYPES
════════════════════════════════════════════════════════════
DAILY/HOURLY (KXBTCD / KXETH):
   KXBTCD-26APR09-T85000 = "Will BTC close ABOVE $85,000 on Apr 9?"
   Ticker format: -T<threshold>   threshold in dollars
   yes_price = market's implied probability BTC stays above threshold
   Example: BTC=$84,500,  model says 55%,  market says 42¢  → edge=+13%
     → place_bet("KXBTCD-26APR09-T85000", "yes", 3, 42, "model 55% vs market 42%")

15-MINUTE DIRECTIONAL (KXBTC15M / KXETH15M only — SOL/DOGE/XRP disabled):
   These resolve YES if price at 15-min CLOSE ≥ price at 15-min OPEN
   = pure UP/DOWN bets.  New market every 15 minutes.
   floor_strike = opening reference price (BRTI 60-sec average at window open)
   Example: BTC gaining +0.15% in 5 min, momentum up, model 64% YES at 51¢
     → edge = 13%, grade B → place_bet(ticker, "yes", 2, 51, "momentum 64% vs 51¢")
   WHEN TO USE: call get_intraday_picks() first, then validate with run_deep_analysis()

════════════════════════════════════════════════════════════
  HOW TO READ MODEL OUTPUTS
════════════════════════════════════════════════════════════
From get_crypto_snapshot():
   model_prob=55%, market_prob=42%, edge=+13%, trend=up → BET YES at 42¢ (confirmed)
   model_prob=35%, market_prob=48%, edge=+13% on NO side → BET NO, yes_price=48
   NOTE: yes_price is ALWAYS the YES market price (even for NO bets)

From run_deep_analysis():
   grade="A+", action="BET_NOW", n_models=4 → highest confidence
   diffusion.prob_avg=58%, monte_carlo=57%, neural.prob=56% → models agree
   rsi=28 (OVERSOLD) + bollinger.pct_b=0.15 (OVERSOLD) → double bullish signal
   kelly.rec_contracts=3, kelly.stake_usd=$4.50 → size the bet

From get_intraday_picks():
   gap_pct=+0.12% (price moved UP from open), trend=up, model=64%
   minutes_remaining=8 → blend is 60% momentum / 40% position
   → high-confidence YES bet if edge ≥ 8%

════════════════════════════════════════════════════════════
  MOMENTUM ALIGNMENT RULES
════════════════════════════════════════════════════════════
  Uptrend   + YES bet (price stays/goes above threshold) = CONFIRMED   → full Kelly
  Downtrend + NO  bet (price falls below threshold)      = CONFIRMED   → full Kelly
  Uptrend   + NO  bet                                    = CAUTIOUS    → 1 contract only
  Downtrend + YES bet                                    = CAUTIOUS    → 1 contract only
  Flat trend                                             = NEUTRAL     → model prob alone

Technical confluence (RSI + Bollinger + momentum all aligned) = add 1 extra contract

════════════════════════════════════════════════════════════
  STRICT RULES (enforced — violations auto-rejected)
════════════════════════════════════════════════════════════
  X NEVER bet SOL, DOGE, or XRP — BTC only; ETH only as second choice
  X NEVER bet if yes_price < 10 or > 90 cents
  X NEVER bet more than 2 contracts per market (all market types)
  X NEVER bet if edge_pct < 8% for daily markets, < 10% for 15-min intraday
  X NEVER bet intraday if momentum_trend is FLAT — skip, flat = no signal
  X NEVER place two bets on the same asset with strikes within 1.5% of each other
  X NEVER mix YES and NO on adjacent strikes in the same session
  OK Max 3 bets per session, max $3.00 TOTAL session spend across all bets
  OK ONE setup per session: momentum continuation after a real break OR exhaustion fade — not both
  OK ONE asset per session: BTC first; ETH only if BTC has no clear setup
  OK 15-MIN INTRADAY only when: edge >= 10% AND momentum clearly confirms direction
  OK Use run_deep_analysis() before placing any daily market bet
  OK Show your math: "BTC=$85k | model=58% | market=42c | edge=+16% | grade=A+ | placing 2 YES"
  OK Then call place_bet() immediately — do not describe without acting"""


async def run_agent(dry_run: bool = True, max_bets: int = 3) -> dict:
    """Run one full autonomous betting session."""
    from agents.brain import get_brain

    brain = get_brain()
    if not brain.available:
        return {"error": "Brain not available - check provider config in .env"}

    # ── Neural model hot-reload: pick up nightly retrain without restart ──────
    try:
        import os as _os
        from engine.neural_model import _MODEL_PATH as _NMP
        import engine.neural_ev as _nev
        if _NMP.exists():
            _cur_mtime = _os.path.getmtime(_NMP)
            _prev_mtime = getattr(_nev, "_model_mtime", 0.0)
            if _cur_mtime != _prev_mtime:
                _nev._model       = None
                _nev._model_loaded = False
                _nev._model_mtime  = _cur_mtime
                log.info("[hot-reload] kalshi_net.pt updated (mtime %.0f → %.0f) — reloading",
                         _prev_mtime, _cur_mtime)
    except Exception as _hr_err:
        log.debug("[hot-reload] check failed (non-blocking): %s", _hr_err)

    log.info("Autonomous agent starting. Provider: %s  dry_run=%s", brain.provider_info, dry_run)

    # ── Build live session brief for LLM context ─────────────────────────────
    _live_ctx = ""
    try:
        from engine.session_context import build_session_context
        _live_ctx = build_session_context(max_chars=1600)
    except Exception as _ctx_err:
        log.debug("[run_agent] session_context failed (non-blocking): %s", _ctx_err)

    messages = [
        {"role": "system", "content": AGENT_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Date/time: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}. "
                f"Mode: {'DRY RUN (simulated)' if dry_run else 'LIVE — real money'}. "
                f"Max bets this session: {max_bets}. "
                + (f"\n\n{_live_ctx}\n\n" if _live_ctx else "")
                + "Run the intelligence sweep: get_balance(), get_crypto_snapshot(). "
                "Then scan_markets for DAILY BTC/ETH picks (series KXBTCD or KXETHD). "
                "Only call get_intraday_picks() if the daily scan shows nothing with edge >= 8%. "
                "Each tool may only be called ONCE per session — do not repeat. "
                "FOCUS: BTC first. ETH only if BTC has no clear setup. Do NOT bet SOL/DOGE/XRP. "
                "Do NOT bet 15-min target-price markets (tickers with 15M AND -T). "
                "ONE SETUP only: momentum continuation after a clear break, OR exhaustion fade — not both. "
                "Daily picks: edge >= 8%, run run_deep_analysis() first, max 2 contracts. "
                "Intraday directional: edge >= 10%, momentum clearly directional (not flat), max 2 contracts. "
                f"Total session budget is ${_SESSION_BUDGET:.2f} across all bets combined. "
                "Show your math then call place_bet() immediately."
            ),
        },
    ]

    global _session_spent, _session_bets, _daily_pnl
    _session_spent = 0.0
    _session_bets  = []
    # Refresh settled P&L each session so stop-win can trigger mid-run
    _daily_pnl = await _fetch_daily_realized_pnl()

    bets_placed = 0
    tool_calls_made = 0
    session_log = []
    nudges_sent = 0
    MAX_TOOL_CALLS = 20  # safety cap
    MAX_NUDGES = 1       # only nudge once; if brain still won't act, accept and exit

    while tool_calls_made < MAX_TOOL_CALLS:
        # Call the brain with tool support
        resp = await brain._client.chat.completions.create(
            model=brain._model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.15,
            max_tokens=2000,
        )

        msg = resp.choices[0].message
        finish = resp.choices[0].finish_reason

        # Append assistant message to history
        messages.append(msg.model_dump(exclude_none=True))

        # If the brain wants to call tools
        if finish == "tool_calls" and msg.tool_calls:
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_args = {}

                tool_calls_made += 1
                log.info("Brain calling tool: %s(%s)", tool_name, json.dumps(tool_args)[:120])

                result = await dispatch_tool(tool_name, tool_args, dry_run=dry_run)

                if tool_name == "place_bet" and isinstance(result, dict):
                    status = result.get("status", "")
                    if status in ("PLACED", "DRY_RUN"):
                        bets_placed += 1
                        session_log.append(result)
                    elif status == "REJECTED":
                        log.info("BET REJECTED: %s — %s",
                                 tool_args.get("ticker", "?"),
                                 result.get("reason", "unknown")[:120])

                # Feed tool result back to brain
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str),
                })

            continue  # let brain process tool results

        # Brain finished (stop or no more tool calls)
        final_text = msg.content or ""

        # If the brain described placing bets but never called place_bet, nudge it once
        bet_words = ["place", "placing", "bet", "betting", "let's place", "place these", "placed"]
        described_betting = bets_placed == 0 and any(w in final_text.lower() for w in bet_words)
        if described_betting and nudges_sent < MAX_NUDGES and tool_calls_made < MAX_TOOL_CALLS - 2:
            nudges_sent += 1
            log.info("Brain described bets but didn't call place_bet — sending action nudge (%d/%d)", nudges_sent, MAX_NUDGES)
            messages.append({
                "role": "user",
                "content": (
                    "IMPORTANT: Do NOT write JSON code blocks or markdown. "
                    "You must call the place_bet FUNCTION TOOL directly using the tool-calling system. "
                    "Select the tool from the tools list and invoke it now with: "
                    "ticker, side ('yes' or 'no'), contracts (1-5), yes_price (integer cents), reasoning. "
                    "Do not output any text — just invoke the tool."
                ),
            })
            continue  # give brain one more chance to call the tool

        # ── Rescue: parse place_bet calls embedded as JSON in brain's text ────
        # qwen2.5 sometimes writes the tool call as a JSON code block instead
        # of using the function-calling API. Extract and execute any such calls.
        if bets_placed < max_bets and final_text:
            import re as _re
            _json_blocks = _re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', final_text, _re.DOTALL)
            for _block in _json_blocks:
                if bets_placed >= max_bets:
                    break
                try:
                    _obj = json.loads(_block.strip())
                    # Direct bet args: {"ticker":..., "side":..., "contracts":...}
                    if "ticker" in _obj and "side" in _obj and "contracts" in _obj:
                        _bargs = _obj
                    # Tool call wrapper: {"name":"place_bet","arguments":{...}}
                    elif _obj.get("name") == "place_bet" and "arguments" in _obj:
                        _bargs = _obj["arguments"]
                    else:
                        continue
                    _yp = int(_bargs.get("yes_price") or _bargs.get("yes_price_cents") or 0)
                    if not _yp:
                        continue
                    log.info("RESCUE: executing place_bet from brain text: %s", json.dumps(_bargs)[:120])
                    _res = await _tool_place_bet(
                        _bargs.get("ticker", ""),
                        _bargs.get("side", "yes"),
                        int(_bargs.get("contracts", 1)),
                        _yp,
                        _bargs.get("reasoning", "rescued from brain text"),
                        dry_run=dry_run,
                    )
                    tool_calls_made += 1
                    if _res.get("status") in ("PLACED", "DRY_RUN"):
                        bets_placed += 1
                        session_log.append(_res)
                        log.info("RESCUE BET %s: %s", _res["status"], json.dumps(_res)[:120])
                except Exception:
                    pass

        log.info("Agent session complete. Bets: %d  Tool calls: %d", bets_placed, tool_calls_made)

        result = {
            "provider":       brain.provider_info,
            "dry_run":        dry_run,
            "bets_placed":    bets_placed,
            "tool_calls":     tool_calls_made,
            "session_log":    session_log,
            "final_analysis": final_text,
            "timestamp":      datetime.now().isoformat(),
        }
        # ── Write session summary to RAG daily_picks collection ──────────
        try:
            from scripts.rag_ingest import ingest_session_summary as _rag_session
            _rag_result = {
                "date":           datetime.now().strftime("%Y-%m-%d"),
                "session_id":     datetime.now().strftime("%Y%m%d_%H%M%S"),
                "bets_placed":    session_log,
                "session_spend":  _session_spent,
                "decisions_skipped": tool_calls_made - bets_placed,
                "market_summary": final_text[:300] if final_text else "",
            }
            _rag_session(_rag_result)
        except Exception as _rag_err:
            log.debug("[run_agent] RAG session ingest failed (non-blocking): %s", _rag_err)
        return result

    return {
        "error": "Max tool calls reached",
        "provider":    brain.provider_info,
        "bets_placed": bets_placed,
        "session_log": session_log,
    }


# ── Scheduled loop ────────────────────────────────────────────────────────────

async def run_loop(dry_run: bool, interval_minutes: int = 15, hours: float = 10.0, max_bets: int = 3):
    """Run the agent on a schedule for up to `hours` hours."""
    import time
    import pathlib

    deadline = time.time() + hours * 3600
    session_num = 0
    total_bets = 0
    total_spent = 0.0

    # Per-run log file in logs/
    log_dir = pathlib.Path("logs")
    log_dir.mkdir(exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"autonomous_{run_id}.jsonl"

    # ── Restore (or initialize) daily risk state ─────────────────────────────
    # Loads persisted state from disk so cooldowns and spend survive restarts.
    # State is keyed by calendar date; a new day gets a clean slate automatically.
    global _daily_spend, _daily_pnl, _asset_cooldown
    global _reopen_mode, _reopen_mode_expires, _reopen_no_trade_until
    import time as _t
    (
        _daily_spend, _asset_cooldown,
        _reopen_mode, _reopen_mode_expires, _reopen_no_trade_until
    ) = _load_daily_state()
    # Discard expired cooldowns (avoid stale locks after long downtime)
    _asset_cooldown = {k: v for k, v in _asset_cooldown.items() if v > _t.time()}
    # Discard expired reopen mode
    if _reopen_mode and _t.time() > _reopen_mode_expires:
        _reopen_mode = False
    _daily_pnl = await _fetch_daily_realized_pnl()

    log.info("=" * 60)
    log.info("KALISHI AUTONOMOUS — %s-hour run starting", hours)
    log.info("Interval: %d min | Session log: %s | Daily P&L so far: $%+.2f",
             interval_minutes, log_file, _daily_pnl)
    log.info("=" * 60)

    while time.time() < deadline:
        session_num += 1
        remaining_h = (deadline - time.time()) / 3600
        log.info("--- Session #%d | %.1fh remaining ---", session_num, remaining_h)

        try:
            result = await run_agent(dry_run=dry_run, max_bets=max_bets)
        except Exception as exc:
            log.error("Session #%d failed: %s", session_num, exc)
            result = {"error": str(exc), "session": session_num}

        result["session"] = session_num
        # Append to JSONL log
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(result, default=str) + "\n")

        bets_this = result.get("bets_placed", 0)
        total_bets += bets_this
        for b in result.get("session_log", []):
            total_spent += b.get("cost_usd", 0)

        log.info("Session #%d done. Bets this session: %d | Total bets: %d | Total spent: $%.2f",
                 session_num, bets_this, total_bets, total_spent)

        # Sleep until next session (or until deadline)
        next_run = time.time() + interval_minutes * 60
        sleep_secs = min(next_run, deadline) - time.time()
        if sleep_secs > 5:
            log.info("Next session in %d min...", round(sleep_secs / 60))
            await asyncio.sleep(sleep_secs)

    log.info("=" * 60)
    log.info("Run complete. Sessions: %d | Total bets: %d | Total spent: $%.2f",
             session_num, total_bets, total_spent)
    log.info("Full log: %s", log_file)
    log.info("=" * 60)
    return {"sessions": session_num, "total_bets": total_bets, "total_spent": total_spent, "log_file": str(log_file)}


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KALISHI Autonomous Betting Agent")
    parser.add_argument("--live",     action="store_true", help="Place real bets (default: dry run)")
    parser.add_argument("--loop",     action="store_true", help="Run on a schedule")
    parser.add_argument("--interval", type=int,   default=15,   help="Minutes between sessions (default 15)")
    parser.add_argument("--hours",    type=float, default=10.0, help="Total hours to run (default 10)")
    parser.add_argument("--max-bets", type=int,   default=3,    help="Max bets per session (default 3)")
    args = parser.parse_args()

    dry_run = not args.live

    if dry_run:
        log.info("DRY RUN MODE - no real money will be spent. Pass --live for real orders.")
    else:
        log.warning("LIVE MODE - REAL MONEY WILL BE SPENT ON KALSHI")

    if args.loop:
        summary = asyncio.run(run_loop(
            dry_run=dry_run,
            interval_minutes=args.interval,
            hours=args.hours,
            max_bets=args.max_bets,
        ))
        print(json.dumps(summary, indent=2, default=str))
    else:
        result = asyncio.run(run_agent(dry_run=dry_run, max_bets=args.max_bets))
        print(json.dumps(result, indent=2, default=str))
    sys.exit(0)
