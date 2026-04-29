#!/usr/bin/env python3
"""scripts/test_quoter_tracking.py — TWAP + quoter tracking smoke test (Task 19).

Drives math_core.quoter_policy with the math_core.schedulers.twap target curve
under three canonical synthetic scenarios:

  1. neutral OFI, normal fills
  2. toxic OFI, weak passive fills
  3. favorable OFI, strong passive fills

Acceptance bar (smoke; per docs/quoter_design_spec.md, Task 19 minimum):
  1. tracking error remains bounded
  2. no sign crossing
  3. terminal completion within tolerance
  4. toxic OFI completes faster than favorable OFI
  5. maker share positive in non-catchup regimes
  6. no forced terminal flush in normal scenarios

Writes the per-scenario record to autoresearch_gated/quoter_tracking_matrix.json
together with the parameter block, branch SHA, and per-step timeseries summary
diagnostics so the run is fully reproducible.

Pure simulation. No live imports, no network, no engine state.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from math_core.quoter_policy import (
    ExecutionIntent,
    OrderType,
    QuoterInputs,
    QuoterParams,
    Regime,
    Side,
    TrackingMetrics,
    build_intent,
)
from math_core.schedulers import twap


# ── Synthetic environment ─────────────────────────────────────────────────


def synthetic_fill(
    delta_bps: float,
    y_toxicity: float,
    dt_s: float,
    A: float,
    k: float,
    alpha_y_attenuation: float,
    rng: random.Random,
) -> bool:
    """Bernoulli post-only fill draw.

    λ(δ, Y) = A · exp(-k·δ_bps - α_y · max(0, Y))   [fills/sec]
    P(fill in dt) = 1 − exp(−λ·dt)

    Toxicity attenuates passive fills: in a market drifting against us, fewer
    counterparties cross our resting limit, so the effective rate drops. This
    is the synthetic mechanism that makes "toxic → behind → escalate" emerge
    naturally rather than being hard-coded.
    """
    lambd = A * math.exp(-k * max(0.0, delta_bps) - alpha_y_attenuation * max(0.0, y_toxicity))
    if lambd * dt_s > 30.0:
        return True
    p = 1.0 - math.exp(-lambd * dt_s)
    return rng.random() < p


def step_mid(mid: float, drift_bps_per_step: float, vol_bps_per_step: float, rng: random.Random) -> float:
    shock_bps = rng.gauss(0.0, vol_bps_per_step)
    bps = drift_bps_per_step + shock_bps
    return max(1e-9, mid * (1.0 + bps / 10_000.0))


# ── One scenario run ──────────────────────────────────────────────────────


def run_scenario(
    label: str,
    *,
    initial_inventory: float,
    horizon_s: float,
    dt_s: float,
    mid0: float,
    y_toxicity: float,
    drift_bps_per_step: float,
    vol_bps_per_step: float,
    fill_A: float,
    fill_k: float,
    fill_alpha_y: float,
    seeds: list[int],
    params: QuoterParams,
    completion_tol_dollars: float,
) -> dict:
    per_seed_results: list[dict] = []

    for seed in seeds:
        rng = random.Random(seed)

        n_steps = int(round(horizon_s / dt_s))
        q_t = initial_inventory
        mid = mid0
        sign0 = 1.0 if initial_inventory >= 0 else -1.0

        regime_counts = {Regime.PASSIVE: 0, Regime.TOUCH: 0, Regime.CATCHUP: 0}
        maker_fills = 0
        taker_fills = 0
        sign_crossings = 0
        max_abs_e = 0.0
        max_behind_e = 0.0
        completion_step: Optional[int] = None
        forced_flush = False

        prev_q_sign = 1.0 if q_t > 0 else (-1.0 if q_t < 0 else 0.0)

        for i in range(n_steps + 1):
            t = i * dt_s
            t_clamped = min(t, horizon_s)
            q_star = twap(initial_inventory, horizon_s, t_clamped)

            inp = QuoterInputs(
                t=t_clamped,
                T=horizon_s,
                q_t=q_t,
                q_star_t=q_star,
                mid=mid,
                touch_spread_bps=2.0,
                y_toxicity=y_toxicity,
                initial_inventory=initial_inventory,
            )
            intent = build_intent(inp, params)
            regime_counts[intent.regime] += 1
            max_abs_e = max(max_abs_e, abs(intent.e_t))
            max_behind_e = max(max_behind_e, max(0.0, intent.e_t))

            if intent.side is Side.HOLD or intent.clip_size <= 0.0:
                if completion_step is None and abs(q_t) <= completion_tol_dollars:
                    completion_step = i
                if i == n_steps and abs(q_t) > completion_tol_dollars:
                    forced_flush = True
                    q_t = 0.0
                continue

            if intent.order_type is OrderType.POST_ONLY:
                delta_bps = (
                    intent.delta_a_bps if intent.side is Side.SELL else intent.delta_b_bps
                )
                if delta_bps is None:
                    delta_bps = 0.0
                filled = synthetic_fill(
                    delta_bps=delta_bps,
                    y_toxicity=y_toxicity,
                    dt_s=dt_s,
                    A=fill_A,
                    k=fill_k,
                    alpha_y_attenuation=fill_alpha_y,
                    rng=rng,
                )
                if filled:
                    fill_size = min(intent.clip_size, abs(q_t))
                    q_t -= sign0 * fill_size
                    maker_fills += 1
            else:
                fill_size = min(intent.clip_size, abs(q_t))
                q_t -= sign0 * fill_size
                taker_fills += 1

            if abs(q_t) < 1e-6:
                q_t = 0.0
            cur_sign = 1.0 if q_t > 0 else (-1.0 if q_t < 0 else 0.0)
            if prev_q_sign != 0.0 and cur_sign != 0.0 and cur_sign != prev_q_sign:
                sign_crossings += 1
            prev_q_sign = cur_sign if cur_sign != 0.0 else prev_q_sign

            if completion_step is None and abs(q_t) <= completion_tol_dollars:
                completion_step = i

            if i < n_steps:
                mid = step_mid(mid, drift_bps_per_step, vol_bps_per_step, rng)

        total_steps = sum(regime_counts.values())
        metrics = TrackingMetrics(
            max_abs_e=max_abs_e,
            max_behind_e=max_behind_e,
            terminal_q=q_t,
            sign_crossings=sign_crossings,
            catchup_step_fraction=regime_counts[Regime.CATCHUP] / total_steps,
            passive_step_fraction=regime_counts[Regime.PASSIVE] / total_steps,
            touch_step_fraction=regime_counts[Regime.TOUCH] / total_steps,
            maker_fill_count=maker_fills,
            taker_fill_count=taker_fills,
            forced_terminal_flush=forced_flush,
            completion_time_s=(completion_step * dt_s) if completion_step is not None else None,
        )
        per_seed_results.append({"seed": seed, **asdict(metrics)})

    aggregated = _aggregate(per_seed_results)
    return {
        "label": label,
        "scenario": {
            "y_toxicity": y_toxicity,
            "drift_bps_per_step": drift_bps_per_step,
            "vol_bps_per_step": vol_bps_per_step,
            "fill_A": fill_A,
            "fill_k": fill_k,
            "fill_alpha_y": fill_alpha_y,
        },
        "per_seed": per_seed_results,
        "aggregate": aggregated,
    }


def _aggregate(per_seed: list[dict]) -> dict:
    keys_numeric = (
        "max_abs_e",
        "max_behind_e",
        "terminal_q",
        "sign_crossings",
        "catchup_step_fraction",
        "passive_step_fraction",
        "touch_step_fraction",
        "maker_fill_count",
        "taker_fill_count",
    )
    out = {}
    for k in keys_numeric:
        vals = [r[k] for r in per_seed]
        out[f"{k}_mean"] = sum(vals) / len(vals)
        out[f"{k}_min"] = min(vals)
        out[f"{k}_max"] = max(vals)

    completion_vals = [r["completion_time_s"] for r in per_seed if r["completion_time_s"] is not None]
    out["completion_time_s_mean"] = (
        sum(completion_vals) / len(completion_vals) if completion_vals else None
    )
    out["completion_count"] = len(completion_vals)
    out["forced_terminal_flush_count"] = sum(1 for r in per_seed if r["forced_terminal_flush"])
    return out


# ── Acceptance bar ────────────────────────────────────────────────────────


def evaluate_acceptance(
    results: dict,
    initial_inventory: float,
    completion_tol_dollars: float,
) -> dict:
    neutral = results["scenarios"]["neutral"]["aggregate"]
    toxic = results["scenarios"]["toxic"]["aggregate"]
    favorable = results["scenarios"]["favorable"]["aggregate"]

    bound_frac = 0.50
    bound = bound_frac * abs(initial_inventory)

    crit_1 = (
        neutral["max_behind_e_max"] <= bound
        and toxic["max_behind_e_max"] <= bound
        and favorable["max_behind_e_max"] <= bound
    )
    crit_2 = (
        neutral["sign_crossings_max"] == 0
        and toxic["sign_crossings_max"] == 0
        and favorable["sign_crossings_max"] == 0
    )
    crit_3 = (
        abs(neutral["terminal_q_max"]) <= completion_tol_dollars
        and abs(toxic["terminal_q_max"]) <= completion_tol_dollars
        and abs(favorable["terminal_q_max"]) <= completion_tol_dollars
    )
    tox_t = toxic["completion_time_s_mean"]
    fav_t = favorable["completion_time_s_mean"]
    crit_4 = (
        tox_t is not None
        and fav_t is not None
        and tox_t < fav_t
    )
    non_catchup_share = 1.0 - neutral["catchup_step_fraction_mean"]
    crit_5 = (
        non_catchup_share > 0.0
        and neutral["maker_fill_count_mean"] > 0
    )
    crit_6 = (
        neutral["forced_terminal_flush_count"] == 0
        and favorable["forced_terminal_flush_count"] == 0
    )

    return {
        "crit_1_tracking_bounded": crit_1,
        "crit_2_no_sign_crossing": crit_2,
        "crit_3_terminal_completion": crit_3,
        "crit_4_toxic_faster_than_favorable": crit_4,
        "crit_5_maker_share_in_non_catchup": crit_5,
        "crit_6_no_forced_flush_normal": crit_6,
        "all_pass": all([crit_1, crit_2, crit_3, crit_4, crit_5, crit_6]),
        "details": {
            "tracking_bound_dollars": bound,
            "completion_tol_dollars": completion_tol_dollars,
            "toxic_completion_mean_s": tox_t,
            "favorable_completion_mean_s": fav_t,
        },
    }


# ── Driver ────────────────────────────────────────────────────────────────


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "autoresearch_gated/quoter_tracking_matrix.json")
    ap.add_argument("--seeds", type=int, default=10)
    args = ap.parse_args()

    params = QuoterParams()
    initial_inventory = 10_000.0
    horizon_s = 3600.0
    dt_s = 10.0
    mid0 = 100.0
    completion_tol_dollars = 50.0
    seeds = list(range(args.seeds))

    common = dict(
        initial_inventory=initial_inventory,
        horizon_s=horizon_s,
        dt_s=dt_s,
        mid0=mid0,
        seeds=seeds,
        params=params,
        completion_tol_dollars=completion_tol_dollars,
    )

    scenarios = {
        "neutral": run_scenario(
            "neutral OFI, normal fills",
            y_toxicity=0.0,
            drift_bps_per_step=0.0,
            vol_bps_per_step=0.5,
            fill_A=1.0,
            fill_k=0.5,
            fill_alpha_y=0.0,
            **common,
        ),
        "toxic": run_scenario(
            "toxic OFI, weak passive fills",
            y_toxicity=0.6,
            drift_bps_per_step=-0.05,
            vol_bps_per_step=0.5,
            fill_A=1.0,
            fill_k=0.5,
            fill_alpha_y=2.0,
            **common,
        ),
        "favorable": run_scenario(
            "favorable OFI, strong passive fills",
            y_toxicity=-0.4,
            drift_bps_per_step=0.02,
            vol_bps_per_step=0.5,
            fill_A=1.0,
            fill_k=0.5,
            fill_alpha_y=0.0,
            **common,
        ),
    }

    results = {
        "kind": "quoter_tracking_matrix",
        "task": "task_19_twap_smoke",
        "git_sha": _git_sha(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "scheduler_family": "twap",
        "params": {
            "quoter_params": asdict(params),
            "initial_inventory": initial_inventory,
            "horizon_s": horizon_s,
            "dt_s": dt_s,
            "mid0": mid0,
            "completion_tol_dollars": completion_tol_dollars,
            "n_seeds": len(seeds),
        },
        "scenarios": scenarios,
    }
    results["acceptance"] = evaluate_acceptance(
        results, initial_inventory, completion_tol_dollars
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2, default=str))

    print(f"\nQuoter tracking matrix written: {args.out}")
    print(f"Git SHA: {results['git_sha']}")
    print(f"Seeds: {len(seeds)}\n")
    print("=" * 80)
    for name, scen in scenarios.items():
        agg = scen["aggregate"]
        print(f"\n[{name}] {scen['label']}")
        print(
            f"  terminal_q (mean/max): "
            f"${agg['terminal_q_mean']:>9.2f} / ${agg['terminal_q_max']:>9.2f}"
        )
        print(
            f"  max |e_t|  (mean/max): "
            f"${agg['max_abs_e_mean']:>9.2f} / ${agg['max_abs_e_max']:>9.2f}"
        )
        print(
            f"  max e_behind (mean/max): "
            f"${agg['max_behind_e_mean']:>9.2f} / ${agg['max_behind_e_max']:>9.2f}"
        )
        print(
            f"  regime mix (mean): "
            f"PASSIVE={agg['passive_step_fraction_mean']:.2%}  "
            f"TOUCH={agg['touch_step_fraction_mean']:.2%}  "
            f"CATCHUP={agg['catchup_step_fraction_mean']:.2%}"
        )
        print(
            f"  fills: maker={agg['maker_fill_count_mean']:.1f}  "
            f"taker={agg['taker_fill_count_mean']:.1f}  "
            f"completion_time(s)={agg['completion_time_s_mean']}"
        )
        print(f"  sign_crossings_max={agg['sign_crossings_max']}  "
              f"forced_flushes={agg['forced_terminal_flush_count']}")

    print("\n" + "=" * 80)
    print("ACCEPTANCE BAR")
    print("=" * 80)
    a = results["acceptance"]
    for k in ("crit_1_tracking_bounded", "crit_2_no_sign_crossing",
              "crit_3_terminal_completion", "crit_4_toxic_faster_than_favorable",
              "crit_5_maker_share_in_non_catchup", "crit_6_no_forced_flush_normal"):
        mark = "PASS" if a[k] else "FAIL"
        print(f"  [{mark}] {k}")
    print(f"\n  ALL_PASS = {a['all_pass']}")
    print(f"  toxic completion mean: {a['details']['toxic_completion_mean_s']}s")
    print(f"  favorable completion mean: {a['details']['favorable_completion_mean_s']}s")

    return 0 if a["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
