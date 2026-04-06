"""
KALISHI EDGE — MCP Server
=========================
FastAPI-based MCP (Model Context Protocol) server exposing all betting
tools as callable endpoints for AI agents and the dashboard.

Port: 8420 (configurable via MCP_PORT env var)
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, List
import asyncio
import json
import uvicorn
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from engine.kelly import calculate_kelly, profit_machine_split, american_to_decimal
from engine.ev import calculate_ev, true_probability_no_vig, acts_of_god_adjustment
from engine.arbitrage import find_two_way_arb, find_three_way_arb, scan_multibook_lines
from engine.monte_carlo import mlb_game_sim, nba_game_sim, nfl_game_sim, nhl_game_sim
from engine.mlb_metrics import analyze_mlb_matchup, fip, woba, era
from engine.bankroll import BankrollManager

# ── AI / RAG / Intelligence (lazy-loaded so server starts without optional deps) ──
def _get_brain():
    try:
        from agents.brain import get_brain
        return get_brain()
    except Exception:
        return None

def _get_steam():
    try:
        from intelligence.steam_detector import get_steam_detector
        return get_steam_detector()
    except Exception:
        return None

def _get_consensus():
    try:
        from intelligence.consensus import MarketConsensus
        return MarketConsensus()
    except Exception:
        return None

def _get_rag():
    try:
        from rag.knowledge_base import KnowledgeBase
        kb = KnowledgeBase()
        kb.seed_static_knowledge()
        return kb
    except Exception:
        return None

def _get_retriever():
    try:
        from rag.retriever import KalishiRetriever
        return KalishiRetriever()
    except Exception:
        return None

# ── App ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="KALISHI EDGE — Personal Sports Betting AI",
    description="Your ultimate edge: Kelly, EV, arbitrage, Monte Carlo, and AI predictions",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BANKROLL = float(os.getenv("BANKROLL_TOTAL", "10000"))
bankroll_mgr = BankrollManager(BANKROLL)

# ── Startup: seed RAG knowledge base ─────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    try:
        from rag.knowledge_base import KnowledgeBase
        kb = KnowledgeBase()
        await asyncio.to_thread(kb.seed_static_knowledge)
        await asyncio.to_thread(kb.ingest_bets_from_db)
        print("[KALISHI] RAG knowledge base seeded")
    except Exception as e:
        print(f"[KALISHI] RAG startup skipped: {e}")

# WebSocket connections for live dashboard updates
_ws_clients: list[WebSocket] = []
_ai_ws_clients: list[WebSocket] = []

async def broadcast_ai(data: dict):
    """Broadcast AI agent events to AI WebSocket clients."""
    msg = json.dumps(data)
    disconnected = []
    for ws in _ai_ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in _ai_ws_clients:
            _ai_ws_clients.remove(ws)

async def broadcast(data: dict):
    """Broadcast update to all dashboard WebSocket clients."""
    msg = json.dumps(data)
    disconnected = []
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        _ws_clients.remove(ws)


# ── Models ─────────────────────────────────────────────────────────────────

class KellyRequest(BaseModel):
    our_prob: float = Field(..., ge=0.01, le=0.99, description="Our estimated win probability")
    american_odds: int = Field(..., description="American odds (+150, -110, etc)")
    bankroll: Optional[float] = None
    kelly_fraction: float = Field(0.25, ge=0.1, le=1.0)
    min_edge: float = 0.03

class EVRequest(BaseModel):
    our_prob: float = Field(..., ge=0.01, le=0.99)
    decimal_odds: float = Field(..., gt=1.0)

class ArbRequest(BaseModel):
    side_a_odds: float = Field(..., gt=1.0)
    side_b_odds: float = Field(..., gt=1.0)
    draw_odds: Optional[float] = None
    stake: float = 100.0

class MLBRequest(BaseModel):
    home_team: str
    away_team: str
    home_starter_fip: float = 4.00
    away_starter_fip: float = 4.00
    home_wrc_plus: float = 100.0
    away_wrc_plus: float = 100.0
    park_factor: float = 1.00
    home_bullpen_era: float = 4.00
    away_bullpen_era: float = 4.00
    temp_f: float = 72.0
    wind_mph: float = 0.0
    wind_out: bool = False
    total_line: float = 8.5
    n_sims: int = Field(50000, ge=1000, le=200000)

class NBARequest(BaseModel):
    home_team: str
    away_team: str
    home_off_rtg: float = 112.0
    home_def_rtg: float = 112.0
    away_off_rtg: float = 112.0
    away_def_rtg: float = 112.0
    home_pace: float = 100.0
    away_pace: float = 100.0
    back_to_back_home: bool = False
    back_to_back_away: bool = False
    spread: float = 0.0
    total_line: float = 220.0
    n_sims: int = 50000

class NFLRequest(BaseModel):
    home_team: str
    away_team: str
    home_dvoa: float = 0.0
    away_dvoa: float = 0.0
    home_epa: float = 0.0
    away_epa: float = 0.0
    weather_factor: float = 1.0
    short_week_home: bool = False
    short_week_away: bool = False
    spread: float = 0.0
    total_line: float = 44.5
    n_sims: int = 50000

class BetSlipRequest(BaseModel):
    sport: str
    event: str
    market: str
    pick: str
    american_odds: int
    stake: float
    ev: float
    edge: float
    strategy: str = "value"

class ActsOfGodRequest(BaseModel):
    base_prob: float
    weather_impact: float = 0.0
    travel_impact: float = 0.0
    injury_impact: float = 0.0
    altitude_impact: float = 0.0
    rest_impact: float = 0.0

class ProfitMachineRequest(BaseModel):
    bankroll: Optional[float] = None
    confidence: str = "standard"

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    clear_history: bool = False

# ── Player Props Request Models ────────────────────────────────────────────

class NBAPropsRequest(BaseModel):
    player_name: str
    prop_type: str = "points"           # points, rebounds, assists, 3pm, pra, blocks, steals
    line: float
    american_odds_over: int = -110
    american_odds_under: int = -110
    season_avg: float
    opp_def_rtg: float = 112.0
    opp_pace: float = 100.0
    usage_rate: float = 0.25
    minutes_avg: float = 33.0
    last_5_avg: Optional[float] = None
    back_to_back: bool = False
    home_game: bool = True
    bankroll: Optional[float] = None

class NFLPropsRequest(BaseModel):
    player_name: str
    prop_type: str = "passing_yards"    # passing_yards, rushing_yards, receiving_yards, pass_tds, rush_tds, receptions
    line: float
    american_odds_over: int = -115
    american_odds_under: int = -115
    season_avg: float
    opp_pass_def_rank: int = 16
    opp_rush_def_rank: int = 16
    game_total: float = 44.5
    pass_volume: float = 35.0
    implied_team_score: float = 22.0
    last_3_avg: Optional[float] = None
    weather_wind_mph: float = 0.0
    dome_game: bool = False
    back_to_back_short_week: bool = False
    bankroll: Optional[float] = None

class MLBPropsRequest(BaseModel):
    player_name: str
    prop_type: str = "hits"             # hits, total_bases, strikeouts, rbis, runs, hrs
    line: float
    american_odds_over: int = -115
    american_odds_under: int = -115
    season_avg: float
    opp_starter_fip: float = 4.00
    opp_starter_k9: float = 8.5
    batter_hand: str = "R"
    pitcher_hand: str = "R"
    batter_ba_vs_hand: Optional[float] = None
    batter_slg_vs_hand: Optional[float] = None
    park_factor: float = 1.00
    temp_f: float = 72.0
    wind_out: bool = False
    wind_mph: float = 0.0
    last_7_avg: Optional[float] = None
    bankroll: Optional[float] = None

class NHLPropsRequest(BaseModel):
    player_name: str
    prop_type: str = "shots"            # shots, goals, assists, points, saves
    line: float
    american_odds_over: int = -115
    american_odds_under: int = -115
    season_avg: float
    opp_shots_allowed_pg: float = 30.0
    opp_save_pct: float = 0.910
    toi_avg: float = 18.0
    pp_time: float = 2.0
    line_mates_quality: float = 1.0
    back_to_back: bool = False
    last_5_avg: Optional[float] = None
    bankroll: Optional[float] = None

class NCAAMatchupRequest(BaseModel):
    team_a: str
    team_b: str
    seed_a: int = Field(1, ge=1, le=16)
    seed_b: int = Field(16, ge=1, le=16)
    adj_em_a: float = 20.0              # KenPom Adjusted Efficiency Margin
    adj_em_b: float = 10.0
    adj_off_a: float = 110.0
    adj_def_a: float = 95.0
    adj_off_b: float = 105.0
    adj_def_b: float = 100.0
    momentum_a: float = 0.0             # net wins last 5 (+5 to -5)
    momentum_b: float = 0.0
    conf_games_played_a: int = 0        # conference tourney games played (fatigue)
    conf_games_played_b: int = 0
    american_odds_a: int = -200
    american_odds_b: int = +170
    round_name: str = "First Round"
    conference_a: str = "SEC"
    conference_b: str = "ACC"
    bankroll: Optional[float] = None

class PickAnalysisRequest(BaseModel):
    sport: str
    event: str
    market: str
    edge_pct: float
    ev_pct: float
    our_prob: float
    implied_prob: float
    american_odds: int
    stake: float
    additional_context: Optional[dict] = None

class ConsensusRequest(BaseModel):
    event:         str
    sport:         str
    market:        str
    outcome:       str
    model_prob:    float
    market_odds:   float
    sharp_odds:    Optional[float] = None
    steam_alert:   bool = False
    rlm_signal:    bool = False
    injury_impact: float = 0.0

class BetfairPlaceRequest(BaseModel):
    """Manual single-bet placement on Betfair."""
    sport:         str                      # nba | nfl | mlb | nhl
    team:          str                      # team/player we are backing
    opponent:      str
    american_odds: int
    edge_pct:      float
    kelly_fraction: float = 0.02
    stake:         Optional[float] = None   # override Kelly sizing
    dry_run:       bool = True              # must be False to place real money

class BetfairAutoRequest(BaseModel):
    """Auto-execute all of today's value picks on Betfair."""
    min_edge:  float = 0.04          # only execute picks above this edge
    bankroll:  Optional[float] = None
    dry_run:   bool = True           # must be False to place real money

