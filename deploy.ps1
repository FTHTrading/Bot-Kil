# deploy.ps1 — Deploy Kalishi Edge to Cloudflare
# Usage: .\deploy.ps1

$ErrorActionPreference = "Stop"

$CF_WORKERS_AI_TOKEN = "cfut_fvYvBwUkOknmf8sy3lwFnJRJ6l65ygqEYSl8EcvLdc0cef0c"
$CF_DNS_TOKEN        = "cfut_BuTCsrHN3M8C8ZeXLW3I9dVlCvKJ6XzBP5i85Jxp551314a6"

$env:CLOUDFLARE_API_TOKEN = $CF_WORKERS_AI_TOKEN

Write-Host "`n=== Kalishi Edge — Cloudflare Deployment ===" -ForegroundColor Cyan

# ─── 1. Build the dashboard ───────────────────────────────────────────────────
Write-Host "`n[1/3] Building dashboard..." -ForegroundColor Yellow
Push-Location dashboard
npm run build
if ($LASTEXITCODE -ne 0) { throw "Dashboard build failed" }
Pop-Location

# ─── 2. Deploy dashboard to Cloudflare Pages ─────────────────────────────────
Write-Host "`n[2/3] Deploying to Cloudflare Pages..." -ForegroundColor Yellow
Push-Location dashboard
npx wrangler pages deploy out --project-name kalishi-edge-dashboard
if ($LASTEXITCODE -ne 0) { throw "Pages deploy failed" }
Pop-Location

# ─── 3. Deploy Workers AI briefing endpoint ──────────────────────────────────
Write-Host "`n[3/3] Deploying Workers AI briefing..." -ForegroundColor Yellow
Push-Location workers\ai-briefing

if (-not (Test-Path node_modules)) {
    Write-Host "  Installing worker dependencies..."
    npm install
}

npx wrangler deploy
if ($LASTEXITCODE -ne 0) { throw "Worker deploy failed" }
Pop-Location

Write-Host "`n=== Deployment complete! ===" -ForegroundColor Green
Write-Host "  Dashboard : https://kalishi-edge-dashboard.pages.dev" -ForegroundColor White
Write-Host "  AI Worker : https://kalishi-ai-briefing.<account>.workers.dev" -ForegroundColor White
Write-Host ""
