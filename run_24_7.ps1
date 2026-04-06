# run_24_7.ps1 — Kalshi intraday bot, restarts automatically on crash
# Runs 24/7, polling every 30 seconds, firing bets at the last 3 min of each 15-min window.
# Set up via Windows Task Scheduler to launch on boot (see README or register_task.ps1).

$ProjectRoot = "C:\Users\Kevan\kalishi-edge"
$Python      = "python"
$Script      = "$ProjectRoot\scripts\run_intraday.py"
$LogDir      = "$ProjectRoot\logs"
$BankRoll    = 10       # ← update as your balance grows
$Assets      = "BTC ETH SOL"
$WaitMin     = 3        # fire bets only in last N minutes of each window
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
    $log = "$LogDir\intraday_$ts.log"

    Write-Host "$(Get-Date -Format 'HH:mm:ss')  [run #$run] Starting Kalshi intraday loop → $log"

    $argList = @(
        "-u", $Script,
        "--bankroll", $BankRoll,
        "--execute",
        "--yes",
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