class KalshiPlaceRequest(BaseModel):
    """Place a single order on Kalshi (US-legal, CFTC-regulated)."""
    sport:          str                   # nba | nfl | mlb | nhl | ncaab
    team:           str                   # team we are backing
    our_prob:       float                 # our model's win probability (0-1 or 0-100)
    edge_pct:       float                 # our edge vs market
    kelly_fraction: float = 0.02
    dry_run:        bool  = True          # must be False to place real order

class KalshiAutoRequest(BaseModel):
    """Auto-execute today's value picks on Kalshi."""
    min_edge: float = 0.04
    bankroll: Optional[float] = None
    dry_run:  bool  = True               # must be False to place real orders

class LineFeedRequest(BaseModel):
    event:   str
    sport:   str
    market:  str
    book:    str
    outcome: str
    odds:    float
    public_home_pct: Optional[float] = None

class RAGSearchRequest(BaseModel):
    query:             str
    collections:       Optional[List[str]] = None
    n_per_collection:  int = Field(3, ge=1, le=10)


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "system": "KALISHI EDGE",
        "version": "1.0.0",
        "status": "operational",
        "tools": [
            "/kelly", "/ev", "/arbitrage", "/no-vig",
            "/simulate/mlb", "/simulate/nba", "/simulate/nfl", "/simulate/ncaa",
            "/profit-machine", "/acts-of-god",
            "/bankroll", "/bets", "/picks/today",
            "/picks/college", "/picks/props",
            "/props/nba", "/props/nfl", "/props/mlb", "/props/nhl",
        ]
    }


@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now().isoformat()}


# ── Kelly Criterion ────────────────────────────────────────────────────────

@app.post("/kelly")
def kelly_endpoint(req: KellyRequest):
    """Calculate optimal bet size using Kelly Criterion."""
    bankroll = req.bankroll or bankroll_mgr.current
    decimal_odds = american_to_decimal(req.american_odds)
    result = calculate_kelly(
        our_prob=req.our_prob,
        decimal_odds=decimal_odds,
        bankroll=bankroll,
        kelly_multiplier=req.kelly_fraction,
        min_edge=req.min_edge,
    )
    return {
        "edge_pct": round(result.edge * 100, 2),
        "raw_kelly_pct": round(result.fraction * 100, 2),
        "recommended_pct": round(result.recommended * 100, 2),
        "bet_amount": result.bet_amount,
        "ev_per_100": round(result.ev * 100, 2),
        "implied_prob": round(result.implied_prob * 100, 2),
        "our_prob": round(result.our_prob * 100, 2),
        "verdict": result.verdict,
        "bankroll": bankroll,
    }


# ── Expected Value ─────────────────────────────────────────────────────────

@app.post("/ev")
def ev_endpoint(req: EVRequest):
    """Calculate Expected Value for a bet."""
    result = calculate_ev(req.our_prob, req.decimal_odds)
    return {
        "ev": round(result.ev, 4),
        "ev_pct": round(result.ev_pct, 2),
        "edge_pct": round(result.edge * 100, 2),
        "break_even_prob": round(result.break_even_prob * 100, 2),
        "confidence": result.confidence,
        "positive": result.positive,
    }


# ── Arbitrage ──────────────────────────────────────────────────────────────

@app.post("/arbitrage")
def arbitrage_endpoint(req: ArbRequest):
    """Find arbitrage opportunity between two books."""
    if req.draw_odds:
        result = find_three_way_arb(req.side_a_odds, req.draw_odds, req.side_b_odds, req.stake)
    else:
        result = find_two_way_arb(req.side_a_odds, req.side_b_odds, req.stake)
    
    if result is None:
        return {"arb_exists": False, "message": "No arbitrage opportunity found"}
    return result


# ── No-Vig True Probability ────────────────────────────────────────────────

@app.get("/no-vig")
def no_vig(home: float, away: float, draw: Optional[float] = None):
    """Remove bookmaker vig and return true market probabilities."""
    return true_probability_no_vig(home, away, draw)


# ── Profit Machine Protocol 2.0 ────────────────────────────────────────────

@app.post("/profit-machine")
def profit_machine_endpoint(req: ProfitMachineRequest):
    """Generate Profit Machine Protocol 2.0 bet allocation."""
    bankroll = req.bankroll or bankroll_mgr.current
    split = profit_machine_split(bankroll, req.confidence)
    return {
        **split,
        "protocol": "Profit Machine Protocol 2.0",
        "expected_win_rate": "70-80% (compound across all legs)",
        "strategy": {
            "primary_50pct": "Moneyline or spread — data-driven favorite",
            "hedge_20pct": "Alternate spread or opposite side protection",
            "props_20pct": "Player props (65-75% individual win rate)",
            "high_payout_10pct": "Parlay or alt spread (+150 or better)",
        }
    }


# ── Acts of God Adjustment ─────────────────────────────────────────────────

@app.post("/acts-of-god")
def acts_of_god_endpoint(req: ActsOfGodRequest):
    """Adjust probability for exogenous 'Acts of God' factors."""
    adjusted = acts_of_god_adjustment(
        base_prob=req.base_prob,
        weather_impact=req.weather_impact,
        travel_impact=req.travel_impact,
        injury_impact=req.injury_impact,
        altitude_impact=req.altitude_impact,
        rest_impact=req.rest_impact,
    )
    delta = adjusted - req.base_prob
    return {
        "base_prob": req.base_prob,
        "adjusted_prob": round(adjusted, 4),
        "delta": round(delta, 4),
        "factors": {
            "weather": req.weather_impact,
            "travel": req.travel_impact,
            "injury": req.injury_impact,
            "altitude": req.altitude_impact,
            "rest": req.rest_impact,
        }
    }


# ── Sport Simulations ──────────────────────────────────────────────────────

@app.post("/simulate/mlb")
def simulate_mlb(req: MLBRequest):
    """Run Monte Carlo MLB game simulation using sabermetrics."""
    from engine.mlb_metrics import analyze_mlb_matchup
    
    # Sabermetric matchup analysis
    matchup = analyze_mlb_matchup(
        home_team=req.home_team,
        away_team=req.away_team,
        home_starter_fip=req.home_starter_fip,
        away_starter_fip=req.away_starter_fip,
        home_team_wrc_plus=req.home_wrc_plus,
        away_team_wrc_plus=req.away_wrc_plus,
        park_factor=req.park_factor,
        home_bullpen_era=req.home_bullpen_era,
        away_bullpen_era=req.away_bullpen_era,
        temp_f=req.temp_f,
        wind_mph=req.wind_mph,
        wind_out=req.wind_out,
        total_line=req.total_line,
    )
    
    # Monte Carlo simulation
    sim = mlb_game_sim(
        home_era=req.home_starter_fip,
        away_era=req.away_starter_fip,
        home_wrc_plus=req.home_wrc_plus,
        away_wrc_plus=req.away_wrc_plus,
        park_factor=req.park_factor,
        wind_mph=req.wind_mph,
        dome=False,
        total_line=req.total_line,
        n_sims=req.n_sims,
    )
    
    return {
        "matchup": {
            "home": req.home_team,
            "away": req.away_team,
            "predicted_score": f"{matchup.predicted_home_runs:.1f} — {matchup.predicted_away_runs:.1f}",
            "total_predicted": matchup.total_predicted,
        },
        "probabilities": {
            "home_win": round(sim.home_win_prob * 100, 1),
            "away_win": round(sim.away_win_prob * 100, 1),
            "over": round(sim.over_prob * 100, 1),
            "under": round((1 - sim.over_prob) * 100, 1),
        },
        "sabermetrics": {
            "home_starter_fip": req.home_starter_fip,
            "away_starter_fip": req.away_starter_fip,
            "home_wrc_plus": req.home_wrc_plus,
            "away_wrc_plus": req.away_wrc_plus,
            "park_factor": req.park_factor,
            "weather_adj": matchup.weather_adjustment,
        },
        "edge": {
            "over_line": req.total_line,
            "over_prob": matchup.edge_over,
            "under_prob": matchup.edge_under,
        },
        "confidence_interval_95": [round(x*100, 1) for x in sim.confidence_interval_95],
        "n_simulations": req.n_sims,
    }


