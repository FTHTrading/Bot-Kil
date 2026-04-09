"""
sports_ev.py — Sports edge evaluation using ESPN data + spread models.
======================================================================
Three probability sources, used in priority order:
 1. ESPN DraftKings spread → spread-to-win-prob conversion (NBA, NHL)
 2. ESPN predictor/odds moneyline (when available)
 3. Log5 record-based estimate (MLB fallback)

When Kalshi's implied probability differs from our estimate by
more than MIN_EDGE, we have a bet.
"""
from __future__ import annotations
import asyncio, re, math
from typing import Optional
import httpx

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
SPORT_MAP = {
    "MLB":   ("baseball", "mlb"),
    "NBA":   ("basketball", "nba"),
    "NHL":   ("hockey", "nhl"),
    "NCAAB": ("basketball", "mens-college-basketball"),
}

MIN_EDGE  = 0.06      # 6% minimum edge to fire
MIN_PRICE = 0.10      # don't buy below 10¢
MAX_PRICE = 0.70      # don't buy above 70¢
KELLY_FRAC = 0.08     # fractional Kelly
MIN_OI    = 1000       # minimum open-interest on Kalshi market

# ── Spread → Win Probability ─────────────────────────────────────────────
# Logistic model: P(win) = 1 / (1 + 10^(-spread / divisor))
# Calibrated divisors per sport from historical closing lines
_SPREAD_DIVISORS = {
    "NBA":   12.5,     # NBA: -5.5 → ~64%, -10 → ~76%
    "NHL":    5.0,     # NHL: puck line usually ±1.5
    "MLB":    6.0,     # MLB: run line ±1.5
    "NCAAB": 11.0,     # college hoops similar to NBA
}

def spread_to_prob(spread: float, sport: str) -> float:
    """Convert a point-spread to implied win probability for the favorite.
    Positive spread = home is underdog by that many pts.
    Returns probability the FAVORITE wins."""
    d = _SPREAD_DIVISORS.get(sport, 12.0)
    return 1.0 / (1.0 + math.pow(10, spread / d))


def _log5(wp_a: float, wp_b: float) -> float:
    """Log5 head-to-head probability: P(A beats B) given season win%."""
    if wp_a <= 0 or wp_b <= 0 or (wp_a + wp_b) == 0:
        return 0.5
    return (wp_a * (1 - wp_b)) / (wp_a * (1 - wp_b) + wp_b * (1 - wp_a))


def _parse_record(rec_str: str) -> float:
    """Parse W-L record string to win%."""
    parts = rec_str.split("-")
    if len(parts) >= 2:
        try:
            w, l = int(parts[0]), int(parts[1])
            return w / (w + l) if (w + l) > 0 else 0.5
        except ValueError:
            pass
    return 0.5

# ── ESPN team-abbreviation aliases ────────────────────────────────────────
# Kalshi uses e.g. "CWS" for Chicago White Sox, ESPN uses "CHW"
_KALSHI_TO_ESPN = {
    "CWS": "CHW", "WSN": "WSH", "LAD": "LAD", "SFG": "SF",
    "SDP": "SD", "TBR": "TB", "KCR": "KC",
    "ARI": "ARI", "NYM": "NYM", "NYY": "NYY", "LAA": "LAA",
    "SAS": "SA", "NOP": "NO", "BKN": "BKN", "GSW": "GS",
    "OKC": "OKC", "NYK": "NY", "PHX": "PHX", "UTA": "UTAH",
    "VGK": "VGK", "NJD": "NJ",
}

def _abbr_match(kalshi_abbr: str, espn_abbr: str) -> bool:
    """Fuzzy match Kalshi team abbr to ESPN."""
    if not kalshi_abbr or not espn_abbr:
        return False
    a = kalshi_abbr.upper()
    b = espn_abbr.upper()
    if a == b:
        return True
    if _KALSHI_TO_ESPN.get(a) == b or _KALSHI_TO_ESPN.get(b) == a:
        return True
    # partial match (e.g. "MICH" matches "MICH" or "ATL" matches "ATL")
    return a.startswith(b) or b.startswith(a)


