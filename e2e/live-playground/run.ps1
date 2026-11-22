<#
.SYNOPSIS
  Drive the deployed Helixis web app as a property manager (visible browser).

.DESCRIPTION
  Installs the local Playwright + Chromium (first run only), then launches
  playground.mjs. By default you log in yourself in the opened browser
  (password OR Google) -- the script never handles your credentials. Pass
  -Email/-Password only if your account uses email+password and you want an
  automatic login.

  Read-mostly + safe: it only types chat prompts and navigates, and it CANCELS
  any confirm gate, so nothing is ever filed or sent.

.EXAMPLE
  ./run.ps1
      Manual login (recommended). Opens the deployed app; you sign in; it drives.

.EXAMPLE
  ./run.ps1 -Email you@company.com -Password 's3cret'
      Automatic email+password login.

.EXAMPLE
  ./run.ps1 -Url http://localhost:5173
      Point at a local dev build instead of the deployed app.
#>
param(
  [string]$Url      = "https://your-app.vercel.app",
  [string]$Email    = "",
  [string]$Password = "",
  [int]   $SlowMo   = 250,
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

# Hand config to the Node script via env vars.
$env:HELIXIS_URL = $Url
$env:E2E_EMAIL    = $Email
$env:E2E_PASSWORD = $Password
$env:SLOWMO       = "$SlowMo"
$env:HEADLESS     = if ($Headless) { "true" } else { "false" }

Write-Host "`nLaunching PM playground -> $Url" -ForegroundColor Green
if (-not $Email) {
  Write-Host "You'll log in yourself in the browser window that opens.`n" -ForegroundColor Yellow
}

node playground.mjs

# Don't leave the password sitting in the session env.
Remove-Item Env:\E2E_PASSWORD -ErrorAction SilentlyContinue
Write-Host "`nDone. Screenshots + summary.md are in .\shots\" -ForegroundColor Green
