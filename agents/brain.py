"""
KALISHI EDGE - AI Brain
========================
Multi-provider intelligence layer with NVIDIA NIM, local Ollama, and OpenAI fallback.
Every response is augmented with RAG context from the knowledge base,
historical bets, and live market intelligence.

Provider priority:
  1. NVIDIA NIM  (NGC API key -> Llama 3.3 70B on NVIDIA cloud)
  2. Ollama      (local RTX 5090 -> qwen2.5:7b, offline capable)
  3. OpenAI      (gpt-4o cloud fallback)

Capabilities:
  - Conversational sports betting analysis (chat)
  - Pick generation with full reasoning chains
  - Matchup breakdown with sabermetric/advanced stats
  - Real-time market context injection
  - Streaming responses for live dashboard
"""
from __future__ import annotations
import os
import json
import asyncio
from datetime import datetime
from typing import AsyncIterator, Optional
from dotenv import load_dotenv

load_dotenv()

OPENAI_KEY      = os.getenv("OPENAI_API_KEY", "")
NGC_API_KEY     = os.getenv("NGC_API_KEY", "")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
BRAIN_PROVIDER  = os.getenv("BRAIN_PROVIDER", "auto")  # nvidia | ollama | openai | auto


def _resolve_provider():
    """Returns (provider_name, base_url, api_key, model). Priority: nvidia -> ollama -> openai."""
    pref = BRAIN_PROVIDER.lower()

    if pref == "nvidia" or (pref == "auto" and NGC_API_KEY):
        return (
            "nvidia",
            "https://integrate.api.nvidia.com/v1",
            NGC_API_KEY,
            os.getenv("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct"),
        )
    if pref == "ollama" or (pref == "auto" and not NGC_API_KEY and not OPENAI_KEY):
        return (
            "ollama",
            OLLAMA_BASE_URL,
            "ollama",
            OLLAMA_MODEL,
        )
    return ("openai", "https://api.openai.com/v1", OPENAI_KEY, "gpt-4o")


try:
    from openai import AsyncOpenAI
    _PROVIDER, _BASE_URL, _API_KEY, _MODEL = _resolve_provider()
    OAI_AVAILABLE = bool(_API_KEY)
except ImportError:
    OAI_AVAILABLE = False
    _PROVIDER, _BASE_URL, _API_KEY, _MODEL = "none", "", "", ""

from rag.retriever import KalishiRetriever

SYSTEM_PROMPT = """You are KALISHI - a hyper-intelligent sports betting AI engineered to find and exploit market inefficiencies.

Your core capabilities:
* Kelly Criterion sizing -> never overbetting, always mathematically optimal
* Expected Value (EV) analysis -> only positive EV bets, minimum 3% edge
* Closing Line Value (CLV) tracking -> the gold standard for long-term edge
* Monte Carlo simulation -> 50,000+ simulations per game
* Sharp money detection -> steam moves, reverse line movement (RLM), limit reductions
* Arbitrage & middles -> guaranteed profit when books disagree
* Advanced sabermetrics: FIP, wRC+, DVOA, EPA, pace, net rating

Behavioral rules:
* Always show the math - edge %, EV, Kelly fraction, recommended stake
* Be direct, be fast, be sharp
* Flag steam moves and RLM as top priority intelligence
* Never recommend a bet without positive EV and minimum 3% edge
* Warn about injury/weather impacts on your analysis

When analyzing picks, structure your output:
1. THE PLAY: [Pick] [Market] [Odds] @ [Book]
2. THE MATH: Edge X%, EV +X%, Kelly X% -> $X stake
3. THE EDGE: Why this bet has value over the market
4. THE RISK: What kills this bet (injuries, weather, line steam against)
5. CONVICTION: LOW / MEDIUM / HIGH / STRONG BUY

You have access to real-time context injected below each query. Use it."""


class AIBrain:
    """
    Master LLM intelligence layer for KALISHI EDGE.
    Supports NVIDIA NIM, Ollama (local GPU), and OpenAI with automatic provider selection.
    """

    def __init__(self):
        self._retriever = KalishiRetriever()
        self._provider = _PROVIDER
        self._model = _MODEL
        self._client = AsyncOpenAI(base_url=_BASE_URL, api_key=_API_KEY) if OAI_AVAILABLE else None
        self._history: list[dict] = []

    @property
    def available(self) -> bool:
        return OAI_AVAILABLE and bool(_API_KEY)

    @property
    def provider_info(self) -> str:
        return f"{self._provider} / {self._model}"

    def _build_messages(self, user_message: str, context: str) -> list[dict]:
        """Assemble message array with system prompt + RAG context + history."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(self._history[-20:])
        full_user = user_message
        if context:
            full_user = f"{context}\n\n---\nUSER QUERY: {user_message}"
        messages.append({"role": "user", "content": full_user})
        return messages

    async def chat(self, user_message: str, remember: bool = True) -> str:
        if not self.available:
            return self._fallback_response(user_message)

        context = self._retriever.retrieve_for_chat(user_message)
        messages = self._build_messages(user_message, context)

        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0.2,
                max_tokens=1500,
            )
            content = resp.choices[0].message.content or ""
        except Exception as e:
            return f"[KALISHI Brain offline: {e}]"

        if remember:
            self._history.append({"role": "user", "content": user_message})
            self._history.append({"role": "assistant", "content": content})
            self._history = self._history[-40:]

        return content

    async def stream_chat(self, user_message: str, remember: bool = True) -> AsyncIterator[str]:
        if not self.available:
            yield self._fallback_response(user_message)
            return

        context = self._retriever.retrieve_for_chat(user_message)
        messages = self._build_messages(user_message, context)
        full_response = []

        try:
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0.2,
                max_tokens=1500,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    full_response.append(delta)
                    yield delta
        except Exception as e:
            yield f"\n[KALISHI Brain error: {e}]"
            return

        if remember:
            self._history.append({"role": "user",      "content": user_message})
            self._history.append({"role": "assistant", "content": "".join(full_response)})
            self._history = self._history[-40:]

    async def analyze_pick(
        self,
        sport: str,
        event: str,
        market: str,
        edge_pct: float,
        ev_pct: float,
        our_prob: float,
        implied_prob: float,
        american_odds: int,
        stake: float,
        additional_context: Optional[dict] = None,
    ) -> dict:
        context = self._retriever.retrieve_for_pick(sport, event, market)
        similar = self._retriever.retrieve_similar_bets(sport, market, edge_pct)

        user_msg = f"""Analyze this betting opportunity:

