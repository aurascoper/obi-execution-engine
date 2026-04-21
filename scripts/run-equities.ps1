# run-equities.ps1 — Windows launcher for the Alpaca equities engine.
$ErrorActionPreference = "Stop"

Set-Location -Path (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "No .venv found. Run scripts\run-launch.ps1 once to bootstrap it."
    exit 1
}

if (-not $env:EXECUTION_MODE) { $env:EXECUTION_MODE = "PAPER" }
if (-not $env:ALPACA_TRADING_MODE) { $env:ALPACA_TRADING_MODE = "paper" }

.\.venv\Scripts\python.exe .\equities_engine.py
