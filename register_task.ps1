$TaskName    = "KalshiIntradayBot"
$ProjectRoot = "C:\Users\Kevan\kalishi-edge"
$Script      = "$ProjectRoot\run_24_7.ps1"

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Script`"" `
    -WorkingDirectory $ProjectRoot

$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Kalshi 15-min intraday crypto betting bot" | Out-Null

Write-Host "Task registered: $TaskName"
Write-Host "Starting now..."
Start-ScheduledTask -TaskName $TaskName
Write-Host "Done. Bot is running in background. Logs at: $ProjectRoot\logs\"
