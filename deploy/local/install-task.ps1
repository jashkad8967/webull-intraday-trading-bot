# Register the trading bot as a Windows Scheduled Task.
#
# The task is what makes the bot ALWAYS-ON rather than merely running.
# A process launched from a terminal - or from an AI assistant's shell -
# is a child of that terminal and dies with it, which for this bot means
# dying mid-position with no stop loss running. That is the single most
# expensive failure mode this project has: the GCP logs show
# "HOST UNREACHABLE - bot may be down, positions unmanaged" four times.
#
# Trigger is AT LOGON rather than AT STARTUP deliberately. "Run whether
# the user is logged on or not" requires storing the account password in
# the task, or running as SYSTEM with no user profile. Logon needs
# neither, and this is a workstation that stays logged in. The tradeoff:
# the bot does not run while logged out, so do not log out on a trading
# day.
#
# Idempotent - re-running replaces the existing task.
#
#   powershell -ExecutionPolicy Bypass -File deploy\local\install-task.ps1

$ErrorActionPreference = 'Stop'

$TaskName = 'WebullTradingBot'
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Runner = Join-Path $Repo 'deploy\local\run-bot.ps1'

if (-not (Test-Path $Runner)) { throw "runner not found: $Runner" }

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`"" `
    -WorkingDirectory $Repo

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# RestartCount/RestartInterval cover the task HOST dying (not the bot -
# run-bot.ps1's own loop handles that). ExecutionTimeLimit 0 = never
# killed for running long, which a trading bot must not be.
# MultipleInstances IgnoreNew stops a second copy racing the first on the
# same state files and placing duplicate orders.
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
    -Description 'Webull intraday trading bot supervisor (restarts on exit, incl. watchdog code 75)' | Out-Null

Write-Host "Registered scheduled task '$TaskName'"
Get-ScheduledTask -TaskName $TaskName |
    Select-Object TaskName, State |
    Format-Table -AutoSize
Write-Host "Start now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Logs:            $(Join-Path $Repo 'logs\supervisor.log')"