# ── ESPN Fetcher ──────────────────────────────────────────────────────────
async def get_espn_games(sport: str) -> list[dict]:
    """Fetch today's games with spread/odds data from ESPN."""
    key = sport.upper()
    if key not in SPORT_MAP:
        return []
    s, l = SPORT_MAP[key]
    url = f"{ESPN_BASE}/{s}/{l}/scoreboard"

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params={"limit": 50})
        if r.status_code != 200:
            return []
        data = r.json()

    games = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        home_c = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away_c = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])

        home_abbr = home_c.get("team", {}).get("abbreviation", "")
        away_abbr = away_c.get("team", {}).get("abbreviation", "")
        home_name = home_c.get("team", {}).get("displayName", "")
        away_name = away_c.get("team", {}).get("displayName", "")

        home_wp: Optional[float] = None
        away_wp: Optional[float] = None
        prob_source = "none"

        # --- Source 1: ESPN DraftKings spread ---
        odds_list = comp.get("odds", [])
        if odds_list:
            o = odds_list[0]
            spread_val = o.get("spread")
            home_fav = o.get("homeTeamOdds", {}).get("favorite", False)
            away_fav = o.get("awayTeamOdds", {}).get("favorite", False)

            # MLB always has spread=±1.5 (run line) — not informative
            # Only use spread for sports where it varies (NBA, NHL, NCAAB)
            use_spread = spread_val is not None and key != "MLB"
            if use_spread:
                try:
                    sp = abs(float(spread_val))
                    fav_prob = spread_to_prob(sp, key)
                    if home_fav:
                        home_wp = fav_prob
                        away_wp = 1.0 - fav_prob
                    elif away_fav:
                        away_wp = fav_prob
                        home_wp = 1.0 - fav_prob
                    else:
                        home_wp = 0.5
                        away_wp = 0.5
                    prob_source = f"spread({spread_val})"
                except (ValueError, TypeError):
                    pass

            # --- Source 1b: moneyline from odds ---
            if home_wp is None:
                home_ml = o.get("homeTeamOdds", {}).get("moneyLine")
                away_ml = o.get("awayTeamOdds", {}).get("moneyLine")
                if home_ml and away_ml:
                    try:
                        hm, am = float(home_ml), float(away_ml)
                        hp = 100/(100+hm) if hm > 0 else -hm/(-hm+100)
                        ap = 100/(100+am) if am > 0 else -am/(-am+100)
                        total = hp + ap
                        home_wp = hp / total
                        away_wp = ap / total
                        prob_source = f"moneyline({home_ml}/{away_ml})"
                    except (ValueError, ZeroDivisionError):
                        pass

        # --- Source 2: ESPN predictor ---
        if home_wp is None:
            predictor = comp.get("predictor", {})
            if predictor:
                hp = predictor.get("homeTeam", {}).get("gameProjection")
                ap = predictor.get("awayTeam", {}).get("gameProjection")
                if hp:
                    home_wp = float(hp) / 100.0
                    away_wp = float(ap) / 100.0 if ap else 1.0 - home_wp
                    prob_source = "predictor"

        # --- Source 3: record-based Log5 ---
        home_rec = home_c.get("records", [{}])[0].get("summary", "0-0") if home_c.get("records") else "0-0"
        away_rec = away_c.get("records", [{}])[0].get("summary", "0-0") if away_c.get("records") else "0-0"

        if home_wp is None:
            hw = _parse_record(home_rec)
            aw = _parse_record(away_rec)
            # Add small home-field advantage
            hfa = {"MLB": 0.02, "NBA": 0.03, "NHL": 0.02, "NCAAB": 0.04}.get(key, 0.02)
            home_wp = min(_log5(hw + hfa, aw), 0.95)
            away_wp = 1.0 - home_wp
            prob_source = f"log5({home_rec}/{away_rec})"

        status = event.get("status", {}).get("type", {}).get("name", "")

        games.append({
            "event_id": event.get("id"),
            "name": event.get("shortName", ""),
            "status": status,
            "sport": key,
            "home_team": home_name,
            "home_abbr": home_abbr,
            "home_record": home_rec,
            "home_score": home_c.get("score"),
            "home_win_prob": home_wp,
            "away_team": away_name,
            "away_abbr": away_abbr,
            "away_record": away_rec,
            "away_score": away_c.get("score"),
            "away_win_prob": away_wp,
            "prob_source": prob_source,
            "start_time": event.get("date"),
        })

    return games


