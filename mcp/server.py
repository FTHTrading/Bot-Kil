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

# WebSocket connections for live dashboard updates
_ws_clients: list[WebSocket] = []

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


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "system": "KALISHI EDGE",
        "version": "1.0.0",
        "status": "operational",
        "tools": [
            "/kelly", "/ev", "/arbitrage", "/no-vig",
            "/simulate/mlb", "/simulate/nba", "/simulate/nfl",
            "/profit-machine", "/acts-of-god",
            "/bankroll", "/bets", "/picks/today",
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


# ── MCP Tool Manifest ──────────────────────────────────────────────────────

@app.get("/mcp/tools")
def mcp_tools():
    """Return MCP tool manifest for AI agent discovery."""
    return {
        "tools": [
            {"name": "kelly_criterion", "endpoint": "/kelly", "method": "POST", "description": "Optimal bet sizing"},
            {"name": "expected_value", "endpoint": "/ev", "method": "POST", "description": "EV calculation"},
            {"name": "arbitrage_finder", "endpoint": "/arbitrage", "method": "POST", "description": "Cross-book arb"},
            {"name": "no_vig_probability", "endpoint": "/no-vig", "method": "GET", "description": "True market prob"},
            {"name": "profit_machine", "endpoint": "/profit-machine", "method": "POST", "description": "PMP 2.0 allocation"},
            {"name": "acts_of_god", "endpoint": "/acts-of-god", "method": "POST", "description": "Exogenous adjustments"},
            {"name": "simulate_mlb", "endpoint": "/simulate/mlb", "method": "POST", "description": "MLB Monte Carlo"},
            {"name": "simulate_nba", "endpoint": "/simulate/nba", "method": "POST", "description": "NBA Monte Carlo"},
            {"name": "simulate_nfl", "endpoint": "/simulate/nfl", "method": "POST", "description": "NFL Monte Carlo"},
            {"name": "get_bankroll", "endpoint": "/bankroll", "method": "GET", "description": "Bankroll status"},
            {"name": "place_bet", "endpoint": "/bets", "method": "POST", "description": "Record a bet"},
            {"name": "todays_picks", "endpoint": "/picks/today", "method": "GET", "description": "AI-generated picks"},
        ]
    }


if __name__ == "__main__":
    port = int(os.getenv("MCP_PORT", "8420"))
    print(f"🎯 KALISHI EDGE MCP Server starting on port {port}")
    uvicorn.run(
        "mcp.server:app", host="0.0.0.0", port=port,
        reload=True, reload_dirs=["mcp", "engine", "agents", "data"],
    )
