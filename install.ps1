$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"

function New-Venv {
    param(
        [string]$TargetPath
    )

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & $py.Source -3 -m venv $TargetPath
        return
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -m venv $TargetPath
        return
    }

    throw "Python was not found. Install Python 3 first, then run .\install.ps1 again."
}

if (-not (Test-Path $Venv)) {
    New-Venv -TargetPath $Venv
}

& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Failed to upgrade pip."
}

& $Python -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install Python packages."
}

& $Python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install Playwright Chromium."
}

Write-Host "Install complete."
