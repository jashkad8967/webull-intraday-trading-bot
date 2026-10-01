# Register the daily post-close report as a Scheduled Task.
#
# WHY THIS EXISTS: an assistant session cannot be made always-on. A
# monitor dies when the session ends, and so does a scheduled Claude job
# ("jobs live only in this Claude session"). What CAN survive is the
# EVIDENCE. This task runs after every close and leaves a reconciled,
# self-contained record under reports/, so the next session starts from
# measurements instead of from scratch.
#
# Reconciliation is the point, not the summary. trade_history is written
# at order SUBMISSION rather than fill, so a duplicate or rejected SELL
# writes a phantom PROFIT while a rejected order never writes a loss -
# the error only ever flatters. On 2026-10-01 the records said -$1.20 and
# the account said -$4.16. A report that did not compare the two would
# quietly teach the wrong lesson every day.
#
# 15:32 local, after option_eod_close_time (14:50) and the equity
# closeout, with a couple of minutes for fills to settle. Deliberately
# not :00 or :30.
#
#   powershell -ExecutionPolicy Bypass -File deploy\local\install-report-task.ps1

$ErrorActionPreference = 'Stop'

$TaskName = 'WebullDailyReport'
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$Script = Join-Path $Repo 'scripts\daily_report.py'

if (-not (Test-Path $Python)) { throw "interpreter not found: $Python" }
if (-not (Test-Path $Script)) { throw "report script not found: $Script" }

# -NoProfile so a slow or broken user profile cannot stall it, and the
# PYTHONPATH is set inline because a Scheduled Task gets no shell rc.
$inner = "`$env:PYTHONPATH='$(Join-Path $Repo 'src')'; " +
         "& '$Python' '$Script' *> '$(Join-Path $Repo 'logs\daily_report.log')'"

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$inner`"" `
    -WorkingDirectory $Repo

# Weekdays only - there is no session to report on at a weekend.
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At '15:32'

# StartWhenAvailable matters: if the machine was asleep or off at 15:32
# the report still runs late rather than skipping the day silently, which
# is exactly how a gap in the evidence appears without anyone noticing.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Reconciled post-close session report (reports/YYYY-MM-DD.md)' | Out-Null

Write-Host "Registered scheduled task '$TaskName' (weekdays 15:32)"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Run now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Reports:  $(Join-Path $Repo 'reports')"
