#!/usr/bin/env pwsh
# ─── KALISHI EDGE — Startup Script ─────────────────────────────────────────
# Starts both the MCP API server and the Next.js dashboard.
# Usage:  .\start.ps1
#         .\start.ps1 -ApiOnly
#         .\start.ps1 -DashOnly

param(
    [switch]$ApiOnly,
    [switch]$DashOnly
)

$Root = $PSScriptRoot

function Assert-Env {
    if (-not (Test-Path "$Root\.env")) {
        Write-Host "[setup] .env not found — copying from .env.example" -ForegroundColor Yellow
        Copy-Item "$Root\.env.example" "$Root\.env"
        Write-Host "[setup] Edit $Root\.env and add your ODDS_API_KEY, then re-run." -ForegroundColor Yellow
    }
}

function Assert-DB {
    if (-not (Test-Path "$Root\db\kalishi_edge.db")) {
        Write-Host "[setup] Creating database..." -ForegroundColor Cyan
        Push-Location $Root
        python db\setup.py
        Pop-Location
    }
}

function Start-Api {
    Write-Host "[api] Starting MCP server on :8420..." -ForegroundColor Green
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        "Set-Location '$Root'; python -m uvicorn mcp.server:app --host 0.0.0.0 --port 8420 --reload-dir '$Root\mcp' --reload-dir '$Root\engine' --reload-dir '$Root\agents' --reload-dir '$Root\data'"
    ) -WindowStyle Normal
}

function Start-Dashboard {
    if (-not (Test-Path "$Root\dashboard\node_modules\.bin\next.cmd")) {
        Write-Host "[dash] node_modules missing — running npm install..." -ForegroundColor Yellow
        Push-Location "$Root\dashboard"
        npm install --legacy-peer-deps
        Pop-Location
    }
    Write-Host "[dash] Starting dashboard on :3420..." -ForegroundColor Green
    Start-Process cmd -ArgumentList @(
        "/c",
        "cd /d `"$Root\dashboard`" && node_modules\.bin\next dev --port 3420"
    ) -WindowStyle Normal
}

# ── Main ────────────────────────────────────────────────────────────────────
Assert-Env
Assert-DB

if (-not $DashOnly) { Start-Api }
if (-not $ApiOnly)  { Start-Dashboard }

Write-Host ""
Write-Host "┌─────────────────────────────────────────┐" -ForegroundColor Cyan
Write-Host "│  KALISHI EDGE  —  Starting up            │" -ForegroundColor Cyan
Write-Host "│                                          │" -ForegroundColor Cyan
Write-Host "│  MCP API  ▶  http://localhost:8420       │" -ForegroundColor Cyan
Write-Host "│  Dashboard ▶  http://localhost:3420      │" -ForegroundColor Cyan
Write-Host "│                                          │" -ForegroundColor Cyan
Write-Host "│  First run picks:                        │" -ForegroundColor Cyan
Write-Host "│    python workflows/daily_picks.py       │" -ForegroundColor Cyan
Write-Host "└─────────────────────────────────────────┘" -ForegroundColor Cyan
