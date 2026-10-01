# Keep the dashboard alive on a Windows workstation.
#
# Separate from run-bot.ps1 on purpose. On the GCP host both ran inside
# one container with uvicorn backgrounded and the bot as PID 1, so the
# dashboard dying could not take the trader with it. Two independent
# Scheduled Tasks preserve that property here: the dashboard is a
# convenience, the trader holds positions, and a crash in the former must
# never restart the latter.
#
# It binds to LOOPBACK ONLY, matching the GCP compose file. The dashboard
# can enqueue sell/buy/close-all commands, so exposing it on a LAN
# interface would let anything on the network place orders on this
# account. It has no authentication of its own.
#
#   deploy/local/install-dashboard-task.ps1   registers it
#
# Logs to logs/dashboard.log (gitignored).

$ErrorActionPreference = 'Continue'

$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$LogDir = Join-Path $Repo 'logs'
$Log = Join-Path $LogDir 'dashboard-supervisor.log'
$Port = if ($env:DASHBOARD_PORT) { $env:DASHBOARD_PORT } else { '8080' }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log($Message) {
    Add-Content -Path $Log -Encoding utf8 -Value (
        "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
}

if (-not (Test-Path $Python)) {
    Write-Log "FATAL: no interpreter at $Python"
    exit 1
}

# The dashboard reads the same state files as the trader, so it needs the
# same configuration - STATUS_FILE and COMMAND_FILE in particular. Without
# these it would read a status.json that nothing writes and silently show
# an empty account.
$ProdEnv = Join-Path $env:USERPROFILE '.webull-bot\env.production'
if (Test-Path $ProdEnv) {
    foreach ($line in Get-Content $ProdEnv) {
        if ($line -match '^\s*#') { continue }
        $i = $line.IndexOf('=')
        if ($i -lt 1) { continue }
        [Environment]::SetEnvironmentVariable(
            $line.Substring(0, $i).Trim(), $line.Substring($i + 1), 'Process')
    }
    Write-Log "loaded config from $ProdEnv"
}

$env:PYTHONPATH = Join-Path $Repo 'src'
$env:PYTHONUNBUFFERED = '1'

# ABSOLUTISE the state paths before launching.
#
# uvicorn must run with cwd = ui/ because server.py is imported as a
# top-level module ("uvicorn server:app"), exactly as the container did.
# But the config holds RELATIVE paths - STATUS_FILE=status.json,
# COMMAND_FILE=commands/commands.json - which the trader resolves against
# the REPO root. Left alone the dashboard resolves them against ui/,
# reads a status.json that does not exist, and serves HTTP 200 with every
# field blank. It looks like it works.
#
# Caught exactly that way: /api/status returned 200 with an empty
# account_value while the real file at the repo root said 80.55. This is
# the same relative-path-under-the-wrong-root failure that silently
# destroyed state files on the GCP host until deploy/compose.yaml pinned
# them absolutely.
foreach ($name in @('STATUS_FILE', 'COMMAND_FILE')) {
    $value = [Environment]::GetEnvironmentVariable($name, 'Process')
    if ($value -and -not [System.IO.Path]::IsPathRooted($value)) {
        $absolute = Join-Path $Repo $value
        [Environment]::SetEnvironmentVariable($name, $absolute, 'Process')
        Write-Log "$name -> $absolute"
    }
}

$recent = New-Object System.Collections.Generic.Queue[datetime]

while ($true) {
    $started = Get-Date
    Write-Log "launching uvicorn on 127.0.0.1:$Port"

    $out = Join-Path $LogDir 'dashboard.log'
    $err = Join-Path $LogDir 'dashboard.err.log'
    foreach ($pair in @(@($err, "$err.prev"), @($out, "$out.prev"))) {
        if ((Test-Path $pair[0]) -and ((Get-Item $pair[0]).Length -gt 0)) {
            Move-Item $pair[0] $pair[1] -Force -ErrorAction SilentlyContinue
        }
    }

    try {
        # Run from ui/ because server.py is imported as a top-level
        # module ("uvicorn server:app"), exactly as the container did.
        $proc = Start-Process -FilePath $Python `
            -ArgumentList '-m', 'uvicorn', 'server:app',
                          '--host', '127.0.0.1', '--port', $Port,
                          '--log-level', 'warning' `
            -WorkingDirectory (Join-Path $Repo 'ui') `
            -NoNewWindow -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError $err
        $proc.WaitForExit()
        $code = $proc.ExitCode
    } catch {
        $code = -1
        Write-Log "launch threw: $_"
    }

    $ran = [int]((Get-Date) - $started).TotalSeconds
    Write-Log "uvicorn exited $code after ${ran}s - restarting"

    $now = Get-Date
    $recent.Enqueue($now)
    while ($recent.Count -gt 0 -and ($now - $recent.Peek()).TotalMinutes -gt 10) {
        [void]$recent.Dequeue()
    }
    if ($recent.Count -ge 5) {
        Write-Log ("5 restarts in 10m - backing off 300s. Check " +
                   "dashboard.err.log; a port conflict or an import error " +
                   "will not fix itself.")
        Start-Sleep -Seconds 300
        $recent.Clear()
    } else {
        Start-Sleep -Seconds 10
    }
}
