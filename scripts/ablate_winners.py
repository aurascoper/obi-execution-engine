#!/usr/bin/env python3
"""Ablate each deployed autoresearch winner; measure ρ vs HL closedPnl.

For every ablation we:
  1. Snapshot the file/config that holds the winner.
  2. Mutate it back to the pre-winner baseline.
  3. Run validate_replay_fit.py against the same 14d window.
  4. Capture portfolio ρ + replay total + live total.
  5. Restore the original.

Decision rule (caller's): keep the winner only if reverting it drops ρ by
≥ +0.03 OR worsens net PnL on top-10 abs-PnL symbols.

Levers tested:
  - min_hold_revert      (strategy/signals.py constants)
  - obi_theta            (config/gates/obi.json — winner=0.30 NOT deployed; test forward)
  - z4h_exit_overrides   (config/z4h_exit_params.json — drop the 7 per-sym overrides)
  - regime_pause         (REGIME_GATE env var — production-only; skipped in replay)

Ratchet (SHOCK_STEP/SLIP) and meta_discount affect daemons outside replay,
so an in-replay ablation is not informative — flagged but skipped.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALIDATE = ROOT / "scripts" / "validate_replay_fit.py"

WINDOW_DAYS = int(os.environ.get("ABLATE_WINDOW_DAYS", "14"))


@dataclass
class AblationResult:
    name: str
    rho: float | None
    replay_total: float
    live_total: float
    diff: float
    notes: str


def run_validate() -> AblationResult:
    cmd = [sys.executable, str(VALIDATE), "--window", f"{WINDOW_DAYS}d"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    # validate_replay_fit returns 1 when ρ < 0.80 (gate fail) but the run
    # succeeded; only rc>=2 (or signals) are real errors.
    if r.returncode not in (0, 1):
        return AblationResult(
            "?", None, 0.0, 0.0, 0.0, f"FAIL rc={r.returncode}: {r.stderr[-200:]}"
        )
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    rho = None
    rt = lt = 0.0
    m = re.search(r"portfolio rho:\s*(-?\d+\.\d+)", out)
    if m:
        rho = float(m.group(1))
    m = re.search(r"live \$([+-]?\d+\.\d+)\s+replay \$([+-]?\d+\.\d+)", out)
    if m:
        lt = float(m.group(1))
        rt = float(m.group(2))
    return AblationResult("?", rho, rt, lt, rt - lt, "ok")


def with_file_swap(path: Path, content_for_run: str):
    """Context manager replacement: snapshot, swap, yield, restore."""
    original = path.read_text()

    class _Ctx:
        def __enter__(self_inner):
            path.write_text(content_for_run)
            return self_inner

        def __exit__(self_inner, *exc):
            path.write_text(original)
            return False

    return _Ctx()


def ablate_min_hold_revert() -> AblationResult:
    """Revert MIN_HOLD_FOR_REVERT_S 60 → 900 (pre-winner baseline)."""
    sig = ROOT / "strategy" / "signals.py"
    body = sig.read_text()
    new = re.sub(
        r"^MIN_HOLD_FOR_REVERT_S = 60\b.*$",
        "MIN_HOLD_FOR_REVERT_S = 900  # ablate: pre-winner",
        body,
        count=1,
        flags=re.M,
    )
    if new == body:
        return AblationResult(
            "min_hold_revert", None, 0.0, 0.0, 0.0, "no match — line drift?"
        )
    with with_file_swap(sig, new):
        r = run_validate()
    r.name = "min_hold_revert (60→900)"
    return r


def ablate_obi_theta_forward() -> AblationResult:
    """Forward-test OBI_THETA=0.30 (autoresearch winner not deployed).

    A "forward" probe rather than ablation: we keep the rest of the
    constellation intact and switch only obi.json. Improvement here would
    mean the winner survives HL-truth scrutiny.
    """
    obi = ROOT / "config" / "gates" / "obi.json"
    body = obi.read_text()
    cfg = json.loads(body)
    cfg_new = dict(cfg)
    cfg_new["OBI_THETA"] = 0.30
    new = json.dumps(cfg_new, indent=2)
    with with_file_swap(obi, new):
        r = run_validate()
    r.name = "obi_theta=0.30 (forward)"
    return r


def ablate_z4h_overrides() -> AblationResult:
    """Drop the 7 per-symbol z4h overrides (revert to base 7.0 only)."""
    z4 = ROOT / "config" / "z4h_exit_params.json"
    body = z4.read_text()
    cfg = json.loads(body)
    cfg_new = dict(cfg)
    cfg_new["per_symbol_overrides"] = {}
    new = json.dumps(cfg_new, indent=2)
    with with_file_swap(z4, new):
        r = run_validate()
    r.name = "z4h_overrides (drop 7 syms)"
    return r


def main():
    print(f"# ablation runner — window {WINDOW_DAYS}d, ground truth = HL closedPnl")
    print("# baseline (current production):")
    base = run_validate()
    base.name = "BASELINE (current prod)"
    print(
        f"  {base.name:40s}  ρ={('N/A' if base.rho is None else f'{base.rho:+.4f}'):>8s}  "
        f"replay=${base.replay_total:>+9.2f}  live=${base.live_total:>+9.2f}  "
        f"diff=${base.diff:>+9.2f}"
    )
    if base.rho is None:
        print(f"# baseline failed — abort  notes={base.notes}")
        return 1

    print()
    print("# ablations (each reverts one winner; ρ should DROP if winner real):")
    results = [base]
    for fn in (ablate_min_hold_revert, ablate_z4h_overrides):
        r = fn()
        results.append(r)
        d_rho = (r.rho - base.rho) if (r.rho is not None) else None
        d_replay = r.replay_total - base.replay_total
        print(
            f"  {r.name:40s}  ρ={('N/A' if r.rho is None else f'{r.rho:+.4f}'):>8s}  "
            f"Δρ={(f'{d_rho:+.4f}') if d_rho is not None else '   N/A':>8s}  "
            f"Δreplay=${d_replay:>+9.2f}  notes={r.notes}"
        )

    print()
    print("# forward probe (NOT-yet-deployed candidate; ρ should RISE if real):")
    for fn in (ablate_obi_theta_forward,):
        r = fn()
        results.append(r)
        d_rho = (r.rho - base.rho) if (r.rho is not None) else None
        d_replay = r.replay_total - base.replay_total
        print(
            f"  {r.name:40s}  ρ={('N/A' if r.rho is None else f'{r.rho:+.4f}'):>8s}  "
            f"Δρ={(f'{d_rho:+.4f}') if d_rho is not None else '   N/A':>8s}  "
            f"Δreplay=${d_replay:>+9.2f}  notes={r.notes}"
        )

    print()
    print("# skipped (out-of-replay):")
    print("  shock_ratchet step/slip       — daemon, not in replay exit chain")
    print("  meta_discount γ=0.991         — META_CONTROLLER=off in production")
    print(
        "  regime_pause REGIME_1H_ABS_RETURN — engine-side gate, replay only with REGIME_GATE=1"
    )

    out = ROOT / "autoresearch_gated" / "ablation_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "window_days": WINDOW_DAYS,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "results": [
                    {
                        "name": r.name,
                        "rho": r.rho,
                        "replay_total": r.replay_total,
                        "live_total": r.live_total,
                        "diff": r.diff,
                        "notes": r.notes,
                    }
                    for r in results
                ],
            },
            indent=2,
        )
    )
    print(f"\n# wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
