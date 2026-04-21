# run-hl.ps1 — Windows launcher for the Hyperliquid engine.
#
# Requires HL_WALLET_ADDRESS and HL_PRIVATE_KEY in .env or the environment.
$ErrorActionPreference = "Stop"

Set-Location -Path (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "No .venv found. Run scripts\run-launch.ps1 once to bootstrap it."
    exit 1
}

if (-not $env:EXECUTION_MODE) { $env:EXECUTION_MODE = "PAPER" }

.\.venv\Scripts\python.exe .\hl_engine.py
