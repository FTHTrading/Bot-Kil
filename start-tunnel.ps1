# start-tunnel.ps1
# ─────────────────────────────────────────────────────────────────────────────
# Starts the Cloudflare Tunnel that exposes the local MCP API at
#   https://api.bet.drunks.app  →  http://localhost:8420
#
# FIRST-TIME SETUP (run once as Administrator):
#   1. Install cloudflared:
#      winget install --id Cloudflare.cloudflared
#
#   2. Authenticate (opens browser):
#      cloudflared tunnel login
#
#   3. Create the tunnel:
#      cloudflared tunnel create kalishi-edge-api
#
#   4. Copy the tunnel UUID into cloudflared\config.yml (replace <TUNNEL-ID>)
#
#   5. Create the DNS route (replace <TUNNEL-ID>):
#      cloudflared tunnel route dns <TUNNEL-ID> api.bet.drunks.app
#
#   6. (Optional) Install as Windows service so it auto-starts:
#      cloudflared service install
#      Start-Service cloudflared
#
# DAILY USE — just run this script:
#   .\start-tunnel.ps1
# ─────────────────────────────────────────────────────────────────────────────

$configPath = "$PSScriptRoot\cloudflared\config.yml"

if (-not (Test-Path $configPath)) {
    Write-Error "Config not found at $configPath — fill in <TUNNEL-ID> first."
    exit 1
}

Write-Host "Starting Cloudflare Tunnel → api.bet.drunks.app" -ForegroundColor Cyan
cloudflared tunnel --config $configPath run
