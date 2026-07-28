$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $resolved = & py "-3.12" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) {
                return $resolved.Trim()
            }
        } catch {
            $resolved = $null
        }
        try {
            $resolved = & py -c "import sys; assert sys.version_info >= (3, 12); print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) {
                return $resolved.Trim()
            }
        } catch {
            $resolved = $null
        }
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        try {
            $resolved = & python -c "import sys; assert sys.version_info >= (3, 12); print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) {
                return $resolved.Trim()
            }
        } catch {
            $resolved = $null
        }
    }

    return $null
}

$pythonExe = Find-Python
if (-not $pythonExe) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Python 3.12+ and winget were not found. Install Python 3.12 or newer from python.org, then rerun setup.ps1."
    }

    Write-Host "Installing Python 3.12..."
    winget install --id Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements

    $pythonExe = Find-Python
    if (-not $pythonExe) {
        $installed = Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"
        if (Test-Path -LiteralPath $installed) {
            $pythonExe = $installed
        }
    }
}

if (-not $pythonExe) {
    throw "Python was installed but could not be located. Open a new PowerShell window and rerun setup.ps1."
}

Write-Host "Using Python: $pythonExe"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$venvHealthy = $false
if (Test-Path -LiteralPath $venvPython) {
    try {
        & $venvPython --version 2>$null
        $venvHealthy = $LASTEXITCODE -eq 0
    } catch {
        $venvHealthy = $false
    }
}
if (-not $venvHealthy) {
    $venvDirectory = Join-Path $PSScriptRoot ".venv"
    if (Test-Path -LiteralPath $venvDirectory) {
        Remove-Item -LiteralPath $venvDirectory -Recurse -Force
    }
    & $pythonExe -m venv .venv
}

& $venvPython -m pip install --upgrade pip wheel
& $venvPython -m pip install -r requirements.txt

$skillVenvPython = Join-Path $PSScriptRoot ".webull-skill-venv\Scripts\python.exe"
$skillVenvHealthy = $false
if (Test-Path -LiteralPath $skillVenvPython) {
    try {
        & $skillVenvPython --version 2>$null
        $skillVenvHealthy = $LASTEXITCODE -eq 0
    } catch {
        $skillVenvHealthy = $false
    }
}
if (-not $skillVenvHealthy) {
    $skillVenvDirectory = Join-Path $PSScriptRoot ".webull-skill-venv"
    if (Test-Path -LiteralPath $skillVenvDirectory) {
        Remove-Item -LiteralPath $skillVenvDirectory -Recurse -Force
    }
    & $pythonExe -m venv .webull-skill-venv
}

& $skillVenvPython -m pip install --upgrade pip wheel
& $skillVenvPython -m pip install "https://github.com/webull-inc/webull-openapi-skills/archive/refs/tags/1.1.2.zip"

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
}

$env:PYTHONPATH = Join-Path $PSScriptRoot "src"

Write-Host ""
Write-Host "Setup complete."
Write-Host "1. Edit .env and add the Webull credentials and GROQ_API_KEY."
Write-Host "2. If 2FA is enabled: .\.webull-skill-venv\Scripts\webull-skill.exe auth"
Write-Host "3. Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m webull_bot.connect"
Write-Host "4. Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m webull_bot"
