"""
Kalshi Prediction Market auto-execution engine.

Kalshi is CFTC-regulated and available in all 50 US states.
Contracts: binary yes/no, each pays $1 if correct.
A "yes" contract at 55 cents costs $0.55 and pays $1 → profit $0.45 if correct.

How it maps to our picks:
  - Our pick has an edge_pct and a probability estimate
  - We search Kalshi for a matching sports market
  - Buy YES if our model says event is more likely than Kalshi's price implies
  - Buy NO if our model says event is less likely than Kalshi's price implies

Safety gates (all must pass before real money moves):
  1. KALSHI_API_KEY configured in .env
  2. Edge ≥ MIN_EDGE_TO_EXECUTE (default 4%)
  3. Kalshi yes_price within [5, 95] cents (avoid illiquid extremes)
  4. Available liquidity ≥ 5× our intended spend
  5. dry_run=True by default — must explicitly set False for real orders
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ── Safety parameters ────────────────────────────────────────────────────────
MIN_EDGE_TO_EXECUTE   = 0.06   # 6% minimum edge (V5: raised from 4%)
MIN_PRICE_CENTS       = 10     # V5: raised from 4¢ — cheap contracts are losers (post-mortem)
MAX_PRICE_CENTS       = 65     # V6: lowered from 96¢ — expensive contracts have thin margins
MAX_CONTRACTS         = 5      # V5: hard cap 5 per order (was 500 — way too loose)
MIN_SPEND_USD         = 1.0    # minimum $1 order value (supports small $10 accounts)
MAX_SPEND_USD         = 500.0  # hard dollar cap per order
MIN_LIQUIDITY_MULT    = 5.0    # market liquidity ≥ 5× our spend


# ── Price / odds helpers ──────────────────────────────────────────────────────

def prob_to_yes_cents(prob: float) -> int:
    """Convert our win probability (0-1) to Kalshi yes price in cents."""
    return max(1, min(99, round(prob * 100)))


def yes_cents_to_prob(cents: int) -> float:
    """Convert Kalshi yes price (cents) back to implied probability."""
    return cents / 100.0


def yes_cents_to_american(cents: int) -> int:
    """Convert Kalshi yes price cents to American odds equivalent."""
    prob = yes_cents_to_prob(cents)
    if prob <= 0 or prob >= 1:
        return 0
    if prob >= 0.5:
        return -round((prob / (1 - prob)) * 100)
    return round(((1 - prob) / prob) * 100)


def contracts_for_spend(spend_usd: float, yes_price_cents: int) -> int:
    """How many contracts can we buy for a given dollar spend?"""
    if yes_price_cents <= 0:
        return 0
    return max(1, int(spend_usd / (yes_price_cents / 100.0)))


def potential_profit(contracts: int, yes_price_cents: int, side: str = "yes") -> float:
    """Gross profit if the bet wins (before fees)."""
    cost_per = yes_price_cents / 100.0 if side == "yes" else (100 - yes_price_cents) / 100.0
    return round(contracts * (1.0 - cost_per), 2)


# ── Market matching ───────────────────────────────────────────────────────────

async def find_kalshi_match(team: str, sport: str) -> Optional[dict]:
    """
    Search Kalshi markets for one matching a team/sport.
    Returns the normalized market dict or None.
    """
    from data.feeds.kalshi import get_active_markets, normalize_kalshi_market

    markets = await get_active_markets(category="sports")
    team_lc  = team.lower()
    sport_lc = sport.lower()

    # Exact word match > partial match
    scored: list[tuple[int, dict]] = []
    for raw in markets:
        title  = raw.get("title", "").lower()
        ticker = raw.get("ticker", "").lower()
        score  = 0
        if team_lc in title:
            score += 2
        if sport_lc[:3] in ticker or sport_lc[:3] in title:
            score += 1
        if any(word in title for word in team_lc.split() if len(word) > 3):
            score += 1
        if score > 0:
            scored.append((score, raw))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    return normalize_kalshi_market(scored[0][1])


# ── Crypto pick execution (ticker already known) ─────────────────────────────

async def _execute_crypto_pick(pick: dict, bankroll: float, dry_run: bool = True) -> dict:
    """
    Execute a CRYPTO pick where the Kalshi ticker is already embedded in pick["market"].
    Bypasses the team-name market-search step.
    """
    from data.feeds.kalshi import get_market, place_order as kalshi_place

    crypto_meta = pick.get("crypto_meta", {})
    ticker      = pick.get("market", "")
    asset       = crypto_meta.get("asset", "CRYPTO")
    side_str    = crypto_meta.get("side", "YES")     # "YES" or "NO"
    side        = side_str.lower()                   # "yes" or "no"
    edge_pct    = float(pick.get("edge_pct", 0.0))  # already in 0-100 scale
    our_prob_pct = float(pick.get("our_prob", 0.0)) # 0-100 scale
    stake_usd   = float(pick.get("recommended_stake", 0.0))

    result: dict = {
        "pick":             pick.get("pick", f"{asset} {side_str}"),
        "sport":            "CRYPTO",
        "team":             pick.get("pick", ""),
        "edge_pct":         edge_pct,
        "status":           "NOT_EXECUTED",
        "reason":           None,
        "order_id":         None,
        "contracts":        None,
        "spend_usd":        None,
        "potential_profit": None,
        "side":             side,
        "dry_run":          dry_run,
    }

    # Gate 1 — API key required for live orders
    if not dry_run and not os.getenv("KALSHI_API_KEY", ""):
        result["reason"] = "KALSHI_API_KEY not configured"
        return result

    # Gate 2 — Edge threshold
    if edge_pct < MIN_EDGE_TO_EXECUTE * 100:  # edge_pct is in % (e.g. 11.1), threshold in decimal (0.04)
        result["reason"] = f"Edge {edge_pct:.1f}% below minimum {MIN_EDGE_TO_EXECUTE * 100:.0f}%"
        return result

    # Gate 3 — Fetch live market from Kalshi by ticker
    if not ticker:
        result["reason"] = "No Kalshi ticker in crypto pick (pick['market'] missing)"
        return result

    raw = await get_market(ticker)
    if not raw:
        result["reason"] = f"Kalshi market not found: {ticker}"
        return result

    # Prices are string dollars "0.8400" → convert to cents (1-99)
    try:
        yes_ask_cents = round(float(raw.get("yes_ask_dollars") or raw.get("yes_ask", 0)) * 100)
        no_ask_cents  = round(float(raw.get("no_ask_dollars")  or raw.get("no_ask",  0)) * 100)
    except (TypeError, ValueError):
        result["reason"] = f"Could not parse price from market {ticker}"
        return result

    price_cents = yes_ask_cents if side == "yes" else no_ask_cents

    # Gate 4 — Price in tradeable range
    if not (MIN_PRICE_CENTS <= price_cents <= MAX_PRICE_CENTS):
        result["reason"] = (
            f"{side_str} price {price_cents}¢ outside tradeable range "
            f"[{MIN_PRICE_CENTS},{MAX_PRICE_CENTS}]"
        )
        return result

    # Gate 5 — Liquidity (use open_interest as proxy; liquidity_dollars is unreliable)
    open_interest = float(raw.get("open_interest_fp") or raw.get("open_interest", 0))
    liquidity_usd = open_interest  # each Kalshi contract pays $1, so OI ≈ $ liquidity
    spend_usd     = min(max(round(stake_usd, 2), MIN_SPEND_USD), MAX_SPEND_USD)

    if liquidity_usd < spend_usd * MIN_LIQUIDITY_MULT:
        result["reason"] = (
            f"Insufficient liquidity: OI=${liquidity_usd:.0f}, "
            f"need ${spend_usd * MIN_LIQUIDITY_MULT:.2f} (5× stake)"
        )
        return result

    contracts    = min(contracts_for_spend(spend_usd, price_cents),
                       pick.get("_max_contracts_override", MAX_CONTRACTS))
    actual_spend = round(contracts * (price_cents / 100.0), 2)
    profit_win   = potential_profit(contracts, yes_ask_cents, side)

    # Kalshi order API always takes a yes_price reference:
    #   YES order → yes_price = yes_ask_cents
    #   NO  order → yes_price = 100 - no_ask_cents (complement)
    yes_price_ref = yes_ask_cents if side == "yes" else (100 - no_ask_cents)

    result.update({
        "market_ticker":    ticker,
        "market_title":     raw.get("title", ""),
        "market_yes_prob":  yes_ask_cents / 100.0,
        "our_prob":         our_prob_pct / 100.0,
        "our_edge":         round(edge_pct / 100.0, 4),
        "side":             side,
        "yes_price_cents":  yes_ask_cents,
        "price_cents":      price_cents,    # actual cost per contract for the chosen side
        "contracts":        contracts,
        "spend_usd":        actual_spend,
        "potential_profit": profit_win,
        "net_roi":          round(profit_win / actual_spend, 3) if actual_spend else 0,
    })

    if dry_run:
        result["status"] = "DRY_RUN"
        result["reason"]  = (
            f"Dry run — would buy {contracts} {side_str} contracts "
            f"of {ticker} @ {price_cents}¢"
        )
        return result

    # ── Place live order ──────────────────────────────────────────────────
    resp = await kalshi_place(
        ticker    = ticker,
        side      = side,
        count     = contracts,
        yes_price = yes_price_ref,   # YES-side reference price (required by Kalshi API)
    )

    if "error" in resp:
        result["status"] = "ERROR"
        result["reason"] = resp["error"]
        logger.error("Kalshi crypto order failed: %s  ticker=%s", resp["error"], ticker)
    else:
        order = resp.get("order", {})
        result["status"]   = "PLACED"
        result["order_id"] = order.get("order_id", "")
        result["reason"]   = "Order placed successfully"
        logger.info(
            "KALSHI CRYPTO ORDER PLACED: %s side=%s contracts=%d price=%d¢ order_id=%s",
            ticker, side, contracts, price_cents, result["order_id"],
        )

    return result


# ── Single pick execution ─────────────────────────────────────────────────────

async def execute_pick(
    pick:     dict,
    bankroll: float,
    dry_run:  bool = True,
) -> dict:
    """
    Attempt to execute one pick on Kalshi.

    Expected pick keys:
      sport, team (our pick), edge_pct, our_prob (0-1), kelly_fraction

    Returns a result dict. dry_run=True by default.
    """
    sport        = pick.get("sport", "").lower()

    # Crypto picks already carry the Kalshi ticker — use direct execution path
    if sport == "crypto":
        return await _execute_crypto_pick(pick, bankroll, dry_run=dry_run)

    team         = pick.get("team") or pick.get("pick", "")
    edge_pct     = float(pick.get("edge_pct", 0.0))
    our_prob     = float(pick.get("our_prob", 0.0))
    kelly_frac   = float(pick.get("kelly_fraction", 0.02))

    # Normalise our_prob: API returns 0-100, we want 0-1
    if our_prob > 1:
        our_prob /= 100.0

    result: dict = {
        "pick":    f"{team} ({sport.upper()})",
        "sport":   sport,
        "team":    team,
        "edge_pct": edge_pct,
        "status":  "NOT_EXECUTED",
        "reason":  None,
        "order_id": None,
        "contracts": None,
        "spend_usd": None,
        "potential_profit": None,
        "side":    None,
        "dry_run": dry_run,
    }

    # ── Gate 1: API key (skip for dry_run — uses mock market data) ──────────
    if not dry_run and not os.getenv("KALSHI_API_KEY", ""):
        result["reason"] = "KALSHI_API_KEY not configured — add to .env for live orders"
        return result

    # ── Gate 2: Edge ─────────────────────────────────────────────────────────
    if edge_pct < MIN_EDGE_TO_EXECUTE:
        result["reason"] = f"Edge {edge_pct:.1%} below minimum {MIN_EDGE_TO_EXECUTE:.0%}"
        return result

    # ── Gate 3: Find market ───────────────────────────────────────────────────
    market = await find_kalshi_match(team, sport)
    if not market:
        result["reason"] = f"No Kalshi market found for '{team}'"
        return result

    yes_ask    = market.get("yes_prob", 0) * 100   # convert prob back to cents
    market_prob = market.get("yes_prob", 0)

    # Determine side: buy YES if our prob > market, NO if our prob < market
    if our_prob >= market_prob:
        side       = "yes"
        yes_price  = round(yes_ask)      # we pay yes_ask cents
        our_edge   = our_prob - market_prob
    else:
        side       = "no"
        no_ask     = round((1 - market.get("yes_prob", 0)) * 100)
        yes_price  = round(yes_ask)      # API always uses yes_price reference
        our_edge   = market_prob - our_prob

    # ── Gate 4: Price range ───────────────────────────────────────────────────
    if not (MIN_PRICE_CENTS <= yes_price <= MAX_PRICE_CENTS):
        result["reason"] = f"Kalshi yes_price {yes_price}¢ outside tradeable range [{MIN_PRICE_CENTS},{MAX_PRICE_CENTS}]"
        return result

    # ── Gate 5: Liquidity ────────────────────────────────────────────────────
    liquidity_usd = market.get("liquidity", 0) / 100.0   # liquidity in cents → USD

    # ── Stake sizing ─────────────────────────────────────────────────────────
    kelly_spend   = bankroll * kelly_frac * 0.25   # quarter-Kelly in USD
    spend_usd     = min(kelly_spend, MAX_SPEND_USD)
    spend_usd     = max(spend_usd, MIN_SPEND_USD)
    spend_usd     = round(spend_usd, 2)

    if liquidity_usd < spend_usd * MIN_LIQUIDITY_MULT:
        result["reason"] = (
            f"Insufficient Kalshi liquidity: ${liquidity_usd:.2f} available, "
            f"need ${spend_usd * MIN_LIQUIDITY_MULT:.2f} (5× stake)"
        )
        return result

    contracts      = min(contracts_for_spend(spend_usd, yes_price), MAX_CONTRACTS)
    actual_spend   = round(contracts * (yes_price / 100.0), 2)
    profit_if_win  = potential_profit(contracts, yes_price, side)

    result.update({
        "market_ticker":    market.get("ticker"),
        "market_title":     market.get("title"),
        "market_yes_prob":  market_prob,
        "our_prob":         our_prob,
        "our_edge":         round(our_edge, 4),
        "side":             side,
        "yes_price_cents":  yes_price,
        "contracts":        contracts,
        "spend_usd":        actual_spend,
        "potential_profit": profit_if_win,
        "net_roi":          round(profit_if_win / actual_spend, 3) if actual_spend else 0,
    })

    if dry_run:
        result["status"] = "DRY_RUN"
        result["reason"] = "Simulation only — set dry_run=false to place real orders"
        return result

    # ── Place order ───────────────────────────────────────────────────────────
    from data.feeds.kalshi import place_order as kalshi_place

    resp = await kalshi_place(
        ticker    = market["ticker"],
        side      = side,
        count     = contracts,
        yes_price = yes_price,
    )

    if "error" in resp:
        result["status"] = "ERROR"
        result["reason"] = resp["error"]
        logger.error("Kalshi order failed: %s  pick=%s", resp["error"], team)
    else:
        order = resp.get("order", {})
        result["status"]   = "PLACED"
        result["order_id"] = order.get("order_id", "")
        result["reason"]   = "Order placed successfully"
        logger.info(
            "KALSHI ORDER PLACED: %s side=%s contracts=%d price=%d¢ order_id=%s",
            team, side, contracts, yes_price, result["order_id"],
        )

    return result


# ── Batch auto-execution ──────────────────────────────────────────────────────

async def auto_execute_picks(
    picks:    list,
    bankroll: float,
    min_edge: float = MIN_EDGE_TO_EXECUTE,
    dry_run:  bool  = True,
) -> dict:
    """
    Auto-execute all picks meeting the edge threshold on Kalshi.
    dry_run=True by default — no real money placed unless explicitly False.
    """
    results         = []
    placed          = 0
    skipped_edge    = 0
    total_spend     = 0.0
    total_potential = 0.0

    for pick in picks:
        if float(pick.get("edge_pct", 0.0)) < min_edge:
            skipped_edge += 1
            continue

        res = await execute_pick(pick, bankroll, dry_run=dry_run)
        results.append(res)

        if res["status"] in ("PLACED", "DRY_RUN"):
            placed         += 1
            total_spend    += res.get("spend_usd") or 0.0
            total_potential += res.get("potential_profit") or 0.0

    return {
        "mode":               "DRY_RUN" if dry_run else "LIVE",
        "platform":           "Kalshi (CFTC-regulated, US legal)",
        "total_picks_in":     len(picks),
        "eligible":           len(results),
        "skipped_below_edge": skipped_edge,
        "placed":             placed,
        "total_spend_usd":    round(total_spend, 2),
        "total_potential_profit": round(total_potential, 2),
        "results":            results,
    }


# ── P&L from settlements ──────────────────────────────────────────────────────

async def get_pnl_summary() -> dict:
    """Compute P&L from Kalshi settled positions."""
    from data.feeds.kalshi import get_settlements

    settlements = await get_settlements()
    if not settlements:
        return {
            "settled_orders": 0,
            "wins": 0, "losses": 0,
            "win_rate": 0.0,
            "total_spent": 0.0,
            "total_revenue": 0.0,
            "total_profit": 0.0,
            "roi": 0.0,
            "recent_settlements": [],
        }

    wins    = sum(1 for s in settlements if s.get("profit", 0) > 0)
    losses  = sum(1 for s in settlements if s.get("profit", 0) <= 0)
    spent   = sum(s.get("no_total_cost", 0) + s.get("yes_total_cost", 0) for s in settlements) / 100.0
    revenue = sum(s.get("revenue", 0) for s in settlements) / 100.0
    profit  = revenue - spent

    by_sport: dict = {}
    for s in settlements:
        ticker = s.get("ticker", "")
        sport  = ticker.split("-")[0] if "-" in ticker else "OTHER"
        by_sport.setdefault(sport, {"count": 0, "profit": 0.0})
        by_sport[sport]["count"]  += 1
        by_sport[sport]["profit"] += s.get("profit", 0) / 100.0

    return {
        "settled_orders": len(settlements),
        "wins":           wins,
        "losses":         losses,
        "win_rate":       round(wins / len(settlements), 3),
        "total_spent":    round(spent, 2),
        "total_revenue":  round(revenue, 2),
        "total_profit":   round(profit, 2),
        "roi":            round(profit / spent, 3) if spent else 0.0,
        "by_sport":       {k: {**v, "profit": round(v["profit"], 2)} for k, v in sorted(by_sport.items(), key=lambda x: x[1]["profit"], reverse=True)},
        "recent_settlements": settlements[:20],
    }
