# Optional per-user Windows installer. Does not alter ResearchX data or configuration.
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "ResearchX\venv"),
    [string]$Package = "researchx-ai[web]"
)
$ErrorActionPreference = "Stop"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "Install uv first: https://docs.astral.sh/uv/getting-started/installation/"
}
$Python = Join-Path $InstallDir "Scripts\python.exe"
if (-not (Test-Path $Python)) {
    if (Test-Path $InstallDir) { throw "Install directory exists without Python; choose a new -InstallDir." }
    & uv venv --python 3.11 $InstallDir | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the Python environment." }
}
& uv pip install --python $Python $Package | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Package installation failed." }
$Bin = Join-Path $InstallDir "Scripts"
$Launcher = $null
if (Test-Path (Join-Path $Bin "openh.exe")) {
    $Launcher = Join-Path $Bin "openh.exe"
    Write-Host "Launch (PowerShell):     openh"
} elseif (Test-Path (Join-Path $Bin "rx.exe")) {
    $Launcher = Join-Path $Bin "rx.exe"
    Write-Host "Launch (PowerShell):     rx"
} elseif (Test-Path (Join-Path $Bin "researchx.exe")) {
    $Launcher = Join-Path $Bin "researchx.exe"
    Write-Host "Launch (PowerShell):     researchx"
} elseif (Test-Path (Join-Path $Bin "oh.exe")) {
    $Launcher = Join-Path $Bin "oh.exe"
    Write-Host "Launch (PowerShell):     oh.exe"
} else {
    throw "No supported command entry point was installed."
}
& $Launcher --version | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Installed entry point failed verification." }
Write-Host "Executable: $Launcher"
Write-Host "Activate for this terminal: & '$Bin\Activate.ps1'"
Write-Host "Before starting web: configure RESEARCHX_DATABASE_URL and run python -m researchx.storage.migrate."
