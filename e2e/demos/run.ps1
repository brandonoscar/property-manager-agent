# Record + render the landing-page demo clips (PowerShell 5.1 compatible).
#
# !! ASCII ONLY. PowerShell 5.1 reads a BOM-less .ps1 as CP1252, so an em dash
# lands as 0x94 and the file fails to PARSE (CLAUDE.md gotcha 49 / the section 4 note
# about this machine having no pwsh). Keep every character in this file ASCII.
#
#   ./run.ps1 -Login              # sign in once, save the session
#   ./run.ps1                     # record + render the default reel
#   ./run.ps1 -Only rent-and-risk # one scenario
#   ./run.ps1 -RenderOnly         # re-render existing footage (e.g. new -Wait)

param(
    [switch] $Login,
    [switch] $RecordOnly,
    [switch] $RenderOnly,
    [string] $Only = "",
    [string] $Url = "",
    [double] $Wait = 1.8,
    [switch] $Headless
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path "node_modules")) {
    Write-Host "installing dependencies (playwright + ffmpeg)..." -ForegroundColor Cyan
    npm install
    # Playwright needs its own browser binary; npm install does not fetch it.
    npx playwright install chromium
}

if ($Url)      { $env:HELIXIS_URL = $Url }
if ($Headless) { $env:HEADLESS = "true" }

if ($Login) {
    node record.mjs --login
    exit $LASTEXITCODE
}

if (-not $RenderOnly) {
    $recordArgs = @()
    if ($Only) { $recordArgs += @("--only", $Only) }
    node record.mjs @recordArgs
    # Deliberately NOT fatal: a run where one scenario timed out still has
    # usable footage for the others, and throwing here would discard it.
    if ($LASTEXITCODE -ne 0) {
        Write-Host "one or more scenarios failed - rendering what was captured" -ForegroundColor Yellow
    }
}

if (-not $RecordOnly) {
    node render.mjs --wait $Wait
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host ""
Write-Host "clips -> out/final  (watch them before publishing: real tenant data)" -ForegroundColor Green
Write-Host "caption them 'waiting time compressed'" -ForegroundColor Green
