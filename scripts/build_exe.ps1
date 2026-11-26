param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Push-Location $root
try {
    & $Python -m pip install --disable-pip-version-check -r requirement.txt -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }

    & $Python -m PyInstaller `
        --noconfirm --clean `
        --onefile --console `
        --name WelearnHelper `
        --distpath dist `
        --workpath build `
        web_gui.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

    Write-Host "Build complete: $root\dist\WelearnHelper.exe"
} finally {
    Pop-Location
}
