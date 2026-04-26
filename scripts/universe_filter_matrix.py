#!/usr/bin/env python3
"""Run validate_replay_fit across 4 universe modes; report ρ + replay$.

Modes (per residual-decomposition Phase 1 plan):
  - all                    baseline, no universe filter
  - live_fills_window      DIAGNOSTIC upper bound (lookahead via fills)
  - entry_signal_window    DIAGNOSTIC weaker upper bound (lookahead via signals)
  - configured_live        DEPLOYABLE; uses HL_UNIVERSE + HIP3_UNIVERSE env

Decision rules (from caller):
  Δρ(live_fills_window) >= +0.05  → phantom-symbol mismatch confirmed
  Δρ(configured_live)   >= +0.04  → promote configured_live filter
  Only live_fills lifts           → universe list is fine; the issue is
                                    live entry-gating that replay doesn't
                                    mirror; revisit entry diagnostics.

Note: live_fills_window is for diagnostic only. NEVER used as the
validation rule for autoresearch promotion — it leaks live outcome.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALIDATE = ROOT / "scripts" / "validate_replay_fit.py"

WINDOW_DAYS = int(os.environ.get("UNIVERSE_MATRIX_WINDOW_DAYS", "14"))


_ENGINE_ENV_FILE = Path("/tmp/engine_env.txt")


def _inject_engine_universe(env: dict) -> None:
    """Inject HL_UNIVERSE/HIP3_UNIVERSE from the captured engine env so
    `configured_live` mode resolves to the live trading universe even when
    this script is run from a shell that lacks them."""
    if not _ENGINE_ENV_FILE.exists():
        return
    for line in _ENGINE_ENV_FILE.read_text().splitlines():
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k in ("HL_UNIVERSE", "HIP3_UNIVERSE", "HIP3_DEXS") and k not in env:
            env[k] = v


def run(mode: str) -> tuple[float | None, float, float, int]:
    env = os.environ.copy()
    env["REPLAY_UNIVERSE"] = mode
    if mode in ("configured_live", "configured_or_held"):
        _inject_engine_universe(env)
    cmd = [sys.executable, str(VALIDATE), "--window", f"{WINDOW_DAYS}d"]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    if r.returncode not in (0, 1):
        print(f"# {mode}: rc={r.returncode}\n{r.stderr[-800:]}", file=sys.stderr)
        return None, 0.0, 0.0, 0
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    rho = None
    rt = lt = 0.0
    n_allow = 0
    m = re.search(r"portfolio rho:\s*(-?\d+\.\d+)", out)
    if m:
        rho = float(m.group(1))
    m = re.search(r"live \$([+-]?\d+\.\d+)\s+replay \$([+-]?\d+\.\d+)", out)
    if m:
        lt = float(m.group(1))
        rt = float(m.group(2))
    m = re.search(r"REPLAY_UNIVERSE=\S+\s+allowed_symbols=(\d+)", out)
    if m:
        n_allow = int(m.group(1))
    return rho, rt, lt, n_allow


def main():
    print(f"# universe-filter matrix — window {WINDOW_DAYS}d, ground truth = HL closedPnl")
    print()
    modes = [
        "all",
        "live_fills_window",
        "entry_signal_window",
        "configured_live",
        "configured_or_held",
    ]
    rows = []
    for m in modes:
        print(f"# running {m}...", file=sys.stderr)
        rho, rt, lt, n = run(m)
        rows.append((m, rho, rt, lt, n))

    base_rho = rows[0][1]
    print(
        f"  {'mode':24s}  {'allowed':>8s}  {'ρ':>8s}  {'Δρ':>8s}  "
        f"{'replay$':>10s}  {'live$':>10s}  {'gap$':>10s}"
    )
    print("  " + "-" * 90)
    for m, rho, rt, lt, n in rows:
        d_rho = (rho - base_rho) if (rho is not None and base_rho is not None) else None
        rho_s = f"{rho:+.4f}" if rho is not None else "N/A"
        drho_s = f"{d_rho:+.4f}" if d_rho is not None else "  —    "
        n_s = "all" if n == 0 else str(n)
        print(
            f"  {m:24s}  {n_s:>8s}  {rho_s:>8s}  {drho_s:>8s}  "
            f"${rt:>+9.2f}  ${lt:>+9.2f}  ${rt - lt:>+9.2f}"
        )

    print()
    # Decision logic
    diag = next((r for r in rows if r[0] == "live_fills_window"), None)
    deploy_strict = next((r for r in rows if r[0] == "configured_live"), None)
    deploy = next((r for r in rows if r[0] == "configured_or_held"), None)
    if deploy_strict and deploy_strict[1] is not None and base_rho is not None:
        d_strict = deploy_strict[1] - base_rho
        print(f"  entry_policy_ρ      = {deploy_strict[1]:.4f}  (Δ={d_strict:+.4f})  configured_live, narrow")
    if diag and diag[1] is not None and base_rho is not None:
        d_diag = diag[1] - base_rho
        d_deploy = (deploy[1] - base_rho) if (deploy and deploy[1] is not None) else None
        print("=== verdict ===")
        if d_diag >= 0.05:
            print(f"  Δρ(live_fills_window) = {d_diag:+.4f} >= +0.05  → phantom-symbol mismatch CONFIRMED")
        else:
            print(f"  Δρ(live_fills_window) = {d_diag:+.4f}  → phantom-symbol effect smaller than expected")
        if d_deploy is not None and deploy is not None:
            print(
                f"  production_state_ρ  = {deploy[1]:.4f}  (Δ={d_deploy:+.4f})  configured_or_held, deployable"
            )
            if d_deploy >= 0.04:
                print(
                    f"  → PROMOTE configured_or_held as default validation universe (production_state_ρ improves materially)."
                )
            elif d_diag >= 0.05 and d_deploy < 0.04:
                print(
                    f"  → universe list alone insufficient. Live entry gating filters more than the configured list. "
                    f"Diagnose entry-gate identity per symbol next."
                )
            else:
                print(
                    f"  → no significant production_state_ρ lift; legacy positions are NOT the main residual driver. "
                    f"Hold off on universe-filter promotion."
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
