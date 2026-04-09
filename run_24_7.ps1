# run_24_7.ps1 — Kalshi intraday bot, restarts automatically on crash
# V6 PAPER MODE: logs picks but does NOT execute real trades.
# V6 CHANGES: trend-only (contrarian ban), 1m momentum fix, price cap 10-65c
# Switch --paper to --execute --yes when model achieves >55% win rate over 20+ trades.

$ProjectRoot = "C:\Users\Kevan\kalishi-edge"
$Python      = "python"
$Script      = "$ProjectRoot\scripts\run_intraday.py"
$LogDir      = "$ProjectRoot\logs"
$Assets      = "BTC ETH SOL DOGE XRP"
$WaitMin     = 3.0      # fire bets in last 3 min — gives 6+ poll cycles to fire
$PollSec     = 30       # seconds between scans

# Kalshi is only open during certain hours (Mon–Fri, roughly 8am–11pm ET).
# Outside those hours the script will find no markets and sleep harmlessly.

Set-Location $ProjectRoot

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$run = 0
while ($true) {
    $run++
    $ts  = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
    $log = "$LogDir\intraday_V6_paper_$ts.log"

    Write-Host "$(Get-Date -Format 'HH:mm:ss')  [run #$run] Starting Kalshi V6 PAPER MODE → $log"

    $argList = @(
        "-u", $Script,
        "--paper",
        "--loop",
        "--loop-seconds", $PollSec,
        "--wait",
        "--wait-minutes", $WaitMin,
        "--asset"
    ) + ($Assets -split " ")

    & $Python @argList > $log 2>&1

    $exit = $LASTEXITCODE
    Write-Host "$(Get-Date -Format 'HH:mm:ss')  [run #$run] Process exited (code $exit). Restarting in 10 seconds…"

    # Rotate old logs — keep only the 20 most recent
    Get-ChildItem "$LogDir\intraday_*.log" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 20 |
        Remove-Item -Force

    Start-Sleep -Seconds 10
}
