<#
.SYNOPSIS
  Run the full E2E scenario battery against the deployed Helixis app.

.DESCRIPTION
  Drives ~34 realistic property-management workflows through the live chat as a
  fictional company, screenshots each, and writes shots-battery/battery-report.md.
  Read/draft-only + auto-cancels every confirm gate -- nothing is ever sent/filed.
  You log in yourself unless -Email/-Password are given.

.EXAMPLE
  ./run-battery.ps1
      Full battery, manual login.

.EXAMPLE
  ./run-battery.ps1 -Only "Maintenance,Web,Safety"
      Just those categories.

.EXAMPLE
  ./run-battery.ps1 -Email you@co.com -Password 's3cret' -SlowMo 400
#>
param(
  [string]$Url      = "https://your-app.vercel.app",
  [string]$Email    = "",
  [string]$Password = "",
  [string]$Only     = "",
  [int]   $SlowMo   = 150,
  [switch]$Headless
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  throw "Node.js is not installed / not on PATH. Install Node 18+ and retry."
}
if (-not (Test-Path "node_modules/playwright")) {
  Write-Host "Installing Playwright (first run only)..." -ForegroundColor Cyan
  npm install --no-audit --no-fund
}
Write-Host "Ensuring the Chromium browser binary is present..." -ForegroundColor Cyan
npx playwright install chromium

$env:HELIXIS_URL   = $Url
$env:E2E_EMAIL     = $Email
$env:E2E_PASSWORD  = $Password
$env:SLOWMO        = "$SlowMo"
$env:HEADLESS      = if ($Headless) { "true" } else { "false" }
$env:SCENARIO_ONLY = $Only

Write-Host "`nRunning E2E battery -> $Url" -ForegroundColor Green
if ($Only) { Write-Host "Categories: $Only" -ForegroundColor Yellow }
if (-not $Email) { Write-Host "You'll log in yourself in the browser window.`n" -ForegroundColor Yellow }
Write-Host "This walks ~34 scenarios and can take 20-40 min. Every confirm gate is auto-cancelled.`n" -ForegroundColor Yellow

node battery.mjs

Remove-Item Env:\E2E_PASSWORD -ErrorAction SilentlyContinue
Write-Host "`nDone. Report + screenshots in .\shots-battery\ (battery-report.md)" -ForegroundColor Green