SPORT: {sport}
EVENT: {event}
MARKET: {market}
ODDS: {american_odds:+d}
OUR PROBABILITY: {our_prob*100:.1f}%
IMPLIED PROBABILITY: {implied_prob*100:.1f}%
EDGE: {edge_pct:.2f}%
EXPECTED VALUE: +{ev_pct:.2f}%
RECOMMENDED STAKE: ${stake:.2f}

{json.dumps(additional_context, indent=2) if additional_context else ''}

{similar}

Provide a structured pick analysis as JSON with keys:
  conviction (STRONG_BUY|BUY|HOLD|PASS),
  one_line_thesis (max 20 words),
  reasoning (3-4 sentences),
  key_edge (the specific market inefficiency),
  risk_factors (list of 2-3 risks),
  sharp_signal (any steam/RLM indicators or null),
  recommended_action (PLACE_NOW|WAIT_FOR_LINE|MONITOR|SKIP)
"""
        if not self.available:
            return self._fallback_pick_analysis(edge_pct, ev_pct)

        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "assistant", "content": context or ""},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.1,
                max_tokens=800,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or "{}"
            return json.loads(raw)
        except Exception as e:
            return {"error": str(e), **self._fallback_pick_analysis(edge_pct, ev_pct)}

    async def generate_daily_briefing(self, picks: list[dict], market_summary: dict) -> str:
        if not self.available:
            return "AI Brain unavailable - connect a provider in .env to activate full AI analysis."

        context = self._retriever.retrieve(
            "daily picks strategy bankroll discipline sharp money",
            collections=["knowledge", "market_moves"],
            n_per_collection=3,
        )

        picks_text = json.dumps(picks[:10], indent=2)
        prompt = f"""Generate the KALISHI EDGE daily betting briefing.

TODAY'S PICKS (top candidates):
{picks_text}

MARKET SUMMARY:
{json.dumps(market_summary, indent=2)}

{context}

Write a sharp, concise daily briefing covering:
1. EXECUTIVE SUMMARY (2 sentences - what's the play today)
2. TOP 3 PLAYS ranked by conviction (each with 1-sentence thesis + bet sizing)
3. MARKET INTELLIGENCE (sharp moves, steam, notable line movement)
4. BANKROLL NOTE (any sizing adjustments based on recent performance)
5. AVOID LIST (overvalued favorites, public square plays to fade)

Style: Sharp, direct, no fluff. This is an institutional intelligence briefing."""

        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.3,
                max_tokens=1200,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            return f"[Briefing generation failed: {e}]"

    def clear_history(self):
        self._history.clear()

    def _fallback_response(self, query: str) -> str:
        return (
            f"[KALISHI Operating in offline mode - no AI provider configured]\n\n"
            f"Your query was: '{query}'\n\n"
            f"Set BRAIN_PROVIDER in .env (nvidia/ollama/openai) to activate full AI analysis. "
            f"All quantitative tools (Kelly, EV, Monte Carlo) remain fully functional."
        )

    def _fallback_pick_analysis(self, edge_pct: float, ev_pct: float) -> dict:
        conviction = "STRONG_BUY" if edge_pct > 8 else "BUY" if edge_pct > 5 else "HOLD"
        return {
            "conviction": conviction,
            "one_line_thesis": f"Quantitative edge {edge_pct:.1f}% above market",
            "reasoning": "AI Brain offline. Kelly/EV analysis confirms positive expected value.",
            "key_edge": f"+{ev_pct:.2f}% EV over implied probability",
            "risk_factors": ["Connect AI provider for full risk analysis"],
            "sharp_signal": None,
            "recommended_action": "PLACE_NOW" if edge_pct > 5 else "MONITOR",
        }


_brain: Optional[AIBrain] = None


def get_brain() -> AIBrain:
    global _brain
    if _brain is None:
        _brain = AIBrain()
    return _brain