@app.post("/simulate/nba")
def simulate_nba(req: NBARequest):
    """Run Monte Carlo NBA game simulation."""
    sim = nba_game_sim(
        home_off_rtg=req.home_off_rtg,
        home_def_rtg=req.home_def_rtg,
        away_off_rtg=req.away_off_rtg,
        away_def_rtg=req.away_def_rtg,
        home_pace=req.home_pace,
        away_pace=req.away_pace,
        back_to_back_home=req.back_to_back_home,
        back_to_back_away=req.back_to_back_away,
        spread=req.spread,
        total_line=req.total_line,
        n_sims=req.n_sims,
    )
    return {
        "matchup": {"home": req.home_team, "away": req.away_team},
        "probabilities": {
            "home_win": round(sim.home_win_prob * 100, 1),
            "away_win": round(sim.away_win_prob * 100, 1),
            "home_cover_spread": round(sim.spread_cover_prob * 100, 1),
            "over": round(sim.over_prob * 100, 1),
        },
        "predicted_scores": {
            "home_median": round(sim.median_home_score, 1),
            "away_median": round(sim.median_away_score, 1),
        },
        "flags": {
            "home_b2b": req.back_to_back_home,
            "away_b2b": req.back_to_back_away,
        },
    }


@app.post("/simulate/nfl")
def simulate_nfl(req: NFLRequest):
    """Run Monte Carlo NFL game simulation."""
    sim = nfl_game_sim(
        home_dvoa=req.home_dvoa,
        away_dvoa=req.away_dvoa,
        home_epa=req.home_epa,
        away_epa=req.away_epa,
        weather_factor=req.weather_factor,
        short_week_home=req.short_week_home,
        short_week_away=req.short_week_away,
        spread=req.spread,
        total_line=req.total_line,
        n_sims=req.n_sims,
    )
    return {
        "matchup": {"home": req.home_team, "away": req.away_team},
        "probabilities": {
            "home_win": round(sim.home_win_prob * 100, 1),
            "away_win": round(sim.away_win_prob * 100, 1),
            "home_cover_spread": round(sim.spread_cover_prob * 100, 1),
            "over": round(sim.over_prob * 100, 1),
        },
        "flags": {
            "bad_weather": req.weather_factor < 0.95,
            "home_short_week": req.short_week_home,
            "away_short_week": req.short_week_away,
        },
    }


# ── Bankroll ───────────────────────────────────────────────────────────────

@app.get("/bankroll")
def get_bankroll():
    """Get current bankroll state and statistics."""
    state = bankroll_mgr.snapshot()
    return {
        "starting": state.starting,
        "current": round(state.current, 2),
        "pnl": round(state.current - state.starting, 2),
        "pnl_pct": round((state.current - state.starting) / state.starting * 100, 2),
        "roi": round(state.roi * 100, 2),
        "win_rate": round(state.win_rate * 100, 2),
        "bets": {
            "placed": state.bets_placed,
            "won": state.bets_won,
            "lost": state.bets_lost,
            "push": state.bets_push,
        },
        "max_drawdown_pct": round(state.max_drawdown * 100, 2),
        "clv_avg": round(state.clv_avg * 100, 4),
        "high_water_mark": round(state.high_water_mark, 2),
    }


@app.get("/bankroll/history")
def get_bankroll_history(days: int = 30):
    """Return daily bankroll snapshots for the equity curve chart."""
    import sqlite3, pathlib
    db_path = pathlib.Path("./db/kalishi_edge.db")
    snapshots: list[dict] = []
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute(
                "SELECT snapshot_date, bankroll, pnl, roi_pct FROM bankroll_snapshots "
                "ORDER BY snapshot_date DESC LIMIT ?",
                (days,)
            ).fetchall()
            conn.close()
            snapshots = [
                {"date": r[0], "bankroll": r[1], "pnl": r[2], "roi_pct": r[3]}
                for r in reversed(rows)
            ]
        except Exception:
            pass
    if not snapshots:
        # Return seed point so dashboard chart renders immediately
        from datetime import datetime
        snapshots = [{"date": datetime.now().strftime("%Y-%m-%d"),
                      "bankroll": bankroll_mgr.current, "pnl": 0.0, "roi_pct": 0.0}]
    return {"history": snapshots, "count": len(snapshots)}


@app.get("/bets")
def get_bets(status: Optional[str] = None, sport: Optional[str] = None, limit: int = 50):
    """Get bet history with optional filters."""
    bets = bankroll_mgr.bets
    if status:
        bets = [b for b in bets if b.result == status]
    if sport:
        bets = [b for b in bets if b.sport.lower() == sport.lower()]
    bets = bets[-limit:]
    return {
        "bets": [
            {
                "id": b.id,
                "sport": b.sport,
                "event": b.event,
                "pick": b.pick,
                "odds": b.odds_dec,
                "stake": b.stake,
                "ev_pct": round(b.ev * 100, 2),
                "edge_pct": round(b.edge * 100, 2),
                "result": b.result or "pending",
                "pnl": round(b.pnl, 2),
                "strategy": b.strategy,
                "placed_at": b.placed_at.isoformat(),
            }
            for b in reversed(bets)
        ],
        "count": len(bets),
    }


@app.post("/bets")
async def place_bet(req: BetSlipRequest):
    """Record a new bet in the system."""
    decimal_odds = american_to_decimal(req.american_odds)
    ev_result = calculate_ev(1 - (1 / decimal_odds) + req.edge, decimal_odds)
    
    bet = bankroll_mgr.place_bet(
        sport=req.sport,
        event=req.event,
        market=req.market,
        pick=req.pick,
        american_odds=req.american_odds,
        stake=req.stake,
        ev=req.ev,
        edge=req.edge,
        strategy=req.strategy,
    )
    
    await broadcast({"type": "new_bet", "bet_id": bet.id, "event": req.event})
    
    return {"ok": True, "bet_id": bet.id, "bankroll_remaining": round(bankroll_mgr.current, 2)}


# ── Today's Picks ──────────────────────────────────────────────────────────

@app.get("/picks/today")
async def todays_picks():
    """
    Generate today's picks by running all agents.
    Fetches live odds and applies all models.
    """
    from agents.orchestrator import run_daily_picks
    picks = await run_daily_picks()
    return picks


# ── WebSocket for Live Dashboard ───────────────────────────────────────────

@app.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        # Send initial state
        await ws.send_text(json.dumps({
            "type": "connected",
            "bankroll": bankroll_mgr.current,
            "ts": datetime.now().isoformat(),
        }))
        while True:
            # Heartbeat
            await asyncio.sleep(30)
            await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ── Performance Analytics ──────────────────────────────────────────────────

@app.get("/analytics/performance")
def analytics_performance():
    """Full CLV, ROI, edge-bucket, and agent-attribution breakdown."""
    from engine.analytics import build_performance_report
    raw = [
        {
            "sport":        b.sport,
            "market":       b.market,
            "stake":        b.stake,
            "pnl":          b.pnl if b.result else None,
            "result":       b.result,
            "edge_pct":     round(b.edge * 100, 2),
            "closing_odds": b.closing_odds,
            "strategy":     b.strategy,
            "placed_at":    b.placed_at.isoformat(),
        }
        for b in bankroll_mgr.bets
    ]
    return build_performance_report(raw)


# ── Line Shop — Best Available Odds Across Books ───────────────────────────

BOOKS_TO_QUERY = "draftkings,fanduel,betmgm,caesars,pointsbet,barstool,wynn"

@app.get("/lines/best")
async def best_lines(sport: str = "upcoming", limit: int = 10):
    """Return best available moneyline / spread per event across all major books."""
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return {"markets": _mock_line_shop()}

    try:
        import httpx
    except ImportError:
        return {"markets": _mock_line_shop(), "note": "pip install httpx for live data"}

    sports_map = {
        "nfl": "americanfootball_nfl",
        "nba": "basketball_nba",
        "mlb": "baseball_mlb",
        "nhl": "icehockey_nhl",
    }
    live_sports = (
        list(sports_map.values()) if sport == "upcoming"
        else [sports_map.get(sport, sport)]
    )

    results = []
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            for sp in live_sports:
                r = await client.get(
                    f"https://api.the-odds-api.com/v4/sports/{sp}/odds/",
                    params={
                        "apiKey": api_key,
                        "regions": "us",
                        "markets": "h2h,spreads",
                        "oddsFormat": "american",
                        "bookmakers": BOOKS_TO_QUERY,
                    },
                )
                if r.status_code == 200:
                    results.extend(r.json()[:3])
    except Exception as e:
        return {"markets": _mock_line_shop(), "error": str(e)}

    formatted = _format_line_shop(results[:limit])
    return {"markets": formatted}


def _format_line_shop(events: list) -> list:
    markets = []
    for ev in events:
        bms = ev.get("bookmakers", [])
        if not bms:
            continue
        entry = {
            "event":    f"{ev.get('away_team', 'Away')} @ {ev.get('home_team', 'Home')}",
            "sport":    ev.get("sport_key", ""),
            "commence": ev.get("commence_time", ""),
            "books":    {},
        }
        home = ev.get("home_team", "")
        for bm in bms:
            name = bm["key"]
            book_data: dict = {}
            for mkt in bm.get("markets", []):
                if mkt["key"] == "h2h":
                    for o in mkt.get("outcomes", []):
                        side = "h2h_home" if o["name"] == home else "h2h_away"
                        book_data[side] = o["price"]
            if book_data:
                entry["books"][name] = book_data
        markets.append(entry)
    return markets


