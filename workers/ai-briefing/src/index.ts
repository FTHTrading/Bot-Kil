/**
 * Kalishi Edge — Cloudflare Workers AI Briefing
 * ===============================================
 * Runs on the Cloudflare edge. Uses Workers AI (llama-3-8b) to generate 
 * a natural-language trading briefing from live session data.
 *
 * Routes:
 *   POST /briefing  — generate AI briefing from payload
 *   GET  /health    — ping
 */

export interface Env {
  AI: Ai;
  OPENAI_API_KEY?: string;
  MCP_API_URL?: string;
}

const CORS = {
  'Access-Control-Allow-Origin':  '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Authorization',
};

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', ...CORS },
  });
}

function buildPrompt(data: {
  agent_alive: boolean;
  daily_spend: number;
  bets: Array<{ ticker?: string; side?: string; edge_pct?: number; status?: string }>;
  sessions: Array<{ bets_placed: number; tool_calls: number; summary?: string }>;
}): string {
  const betLines = (data.bets || []).slice(0, 6).map(b =>
    `  • ${b.ticker || 'Unknown'} ${(b.side || '').toUpperCase()} — edge ${b.edge_pct?.toFixed(1) ?? '?'}% — ${b.status || '?'}`
  ).join('\n') || '  (no bets yet)';

  const sessionSummary = (data.sessions || []).slice(0, 4).map(s =>
    `  Session: ${s.bets_placed} bets placed, ${s.tool_calls} tool calls`
  ).join('\n') || '  (no sessions yet)';

  return `You are a sharp, concise trading analyst for a Kalshi prediction market bot.
Give a 3-sentence briefing on today's performance. Be direct, no fluff.

AGENT STATUS: ${data.agent_alive ? 'ONLINE' : 'OFFLINE'}
DAILY SPEND: $${data.daily_spend.toFixed(2)}

RECENT BETS:
${betLines}

RECENT SESSIONS:
${sessionSummary}

Briefing (3 sentences max):`;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS });
    }

    // Health check
    if (url.pathname === '/health' || url.pathname === '/') {
      return json({ ok: true, service: 'kalishi-ai-briefing', ts: new Date().toISOString() });
    }

    // AI Briefing endpoint
    if (url.pathname === '/briefing' && request.method === 'POST') {
      let payload: {
        agent_alive?: boolean;
        daily_spend?: number;
        bets?: unknown[];
        sessions?: unknown[];
      } = {};

      try {
        payload = await request.json() as typeof payload;
      } catch {
        // empty payload is ok, use defaults
      }

      const promptData = {
        agent_alive:  payload.agent_alive ?? false,
        daily_spend:  payload.daily_spend ?? 0,
        bets:         (payload.bets ?? []) as Array<{ ticker?: string; side?: string; edge_pct?: number; status?: string }>,
        sessions:     (payload.sessions ?? []) as Array<{ bets_placed: number; tool_calls: number; summary?: string }>,
      };

      try {
        const result = await env.AI.run('@cf/meta/llama-3-8b-instruct', {
          messages: [
            {
              role: 'user',
              content: buildPrompt(promptData),
            },
          ],
          max_tokens: 200,
        }) as { response?: string };

        return json({
          briefing:    result.response?.trim() ?? 'Unable to generate briefing.',
          model:       '@cf/meta/llama-3-8b-instruct',
          generated_at: new Date().toISOString(),
        });
      } catch (err) {
        return json(
          { error: 'AI inference failed', detail: String(err) },
          500,
        );
      }
    }

    return json({ error: 'Not found' }, 404);
  },
};
