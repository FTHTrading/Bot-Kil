"""
Live Game Monitor
==================
Polls live odds every 30 seconds during game hours.
Streams updates to dashboard via WebSocket.
Watches for:
  - Line movement (significant shifts = sharp action signal)
  - Live game state changes (score changes, injuries reported)
  - In-game value opportunities

Run: python workflows/live_monitor.py
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

POLL_INTERVAL = int(os.getenv("LIVE_POLL_INTERVAL_SEC", "30"))
LOG_DIR = Path("./db/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Track previous snapshots for movement detection
_prev_lines: dict[str, dict] = {}


async def poll_live_lines():
    """
    Fetch current odds and compare to previous snapshot.
    Flag any significant line movement.
    """
    from data.feeds.odds_api import OddsAPIClient
    import httpx

    client = OddsAPIClient()
    sports = ["americanfootball_nfl", "basketball_nba", "baseball_mlb", "icehockey_nhl"]
    movements = []

    for sport in sports:
        try:
            games = await client.get_odds(sport)
        except Exception as e:
            print(f"[Monitor] Error fetching {sport}: {e}")
            continue

        for game in games:
            game_id = game.get("id", "")
            if not game_id:
                continue

            for bookmaker in game.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    key = f"{game_id}:{bookmaker['key']}:{market['key']}"
                    current_outcomes = {
                        o["name"]: o.get("price") for o in market.get("outcomes", [])
                    }

                    if key in _prev_lines:
                        prev_outcomes = _prev_lines[key]
                        for side, curr_price in current_outcomes.items():
                            prev_price = prev_outcomes.get(side)
                            if prev_price and curr_price and abs(curr_price - prev_price) >= 0.05:
                                movements.append({
                                    "sport": sport,
                                    "event": f"{game.get('away_team')} @ {game.get('home_team')}",
                                    "book": bookmaker["key"],
                                    "market": market["key"],
                                    "side": side,
                                    "prev_decimal": round(prev_price, 3),
                                    "curr_decimal": round(curr_price, 3),
                                    "movement": round(curr_price - prev_price, 3),
                                    "ts": datetime.utcnow().isoformat(),
                                })

                    _prev_lines[key] = current_outcomes

    return movements


def _movement_significance(movement: dict) -> str:
    """Classify movement size."""
    diff = abs(movement.get("movement", 0))
    if diff >= 0.30:
        return "SIGNIFICANT"
    elif diff >= 0.15:
        return "MODERATE"
    else:
        return "MINOR"


async def run_live_monitor():
    """Main monitoring loop."""
    print(f"\n[LiveMonitor] Starting — polling every {POLL_INTERVAL}s")
    print("[LiveMonitor] Watching for line movement and sharp action...\n")

    while True:
        try:
            movements = await poll_live_lines()

            if movements:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n[{ts}] LINE MOVEMENTS DETECTED ({len(movements)})")

                for mv in movements:
                    sig = _movement_significance(mv)
                    emoji = "🚨" if sig == "SIGNIFICANT" else ("⚠️" if sig == "MODERATE" else "·")
                    direction = "▲" if mv["movement"] > 0 else "▼"
                    pct_chg = abs(mv["movement"] / mv["prev_decimal"]) * 100
                    print(f"  {emoji} [{mv['sport']}] {mv['event']}")
                    print(f"     {mv['side']} {direction} {mv['prev_decimal']} → {mv['curr_decimal']} "
                          f"({pct_chg:.1f}% move) [{mv['book'].upper()}] [{mv['market']}]")
                    if sig == "SIGNIFICANT":
                        print(f"     *** SHARP MONEY SIGNAL — Consider same-side bet immediately ***")

                # Log to file
                log_path = LOG_DIR / "line_movements.jsonl"
                with open(log_path, "a") as f:
                    for mv in movements:
                        f.write(json.dumps(mv) + "\n")

                # Write latest for dashboard WebSocket
                latest_path = LOG_DIR / "movements_latest.json"
                with open(latest_path, "w") as f:
                    json.dump(movements[-20:], f, indent=2)  # last 20 movements

            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Monitoring... no movement")

        except Exception as e:
            print(f"[LiveMonitor] Error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_live_monitor())