# ── Kalshi ticker parsing ─────────────────────────────────────────────────
def _extract_teams_from_event(event_ticker: str) -> tuple[Optional[str], Optional[str]]:
    """Extract team abbreviations from Kalshi event ticker.
    e.g. KXNBAGAME-26APR06DETORL → DET, ORL
         KXMLBGAME-26APR071510BALCWS → BAL, CWS"""
    m = re.search(r'-\d{2}[A-Z]{3}\d{4,6}([A-Z]{2,5})([A-Z]{2,5})$', event_ticker)
    if m:
        return m.group(1), m.group(2)
    return None, None


def _team_from_ticker_suffix(ticker: str) -> Optional[str]:
    """Extract team from market ticker suffix.
    e.g. KXNBAGAME-26APR06DETORL-ORL → ORL
         KXNBAGAME-26APR06DETORL-DET → DET"""
    parts = ticker.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isalpha():
        return parts[1]
    return None


# ── Edge Evaluator ────────────────────────────────────────────────────────
def evaluate_sports_edge(
    kalshi_markets: list[dict],
    espn_games: list[dict],
    bankroll: float = 1000,
) -> list[dict]:
    """
    Compare Kalshi moneyline prices against ESPN-derived win probabilities.
    Returns picks sorted by edge descending.
    """
    # Build ESPN lookups keyed by (sport, abbr) to prevent cross-sport matches
    espn_by_sport_abbr: dict[tuple[str, str], dict] = {}
    for g in espn_games:
        sport = g.get("sport", "").upper()
        espn_by_sport_abbr[(sport, g["home_abbr"].upper())] = g
        espn_by_sport_abbr[(sport, g["away_abbr"].upper())] = g

    # Map Kalshi category to ESPN sport key
    _cat_to_sport = {"MLB": "MLB", "NBA": "NBA", "NHL": "NHL", "NCAAB": "NCAAB"}

    picks = []
    seen_events: set[str] = set()  # one pick per event

    for mkt in kalshi_markets:
        if mkt["market_type"] != "moneyline":
            continue

        if mkt["open_interest"] < MIN_OI:
            continue

        event_tk = mkt.get("event_ticker", "")
        team_a, team_b = _extract_teams_from_event(event_tk)
        if not team_a:
            continue

        # Match only within the same sport
        mkt_sport = _cat_to_sport.get(mkt["category"])
        if not mkt_sport:
            continue

        # Find matching ESPN game (same sport only)
        game = None
        for abbr in [team_a, team_b]:
            key = (mkt_sport, abbr.upper())
            if key in espn_by_sport_abbr:
                game = espn_by_sport_abbr[key]
                break
            # Alias match
            alias = _KALSHI_TO_ESPN.get(abbr.upper())
            if alias:
                key2 = (mkt_sport, alias)
                if key2 in espn_by_sport_abbr:
                    game = espn_by_sport_abbr[key2]
                    break
        if not game:
            continue

        # Which team does this Kalshi market represent? (YES = this team wins)
        mkt_team = _team_from_ticker_suffix(mkt["ticker"])
        if not mkt_team:
            continue

        # Match market team to home or away
        is_home = _abbr_match(mkt_team, game["home_abbr"])
        is_away = _abbr_match(mkt_team, game["away_abbr"])
        if not is_home and not is_away:
            continue

        true_prob_yes = game["home_win_prob"] if is_home else game["away_win_prob"]
        if true_prob_yes is None:
            continue

        ya = mkt["yes_ask"]
        na = mkt["no_ask"]

        # Edge: YES side = true_prob - yes_ask, NO side = (1-true_prob) - no_ask
        edge_yes = true_prob_yes - ya
        edge_no  = (1 - true_prob_yes) - na

        best_side  = "YES" if edge_yes >= edge_no else "NO"
        best_edge  = max(edge_yes, edge_no)
        best_price = ya if best_side == "YES" else na
        true_p     = true_prob_yes if best_side == "YES" else 1 - true_prob_yes

        if best_edge < MIN_EDGE:
            continue
        if best_price < MIN_PRICE or best_price > MAX_PRICE:
            continue

        # Deduplicate — one pick per event (keep highest edge)
        event_key = event_tk
        if event_key in seen_events:
            continue
        seen_events.add(event_key)

        # Kelly sizing
        decimal_odds = 1.0 / best_price if best_price > 0 else 1
        kelly_full = (true_p * decimal_odds - 1) / (decimal_odds - 1) if decimal_odds > 1 else 0
        kelly_bet = max(0, kelly_full * KELLY_FRAC * bankroll)
        kelly_bet = min(kelly_bet, bankroll * 0.10)  # cap at 10%

        picks.append({
            "ticker": mkt["ticker"],
            "category": mkt["category"],
            "market_type": "moneyline",
            "side": best_side,
            "price": best_price,
            "true_prob": round(true_p, 3),
            "edge": round(best_edge, 3),
            "edge_pct": f"{best_edge*100:.1f}%",
            "suggested_stake": round(kelly_bet, 2),
            "title": mkt["title"],
            "mkt_team": mkt_team,
            "espn_game": game["name"],
            "espn_status": game["status"],
            "prob_source": game["prob_source"],
            "home_team": game["home_team"],
            "away_team": game["away_team"],
            "home_wp": round(game["home_win_prob"], 3) if game["home_win_prob"] else None,
            "away_wp": round(game["away_win_prob"], 3) if game["away_win_prob"] else None,
            "open_interest": mkt["open_interest"],
            "minutes_remaining": mkt["minutes_remaining"],
        })

    return sorted(picks, key=lambda p: p["edge"], reverse=True)


