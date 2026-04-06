"""
Continuous Arbitrage Scanner
==============================
Runs every 2 minutes scanning all books for guaranteed profit opportunities.
Logs to file, prints to console, pushes to dashboard.

Covers:
- Standard 2-way arb (moneyline + spread)
- 3-way arb (soccer draw markets)
- Kalshi vs sportsbook cross-market arb
- Middle opportunities (winning both sides of a spread)

Run: python workflows/arbitrage_scan.py
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

SCAN_INTERVAL_SEC = int(os.getenv("ARB_SCAN_INTERVAL_SEC", "120"))  # 2 mins
BANKROLL = float(os.getenv("BANKROLL_TOTAL", "10000"))
MIN_PROFIT_PCT = float(os.getenv("MIN_ARB_PROFIT_PCT", "0.01"))     # 1% minimum

LOG_DIR = Path("./db/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

_scan_count = 0
_total_arbs_found = 0


def _format_arb_display(arb: dict) -> str:
    """Pretty-print a single arb opportunity."""
    lines = [
        f"  EVENT: {arb.get('event', 'N/A')}",
        f"  SPORT: {arb.get('sport', '')}",
        f"  TYPE:  {arb.get('type', '2-way')}",
        f"  PROFIT: {arb.get('profit_pct', 0):.2f}% guaranteed",
        f"  STAKE $: ${arb.get('total_stake', BANKROLL * 0.05):.2f} total",
    ]

    if arb.get("leg_a"):
        a = arb["leg_a"]
        b = arb.get("leg_b", {})
        lines.append(f"  LEG A: {a.get('side')} @ {a.get('odds', 'N/A')} on {a.get('book', '').upper()}"
                     f" → stake ${a.get('stake', 0):.2f}")
        if b:
            lines.append(f"  LEG B: {b.get('side')} @ {b.get('odds', 'N/A')} on {b.get('book', '').upper()}"
                         f" → stake ${b.get('stake', 0):.2f}")

    if arb.get("guaranteed_profit"):
        lines.append(f"  GUARANTEED PROFIT: ${arb['guaranteed_profit']:.2f}")

    return "\n".join(lines)


async def scan_sportsbook_arbs() -> list[dict]:
    """Scan The Odds API for cross-book arbitrage."""
    from data.feeds.odds_api import scan_for_arb_opportunities
    try:
        return await scan_for_arb_opportunities(BANKROLL)
    except Exception as e:
        print(f"[ArbScanner] Sportsbook scan error: {e}")
        return []


async def scan_kalshi_arbs() -> list[dict]:
    """
    Scan Kalshi vs sportsbooks for cross-platform arb.
    """
    from data.feeds.kalshi import get_active_markets, find_kalshi_arb
    from data.feeds.odds_api import OddsAPIClient

    try:
        kalshi_markets = await get_active_markets()
    except Exception as e:
        print(f"[ArbScanner] Kalshi fetch error: {e}")
        return []

    client = OddsAPIClient()
    all_games = []
    for sport in ["americanfootball_nfl", "basketball_nba", "baseball_mlb"]:
        try:
            games = await client.get_odds(sport, markets="h2h")
            all_games.extend(games)
        except Exception:
            pass

    arbs = find_kalshi_arb(kalshi_markets, all_games, MIN_PROFIT_PCT)
    return arbs


async def scan_middles(sportsbook_data: list[dict]) -> list[dict]:
    """
    Detect middling opportunities — spread differences between books
    where you can win both sides if the game lands in the middle.

    Example: Book A: Chiefs -3.5, Book B: Chiefs +4.5
    If Chiefs win by exactly 4, both bets win (but -110 juice applies).
    """
    from engine.arbitrage import midline_value
    middles = []

    game_map: dict[str, list[dict]] = {}  # game_id → list of spread offers

    for game in sportsbook_data:
        game_id = game.get("id", "")
        name = f"{game.get('away_team', '')} @ {game.get('home_team', '')}"
        for book in game.get("bookmakers", []):
            for mkt in book.get("markets", []):
                if mkt.get("key") != "spreads":
                    continue
                for outcome in mkt.get("outcomes", []):
                    entry = {
                        "game_id": game_id,
                        "event": name,
                        "team": outcome.get("name"),
                        "spread": outcome.get("point", 0),
                        "odds_decimal": outcome.get("price", 1.91),
                        "book": book.get("key"),
                    }
                    if game_id not in game_map:
                        game_map[game_id] = []
                    game_map[game_id].append(entry)

    for game_id, offers in game_map.items():
        # Look for same team offered at different spreads across books
        by_team: dict[str, list[dict]] = {}
        for offer in offers:
            t = offer["team"]
            if t not in by_team:
                by_team[t] = []
            by_team[t].append(offer)

        for team, team_offers in by_team.items():
            if len(team_offers) < 2:
                continue
            # Sort by spread (ascending = most favorable first)
            team_offers.sort(key=lambda x: x["spread"])
            best = team_offers[0]    # tightest spread (most favorable for team)
            worst = team_offers[-1]  # loosest spread

            gap = abs(best["spread"] - worst["spread"])
            if gap >= 1.5:
                middles.append({
                    "type": "middle",
                    "event": best["event"],
                    "team": team,
                    "gap_pts": gap,
                    "book_a": worst["book"],
                    "spread_a": worst["spread"],
                    "book_b": best["book"],
                    "spread_b": best["spread"],
                    "middle_range": f"{min(best['spread'], worst['spread']):.1f} to {max(best['spread'], worst['spread']):.1f}",
                    "note": f"Win both sides if {team} wins by exactly {gap:.0f} pts — both bets cover",
                })

    return middles


async def run_arb_scanner():
    """Main arb scanning loop."""
    global _scan_count, _total_arbs_found

    print(f"\n[ArbScanner] Starting — scan every {SCAN_INTERVAL_SEC}s")
    print(f"[ArbScanner] Min profit threshold: {MIN_PROFIT_PCT * 100:.1f}%")
    print(f"[ArbScanner] Bankroll: ${BANKROLL:,.2f}\n")

    while True:
        _scan_count += 1
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] Scan #{_scan_count} starting...")

        # Run all scans in parallel
        sb_arbs, kalshi_arbs = await asyncio.gather(
            scan_sportsbook_arbs(),
            scan_kalshi_arbs(),
            return_exceptions=True,
        )

        # Handle any exceptions from gather
        if isinstance(sb_arbs, Exception):
            sb_arbs = []
        if isinstance(kalshi_arbs, Exception):
            kalshi_arbs = []

        all_arbs = list(sb_arbs) + list(kalshi_arbs)

        if all_arbs:
            _total_arbs_found += len(all_arbs)
            print(f"\n🎯 [{ts}] {len(all_arbs)} ARB OPPORTUNITIES FOUND! (session total: {_total_arbs_found})")
            print("─" * 60)
            for arb in sorted(all_arbs, key=lambda x: x.get("profit_pct", x.get("potential_edge_pct", 0)), reverse=True)[:10]:
                print(_format_arb_display(arb))
                print("─" * 60)

            # Save to log
            log_path = LOG_DIR / "arb_log.jsonl"
            with open(log_path, "a") as f:
                for arb in all_arbs:
                    arb["scan_time"] = datetime.utcnow().isoformat()
                    f.write(json.dumps(arb) + "\n")

        else:
            print(f"[{ts}] No arbs found. Session scans: {_scan_count}, total arbs: {_total_arbs_found}")

        # Write latest for dashboard
        latest = {
            "scan_time": datetime.utcnow().isoformat(),
            "scan_count": _scan_count,
            "total_arbs_found": _total_arbs_found,
            "current_arbs": all_arbs[:20],
        }
        with open(LOG_DIR / "arbs_latest.json", "w") as f:
            json.dump(latest, f, indent=2)

        await asyncio.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(run_arb_scanner())
