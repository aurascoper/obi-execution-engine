# run-launch.ps1 — Windows launcher for the taker + maker pair.
#
# First run: creates .venv, installs deps, and starts launch.py.
# Subsequent runs: reuses the existing venv.

$ErrorActionPreference = "Stop"

Set-Location -Path (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Creating .venv with Python 3.12..."
    py -3.12 -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not $env:EXECUTION_MODE) { $env:EXECUTION_MODE = "PAPER" }
if (-not $env:ALPACA_TRADING_MODE) { $env:ALPACA_TRADING_MODE = "paper" }

.\.venv\Scripts\python.exe .\launch.py