# ── CLI test ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    async def _test():
        print("=== ESPN Data (with spread → win prob) ===\n")
        all_games = []
        for sport in ["MLB", "NBA", "NHL", "NCAAB"]:
            games = await get_espn_games(sport)
            all_games.extend(games)
            live = sum(1 for g in games if "PROGRESS" in g["status"])
            sched = sum(1 for g in games if "SCHEDULED" in g["status"])
            fin = sum(1 for g in games if "FINAL" in g["status"])
            print(f"{sport}: {len(games)} games ({live} live, {sched} upcoming, {fin} final)")
            for g in games:
                hwp = f"{g['home_win_prob']*100:.0f}%" if g["home_win_prob"] else "n/a"
                awp = f"{g['away_win_prob']*100:.0f}%" if g["away_win_prob"] else "n/a"
                print(f"  {g['name']:30s} home={hwp:5s} away={awp:5s}  [{g['prob_source']}]")
            print()

        # Now test against live Kalshi markets
        print("=== Edge Scan vs Kalshi ===\n")
        from data.feeds.kalshi_all_markets import get_all_markets
        mkt_data = await get_all_markets()
        sports_mkts = mkt_data["sports"]
        moneyline = [m for m in sports_mkts if m["market_type"] == "moneyline"]
        print(f"Kalshi moneyline markets: {len(moneyline)}")

        picks = evaluate_sports_edge(moneyline, all_games, bankroll=6.48)
        if picks:
            print(f"\n*** {len(picks)} PICKS WITH EDGE ***\n")
            for p in picks:
                print(f"  {p['ticker']}")
                print(f"    {p['side']} @ ${p['price']:.2f}  edge={p['edge_pct']}  "
                      f"true_prob={p['true_prob']:.0%}  [{p['prob_source']}]")
                print(f"    {p['espn_game']}  OI={p['open_interest']:.0f}  "
                      f"stake=${p['suggested_stake']:.2f}")
                print()
        else:
            print("\nNo picks with sufficient edge right now.")
            print("(This is expected — wait for odds/market mispricing.)")

    asyncio.run(_test())
