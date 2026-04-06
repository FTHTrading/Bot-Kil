"""
strategy_library.py — Algorithm library for every Kalshi market type
=====================================================================
Each strategy is a class with two public methods:

    is_applicable(market: dict) -> bool
        Can this strategy score this type of market?

    score(market: dict, context: dict) -> dict | None
        Returns a scoring result dict or None if not confident:
        {
          "strategy":   str,
          "side":       "yes" | "no",
          "edge_pct":   float,          # our_prob - market_prob (signed)
          "our_prob":   float,          # modelled win probability
          "confidence": float,          # 0..1 — how sure are we?
          "signals":    dict,           # all raw signal values (logged to DB)
          "reason":     str,            # human-readable explanation
        }

Strategies implemented:
  1.  CryptoMomentum       — 5m/15m/1h momentum for BTC/ETH/SOL etc.
  2.  CryptoVolMispricing  — realized vol vs implied vol pricing
  3.  TimedecayExploit     — YES decays to 0/1 as settlement nears
  4.  MeanReversionFade    — fade extreme prices toward 50c
  5.  OpenInterestSignal   — large OI shifts reveal informed money
  6.  CrossTimeframeArb    — inconsistencies across 15m / 1h / 4h windows
  7.  EconConsensus        — Bloomberg consensus vs Kalshi price
  8.  FedWatchArb          — CME FedWatch vs Fed-rate Kalshi markets
  9.  PollingArbitrage     — FiveThirtyEight-style poll vs political market
  10. WeatherForecast      — NOAA official forecast vs weather market
  11. CalendarEffect       — day-of-week / month-end seasonal patterns
  12. VolumeBreakout       — sudden volume spike signals momentum
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Optional


# ─── Gaussian CDF (no scipy) ─────────────────────────────────────────────────

def _ncdf(x: float) -> float:
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    p = (0.319381530 * t - 0.356563782 * t**2 + 1.781477937 * t**3
         - 1.821255978 * t**4 + 1.330274429 * t**5)
    cdf = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x**2) * p
    return cdf if x >= 0 else 1.0 - cdf


# ─── Base class ───────────────────────────────────────────────────────────────

class BaseStrategy:
    name: str = "base"
    min_confidence: float = 0.40     # don't surface scores below this
    min_edge_pct: float = 0.04       # 4% minimum edge

    def is_applicable(self, market: dict) -> bool:
        return False

    def score(self, market: dict, context: dict) -> Optional[dict]:
        return None

    def _result(self, side: str, our_prob: float, market: dict, signals: dict, reason: str) -> Optional[dict]:
        market_prob = market.get("yes_ask", market.get("yes_price", 50) / 100)
        if side == "no":
            market_prob = 1.0 - market_prob
        edge = our_prob - market_prob
        if abs(edge) < self.min_edge_pct:
            return None
        confidence = min(1.0, abs(edge) * 3)  # scale to 0-1 roughly
        if confidence < self.min_confidence:
            return None
        return {
            "strategy": self.name,
            "side": side,
            "edge_pct": round(edge, 4),
            "our_prob": round(our_prob, 4),
            "market_prob": round(market_prob, 4),
            "confidence": round(confidence, 3),
            "signals": signals,
            "reason": reason,
        }


# ─── 1. Crypto Momentum ───────────────────────────────────────────────────────

class CryptoMomentumStrategy(BaseStrategy):
    """
    Uses 5-minute and 15-minute price momentum plus realized volatility
    to estimate the probability that price closes above the floor_strike.

    Strong upward momentum + price already above strike → HIGH YES probability.
    Strong downward momentum + price below strike → HIGH NO probability.
    """
    name = "crypto_momentum"

    ASSETS = {"BTC", "ETH", "SOL", "DOGE", "XRP", "BNB", "AVAX", "LINK"}

    def is_applicable(self, market: dict) -> bool:
        return market.get("asset", "").upper() in self.ASSETS and "floor_strike" in market

    def score(self, market: dict, context: dict) -> Optional[dict]:
        asset = market["asset"].upper()
        momentum = context.get("momentum", {}).get(asset, {})
        if not momentum:
            return None

        current = float(momentum.get("current", 0))
        strike = float(market.get("floor_strike", current))
        mom_5m = float(momentum.get("mom_5m", 0))
        mom_15m = float(momentum.get("mom_15m", 0))
        realized_vol = float(momentum.get("realized_vol", 0.001))
        minutes_remaining = float(market.get("minutes_remaining", 7.5))

        if current <= 0 or strike <= 0 or realized_vol <= 0:
            return None

        # Position signal: gap from strike expressed in vol units
        gap_pct = (current - strike) / strike
        vol_per_min = realized_vol / math.sqrt(15)  # scale 15-min vol to per-minute

        # Momentum signal: blended 5m/15m
        momentum_signal = 0.6 * mom_5m + 0.4 * mom_15m

        # Time-weighted blend: early = momentum dominant, late = position dominant
        time_weight = min(1.0, (15 - minutes_remaining) / 12)  # 0 at start, 1 at 3min left
        position_prob = _ncdf(gap_pct / (vol_per_min * max(minutes_remaining, 0.5) ** 0.5))
        momentum_prob = 0.5 + (momentum_signal / (realized_vol * 2))
        momentum_prob = max(0.05, min(0.95, momentum_prob))

        our_prob = (1 - time_weight) * momentum_prob + time_weight * position_prob
        our_prob = max(0.05, min(0.95, our_prob))

        side = "yes" if our_prob > market.get("yes_ask", 0.5) else "no"
        if side == "no":
            our_prob = 1.0 - our_prob  # our prob of NO winning

        signals = {
            "gap_pct": round(gap_pct, 6),
            "mom_5m": round(mom_5m, 6),
            "mom_15m": round(mom_15m, 6),
            "realized_vol": round(realized_vol, 6),
            "time_weight": round(time_weight, 3),
            "position_prob": round(position_prob, 4),
            "momentum_prob": round(momentum_prob, 4),
            "minutes_remaining": minutes_remaining,
        }
        reason = (
            f"{asset} {'+' if gap_pct > 0 else ''}{gap_pct*100:.3f}% vs strike, "
            f"mom={momentum_signal*100:+.3f}%, {minutes_remaining:.1f}min left"
        )
        return self._result(side, our_prob, market, signals, reason)


# ─── 2. Crypto Vol-Mispricing ────────────────────────────────────────────────

class CryptoVolMispricingStrategy(BaseStrategy):
    """
    Markets price binary options using implied vol.
    When realized vol >> implied vol (the market is pricing too cheaply),
    buy the side closest to the money (highest time value).
    When realized vol << implied vol, fade extreme prices.
    """
    name = "crypto_vol_misprice"

    # Typical daily vol for Kalshi binary pricing
    _IMPLIED_ANNUAL = {"BTC": 0.65, "ETH": 0.80, "SOL": 1.0, "DOGE": 1.2}

    def is_applicable(self, market: dict) -> bool:
        return (market.get("asset", "").upper() in self._IMPLIED_ANNUAL
                and "floor_strike" in market)

    def score(self, market: dict, context: dict) -> Optional[dict]:
        asset = market["asset"].upper()
        momentum = context.get("momentum", {}).get(asset, {})
        if not momentum:
            return None

        realized_vol_5m = float(momentum.get("realized_vol", 0.0))
        if realized_vol_5m <= 0:
            return None

        # Annualise the 5-min realized vol
        realized_annual = realized_vol_5m * math.sqrt(252 * 24 * 12)
        implied_annual = self._IMPLIED_ANNUAL.get(asset, 0.80)
        vol_ratio = realized_annual / implied_annual

        current = float(momentum.get("current", 0))
        strike = float(market.get("floor_strike", current))
        minutes_remaining = float(market.get("minutes_remaining", 7.5))

        if current <= 0 or strike <= 0:
            return None

        # Price the binary option with realized vol
        t = minutes_remaining / (252 * 24 * 60)
        sigma = realized_vol_5m * math.sqrt(minutes_remaining)
        gap_pct = (current - strike) / strike
        if sigma > 0:
            d = (gap_pct - 0.5 * realized_vol_5m**2 * minutes_remaining) / sigma
            prob_yes_realized = _ncdf(d)
        else:
            prob_yes_realized = 1.0 if current > strike else 0.0

        yes_ask = market.get("yes_ask", 0.5)

        # Edge = difference between realized-vol priced and market-implied
        edge = prob_yes_realized - yes_ask
        side = "yes" if edge > 0 else "no"
        our_prob = prob_yes_realized if side == "yes" else (1 - prob_yes_realized)

        signals = {
            "realized_annual_vol": round(realized_annual, 4),
            "implied_annual_vol": round(implied_annual, 4),
            "vol_ratio": round(vol_ratio, 4),
            "prob_yes_realized": round(prob_yes_realized, 4),
            "yes_ask": round(yes_ask, 4),
        }
        reason = (
            f"Realized vol {realized_annual*100:.1f}% vs implied {implied_annual*100:.1f}% "
            f"→ binary mispriced by {abs(edge)*100:.1f}%"
        )
        return self._result(side, our_prob, market, signals, reason)


# ─── 3. Time Decay Exploit ────────────────────────────────────────────────────

class TimedecayExploitStrategy(BaseStrategy):
    """
    As a prediction market nears settlement, the price MUST converge to 0 or 100.
    Markets often lag this convergence.

    If:  price is clearly in-the-money (e.g., current ≥ strike by 0.5%)
         AND ≤ 4 minutes remain
    →  YES should be priced 90%+.  Buy it if priced < 80%.

    Converse for out-of-the-money NO side.
    """
    name = "timedecay_exploit"

    def is_applicable(self, market: dict) -> bool:
        return "floor_strike" in market and "minutes_remaining" in market

    def score(self, market: dict, context: dict) -> Optional[dict]:
        minutes_remaining = float(market.get("minutes_remaining", 15))
        if minutes_remaining > 5:
            return None  # only relevant in final 5 minutes

        yes_ask = market.get("yes_ask", 0.5)
        asset = market.get("asset", "").upper()
        momentum = context.get("momentum", {}).get(asset, {})
        current = float(momentum.get("current", 0)) if momentum else float(market.get("current_price", 0))
        strike = float(market.get("floor_strike", current))

        if current <= 0 or strike <= 0:
            return None

        gap_pct = (current - strike) / strike

        # Realized vol to estimate remaining uncertainty
        realized_vol = float(momentum.get("realized_vol", 0.001)) if momentum else 0.002
        remaining_uncertainty = realized_vol * math.sqrt(minutes_remaining)

        # Z-score of current position
        z = gap_pct / remaining_uncertainty if remaining_uncertainty > 0 else (99 * (1 if gap_pct > 0 else -1))
        prob_yes = _ncdf(z)

        # Only act when very confident
        if abs(z) < 1.5:
            return None

        side = "yes" if gap_pct > 0 else "no"
        our_prob = prob_yes if side == "yes" else (1 - prob_yes)
        market_prob = yes_ask if side == "yes" else (1 - yes_ask)
        edge = our_prob - market_prob

        if edge < 0.08:  # require 8% edge for late-stage bets
            return None

        signals = {
            "minutes_remaining": minutes_remaining,
            "gap_pct": round(gap_pct, 6),
            "z_score": round(z, 3),
            "remaining_uncertainty": round(remaining_uncertainty, 6),
            "prob_yes": round(prob_yes, 4),
        }
        reason = (
            f"{minutes_remaining:.1f}min left, price {'+' if gap_pct>0 else ''}{gap_pct*100:.3f}% "
            f"from strike (z={z:.1f}), convergence play"
        )
        return self._result(side, our_prob, market, signals, reason)


# ─── 4. Mean Reversion Fade ──────────────────────────────────────────────────

class MeanReversionFadeStrategy(BaseStrategy):
    """
    When a YES contract is priced at extreme levels (< 12c or > 88c)
    WITHOUT fundamental justification, markets tend to revert.

    This is most powerful for markets with many hours remaining where price
    has moved to an extreme on thin volume.
    """
    name = "mean_reversion_fade"

    def is_applicable(self, market: dict) -> bool:
        return (market.get("minutes_remaining", 0) > 60
                or market.get("hours_to_expiry", 0) > 1)

    def score(self, market: dict, context: dict) -> Optional[dict]:
        yes_ask = market.get("yes_ask", 0.5)
        hours = float(market.get("hours_to_expiry", market.get("minutes_remaining", 30) / 60))
        oi = market.get("open_interest", 0)

        # Only on thin-OI extremes
        if oi > 5000:
            return None  # high-liquidity markets are efficiently priced
        if 0.15 <= yes_ask <= 0.85:
            return None  # not extreme enough

        # The further from 50, the more we expect reversion
        # Base reversion strength = distance from 50c
        distance = abs(yes_ask - 0.5)
        # Time factor: more time = more chance to revert
        time_factor = min(1.0, hours / 24)
        reversion_strength = distance * time_factor

        if reversion_strength < 0.15:
            return None

        # Our estimate: partially revert toward 50c
        reversion_target = yes_ask + (0.5 - yes_ask) * reversion_strength
        our_prob_yes = reversion_target

        side = "yes" if our_prob_yes > yes_ask else "no"
        our_prob = our_prob_yes if side == "yes" else (1 - our_prob_yes)

        signals = {
            "yes_ask": round(yes_ask, 4),
            "open_interest": oi,
            "hours_to_expiry": round(hours, 2),
            "reversion_strength": round(reversion_strength, 4),
            "reversion_target": round(reversion_target, 4),
        }
        reason = f"Thin-OI extreme price ({yes_ask*100:.0f}c), reversion toward 50c over {hours:.1f}h"
        return self._result(side, our_prob, market, signals, reason)


# ─── 5. Open Interest Signal ─────────────────────────────────────────────────

class OpenInterestSignalStrategy(BaseStrategy):
    """
    Sudden large OI increases (>20% in last snapshot) often signal smart money.
    Direction: if OI grows and price moves up → follow YES.
               if OI grows and price moves down → follow NO.
    """
    name = "open_interest_signal"

    def is_applicable(self, market: dict) -> bool:
        return "open_interest" in market and "oi_delta_pct" in market

    def score(self, market: dict, context: dict) -> Optional[dict]:
        oi_delta = float(market.get("oi_delta_pct", 0))
        price_delta = float(market.get("price_delta_pct", 0))
        yes_ask = market.get("yes_ask", 0.5)

        if abs(oi_delta) < 0.15:  # require 15% OI change
            return None

        # OI up + price up → follow YES
        # OI up + price down → follow NO
        # OI divergence = strong signal
        if oi_delta > 0 and price_delta > 0:
            our_prob = yes_ask + abs(oi_delta) * 0.3
            side = "yes"
        elif oi_delta > 0 and price_delta < 0:
            our_prob = (1 - yes_ask) + abs(oi_delta) * 0.3
            side = "no"
        else:
            return None

        our_prob = max(0.05, min(0.95, our_prob))
        signals = {
            "oi_delta_pct": round(oi_delta, 4),
            "price_delta_pct": round(price_delta, 4),
            "yes_ask": round(yes_ask, 4),
        }
        reason = f"OI +{oi_delta*100:.1f}% with price {'up' if price_delta>0 else 'down'} {price_delta*100:.2f}%"
        return self._result(side, our_prob, market, signals, reason)


# ─── 6. Cross-Timeframe Arbitrage ────────────────────────────────────────────

class CrossTimeframeArbStrategy(BaseStrategy):
    """
    If the 1-hour market prices YES at 60% but the two remaining 30-min sub-markets
    both price YES at 70%, the 1-hour market is underpriced.

    v1 (simple): compare same-asset markets across different timeframes.
    """
    name = "cross_timeframe_arb"

    def is_applicable(self, market: dict) -> bool:
        return "timeframe_peers" in market  # scanner must populate this

    def score(self, market: dict, context: dict) -> Optional[dict]:
        peers = market.get("timeframe_peers", [])
        if len(peers) < 2:
            return None

        yes_ask = market.get("yes_ask", 0.5)
        peer_avg = sum(p.get("yes_ask", 0.5) for p in peers) / len(peers)
        gap = peer_avg - yes_ask

        if abs(gap) < 0.06:
            return None

        side = "yes" if gap > 0 else "no"
        our_prob = peer_avg if side == "yes" else (1 - peer_avg)

        signals = {
            "this_yes_ask": round(yes_ask, 4),
            "peer_avg_yes": round(peer_avg, 4),
            "peer_count": len(peers),
            "gap": round(gap, 4),
        }
        reason = f"Same-asset peers price YES at {peer_avg*100:.0f}c avg vs this market {yes_ask*100:.0f}c"
        return self._result(side, our_prob, market, signals, reason)


# ─── 7. Economic Consensus Arbitrage ─────────────────────────────────────────

class EconConsensusStrategy(BaseStrategy):
    """
    For economic data markets (CPI, NFP, GDP etc.):
    Bloomberg professional forecasters aggregate to a consensus estimate.
    When the Kalshi market price diverges significantly from the consensus,
    we have edge.

    Context key: context["econ_consensus"][series] = {"estimate": float, "std": float}
    These are populated by the market scanner from public forecaster APIs.
    """
    name = "econ_consensus"

    ECON_SERIES = {"CPI", "NFP", "PAYROLLS", "GDP", "PCE", "PPI", "RETAIL", "HOUSING", "ISM", "PMI"}

    def is_applicable(self, market: dict) -> bool:
        series = market.get("series", "").upper()
        return any(k in series for k in self.ECON_SERIES)

    def score(self, market: dict, context: dict) -> Optional[dict]:
        series = market.get("series", "").upper()
        econ = context.get("econ_consensus", {})
        consensus_key = next((k for k in self.ECON_SERIES if k in series), None)
        if not consensus_key:
            return None

        consensus = econ.get(consensus_key)
        if not consensus:
            return None

        estimate = float(consensus.get("estimate", 0))
        std = float(consensus.get("std", 0.1))
        strike = float(market.get("floor_strike", estimate))
        yes_ask = market.get("yes_ask", 0.5)

        # P(actual > strike) under consensus distribution
        z = (estimate - strike) / std if std > 0 else 0
        our_prob_yes = _ncdf(z)

        side = "yes" if our_prob_yes > yes_ask else "no"
        our_prob = our_prob_yes if side == "yes" else (1 - our_prob_yes)

        signals = {
            "consensus_estimate": estimate,
            "consensus_std": round(std, 4),
            "strike": strike,
            "prob_yes_consensus": round(our_prob_yes, 4),
            "yes_ask_market": round(yes_ask, 4),
        }
        reason = (
            f"Consensus {estimate:.3f} vs strike {strike:.3f} (z={z:.2f}): "
            f"consensus implies {our_prob_yes*100:.0f}% YES, market says {yes_ask*100:.0f}%"
        )
        return self._result(side, our_prob, market, signals, reason)


# ─── 8. FedWatch Arbitrage ────────────────────────────────────────────────────

class FedWatchArbStrategy(BaseStrategy):
    """
    CME FedWatch Tool publishes real-time probabilities for Fed rate decisions.
    Kalshi runs FOMC rate markets.  When the two diverge by > 5%, edge exists.

    Context key: context["fedwatch"] = {outcome: probability}
    e.g. {"hold": 0.78, "cut_25": 0.18, "cut_50": 0.04}
    """
    name = "fedwatch_arb"

    def is_applicable(self, market: dict) -> bool:
        return "FOMC" in market.get("series", "").upper() or "FED" in market.get("series", "").upper()

    def score(self, market: dict, context: dict) -> Optional[dict]:
        fedwatch = context.get("fedwatch", {})
        if not fedwatch:
            return None

        # Match market title keywords to FedWatch outcomes
        title = market.get("title", "").lower()
        market_outcome = None
        if "hold" in title or "unchanged" in title or "no change" in title:
            market_outcome = "hold"
        elif "cut 25" in title or "cut by 25" in title or "-25" in title:
            market_outcome = "cut_25"
        elif "cut 50" in title or "cut by 50" in title or "-50" in title:
            market_outcome = "cut_50"
        elif "hike" in title or "increase" in title:
            market_outcome = "hike"

        if market_outcome not in fedwatch:
            return None

        fedwatch_prob = fedwatch[market_outcome]
        yes_ask = market.get("yes_ask", 0.5)
        edge = fedwatch_prob - yes_ask

        side = "yes" if edge > 0 else "no"
        our_prob = fedwatch_prob if side == "yes" else (1 - fedwatch_prob)

        signals = {
            "fedwatch_prob": round(fedwatch_prob, 4),
            "yes_ask": round(yes_ask, 4),
            "matched_outcome": market_outcome,
        }
        reason = f"FedWatch says {market_outcome} = {fedwatch_prob*100:.1f}%, Kalshi = {yes_ask*100:.1f}%"
        return self._result(side, our_prob, market, signals, reason)


# ─── 9. Polling Arbitrage ─────────────────────────────────────────────────────

class PollingArbStrategy(BaseStrategy):
    """
    For political markets (elections, approval ratings):
    Compare polling aggregates vs current Kalshi price.

    Context key: context["polls"] = {race_key: {"prob": float, "source": str, "margin": float}}
    """
    name = "polling_arb"

    def is_applicable(self, market: dict) -> bool:
        mtype = market.get("market_type", "").lower()
        return "politic" in mtype or "election" in mtype or "PRES" in market.get("series", "")

    def score(self, market: dict, context: dict) -> Optional[dict]:
        polls = context.get("polls", {})
        if not polls:
            return None

        ticker = market.get("ticker", "")
        # Find best matching poll
        poll_data = None
        for key, data in polls.items():
            if key.upper() in ticker.upper():
                poll_data = data
                break

        if not poll_data:
            return None

        poll_prob = float(poll_data.get("prob", 0.5))
        yes_ask = market.get("yes_ask", 0.5)
        margin = float(poll_data.get("margin_of_error", 3.0))  # percentage points

        # Apply uncertainty from polling error
        uncertainty = margin / 100.0 * 0.5  # convert MOE to probability uncertainty
        poll_prob_adjusted = 0.5 + (poll_prob - 0.5) * (1 - uncertainty)

        edge = poll_prob_adjusted - yes_ask
        side = "yes" if edge > 0 else "no"
        our_prob = poll_prob_adjusted if side == "yes" else (1 - poll_prob_adjusted)

        signals = {
            "poll_prob": round(poll_prob, 4),
            "poll_prob_adj": round(poll_prob_adjusted, 4),
            "poll_margin_of_error": margin,
            "yes_ask": round(yes_ask, 4),
            "poll_source": poll_data.get("source", "unknown"),
        }
        reason = (
            f"Polls: {poll_prob*100:.1f}% (±{margin}pp) vs market {yes_ask*100:.1f}%"
        )
        return self._result(side, our_prob, market, signals, reason)


# ─── 10. Weather Forecast ─────────────────────────────────────────────────────

class WeatherForecastStrategy(BaseStrategy):
    """
    For temperature/precipitation markets:
    NOAA's official NWS forecast is highly accurate.
    Compare official forecast probability vs Kalshi market price.

    Context key: context["weather"] = {location_key: {"prob_above": float, "threshold": float}}
    """
    name = "weather_forecast"

    def is_applicable(self, market: dict) -> bool:
        return "TEMP" in market.get("series", "").upper() or "WEATHER" in market.get("market_type", "").upper()

    def score(self, market: dict, context: dict) -> Optional[dict]:
        weather = context.get("weather", {})
        if not weather:
            return None

        ticker = market.get("ticker", "")
        weather_data = None
        for key, data in weather.items():
            if key.upper() in ticker.upper():
                weather_data = data
                break

        if not weather_data:
            return None

        noaa_prob = float(weather_data.get("prob_above", 0.5))
        yes_ask = market.get("yes_ask", 0.5)
        edge = noaa_prob - yes_ask

        side = "yes" if edge > 0 else "no"
        our_prob = noaa_prob if side == "yes" else (1 - noaa_prob)

        signals = {
            "noaa_prob": round(noaa_prob, 4),
            "yes_ask": round(yes_ask, 4),
            "noaa_source": weather_data.get("source", "NWS"),
        }
        reason = f"NOAA forecast {noaa_prob*100:.0f}% vs market {yes_ask*100:.0f}%"
        return self._result(side, our_prob, market, signals, reason)


# ─── 11. Calendar Effect ──────────────────────────────────────────────────────

class CalendarEffectStrategy(BaseStrategy):
    """
    Detects calendar-based mispricing:
      - BTC tends to rally Monday mornings (weekend dip reversal)
      - End-of-month window dressing often pushes SPX higher
      - Friday afternoon NO bias (weekend volatility = more downside)
    """
    name = "calendar_effect"

    _CALENDAR_EDGES = {
        # (asset, weekday [0=Mon], hour_range) → (direction, edge_boost)
        ("BTC", 0, (0, 12)): ("yes", 0.06),    # Monday morning UTC — weekend reversal
        ("ETH", 0, (0, 12)): ("yes", 0.05),
        ("BTC", 4, (12, 24)): ("no",  0.04),   # Friday afternoon — weekend risk
    }

    def is_applicable(self, market: dict) -> bool:
        return market.get("asset", "").upper() in {"BTC", "ETH", "SOL", "SPX"}

    def score(self, market: dict, context: dict) -> Optional[dict]:
        now_utc = datetime.now(timezone.utc)
        weekday = now_utc.weekday()
        hour = now_utc.hour
        asset = market.get("asset", "").upper()

        best_edge = None
        for (a, day, hr_range), (direction, edge_boost) in self._CALENDAR_EDGES.items():
            if a == asset and day == weekday and hr_range[0] <= hour < hr_range[1]:
                best_edge = (direction, edge_boost)
                break

        if not best_edge:
            return None

        direction, boost = best_edge
        yes_ask = market.get("yes_ask", 0.5)
        our_prob_yes = yes_ask + (boost if direction == "yes" else -boost)
        our_prob_yes = max(0.1, min(0.9, our_prob_yes))

        side = direction
        our_prob = our_prob_yes if side == "yes" else (1 - our_prob_yes)

        signals = {
            "weekday": weekday,
            "hour_utc": hour,
            "calendar_direction": direction,
            "calendar_edge_boost": boost,
            "yes_ask": round(yes_ask, 4),
        }
        reason = f"Calendar effect: {asset} {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][weekday]} {hour:02d}h UTC → {direction.upper()}"
        return self._result(side, our_prob, market, signals, reason)


# ─── 12. Volume Breakout ──────────────────────────────────────────────────────

class VolumeBreakoutStrategy(BaseStrategy):
    """
    When volume in a market spikes well above its rolling average,
    it signals an informed-money move.  Follow the direction of the price
    move accompanying the volume.
    """
    name = "volume_breakout"

    def is_applicable(self, market: dict) -> bool:
        return "volume_ratio" in market  # scanner fills this from market_snapshots

    def score(self, market: dict, context: dict) -> Optional[dict]:
        volume_ratio = float(market.get("volume_ratio", 1.0))
        price_direction = market.get("price_direction", 0)  # +1 / -1 / 0
        yes_ask = market.get("yes_ask", 0.5)

        if volume_ratio < 2.5 or price_direction == 0:
            return None

        side = "yes" if price_direction > 0 else "no"
        raw_edge = min(0.25, (volume_ratio - 2.0) * 0.05)
        our_prob_yes = yes_ask + (raw_edge if price_direction > 0 else -raw_edge)
        our_prob_yes = max(0.1, min(0.9, our_prob_yes))
        our_prob = our_prob_yes if side == "yes" else (1 - our_prob_yes)

        signals = {
            "volume_ratio": round(volume_ratio, 3),
            "price_direction": price_direction,
            "raw_edge_from_volume": round(raw_edge, 4),
            "yes_ask": round(yes_ask, 4),
        }
        reason = f"Volume {volume_ratio:.1f}× average with price moving {'+' if price_direction>0 else '-'}ve"
        return self._result(side, our_prob, market, signals, reason)


# ─── Strategy Registry ────────────────────────────────────────────────────────

ALL_STRATEGIES: list[BaseStrategy] = [
    CryptoMomentumStrategy(),
    CryptoVolMispricingStrategy(),
    TimedecayExploitStrategy(),
    MeanReversionFadeStrategy(),
    OpenInterestSignalStrategy(),
    CrossTimeframeArbStrategy(),
    EconConsensusStrategy(),
    FedWatchArbStrategy(),
    PollingArbStrategy(),
    WeatherForecastStrategy(),
    CalendarEffectStrategy(),
    VolumeBreakoutStrategy(),
]

_STRATEGY_MAP: dict[str, BaseStrategy] = {s.name: s for s in ALL_STRATEGIES}


def score_market(market: dict, context: dict, weights: dict[str, float] = None) -> list[dict]:
    """
    Apply all applicable strategies to a market.
    Returns a list of result dicts, sorted by weighted edge descending.
    weights: from learning_tracker.get_strategy_weights() — boosts proven strategies.
    """
    weights = weights or {}
    results = []
    for strategy in ALL_STRATEGIES:
        if not strategy.is_applicable(market):
            continue
        try:
            result = strategy.score(market, context)
        except Exception as e:
            continue
        if result is None:
            continue
        w = weights.get(strategy.name, 1.0)
        result["weighted_edge"] = round(result["edge_pct"] * w, 4)
        results.append(result)

    results.sort(key=lambda r: abs(r["weighted_edge"]), reverse=True)
    return results


def best_score(market: dict, context: dict, weights: dict[str, float] = None) -> Optional[dict]:
    """Return the single highest-weighted strategy score, or None."""
    all_scores = score_market(market, context, weights)
    return all_scores[0] if all_scores else None
