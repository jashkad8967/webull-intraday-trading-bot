# Keep the trading bot alive on a Windows workstation.
#
# This replaces what the GCP host got from Docker and systemd:
# compose's `restart: unless-stopped`, plus the fact that nothing in a
# terminal owned the process.
#
# WHY A LOOP AND NOT JUST A LAUNCH: the bot deliberately kills itself.
# loop_watchdog exits with code 75 when the main scan stalls past
# MAIN_LOOP_STALL_SECONDS (900) or the protection loop past 300 - the
# reasoning being that a wedged bot holding an option position with no
# working stop is worse than a dead one, so it dies and expects to be
# restarted. Without a supervisor that exit is permanent, and the
# "protection" turns into the outage it was meant to prevent.
#
# Run via Scheduled Task, never from an interactive shell that can be
# closed: a process started from a terminal is a child of that terminal.
#
#   deploy/local/install-task.ps1   registers it
#
# Logs to logs/supervisor.log (gitignored) alongside the bot's own logs.

$ErrorActionPreference = 'Continue'

$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$LogDir = Join-Path $Repo 'logs'
$Log = Join-Path $LogDir 'supervisor.log'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log($Message) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $Log -Value $line -Encoding utf8
}

if (-not (Test-Path $Python)) {
    Write-Log "FATAL: no interpreter at $Python"
    exit 1
}

# The tuned production config lives OUTSIDE the repo, and is injected as
# real environment variables (which outrank any .env file).
#
# Two reasons it is not simply the repo's .env:
#
# 1. The test suite reads .env and asserts CODE defaults. Dropping the
#    137-setting production config in there broke two tests immediately -
#    MAX_SYMBOLS 5000 vs the expected 800, and a session-window check -
#    which is also why CI passes: CI has no .env. A config file that
#    makes the test suite fail is a config file that will stop being run.
# 2. It holds credentials. Outside the repo they cannot be committed by
#    accident, whatever .gitignore says later.
$ProdEnv = Join-Path $env:USERPROFILE '.webull-bot\env.production'
if (Test-Path $ProdEnv) {
    $loaded = 0
    foreach ($line in Get-Content $ProdEnv) {
        if ($line -match '^\s*#') { continue }
        $i = $line.IndexOf('=')
        if ($i -lt 1) { continue }
        $name = $line.Substring(0, $i).Trim()
        $value = $line.Substring($i + 1)
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        $loaded++
    }
    Write-Log "loaded $loaded settings from $ProdEnv"
} else {
    Write-Log "WARNING: $ProdEnv not found - running on code defaults plus .env only"
}

Write-Log "supervisor starting; repo=$Repo"

# Rate-limit restarts. A bot that cannot start - bad credentials, a
# syntax error, a corrupt state file - would otherwise spin as fast as
# Python can fail, filling the disk with tracebacks. The GCP host filled
# its disk once already and wedged Docker with four open option
# positions; that must not be reproduced here.
$recent = New-Object System.Collections.Generic.Queue[datetime]
$MaxRestartsPerWindow = 5
$WindowMinutes = 10
$BackoffSeconds = 300

while ($true) {
    $started = Get-Date
    Write-Log "launching: python -m webull_bot"

    $env:PYTHONPATH = Join-Path $Repo 'src'
    $env:PYTHONUNBUFFERED = '1'

    # Start-Process with redirect files, NOT PowerShell's `*>>`.
    # PS 5.1 redirection writes UTF-16LE, which made every log line
    # "D A Y E N D" with a NUL between characters - unreadable, and twice
    # the bytes on a project whose host has already filled its disk once
    # and wedged Docker with four open option positions. Start-Process
    # passes the child's own bytes straight through.
    #
    # stdout and stderr go to separate files because Start-Process
    # refuses to point both at one path.
    $out = Join-Path $LogDir 'bot.out.log'
    $err = Join-Path $LogDir 'bot.err.log'

    # Start-Process TRUNCATES its redirect targets on each launch, so the
    # traceback explaining why the bot just died would be destroyed by the
    # restart that follows it - the one thing actually needed. Keep the
    # previous run's stderr as .prev before relaunching: one generation,
    # so this cannot grow without bound.
    foreach ($pair in @(@($err, "$err.prev"), @($out, "$out.prev"))) {
        if ((Test-Path $pair[0]) -and ((Get-Item $pair[0]).Length -gt 0)) {
            Move-Item -Path $pair[0] -Destination $pair[1] -Force -ErrorAction SilentlyContinue
        }
    }
    # The bot caps its own daily logs, but this supervisor log is append-
    # only and nothing else prunes it.
    if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 5MB)) {
        Move-Item -Path $Log -Destination "$Log.prev" -Force -ErrorAction SilentlyContinue
        Write-Log "rotated supervisor log at 5MB"
    }

    try {
        $proc = Start-Process -FilePath $Python `
            -ArgumentList '-m', 'webull_bot' `
            -WorkingDirectory $Repo `
            -NoNewWindow -PassThru `
            -RedirectStandardOutput $out `
            -RedirectStandardError $err
        $proc.WaitForExit()
        $code = $proc.ExitCode
    } catch {
        $code = -1
        Write-Log "launch threw: $_"
    }

    $ran = [int]((Get-Date) - $started).TotalSeconds
    # 75 is the watchdog's own stall exit - expected, not a fault.
    if ($code -eq 75) {
        Write-Log "bot exited 75 (watchdog stall) after ${ran}s - restarting"
    } else {
        Write-Log "bot exited $code after ${ran}s - restarting"
    }

    $now = Get-Date
    $recent.Enqueue($now)
    while ($recent.Count -gt 0 -and ($now - $recent.Peek()).TotalMinutes -gt $WindowMinutes) {
        [void]$recent.Dequeue()
    }
    if ($recent.Count -ge $MaxRestartsPerWindow) {
        Write-Log ("$($recent.Count) restarts in ${WindowMinutes}m - backing off " +
                   "${BackoffSeconds}s. Something is failing at startup; check " +
                   "the traceback above rather than waiting this out.")
        Start-Sleep -Seconds $BackoffSeconds
        $recent.Clear()
    } else {
        Start-Sleep -Seconds 10
    }
}
