# Register the dashboard as its own Scheduled Task.
#
# Deliberately separate from WebullTradingBot. On the GCP host both ran in
# one container with uvicorn backgrounded and the bot as PID 1, so a
# dashboard crash could not take the trader down with it. Two independent
# tasks keep that property: the dashboard is a convenience, the trader
# holds positions.
#
#   powershell -ExecutionPolicy Bypass -File deploy\local\install-dashboard-task.ps1

$ErrorActionPreference = 'Stop'

$TaskName = 'WebullDashboard'
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Runner = Join-Path $Repo 'deploy\local\run-dashboard.ps1'

if (-not (Test-Path $Runner)) { throw "runner not found: $Runner" }

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`"" `
    -WorkingDirectory $Repo

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Webull dashboard (uvicorn on 127.0.0.1:8080, loopback only)' | Out-Null

Write-Host "Registered scheduled task '$TaskName'"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Start now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Dashboard:  http://127.0.0.1:8080"
Write-Host ""
Write-Host "Loopback only, by design: the dashboard can enqueue sell/buy/"
Write-Host "close-all with no authentication of its own, so binding it to a"
Write-Host "LAN interface would let anything on the network trade this account."
