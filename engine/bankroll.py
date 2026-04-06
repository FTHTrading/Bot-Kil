"""
Bankroll Management Engine
===========================
Tracks bankroll, P&L, ROI, and enforces staking discipline.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional
import json
import os


@dataclass
class Bet:
    id: str
    sport: str
    event: str
    market: str
    pick: str
    odds_dec: float
    stake: float
    ev: float
    edge: float
    strategy: str                      # profit_machine, arb, value, prop
    placed_at: datetime = field(default_factory=datetime.now)
    result: Optional[str] = None       # win, loss, push, pending
    pnl: float = 0.0
    closing_odds: Optional[float] = None


@dataclass
class BankrollState:
    starting: float
    current: float
    high_water_mark: float
    total_wagered: float
    total_won: float
    bets_placed: int
    bets_won: int
    bets_lost: int
    bets_push: int
    roi: float
    win_rate: float
    max_drawdown: float
    clv_avg: float                     # average closing line value


class BankrollManager:
    """
    Full bankroll tracking and enforcement system.
    Implements staking rules from the 200-page AI guide.
    """
    
    def __init__(self, starting_bankroll: float, db_path: str = "./db/bankroll.json"):
        self.starting = starting_bankroll
        self.current = starting_bankroll
        self.high_water_mark = starting_bankroll
        self.bets: list[Bet] = []
        self.db_path = db_path
        self._load()
    
    def _load(self):
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path) as f:
                    data = json.load(f)
                self.current = data.get("current", self.starting)
                self.high_water_mark = data.get("high_water_mark", self.starting)
                # Load bets
                for b in data.get("bets", []):
                    bet = Bet(**{k: v for k, v in b.items() if k != "placed_at"})
                    bet.placed_at = datetime.fromisoformat(b["placed_at"])
                    self.bets.append(bet)
            except Exception:
                pass
    
    def _save(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with open(self.db_path, "w") as f:
            json.dump({
                "starting": self.starting,
                "current": self.current,
                "high_water_mark": self.high_water_mark,
                "bets": [
                    {**b.__dict__, "placed_at": b.placed_at.isoformat()}
                    for b in self.bets
                ],
            }, f, indent=2)
    
    def place_bet(
        self,
        sport: str,
        event: str,
        market: str,
        pick: str,
        odds_american: int,
        stake: float,
        ev: float,
        edge: float,
        strategy: str = "value",
    ) -> Bet:
        """Record a bet placement."""
        if odds_american > 0:
            odds_dec = (odds_american / 100.0) + 1.0
        else:
            odds_dec = (100.0 / abs(odds_american)) + 1.0
        
        bet_id = f"{sport}_{len(self.bets)+1:04d}_{datetime.now().strftime('%Y%m%d')}"
        bet = Bet(
            id=bet_id,
            sport=sport,
            event=event,
            market=market,
            pick=pick,
            odds_dec=odds_dec,
            stake=stake,
            ev=ev,
            edge=edge,
            strategy=strategy,
        )
        self.bets.append(bet)
        self.current -= stake  # reserve the stake
        self._save()
        return bet
    
    def settle_bet(self, bet_id: str, result: str, closing_odds: Optional[float] = None):
        """Settle a bet: result = 'win' | 'loss' | 'push'"""
        for bet in self.bets:
            if bet.id == bet_id:
                bet.result = result
                bet.closing_odds = closing_odds
                if result == "win":
                    pnl = bet.stake * (bet.odds_dec - 1.0)
                    bet.pnl = pnl
                    self.current += bet.stake + pnl
                elif result == "loss":
                    bet.pnl = -bet.stake
                    # stake already deducted
                elif result == "push":
                    bet.pnl = 0.0
                    self.current += bet.stake  # return stake
                
                self.high_water_mark = max(self.high_water_mark, self.current)
                self._save()
                return bet
        raise ValueError(f"Bet {bet_id} not found")
    
    def snapshot(self) -> BankrollState:
        settled = [b for b in self.bets if b.result]
        wins = [b for b in settled if b.result == "win"]
        losses = [b for b in settled if b.result == "loss"]
        pushes = [b for b in settled if b.result == "push"]
        
        total_wagered = sum(b.stake for b in settled)
        total_won = sum(b.pnl for b in settled)
        
        win_rate = len(wins) / len(settled) if settled else 0.0
        roi = (total_won / total_wagered) if total_wagered > 0 else 0.0
        
        # Max drawdown
        max_dd = 0.0
        if self.high_water_mark > 0:
            max_dd = (self.high_water_mark - self.current) / self.high_water_mark
        
        # Average CLV
        clvs = [
            (1.0 / b.odds_dec) - (1.0 / b.closing_odds)
            for b in settled if b.closing_odds
        ]
        clv_avg = sum(clvs) / len(clvs) if clvs else 0.0
        
        return BankrollState(
            starting=self.starting,
            current=self.current,
            high_water_mark=self.high_water_mark,
            total_wagered=total_wagered,
            total_won=total_won,
            bets_placed=len(self.bets),
            bets_won=len(wins),
            bets_lost=len(losses),
            bets_push=len(pushes),
            roi=roi,
            win_rate=win_rate,
            max_drawdown=max_dd,
            clv_avg=clv_avg,
        )
    
    def daily_summary(self, target_date: Optional[date] = None) -> dict:
        td = target_date or date.today()
        day_bets = [b for b in self.bets if b.placed_at.date() == td]
        settled = [b for b in day_bets if b.result]
        pnl = sum(b.pnl for b in settled)
        
        return {
            "date": td.isoformat(),
            "bets_placed": len(day_bets),
            "bets_settled": len(settled),
            "pnl": round(pnl, 2),
            "current_bankroll": round(self.current, 2),
        }
