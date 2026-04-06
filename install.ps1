# ─────────────────────────────────────────────────────────────────────────────
#  Kalishi Edge — One-Click Windows Setup
#  Run: .\install.ps1
# ─────────────────────────────────────────────────────────────────────────────
param(
    [switch]$SkipDashboard,   # skip npm install (useful on CI)
    [switch]$Force            # re-create .env even if it exists
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "  KALISHI EDGE — Setup Script" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

# ── 1. Python version check ──────────────────────────────────────────────────
Write-Host "[1/6] Checking Python..." -ForegroundColor Yellow
try {
    $py = python --version 2>&1
    if ($py -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]; $minor = [int]$Matches[2]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
            Write-Host "      ERROR: Python 3.11+ required (found $py)" -ForegroundColor Red
            exit 1
        }
        Write-Host "      OK — $py" -ForegroundColor Green
    }
} catch {
    Write-Host "      ERROR: Python not found. Install from python.org" -ForegroundColor Red
    exit 1
}

# ── 2. pip install ───────────────────────────────────────────────────────────
Write-Host "[2/6] Installing Python dependencies..." -ForegroundColor Yellow
Push-Location $Root
pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "      ERROR: pip install failed" -ForegroundColor Red
    exit 1
}
Write-Host "      OK" -ForegroundColor Green

# ── 3. Database setup ────────────────────────────────────────────────────────
Write-Host "[3/6] Initialising database..." -ForegroundColor Yellow
python db/setup.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "      WARNING: DB setup had errors (may be OK if already exists)" -ForegroundColor DarkYellow
} else {
    Write-Host "      OK — db/kalishi_edge.db ready" -ForegroundColor Green
}

# ── 4. .env ──────────────────────────────────────────────────────────────────
Write-Host "[4/6] Configuring environment..." -ForegroundColor Yellow
$envFile = Join-Path $Root ".env"
$envExample = Join-Path $Root ".env.example"

if (-not (Test-Path $envFile) -or $Force) {
    if (Test-Path $envExample) {
        Copy-Item $envExample $envFile
        Write-Host "      .env created from .env.example" -ForegroundColor Green
    } else {
        # Create a minimal .env
        @"
# Kalishi Edge — Environment Variables
# Fill in real keys to enable live data and betting

# The Odds API — https://the-odds-api.com (free tier available)
ODDS_API_KEY=YOUR_ODDS_API_KEY_HERE

# Kalshi — https://kalshi.com/account/api (requires account)
KALSHI_API_KEY=
KALSHI_API_SECRET=

# OpenAI (optional — used for AI analysis tips)
OPENAI_API_KEY=

# Bankroll settings
BANKROLL_TOTAL=10000
MAX_BET_PCT=0.05
MIN_EDGE_PCT=0.03

# Model alpha (simulated model edge in demo mode)
MODEL_ALPHA=0.035
"@ | Set-Content $envFile
        Write-Host "      .env created (fill in API keys to enable live data)" -ForegroundColor Green
    }
} else {
    Write-Host "      .env already exists — skipping (use -Force to overwrite)" -ForegroundColor DarkYellow
}

# ── 5. Dashboard npm install ──────────────────────────────────────────────────
if (-not $SkipDashboard) {
    Write-Host "[5/6] Installing dashboard dependencies..." -ForegroundColor Yellow
    $dashDir = Join-Path $Root "dashboard"
    if (Test-Path $dashDir) {
        Push-Location $dashDir
        npm install --silent 2>&1 | Out-Null
        Pop-Location
        Write-Host "      OK" -ForegroundColor Green
    } else {
        Write-Host "      WARNING: dashboard/ not found — skipping" -ForegroundColor DarkYellow
    }
} else {
    Write-Host "[5/6] Dashboard install skipped (-SkipDashboard)" -ForegroundColor DarkYellow
}

# ── 6. Done ───────────────────────────────────────────────────────────────────
Write-Host "[6/6] Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "  NEXT STEPS" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  1. Add your API keys to .env:" -ForegroundColor White
Write-Host "       ODDS_API_KEY   — https://the-odds-api.com" -ForegroundColor Gray
Write-Host "       KALSHI_API_KEY — https://kalshi.com/account/api" -ForegroundColor Gray
Write-Host ""
Write-Host "  2. Test your connections:" -ForegroundColor White
Write-Host "       python scripts\test_connections.py" -ForegroundColor Gray
Write-Host ""
Write-Host "  3. Run today's picks (demo mode — no live keys needed):" -ForegroundColor White
Write-Host "       python scripts\run_today.py" -ForegroundColor Gray
Write-Host ""
Write-Host "  4. Start the full server + dashboard:" -ForegroundColor White
Write-Host "       .\start.ps1" -ForegroundColor Gray
Write-Host ""
Write-Host "  Dashboard → http://localhost:3000" -ForegroundColor Cyan
Write-Host "  API       → http://localhost:8420/docs" -ForegroundColor Cyan
Write-Host ""

Pop-Location