def _mock_line_shop() -> list:
    """Realistic multi-book demo — renders when no API key set."""
    return [
        {
            "event": "Lakers @ Celtics", "sport": "basketball_nba",
            "commence": "2026-04-06T23:30:00Z",
            "books": {
                "draftkings": {"h2h_home": -108, "h2h_away": -112},
                "fanduel":    {"h2h_home": -110, "h2h_away": -110},
                "betmgm":     {"h2h_home": -105, "h2h_away": -115},
                "caesars":    {"h2h_home": -112, "h2h_away": -108},
                "pointsbet":  {"h2h_home": +100, "h2h_away": -120},
            },
        },
        {
            "event": "Yankees @ Red Sox", "sport": "baseball_mlb",
            "commence": "2026-04-07T00:05:00Z",
            "books": {
                "draftkings": {"h2h_home": +105, "h2h_away": -125},
                "fanduel":    {"h2h_home": +108, "h2h_away": -128},
                "betmgm":     {"h2h_home": +110, "h2h_away": -130},
                "caesars":    {"h2h_home": +100, "h2h_away": -120},
                "pointsbet":  {"h2h_home": +112, "h2h_away": -132},
            },
        },
        {
            "event": "Chiefs @ Ravens", "sport": "americanfootball_nfl",
            "commence": "2026-04-06T22:00:00Z",
            "books": {
                "draftkings": {"h2h_home": -135, "h2h_away": +115},
                "fanduel":    {"h2h_home": -130, "h2h_away": +110},
                "betmgm":     {"h2h_home": -140, "h2h_away": +120},
                "caesars":    {"h2h_home": -132, "h2h_away": +112},
                "pointsbet":  {"h2h_home": -128, "h2h_away": +108},
            },
        },
        {
            "event": "Heat @ Bucks", "sport": "basketball_nba",
            "commence": "2026-04-07T01:30:00Z",
            "books": {
                "draftkings": {"h2h_home": -155, "h2h_away": +135},
                "fanduel":    {"h2h_home": -150, "h2h_away": +130},
                "betmgm":     {"h2h_home": -160, "h2h_away": +140},
                "caesars":    {"h2h_home": -152, "h2h_away": +132},
                "pointsbet":  {"h2h_home": -148, "h2h_away": +128},
            },
        },
        {
            "event": "Dodgers @ Giants", "sport": "baseball_mlb",
            "commence": "2026-04-07T02:10:00Z",
            "books": {
                "draftkings": {"h2h_home": +120, "h2h_away": -140},
                "fanduel":    {"h2h_home": +118, "h2h_away": -138},
                "betmgm":     {"h2h_home": +125, "h2h_away": -145},
                "caesars":    {"h2h_home": +115, "h2h_away": -135},
                "pointsbet":  {"h2h_home": +122, "h2h_away": -142},
            },
        },
    ]


# ── Sharp Line Movement Feed ───────────────────────────────────────────────

@app.get("/lines/movement")
def line_movement():
    """Recent significant line movements (sharp money indicator)."""
    # In production: pull from a time-series line DB.
    # Seeded with representative sharp-move examples.
    moves = [
        {"event": "Lakers @ Celtics",  "market": "Spread",    "from_odds": -108, "to_odds": -115, "delta": -7,  "book": "DraftKings", "sharp": True,  "sport": "nba", "age_mins": 12},
        {"event": "Yankees @ Red Sox", "market": "Moneyline", "from_odds": +105, "to_odds": +115, "delta": +10, "book": "FanDuel",    "sharp": True,  "sport": "mlb", "age_mins": 23},
        {"event": "Chiefs @ Ravens",   "market": "Spread",    "from_odds": -130, "to_odds": -140, "delta": -10, "book": "BetMGM",     "sharp": True,  "sport": "nfl", "age_mins": 47},
        {"event": "Dodgers @ Giants",  "market": "Total O/U", "from_odds": -110, "to_odds": -122, "delta": -12, "book": "Caesars",    "sharp": True,  "sport": "mlb", "age_mins": 58},
        {"event": "Heat @ Bucks",      "market": "Moneyline", "from_odds": -155, "to_odds": -148, "delta":  +7, "book": "PointsBet",  "sharp": False, "sport": "nba", "age_mins": 72},
        {"event": "Flyers @ Penguins", "market": "Puck Line", "from_odds": +110, "to_odds": +120, "delta": +10, "book": "DraftKings", "sharp": True,  "sport": "nhl", "age_mins": 89},
    ]
    return {"moves": moves, "count": len(moves)}


# ── Middle Finder ──────────────────────────────────────────────────────────

@app.get("/picks/middles")
async def middles_endpoint():
    """Middle opportunities: bet both sides of a spread for a win-win window."""
    # Seed realistic demo middles; expands with live data when Odds API key available.
    middles = [
        {
            "event": "Lakers @ Celtics", "sport": "nba",
            "leg_a": {"side": "Lakers +4.5",  "odds": -108, "book": "DraftKings", "stake": 108},
            "leg_b": {"side": "Celtics -2.5",  "odds": -108, "book": "BetMGM",    "stake": 108},
            "window": 2.0, "max_win": 188, "guaranteed_loss": -16, "ev_pct": 2.3,
        },
        {
            "event": "Chiefs @ Ravens", "sport": "nfl",
            "leg_a": {"side": "Chiefs +7",    "odds": -110, "book": "FanDuel",   "stake": 110},
            "leg_b": {"side": "Ravens -3",    "odds": -110, "book": "Caesars",   "stake": 110},
            "window": 4.0, "max_win": 200, "guaranteed_loss": -20, "ev_pct": 3.1,
        },
        {
            "event": "Yankees @ Red Sox", "sport": "mlb",
            "leg_a": {"side": "Yankees +1.5", "odds": -120, "book": "DraftKings", "stake": 120},
            "leg_b": {"side": "Red Sox -0.5", "odds": -115, "book": "PointsBet",  "stake": 115},
            "window": 1.0, "max_win": 168, "guaranteed_loss": -23, "ev_pct": 1.4,
        },
    ]
    return {"middles": middles, "count": len(middles)}


# ═══════════════════════════════════════════════════════════════════════════
# ── AI BRAIN ENDPOINTS ─────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/ai/chat")
async def ai_chat(req: ChatRequest):
    """
    Conversational AI interface with RAG augmentation.
    GPT-4o powered. Context-aware with conversation memory.
    """
    brain = _get_brain()
    if not brain:
        return {"response": "AI Brain not available — install openai package and set OPENAI_API_KEY", "available": False}
    if req.clear_history:
        brain.clear_history()
    response = await brain.chat(req.message)
    await broadcast_ai({"type": "ai_chat", "query": req.message[:80], "ts": datetime.now().isoformat()})
    return {"response": response, "available": brain.available, "model": "gpt-4o"}


@app.websocket("/ws/ai")
async def websocket_ai(ws: WebSocket):
    """
    Streaming AI WebSocket — token-by-token response streaming.
    Send: {"message": "your question"}
    Receive: {"type": "token", "delta": "..."} + {"type": "done"}
    """
    await ws.accept()
    _ai_ws_clients.append(ws)
    try:
        await ws.send_text(json.dumps({"type": "ready", "model": "gpt-4o", "rag": True}))
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            msg = data.get("message", "").strip()
            if not msg:
                continue
            brain = _get_brain()
            if not brain:
                await ws.send_text(json.dumps({"type": "error", "message": "AI Brain unavailable"}))
                continue
            await ws.send_text(json.dumps({"type": "thinking"}))
            full = []
            async for token in brain.stream_chat(msg):
                full.append(token)
                await ws.send_text(json.dumps({"type": "token", "delta": token}))
            await ws.send_text(json.dumps({"type": "done", "full_response": "".join(full)}))
    except WebSocketDisconnect:
        if ws in _ai_ws_clients:
            _ai_ws_clients.remove(ws)


@app.post("/ai/analyze-pick")
async def ai_analyze_pick(req: PickAnalysisRequest):
    """
    Full AI-powered pick analysis.
    Returns structured: conviction, reasoning, key edge, risks, action.
    """
    brain = _get_brain()
    if not brain:
        return {"error": "AI Brain unavailable", "conviction": "HOLD", "action": "MONITOR"}
    analysis = await brain.analyze_pick(
        sport=req.sport, event=req.event, market=req.market,
        edge_pct=req.edge_pct, ev_pct=req.ev_pct,
        our_prob=req.our_prob, implied_prob=req.implied_prob,
        american_odds=req.american_odds, stake=req.stake,
        additional_context=req.additional_context,
    )
    return {"analysis": analysis, "model": "gpt-4o"}


@app.get("/ai/briefing")
async def ai_daily_briefing():
    """Generate today's full AI-powered betting briefing."""
    brain = _get_brain()
    if not brain:
        return {"briefing": "AI Brain unavailable", "ts": datetime.now().isoformat()}
    from agents.orchestrator import run_daily_picks
    picks_data = await run_daily_picks()
    picks    = picks_data.get("picks", [])
    bankroll = bankroll_mgr.snapshot()
    market_summary = {
        "bankroll": round(bankroll.current, 2),
        "roi_pct":  round(bankroll.roi * 100, 2),
        "win_rate": round(bankroll.win_rate * 100, 2),
        "open_bets": bankroll.bets_placed,
    }
    briefing = await brain.generate_daily_briefing(picks, market_summary)
    return {"briefing": briefing, "ts": datetime.now().isoformat()}


