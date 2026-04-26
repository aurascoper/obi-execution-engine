#!/usr/bin/env python3
"""Validation matrix for ratchet tranche mode.

Runs three configurations:
  full              backward-compat (default)
  tranche f=0.333 N=3
  tranche f=0.5   N=2

For each: 14d + 7d windows. Reports:
  ρ
  replay gross
  HL gross closedPnl
  top-10 abs residual symbols
  ratchet-path residual (symbols with ≥1 shock_ratchet sell in window)
  non-ratchet residual

Acceptance rule (from spec):
  Accept tranche only if:
    full mode reproduces baseline (verified separately)
    14d Δρ ≥ +0.03
    7d ρ does not worsen by >0.02
    ratchet-path residual improves
    non-ratchet residual not materially worse
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.validate_replay_fit import parse_hl_closed_pnl, pearson  # noqa: E402

VALIDATE = ROOT / "scripts" / "validate_replay_fit.py"
RATCHET_LOG = ROOT / "logs" / "shock_ratchet.log"


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def ratchet_active_syms(from_ms: int, to_ms: int) -> set[str]:
    """Symbols with at least one shock_ratchet sell in window."""
    out: set[str] = set()
    if not RATCHET_LOG.exists():
        return out
    sym_re = re.compile(r"symbol=([A-Za-z0-9:_/]+)")
    tag_re = re.compile(r"tag=shock_ratchet")
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
    with RATCHET_LOG.open() as fh:
        for line in fh:
            if not tag_re.search(line):
                continue
            m_ts = ts_re.match(line)
            if m_ts:
                try:
                    ts_ms = int(
                        dt.datetime.fromisoformat(
                            m_ts.group(1).replace(" ", "T") + "+00:00"
                        ).timestamp() * 1000
                    )
                except Exception:
                    ts_ms = 0
                if ts_ms < from_ms or ts_ms >= to_ms:
                    continue
            m_sym = sym_re.search(line)
            if m_sym:
                out.add(_norm(m_sym.group(1)))
    return out


def replay_per_sym(env: dict, from_ms: int, to_ms: int) -> dict[str, float]:
    e = env.copy()
    e["REPLAY_FROM_MS"] = str(from_ms)
    e["REPLAY_TO_MS"] = str(to_ms)
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tmp:
        e["REPLAY_PERSYM_OUT"] = tmp.name
        out_path = tmp.name
    cmd = [sys.executable, str(ROOT / "scripts" / "z_entry_replay_gated.py")]
    r = subprocess.run(cmd, env=e, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print(r.stderr[-1000:], file=sys.stderr)
        raise SystemExit(f"replay failed rc={r.returncode}")
    return json.loads(Path(out_path).read_text())


def measure(mode_name: str, env_overrides: dict, window_days: int) -> dict:
    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - window_days * 86_400_000

    env = os.environ.copy()
    env.update(env_overrides)

    sim = replay_per_sym(env, from_ms, to_ms)
    hl, _per_day, hl_fees = parse_hl_closed_pnl(from_ms, to_ms)
    rsyms = ratchet_active_syms(from_ms, to_ms)

    shared = sorted(set(sim) & set(hl))
    rho = pearson([sim[s] for s in shared], [hl[s] for s in shared])

    rows = []
    for s in shared:
        residual = hl[s] - sim[s]
        rows.append(
            {
                "sym": s,
                "hl": hl[s],
                "sim": sim[s],
                "residual": residual,
                "abs_residual": abs(residual),
                "is_ratchet": s in rsyms,
            }
        )
    rows.sort(key=lambda r: -r["abs_residual"])

    abs_path = sum(r["abs_residual"] for r in rows if r["is_ratchet"])
    abs_other = sum(r["abs_residual"] for r in rows if not r["is_ratchet"])
    return {
        "mode": mode_name,
        "window_days": window_days,
        "rho": rho,
        "replay_total": sum(sim.values()),
        "live_total": sum(hl.values()),
        "shared_n": len(shared),
        "ratchet_n": len(rsyms),
        "ratchet_path_abs_residual": abs_path,
        "non_ratchet_abs_residual": abs_other,
        "top10": rows[:10],
    }


MODES = [
    ("full",                   {"RATCHET_EXIT_MODEL": "full"}),
    ("tranche_f=0.333_N=3",    {"RATCHET_EXIT_MODEL": "tranche",
                                "RATCHET_TRANCHE_FRAC": "0.333333",
                                "RATCHET_TRANCHES_TOTAL": "3"}),
    ("tranche_f=0.5_N=2",      {"RATCHET_EXIT_MODEL": "tranche",
                                "RATCHET_TRANCHE_FRAC": "0.5",
                                "RATCHET_TRANCHES_TOTAL": "2"}),
]


def main():
    results: dict[str, dict[int, dict]] = {}
    for mode_name, overrides in MODES:
        results[mode_name] = {}
        for w in (14, 7):
            print(f"# running {mode_name} @ {w}d ...", file=sys.stderr)
            results[mode_name][w] = measure(mode_name, overrides, w)

    # Print summary
    print()
    print(
        f"  {'mode':22s}  {'win':>3s}  {'ρ':>8s}  {'Δρ vs full':>10s}  "
        f"{'replay$':>10s}  {'live$':>10s}  {'path|res|':>10s}  {'other|res|':>10s}"
    )
    print("  " + "-" * 102)
    base = {w: results["full"][w]["rho"] for w in (14, 7)}
    base_path = {w: results["full"][w]["ratchet_path_abs_residual"] for w in (14, 7)}
    base_other = {w: results["full"][w]["non_ratchet_abs_residual"] for w in (14, 7)}
    for mode_name, _ in MODES:
        for w in (14, 7):
            r = results[mode_name][w]
            d_rho = (r["rho"] - base[w]) if (r["rho"] is not None and base[w] is not None) else None
            d_rho_s = f"{d_rho:+.4f}" if d_rho is not None else "  N/A"
            print(
                f"  {mode_name:22s}  {w:>3d}  {r['rho']:+.4f}  {d_rho_s:>10s}  "
                f"${r['replay_total']:>+9.2f}  ${r['live_total']:>+9.2f}  "
                f"${r['ratchet_path_abs_residual']:>+9.2f}  "
                f"${r['non_ratchet_abs_residual']:>+9.2f}"
            )

    # Top-10 abs residuals per mode/14d
    print()
    print("=== top-10 |residual| by symbol (14d) ===")
    for mode_name, _ in MODES:
        print(f"\n  -- {mode_name} --")
        print(f"    {'sym':<14s}  {'hl$':>9s}  {'sim$':>9s}  {'residual':>9s}  ratchet?")
        for r in results[mode_name][14]["top10"]:
            print(
                f"    {r['sym']:<14s}  ${r['hl']:>+8.2f}  ${r['sim']:>+8.2f}  "
                f"${r['residual']:>+8.2f}  {'Y' if r['is_ratchet'] else 'n'}"
            )

    # Acceptance verdict
    print()
    print("=== acceptance per spec ===")
    for mode_name, _ in MODES:
        if mode_name == "full":
            continue
        r14 = results[mode_name][14]
        r7 = results[mode_name][7]
        d14 = r14["rho"] - base[14]
        d7 = r7["rho"] - base[7]
        path_better = r14["ratchet_path_abs_residual"] < base_path[14]
        other_not_worse = r14["non_ratchet_abs_residual"] <= base_other[14] * 1.05
        rules = [
            ("14d Δρ ≥ +0.03", d14 >= 0.03, f"{d14:+.4f}"),
            ("7d ρ doesn't drop >0.02", d7 >= -0.02, f"{d7:+.4f}"),
            ("ratchet-path |residual| improves",
             path_better,
             f"${r14['ratchet_path_abs_residual']:.2f} vs base ${base_path[14]:.2f}"),
            ("non-ratchet |residual| not worse >5%",
             other_not_worse,
             f"${r14['non_ratchet_abs_residual']:.2f} vs base ${base_other[14]:.2f}"),
        ]
        print(f"\n  -- {mode_name} --")
        for name, ok, val in rules:
            print(f"    [{'PASS' if ok else 'FAIL'}]  {name:32s}  {val}")
        verdict = "ACCEPT" if all(ok for _, ok, _ in rules) else "REJECT"
        print(f"    → {verdict}")

    out = ROOT / "autoresearch_gated" / "ratchet_tranche_matrix.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    serial = {}
    for mode_name in results:
        serial[mode_name] = {}
        for w, r in results[mode_name].items():
            rr = dict(r)
            rr["top10"] = [dict(t) for t in rr["top10"]]
            serial[mode_name][str(w)] = rr
    out.write_text(json.dumps(serial, indent=2, default=str))
    print(f"\n# wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
