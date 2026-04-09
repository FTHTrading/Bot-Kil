# Kalishi Edge — Trading Dashboard

A live trading dashboard for the Kalishi autonomous Kalshi prediction-market agent.

## Stack

| Layer | Tech |
|-------|------|
| Frontend | Next.js 15 + Tailwind + Framer Motion |
| Backend API | FastAPI (MCP server, port 8420) |
| AI Briefing | Cloudflare Workers AI (llama-3-8b-instruct) |
| Deployment | Cloudflare Pages + Workers |

## Running locally

```bash
# Start MCP API server
python -m uvicorn mcp.server:app --port 8420 --reload

# Start dashboard
cd dashboard
npm run dev   # http://localhost:3420
```

## Deploy to Cloudflare

```powershell
.\deploy.ps1
```

This will:
1. Build the Next.js app (`dashboard/out/`)
2. Deploy to Cloudflare Pages as `kalishi-edge-dashboard`
3. Deploy the Workers AI briefing endpoint (`workers/ai-briefing/`)

## Live API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /live/status` | Agent alive, daily spend, cooldowns |
| `GET /live/bets` | Recent bets with edge %, CLV |
| `GET /live/sessions` | Autonomous session summaries |
| `GET /live/pnl` | Performance + daily P&L |
| `POST /briefing` (Worker) | Workers AI trading briefing |

## Environment variables

Set these in `dashboard/.env.local` for local dev:

```env
NEXT_PUBLIC_MCP_API_URL=http://localhost:8420
```

For production, set `NEXT_PUBLIC_MCP_API_URL` in your Cloudflare Pages project settings.