@app.get("/ai/status")
def ai_status():
    """Check status of all AI subsystems."""
    brain = _get_brain()
    steam = _get_steam()
    rag_stats: dict = {}
    try:
        from rag.embeddings import get_store
        rag_stats = get_store().stats()
    except Exception:
        pass
    return {
        "brain":       {"available": brain.available if brain else False, "model": "gpt-4o"},
        "rag":         rag_stats,
        "steam":       steam.stats() if steam else {"available": False},
        "ts":          datetime.now().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# ── INTELLIGENCE ENDPOINTS ──────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/intelligence/steam")
def get_steam_alerts(limit: int = 30, sharp_only: bool = False, sport: Optional[str] = None):
    """Real-time sharp money / steam move alerts."""
    detector = _get_steam()
    if not detector:
        return {"moves": _get_mock_steam_alerts(), "source": "mock"}
    alerts = detector.get_sharp_alerts(limit) if sharp_only else detector.get_alerts(limit, sport)
    if not alerts:
        alerts = _get_mock_steam_alerts()
    return {"moves": alerts, "count": len(alerts), "stats": detector.stats()}


@app.post("/intelligence/feed")
async def feed_line(req: LineFeedRequest):
    """Ingest a line observation into the steam detector."""
    detector = _get_steam()
    if not detector:
        return {"ok": False, "error": "Steam detector unavailable"}
    alert = detector.feed(
        event=req.event, sport=req.sport, market=req.market,
        book=req.book, outcome=req.outcome, odds=req.odds,
        public_home_pct=req.public_home_pct,
    )
    if alert:
        await broadcast({"type": "steam_alert", **alert.to_dict()})
        await broadcast_ai({"type": "steam_alert", "event": req.event})
        return {"ok": True, "alert": alert.to_dict()}
    return {"ok": True, "alert": None}


@app.post("/ai/consensus")
def ai_consensus(req: ConsensusRequest):
    """Full multi-signal consensus analysis for a single opportunity."""
    consensus = _get_consensus()
    if not consensus:
        return {"error": "Consensus engine unavailable"}
    result = consensus.analyze(
        event=req.event, sport=req.sport, market=req.market, outcome=req.outcome,
        model_prob=req.model_prob, market_odds=req.market_odds, sharp_odds=req.sharp_odds,
        steam_alert=req.steam_alert, rlm_signal=req.rlm_signal, injury_impact=req.injury_impact,
    )
    return result.to_dict()


# ═══════════════════════════════════════════════════════════════════════════
# ── RAG ENDPOINTS ───────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/rag/search")
def rag_search(req: RAGSearchRequest):
    """Semantic search across the RAG knowledge base."""
    retriever = _get_retriever()
    if not retriever:
        return {"results": {}, "error": "RAG unavailable — install chromadb and sentence-transformers"}
    results = retriever._store.multi_query(req.query, req.collections, req.n_per_collection)
    return {"query": req.query, "results": results}


@app.get("/rag/stats")
def rag_stats():
    """RAG vector store collection statistics."""
    try:
        from rag.embeddings import get_store
        return get_store().stats()
    except Exception as e:
        return {"ready": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# ── PLAYER PROPS ENDPOINTS ─────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/props/nba")
def props_nba(req: NBAPropsRequest):
    """
    NBA player prop analysis.
    prop_type: points, rebounds, assists, 3pm, pra, blocks, steals.
    Returns projected stat, edge, EV, and Kelly-sized stake recommendation.
    """
    from agents.props_agent import analyze_nba_prop
    bankroll = req.bankroll or BANKROLL
    return analyze_nba_prop(
        player_name=req.player_name, prop_type=req.prop_type,
        line=req.line, american_odds_over=req.american_odds_over, american_odds_under=req.american_odds_under,
        season_avg=req.season_avg, opp_def_rtg=req.opp_def_rtg, opp_pace=req.opp_pace,
        usage_rate=req.usage_rate, minutes_avg=req.minutes_avg, last_5_avg=req.last_5_avg,
        back_to_back=req.back_to_back, home_game=req.home_game, bankroll=bankroll,
    )


@app.post("/props/nfl")
def props_nfl(req: NFLPropsRequest):
    """
    NFL player prop analysis.
    prop_type: passing_yards, rushing_yards, receiving_yards, pass_tds, rush_tds, receptions.
    """
    from agents.props_agent import analyze_nfl_prop
    bankroll = req.bankroll or BANKROLL
    return analyze_nfl_prop(
        player_name=req.player_name, prop_type=req.prop_type,
        line=req.line, american_odds_over=req.american_odds_over, american_odds_under=req.american_odds_under,
        season_avg=req.season_avg, opp_pass_def_rank=req.opp_pass_def_rank,
        opp_rush_def_rank=req.opp_rush_def_rank, game_total=req.game_total,
        pass_volume=req.pass_volume, implied_team_score=req.implied_team_score,
        last_3_avg=req.last_3_avg, weather_wind_mph=req.weather_wind_mph,
        dome_game=req.dome_game, back_to_back_short_week=req.back_to_back_short_week, bankroll=bankroll,
    )


@app.post("/props/mlb")
def props_mlb(req: MLBPropsRequest):
    """
    MLB player prop analysis.
    prop_type: hits, total_bases, strikeouts, rbis, runs, hrs.
    """
    from agents.props_agent import analyze_mlb_prop
    bankroll = req.bankroll or BANKROLL
    return analyze_mlb_prop(
        player_name=req.player_name, prop_type=req.prop_type,
        line=req.line, american_odds_over=req.american_odds_over, american_odds_under=req.american_odds_under,
        season_avg=req.season_avg, opp_starter_fip=req.opp_starter_fip, opp_starter_k9=req.opp_starter_k9,
        batter_hand=req.batter_hand, pitcher_hand=req.pitcher_hand,
        batter_ba_vs_hand=req.batter_ba_vs_hand, batter_slg_vs_hand=req.batter_slg_vs_hand,
        park_factor=req.park_factor, temp_f=req.temp_f,
        wind_out=req.wind_out, wind_mph=req.wind_mph, last_7_avg=req.last_7_avg, bankroll=bankroll,
    )


@app.post("/props/nhl")
def props_nhl(req: NHLPropsRequest):
    """
    NHL player prop analysis.
    prop_type: shots, goals, assists, points, saves.
    """
    from agents.props_agent import analyze_nhl_prop
    bankroll = req.bankroll or BANKROLL
    return analyze_nhl_prop(
        player_name=req.player_name, prop_type=req.prop_type,
        line=req.line, american_odds_over=req.american_odds_over, american_odds_under=req.american_odds_under,
        season_avg=req.season_avg, opp_shots_allowed_pg=req.opp_shots_allowed_pg,
        opp_save_pct=req.opp_save_pct, toi_avg=req.toi_avg, pp_time=req.pp_time,
        line_mates_quality=req.line_mates_quality, back_to_back=req.back_to_back,
        last_5_avg=req.last_5_avg, bankroll=bankroll,
    )


@app.post("/simulate/ncaa")
def simulate_ncaa(req: NCAAMatchupRequest):
    """
    NCAA tournament game simulation using KenPom + seed history model.
    Returns win probabilities, upset likelihood, cinderella score, and recommended bet.
    """
    from agents.ncaa_agent import analyze_tournament_matchup
    bankroll = req.bankroll or BANKROLL
    dec_a = american_to_decimal(req.american_odds_a)
    dec_b = american_to_decimal(req.american_odds_b)
    return analyze_tournament_matchup(
        team_a=req.team_a, team_b=req.team_b,
        seed_a=req.seed_a, seed_b=req.seed_b,
        adj_em_a=req.adj_em_a, adj_em_b=req.adj_em_b,
        adj_off_a=req.adj_off_a, adj_def_a=req.adj_def_a,
        adj_off_b=req.adj_off_b, adj_def_b=req.adj_def_b,
        momentum_a=req.momentum_a, momentum_b=req.momentum_b,
        conf_games_played_a=req.conf_games_played_a, conf_games_played_b=req.conf_games_played_b,
        decimal_odds_a=dec_a, decimal_odds_b=dec_b,
        round_name=req.round_name,
        conference_a=req.conference_a, conference_b=req.conference_b,
        bankroll=bankroll,
    )


@app.get("/picks/college")
async def college_picks():
    """
    NCAA college basketball picks — March Madness / Finals.
    Pulls live NCAAB odds and runs the KenPom-based tournament model.
    """
    from data.feeds.odds_api import get_odds
    from agents.ncaa_agent import generate_bracket_picks

    try:
        games = await get_odds("ncaab", markets="h2h,spreads,totals")
    except Exception as e:
        games = []
        print(f"[college_picks] NCAAB odds error: {e}")

    if not games:
        return {
            "picks": _mock_college_picks(),
            "source": "demo",
            "note": "Live data requires valid ODDS_API_KEY",
            "sport": "NCAAB",
            "generated_at": datetime.now().isoformat(),
        }

    picks = generate_bracket_picks(games)
    picks.sort(key=lambda x: x.get("edge_pct", 0), reverse=True)
    return {
        "picks": picks[:15],
        "source": "live",
        "sport": "NCAAB",
        "games_analyzed": len(games),
        "generated_at": datetime.now().isoformat(),
    }


def _mock_college_picks() -> list:
    return [
        {
            "team_a": "UConn", "team_b": "San Diego State", "round": "National Championship",
            "pick": "UConn", "direction": "moneyline", "american_odds": -165,
            "our_prob": 62.0, "implied_prob": 62.3, "edge_pct": 2.4, "ev_pct": 1.5,
            "confidence": "LEAN", "upset_alert": False, "cinderella_score": 0,
            "narrative": "UConn's elite defense (adj_def 91.2) dominates SDSU's patient offense.",
        },
        {
            "team_a": "Alabama", "team_b": "Iowa State", "round": "Final Four",
            "pick": "Iowa State", "direction": "moneyline", "american_odds": +145,
            "our_prob": 44.0, "implied_prob": 40.8, "edge_pct": 3.2, "ev_pct": 4.6,
            "confidence": "VALUE", "upset_alert": True, "cinderella_score": 52,
            "narrative": "5-seed Iowa State runs disciplined offense; undervalued vs Alabama's turnover-prone guards.",
        },
        {
            "team_a": "Purdue", "team_b": "NC State", "round": "Elite Eight",
            "pick": "NC State", "direction": "spread +7.5", "american_odds": -108,
            "our_prob": 55.0, "implied_prob": 48.1, "edge_pct": 6.9, "ev_pct": 9.1,
            "confidence": "STRONG_VALUE", "upset_alert": True, "cinderella_score": 78,
            "narrative": "NC State on historic run; momentum + cinderella underdog EV is strongly positive.",
        },
    ]


@app.get("/picks/props")
async def props_picks():
    """
    Player prop picks across all four major sports (NBA/NFL/MLB/NHL).
    Returns example props with full edge/EV/Kelly analysis.
    Wire up to a live player-stats API for real-time personalization.
    """
    from agents.props_agent import (
        analyze_nba_prop, analyze_nfl_prop, analyze_mlb_prop, analyze_nhl_prop, scan_props_for_value,
    )
    bankroll = BANKROLL

    raw_props = [
        # NBA
        analyze_nba_prop("Jayson Tatum",       "points",        27.5, -115, -115, 27.1, opp_def_rtg=110.5, opp_pace=102.0, usage_rate=0.32, minutes_avg=36.0, bankroll=bankroll),
        analyze_nba_prop("Nikola Jokic",        "rebounds",      12.5, -120, -110, 12.8, opp_def_rtg=114.0, opp_pace=98.0,  usage_rate=0.30, minutes_avg=34.5, bankroll=bankroll),
        analyze_nba_prop("Stephen Curry",       "3pm",            4.5, -110, -120,  4.2, opp_def_rtg=112.0, opp_pace=101.0, usage_rate=0.28, minutes_avg=32.0, bankroll=bankroll),
        analyze_nba_prop("LeBron James",        "pra",           43.5, -115, -115, 44.2, opp_def_rtg=109.0, opp_pace=100.0, usage_rate=0.30, minutes_avg=35.0, bankroll=bankroll),
        analyze_nba_prop("Luka Doncic",         "assists",        8.5, -110, -120,  9.1, opp_def_rtg=111.0, opp_pace=99.0,  usage_rate=0.34, minutes_avg=36.0, bankroll=bankroll),
        # NFL
        analyze_nfl_prop("Patrick Mahomes",     "passing_yards", 285.5, -115, -115, 284.3, opp_pass_def_rank=22, pass_volume=37, game_total=47.5, implied_team_score=26.0, bankroll=bankroll),
        analyze_nfl_prop("Christian McCaffrey", "rushing_yards",  82.5, -120, -110,  87.0, opp_rush_def_rank=20, implied_team_score=24.0, bankroll=bankroll),
        analyze_nfl_prop("Davante Adams",       "receiving_yards", 72.5, -110, -120, 68.5, opp_pass_def_rank=12, pass_volume=34, bankroll=bankroll),
        # MLB
        analyze_mlb_prop("Juan Soto",           "total_bases",    1.5, -130, +110,  1.62, opp_starter_fip=4.35, park_factor=1.08, wind_out=True, wind_mph=12, bankroll=bankroll),
        analyze_mlb_prop("Gerrit Cole",         "strikeouts",     7.5, -125, +105,  8.1,  opp_starter_k9=9.8, bankroll=bankroll),
        analyze_mlb_prop("Freddie Freeman",     "hits",           1.5, -140, +115,  1.58, opp_starter_fip=3.90, bankroll=bankroll),
        # NHL
        analyze_nhl_prop("Auston Matthews",     "shots",          3.5, -115, -115,  4.1,  opp_shots_allowed_pg=32.0, toi_avg=21.0, pp_time=3.2, bankroll=bankroll),
        analyze_nhl_prop("Connor McDavid",      "points",         1.5, +110, -140,  1.38, opp_save_pct=0.905, toi_avg=22.5, pp_time=3.5, bankroll=bankroll),
        analyze_nhl_prop("David Pastrnak",      "goals",          0.5, -140, +115,  0.52, opp_save_pct=0.900, toi_avg=19.0, bankroll=bankroll),
    ]

    value_props = scan_props_for_value(raw_props, min_edge=0.03)
    all_props_sorted = sorted(raw_props, key=lambda x: x["edge_pct"], reverse=True)

    return {
        "generated_at": datetime.now().isoformat(),
        "total_props_analyzed": len(raw_props),
        "value_props_found": len(value_props),
        "top_value_props": value_props[:8],
        "all_props": all_props_sorted,
        "by_sport": {
            "nba": [p for p in all_props_sorted if p["sport"] == "NBA"][:4],
            "nfl": [p for p in all_props_sorted if p["sport"] == "NFL"][:3],
            "mlb": [p for p in all_props_sorted if p["sport"] == "MLB"][:3],
            "nhl": [p for p in all_props_sorted if p["sport"] == "NHL"][:3],
        },
    }
    """Demo steam alerts for when detector has no live data."""
    return [
        {"event": "Lakers @ Celtics",  "sport": "nba", "market": "Spread",    "from_odds": -108, "to_odds": -118, "delta": -10, "book": "DraftKings", "sharp": True,  "rlm": True,  "conviction": "HIGH",     "reason": "Line moved -10 in 3.2min | RLM: 68% public bets vs line move opposite", "age_mins": 8,  "detected_at": datetime.utcnow().isoformat()},
        {"event": "Yankees @ Red Sox", "sport": "mlb", "market": "Moneyline", "from_odds": +105, "to_odds": +120, "delta": +15, "book": "FanDuel",    "sharp": True,  "rlm": False, "conviction": "CRITICAL",  "reason": "Line moved +15 in 1.8min | Steam confirmed 3 books",                  "age_mins": 14, "detected_at": datetime.utcnow().isoformat()},
        {"event": "Chiefs @ Ravens",   "sport": "nfl", "market": "Spread",    "from_odds": -130, "to_odds": -145, "delta": -15, "book": "BetMGM",     "sharp": True,  "rlm": True,  "conviction": "CRITICAL",  "reason": "Steam: -15 in 4min | RLM: 74% public on Ravens vs sharp move",       "age_mins": 31, "detected_at": datetime.utcnow().isoformat()},
        {"event": "Dodgers @ Giants",  "sport": "mlb", "market": "Total O/U", "from_odds": -110, "to_odds": -125, "delta": -15, "book": "Caesars",    "sharp": True,  "rlm": False, "conviction": "HIGH",     "reason": "Under steam | hit cold number 8.5",                                 "age_mins": 52, "detected_at": datetime.utcnow().isoformat()},
        {"event": "Flyers @ Penguins", "sport": "nhl", "market": "Puck Line", "from_odds": +110, "to_odds": +125, "delta": +15, "book": "PointsBet",  "sharp": False, "rlm": False, "conviction": "MEDIUM",   "reason": "Moderate line drift",                                               "age_mins": 87, "detected_at": datetime.utcnow().isoformat()},
    ]


# ── Betfair Exchange endpoints ─────────────────────────────────────────────

def _get_betfair_client() -> "BetfairClient":
    from data.feeds.betfair import BetfairClient
    client = BetfairClient()
    if not client.is_configured():
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail="Betfair credentials not configured. Set BETFAIR_USERNAME, BETFAIR_PASSWORD, BETFAIR_APP_KEY in .env",
        )
    # Use existing session token if present; otherwise login
    if not client.session_token:
        client.login()
    return client


@app.get("/betfair/balance")
async def betfair_balance():
    """Betfair account available funds."""
    client = _get_betfair_client()
    return client.get_balance()


@app.get("/betfair/bets")
async def betfair_bets():
    """Currently open / unmatched bets on Betfair."""
    client = _get_betfair_client()
    orders = client.list_current_orders()
    return {
        "open_bets":  len(orders),
        "orders":     orders,
    }


@app.get("/betfair/pnl")
async def betfair_pnl():
    """Settled bet P&L summary from Betfair."""
    from agents.betfair_executor import get_pnl_summary
    client = _get_betfair_client()
    return get_pnl_summary(client)


@app.post("/betfair/place")
async def betfair_place(req: BetfairPlaceRequest):
    """
    Place a single bet on Betfair Exchange.
    dry_run=true (default) simulates the bet without spending money.
    Set dry_run=false to place a real bet.
    """
    from agents.betfair_executor import execute_pick
    client = _get_betfair_client()
    bankroll = req.stake * 50 if req.stake else float(os.getenv("BANKROLL_TOTAL", "10000"))
    pick = {
        "sport":          req.sport,
        "team":           req.team,
        "opponent":       req.opponent,
        "american_odds":  req.american_odds,
        "edge_pct":       req.edge_pct,
        "kelly_fraction": req.kelly_fraction,
    }
    result = execute_pick(client, pick, bankroll, dry_run=req.dry_run)
    return result


@app.post("/betfair/auto")
async def betfair_auto(req: BetfairAutoRequest):
    """
    Auto-execute today's value picks on Betfair Exchange.
    dry_run=true (default) shows what WOULD be bet without placing anything.
    Set dry_run=false to place real bets for all picks meeting min_edge.
    """
    from agents.betfair_executor import auto_execute_picks
    from agents.orchestrator import run_daily_picks

    client   = _get_betfair_client()
    bankroll = req.bankroll or float(os.getenv("BANKROLL_TOTAL", "10000"))

    # Get today's picks from the orchestrator
    try:
        daily = await run_daily_picks()
        picks = daily.get("top_picks", [])
    except Exception:
        picks = []

    return auto_execute_picks(
        client   = client,
        picks    = picks,
        bankroll = bankroll,
        min_edge = req.min_edge,
        dry_run  = req.dry_run,
    )


# ── Kalshi Exchange endpoints (US-legal, CFTC-regulated) ─────────────────────

@app.get("/kalshi/balance")
async def kalshi_balance():
    """Kalshi account available balance in USD."""
    from data.feeds.kalshi import get_balance
    balance = await get_balance()
    return {"platform": "Kalshi", "available_usd": balance, "note": "CFTC-regulated, US legal all 50 states"}


@app.get("/kalshi/markets")
async def kalshi_markets_today():
    """Today's open Kalshi sports markets with yes/no prices."""
    from data.feeds.kalshi import get_sports_markets_today
    markets = await get_sports_markets_today()
    return {
        "generated_at": datetime.now().isoformat(),
        "open_markets":  len(markets),
        "markets":       markets,
    }


@app.get("/kalshi/orders")
async def kalshi_orders():
    """Open Kalshi orders."""
    from data.feeds.kalshi import get_orders
    orders = await get_orders(status="resting")
    return {"open_orders": len(orders), "orders": orders}


@app.get("/kalshi/pnl")
async def kalshi_pnl():
    """Settled Kalshi P&L summary."""
    from agents.kalshi_executor import get_pnl_summary
    return await get_pnl_summary()


@app.post("/kalshi/place")
async def kalshi_place(req: KalshiPlaceRequest):
    """
    Place a single order on Kalshi.
    dry_run=true (default) simulates without spending money.
    Set dry_run=false to place a real order.
    """
    from agents.kalshi_executor import execute_pick
    bankroll = float(os.getenv("BANKROLL_TOTAL", "10000"))
    pick = {
        "sport":          req.sport,
        "team":           req.team,
        "our_prob":       req.our_prob,
        "edge_pct":       req.edge_pct,
        "kelly_fraction": req.kelly_fraction,
    }
    return await execute_pick(pick, bankroll, dry_run=req.dry_run)


@app.post("/kalshi/auto")
async def kalshi_auto(req: KalshiAutoRequest):
    """
    Auto-execute all of today's value picks on Kalshi.
    dry_run=true (default) shows what WOULD be ordered without placing anything.
    Set dry_run=false to place real orders for all picks meeting min_edge.
    """
    from agents.kalshi_executor import auto_execute_picks
    from agents.orchestrator import run_daily_picks

    bankroll = req.bankroll or float(os.getenv("BANKROLL_TOTAL", "10000"))

    try:
        daily = await run_daily_picks()
        picks = daily.get("top_picks", [])
    except Exception:
        picks = []

    return await auto_execute_picks(
        picks    = picks,
        bankroll = bankroll,
        min_edge = req.min_edge,
        dry_run  = req.dry_run,
    )


# ── MCP Tool Manifest ──────────────────────────────────────────────────────

@app.get("/mcp/tools")
def mcp_tools():
    """Return MCP tool manifest for AI agent discovery."""
    return {
        "protocol": "MCP/1.0",
        "agent": "KALISHI EDGE",
        "version": "2.0.0",
        "capabilities": ["quantitative", "ai_brain", "rag", "steam_intelligence", "streaming"],
        "tools": [
            # ── Quantitative ──
            {"name": "kelly_criterion",       "endpoint": "/kelly",                  "method": "POST", "category": "quant",         "description": "Optimal Kelly bet sizing"},
            {"name": "expected_value",         "endpoint": "/ev",                     "method": "POST", "category": "quant",         "description": "Expected value calculation"},
            {"name": "arbitrage_finder",       "endpoint": "/arbitrage",              "method": "POST", "category": "quant",         "description": "Cross-book arbitrage finder"},
            {"name": "no_vig_probability",     "endpoint": "/no-vig",                 "method": "GET",  "category": "quant",         "description": "No-vig true market probability"},
            {"name": "profit_machine",         "endpoint": "/profit-machine",         "method": "POST", "category": "quant",         "description": "Profit Machine Protocol 2.0"},
            {"name": "acts_of_god",            "endpoint": "/acts-of-god",            "method": "POST", "category": "quant",         "description": "Exogenous factor adjustments"},
            # ── Simulations ──
            {"name": "simulate_mlb",           "endpoint": "/simulate/mlb",           "method": "POST", "category": "simulation",    "description": "MLB Monte Carlo (50k sims, sabermetrics)"},
            {"name": "simulate_nba",           "endpoint": "/simulate/nba",           "method": "POST", "category": "simulation",    "description": "NBA Monte Carlo (pace, ratings, B2B)"},
            {"name": "simulate_nfl",           "endpoint": "/simulate/nfl",           "method": "POST", "category": "simulation",    "description": "NFL Monte Carlo (DVOA, EPA, weather)"},
            {"name": "simulate_ncaa",          "endpoint": "/simulate/ncaa",          "method": "POST", "category": "simulation",    "description": "NCAA tournament game sim (KenPom + seed history)"},
            # ── Player Props ──
            {"name": "props_nba",              "endpoint": "/props/nba",              "method": "POST", "category": "props",         "description": "NBA player prop: points/reb/ast/3pm/pra/blk/stl"},
            {"name": "props_nfl",              "endpoint": "/props/nfl",              "method": "POST", "category": "props",         "description": "NFL player prop: pass/rush/rec yards, TDs"},
            {"name": "props_mlb",              "endpoint": "/props/mlb",              "method": "POST", "category": "props",         "description": "MLB player prop: hits/TB/Ks/RBI/HR"},
            {"name": "props_nhl",              "endpoint": "/props/nhl",              "method": "POST", "category": "props",         "description": "NHL player prop: shots/goals/assists/points"},
            # ── Bankroll ──
            {"name": "get_bankroll",           "endpoint": "/bankroll",               "method": "GET",  "category": "bankroll",      "description": "Live bankroll state + stats"},
            {"name": "bankroll_history",       "endpoint": "/bankroll/history",       "method": "GET",  "category": "bankroll",      "description": "Daily equity curve"},
            {"name": "place_bet",              "endpoint": "/bets",                   "method": "POST", "category": "bankroll",      "description": "Record a bet"},
            # ── Picks ──
            {"name": "todays_picks",           "endpoint": "/picks/today",            "method": "GET",  "category": "picks",         "description": "AI + model generated picks (all sports)"},
            {"name": "college_picks",          "endpoint": "/picks/college",          "method": "GET",  "category": "picks",         "description": "NCAAB March Madness / Finals picks (KenPom model)"},
            {"name": "props_picks",            "endpoint": "/picks/props",            "method": "GET",  "category": "picks",         "description": "Player prop picks — NBA, NFL, MLB, NHL"},
            {"name": "middles_finder",         "endpoint": "/picks/middles",          "method": "GET",  "category": "picks",         "description": "Middle window opportunities"},
            # ── Analytics ──
            {"name": "analytics_performance",  "endpoint": "/analytics/performance",  "method": "GET",  "category": "analytics",     "description": "CLV + ROI + edge-bucket attribution"},
            # ── Line Shopping ──
            {"name": "line_shop",              "endpoint": "/lines/best",             "method": "GET",  "category": "lines",         "description": "Best available odds across all books"},
            {"name": "sharp_moves",            "endpoint": "/lines/movement",         "method": "GET",  "category": "lines",         "description": "Sharp line movement feed"},
            # ── AI Brain ──
            {"name": "ai_chat",                "endpoint": "/ai/chat",                "method": "POST", "category": "ai",            "description": "GPT-4o conversational analysis with RAG"},
            {"name": "ai_chat_stream",         "endpoint": "/ws/ai",                  "method": "WS",   "category": "ai",            "description": "Streaming AI chat WebSocket"},
            {"name": "ai_analyze_pick",        "endpoint": "/ai/analyze-pick",        "method": "POST", "category": "ai",            "description": "Structured AI pick analysis: conviction + reasoning"},
            {"name": "ai_daily_briefing",      "endpoint": "/ai/briefing",            "method": "GET",  "category": "ai",            "description": "Full AI-powered daily briefing"},
            {"name": "ai_consensus",           "endpoint": "/ai/consensus",           "method": "POST", "category": "ai",            "description": "Multi-signal consensus analysis"},
            {"name": "ai_status",              "endpoint": "/ai/status",              "method": "GET",  "category": "ai",            "description": "AI subsystems health check"},
            # ── Intelligence ──
            {"name": "steam_alerts",           "endpoint": "/intelligence/steam",     "method": "GET",  "category": "intelligence",  "description": "Real-time steam + RLM alerts"},
            {"name": "feed_line",              "endpoint": "/intelligence/feed",      "method": "POST", "category": "intelligence",  "description": "Feed live line for steam detection"},
            # ── RAG ──
            {"name": "rag_search",             "endpoint": "/rag/search",             "method": "POST", "category": "rag",           "description": "Semantic search over knowledge base"},
            {"name": "rag_stats",              "endpoint": "/rag/stats",              "method": "GET",  "category": "rag",           "description": "Vector store collection stats"},
            # ── Betfair Exchange ──
            {"name": "betfair_balance",         "endpoint": "/betfair/balance",         "method": "GET",  "category": "betfair",       "description": "Betfair account balance"},
            {"name": "betfair_bets",            "endpoint": "/betfair/bets",            "method": "GET",  "category": "betfair",       "description": "Current open bets on Betfair"},
            {"name": "betfair_pnl",             "endpoint": "/betfair/pnl",             "method": "GET",  "category": "betfair",       "description": "Settled bet P&L and ROI"},
            {"name": "betfair_place",           "endpoint": "/betfair/place",           "method": "POST", "category": "betfair",       "description": "Place a single bet on Betfair Exchange"},
            {"name": "betfair_auto",            "endpoint": "/betfair/auto",            "method": "POST", "category": "betfair",       "description": "Auto-execute today's value picks on Betfair"},
            # ── Kalshi Exchange (US-legal) ──
            {"name": "kalshi_balance",          "endpoint": "/kalshi/balance",          "method": "GET",  "category": "kalshi",        "description": "Kalshi account balance (CFTC-regulated, all 50 US states)"},
            {"name": "kalshi_markets",          "endpoint": "/kalshi/markets",          "method": "GET",  "category": "kalshi",        "description": "Today's open Kalshi sports markets with yes/no prices"},
            {"name": "kalshi_orders",           "endpoint": "/kalshi/orders",           "method": "GET",  "category": "kalshi",        "description": "Open Kalshi orders"},
            {"name": "kalshi_pnl",              "endpoint": "/kalshi/pnl",              "method": "GET",  "category": "kalshi",        "description": "Settled Kalshi P&L summary"},
            {"name": "kalshi_place",            "endpoint": "/kalshi/place",            "method": "POST", "category": "kalshi",        "description": "Place a single prediction contract on Kalshi"},
            {"name": "kalshi_auto",             "endpoint": "/kalshi/auto",             "method": "POST", "category": "kalshi",        "description": "Auto-execute today's value picks on Kalshi Exchange"},
        ]
    }


# ── Research & Learning endpoints ─────────────────────────────────────────

class ScanRequest(BaseModel):
    categories: Optional[List[str]] = None   # default: all
    min_edge:   float = 0.05
    top_n:      int   = 20

class ExecuteRequest(BaseModel):
    ticker:     str
    side:       str = "yes"
    dry_run:    bool = True

@app.post("/scan/all")
async def scan_all_markets(req: ScanRequest):
    """Scan ALL Kalshi markets for opportunities. Returns ranked list by edge."""
    try:
        from research.market_scanner import scan_all
        opps = await scan_all(
            categories=req.categories,
            min_edge=req.min_edge,
            top_n=req.top_n,
        )
        return {"count": len(opps), "opportunities": opps}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/scan/{category}")
async def scan_category_endpoint(category: str, min_edge: float = 0.05):
    """Scan a single Kalshi category: crypto | econ | political | weather | sports | misc"""
    try:
        from research.market_scanner import scan_category
        opps = await scan_category(category=category, min_edge=min_edge)
        return {"category": category, "count": len(opps), "opportunities": opps}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/research/report")
def get_research_report():
    """Latest autonomous research report (study, learn, act summary)."""
    try:
        from agents.research_agent import get_latest_report
        return get_latest_report()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/research/performance")
def get_research_performance():
    """Combined performance summary: win rate, ROI, top strategies, 7-day P&L."""
    try:
        from agents.research_agent import get_performance_summary
        return get_performance_summary()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/learning/stats")
def get_learning_stats():
    """Full learning tracker stats: all bets, P&L, strategy breakdown."""
    try:
        from research.learning_tracker import get_all_stats
        return get_all_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/learning/best-strategies")
def get_learning_best_strategies(min_bets: int = 3):
    """Top strategies ranked by ROI (must have >= min_bets bets)."""
    try:
        from research.learning_tracker import get_best_strategies
        return {"strategies": get_best_strategies(min_bets=min_bets)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/learning/signal-correlations")
def get_signal_correlations_endpoint():
    """Which signals correlate most strongly with winning bets (Pearson r)."""
    try:
        from research.learning_tracker import get_signal_correlations
        return {"correlations": get_signal_correlations()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/learning/daily-pnl")
def get_daily_pnl_endpoint(days: int = 30):
    """Daily P&L history for last N days."""
    try:
        from research.learning_tracker import get_daily_pnl
        return {"days": days, "pnl": get_daily_pnl(days)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/research/settle")
async def trigger_settlement():
    """Manually trigger auto-settlement of all open bets."""
    try:
        from research.learning_tracker import auto_settle_open_bets
        settled = await auto_settle_open_bets()
        return {"settled": len(settled), "details": settled}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/research/run-cycle")
async def run_research_cycle(execute: bool = False):
    """Trigger one full research cycle (study + act). execute=true places real bets."""
    try:
        from agents.research_agent import study_cycle
        report = await study_cycle(execute=execute)
        return report
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/feed/opportunities")
async def ws_opportunities(ws: WebSocket):
    """WebSocket: streams live opportunities as they are discovered each cycle."""
    await ws.accept()
    _ws_clients.append(ws)
    try:
        while True:
            await asyncio.sleep(30)
            try:
                from research.market_scanner import scan_all
                opps = await scan_all(min_edge=0.05, top_n=10)
                await ws.send_json({"type": "opportunities", "data": opps})
            except Exception:
                pass
    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ─── Conviction / high-value scan endpoints ────────────────────────────────────

@app.get("/scan/locks")
async def scan_locks_endpoint(min_groups: int = 3):
    """
    Return current LOCK-level plays — 4+ independent evidence groups all
    agreeing on the same direction.  These are the 85-90% win-rate bets.
    """
    try:
        from research.market_scanner import scan_for_locks
        locks = await scan_for_locks(min_independent_groups=min_groups)
        return {"count": len(locks), "locks": locks}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/scan/jackpots")
async def scan_jackpots_endpoint(max_price: float = 0.20, min_ev: float = 0.15):
    """
    Return current JACKPOT plays — markets priced <= 20c where the model
    gives >= 28% probability → 5:1+ expected payout.
    """
    try:
        from research.market_scanner import scan_for_jackpots
        jackpots = await scan_for_jackpots(max_market_price=max_price, min_ev_per_dollar=min_ev)
        return {"count": len(jackpots), "jackpots": jackpots}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/scan/highest-value")
async def scan_highest_value_endpoint(top_n: int = 20):
    """
    Composite ranked list of best bets right now.
    Score = tier_bonus (LOCK=1.0, STRONG=0.4, SIGNAL=0.1)
            + jackpot_bonus (0.5 if jackpot) + ev_per_dollar.
    """
    try:
        from research.market_scanner import scan_highest_value
        plays = await scan_highest_value(top_n=top_n)
        return {"count": len(plays), "plays": plays}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/conviction/{ticker}")
async def get_conviction_endpoint(ticker: str):
    """
    Analyse the conviction level for a single market ticker.
    Returns the full ConvictionResult dict, or conviction=NOISE if no signal.
    """
    try:
        from research.conviction_engine import analyze_conviction
        import httpx
        from data.feeds.kalshi_intraday import _headers, _BASE
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_BASE}/markets/{ticker}",
                headers=_headers("GET", f"/markets/{ticker}"),
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code,
                                    detail=f"Kalshi API: {resp.text[:200]}")
            market = resp.json().get("market", {})
        result = analyze_conviction(market, {})
        if result is None:
            return {"ticker": ticker, "conviction": "NOISE", "level": 0}
        return {
            "ticker":             result.ticker,
            "side":               result.side,
            "conviction":         result.level.name,
            "level":              result.level.value,
            "is_jackpot":         result.is_jackpot,
            "independent_groups": result.independent_groups,
            "strategy_count":     result.strategy_count,
            "avg_edge_pct":       round(result.avg_edge_pct * 100, 2),
            "avg_confidence":     round(result.avg_confidence * 100, 1),
            "ev_per_dollar":      round(result.ev_per_dollar, 3),
            "best_reason":        result.best_reason,
            "strategies":         result.strategies,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    port = int(os.getenv("MCP_PORT", "8420"))
    print(f"🎯 KALISHI EDGE MCP Server starting on port {port}")
    uvicorn.run(
        "mcp.server:app", host="0.0.0.0", port=port,
        reload=True, reload_dirs=["mcp", "engine", "agents", "data", "research"],
    )
