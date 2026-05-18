$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$iconPath = Join-Path $projectRoot "assets\web-capture-automation-studio.ico"

if (-not (Test-Path $venvPython)) {
    throw "Virtual environment not found. Run .\install.ps1 first."
}

if (-not (Test-Path $iconPath)) {
    throw "Application icon not found at $iconPath."
}

Push-Location $projectRoot
try {
    if (Test-Path "$projectRoot\dist\WebCaptureAutomationStudio") {
        Remove-Item -Recurse -Force "$projectRoot\dist\WebCaptureAutomationStudio"
    }
    if (Test-Path "$projectRoot\build") {
        Remove-Item -Recurse -Force "$projectRoot\build"
    }

    & $venvPython -m pip install pyinstaller
    & $venvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --name "WebCaptureAutomationStudio" `
        --icon "$iconPath" `
        --collect-all playwright `
        --collect-all bs4 `
        --distpath "$projectRoot\dist" `
        --workpath "$projectRoot\build" `
        "$projectRoot\src\main.py"

    Write-Host ""
    Write-Host "Build complete."
    Write-Host "Executable folder: $projectRoot\dist\WebCaptureAutomationStudio"
    Write-Host "Before distributing, validate the packaged app on a clean Windows machine and run Playwright Chromium install there if needed."
}
finally {
    Pop-Location
}
