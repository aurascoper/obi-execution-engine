#!/usr/bin/env python3
"""scripts/test_quoter_tracking.py — quoter tracking smoke test (Tasks 19, 20).

Drives math_core.quoter_policy with a scheduler family from
math_core.schedulers (TWAP, exponential, sinh-ratio) under three canonical
synthetic scenarios:

  1. neutral OFI, normal fills
  2. toxic OFI, weak passive fills
  3. favorable OFI, strong passive fills

Acceptance bar (six-criterion smoke, applied per family; the quoter itself
does not change between families):

  1. behind-only tracking miss bounded
  2. no sign crossing
  3. terminal completion within tolerance
  4. toxic OFI completes faster than favorable OFI
  5. maker share positive in non-catchup regimes
  6. no forced terminal flush in normal scenarios

CLI:
  --scheduler {twap,exponential,sinh_ratio}   single-family run
  --all-families                              sweep all three; stop at first fail
  --rho FLOAT                                 exponential urgency (default 2.0)
  --kappa FLOAT                               sinh-ratio shape (default 2.0)
  --seeds INT                                 seeds per scenario (default 10)
  --out PATH                                  JSON artifact destination

Single-family runs write autoresearch_gated/quoter_tracking_matrix.json
(legacy Task 19 path). Multi-family sweeps write
autoresearch_gated/quoter_family_matrix.json with one nested record per
family plus a top-level acceptance/decision block.

Pure simulation. No live imports, no network, no engine state.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from math_core.fill_model import (
    FillRecord,
    MicrostructureParams,
    MicrostructureState,
    QuoteState,
    execute_ioc,
    init_state,
    load_obi_ar1_calibration,
    post_quote,
    resolve_markout,
    step_environment,
    step_quote_and_fills,
)
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
from math_core.schedulers import SCHEDULERS, get_target_inventory


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
    scheduler_name: str,
    scheduler_kwargs: dict,
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
            q_star = get_target_inventory(
                scheduler_name,
                initial_inventory,
                horizon_s,
                t_clamped,
                **scheduler_kwargs,
            )

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


# ── Microstructure_v1 path ───────────────────────────────────────────────


def _resolve_aged_markouts(
    pending: list[FillRecord],
    resolved: list[FillRecord],
    state: MicrostructureState,
    params: MicrostructureParams,
) -> None:
    still: list[FillRecord] = []
    for f in pending:
        if state.t - f.t >= params.markout_horizon_s:
            resolve_markout(f, state.mid)
            resolved.append(f)
        else:
            still.append(f)
    pending[:] = still


def run_scenario_microstructure(
    label: str,
    *,
    scheduler_name: str,
    scheduler_kwargs: dict,
    initial_inventory: float,
    horizon_s: float,
    dt_s: float,
    mid0: float,
    seeds: list[int],
    params: QuoterParams,
    completion_tol_dollars: float,
    ms_params: MicrostructureParams,
) -> dict:
    """Microstructure_v1 driver: queue position, partial fills, AR(1) OBI,
    AR(1) spread, post-fill markout. Drives the quoter unchanged."""

    per_seed_results: list[dict] = []
    per_seed_markouts: list[dict] = []

    for seed in seeds:
        rng = random.Random(seed)
        ms_state = init_state(mid0, ms_params)
        q_t = initial_inventory
        sign0 = 1.0 if initial_inventory >= 0 else -1.0

        regime_counts = {Regime.PASSIVE: 0, Regime.TOUCH: 0, Regime.CATCHUP: 0}
        maker_fills = 0
        taker_fills = 0
        partial_fill_count = 0
        sign_crossings = 0
        max_abs_e = 0.0
        max_behind_e = 0.0
        completion_step: Optional[int] = None
        forced_flush = False
        prev_q_sign = 1.0 if q_t > 0 else (-1.0 if q_t < 0 else 0.0)

        quote = QuoteState()
        pending_markouts: list[FillRecord] = []
        all_fills: list[FillRecord] = []
        n_steps = int(round(horizon_s / dt_s))

        for i in range(n_steps + 1):
            if i > 0:
                step_environment(ms_state, ms_params, dt_s, rng)
            t_clamped = min(ms_state.t, horizon_s)

            q_star = get_target_inventory(
                scheduler_name,
                initial_inventory,
                horizon_s,
                t_clamped,
                **scheduler_kwargs,
            )
            inp = QuoterInputs(
                t=t_clamped,
                T=horizon_s,
                q_t=q_t,
                q_star_t=q_star,
                mid=ms_state.mid,
                touch_spread_bps=ms_state.spread_bps,
                y_toxicity=ms_state.y_obi,
                initial_inventory=initial_inventory,
            )
            intent = build_intent(inp, params)
            regime_counts[intent.regime] += 1
            max_abs_e = max(max_abs_e, abs(intent.e_t))
            max_behind_e = max(max_behind_e, max(0.0, intent.e_t))

            if intent.side is Side.HOLD or intent.clip_size <= 0:
                if completion_step is None and abs(q_t) <= completion_tol_dollars:
                    completion_step = i
                if i == n_steps and abs(q_t) > completion_tol_dollars:
                    forced_flush = True
                    q_t = 0.0
                _resolve_aged_markouts(pending_markouts, all_fills, ms_state, ms_params)
                continue

            remaining = abs(q_t)
            if intent.order_type is OrderType.IOC:
                if quote.posted:
                    quote = QuoteState()
                ioc_fills = execute_ioc(intent, ms_state, ms_params)
                for f in ioc_fills:
                    f.size = min(f.size, remaining)
                    remaining -= f.size
                    if f.size <= 0:
                        continue
                    f.regime = intent.regime.value
                    q_t -= sign0 * f.size
                    taker_fills += 1
                    pending_markouts.append(f)
            else:
                desired_delta = (
                    intent.delta_a_bps if intent.side is Side.SELL else intent.delta_b_bps
                )
                if desired_delta is None:
                    desired_delta = 0.0
                if (
                    not quote.posted
                    or quote.side is not intent.side
                    or abs(quote.delta_bps - desired_delta) > 0.5
                ):
                    quote = post_quote(intent, ms_state, ms_params)
                quote.residual = min(quote.residual, remaining)
                if quote.residual <= 0:
                    quote = QuoteState()
                else:
                    quote, step_fills = step_quote_and_fills(
                        quote, ms_state, ms_params, dt_s, rng
                    )
                    for f in step_fills:
                        f.regime = intent.regime.value
                        q_t -= sign0 * f.size
                        maker_fills += 1
                        pending_markouts.append(f)
                        if f.size < intent.clip_size - 1e-6:
                            partial_fill_count += 1

            if abs(q_t) < 1e-6:
                q_t = 0.0
            cur_sign = 1.0 if q_t > 0 else (-1.0 if q_t < 0 else 0.0)
            if prev_q_sign != 0.0 and cur_sign != 0.0 and cur_sign != prev_q_sign:
                sign_crossings += 1
            prev_q_sign = cur_sign if cur_sign != 0.0 else prev_q_sign

            if completion_step is None and abs(q_t) <= completion_tol_dollars:
                completion_step = i

            _resolve_aged_markouts(pending_markouts, all_fills, ms_state, ms_params)

        for f in pending_markouts:
            resolve_markout(f, ms_state.mid)
            all_fills.append(f)

        maker_markouts = [
            f.markout_bps
            for f in all_fills
            if f.is_maker and f.markout_bps is not None
        ]
        taker_markouts = [
            f.markout_bps
            for f in all_fills
            if (not f.is_maker) and f.markout_bps is not None
        ]
        maker_markout_mean = (
            sum(maker_markouts) / len(maker_markouts) if maker_markouts else None
        )
        taker_markout_mean = (
            sum(taker_markouts) / len(taker_markouts) if taker_markouts else None
        )

        total_steps = sum(regime_counts.values())
        per_seed_results.append({
            "seed": seed,
            "max_abs_e": max_abs_e,
            "max_behind_e": max_behind_e,
            "terminal_q": q_t,
            "sign_crossings": sign_crossings,
            "catchup_step_fraction": regime_counts[Regime.CATCHUP] / total_steps,
            "passive_step_fraction": regime_counts[Regime.PASSIVE] / total_steps,
            "touch_step_fraction": regime_counts[Regime.TOUCH] / total_steps,
            "maker_fill_count": maker_fills,
            "taker_fill_count": taker_fills,
            "partial_fill_count": partial_fill_count,
            "forced_terminal_flush": forced_flush,
            "completion_time_s": (completion_step * dt_s) if completion_step is not None else None,
            "maker_markout_bps_mean": maker_markout_mean,
            "taker_markout_bps_mean": taker_markout_mean,
        })

    return {
        "label": label,
        "scenario": {
            "obi_target": ms_params.obi_target,
            "mid_drift_y_coupling_bps": ms_params.mid_drift_y_coupling_bps,
            "obi_phi": ms_params.obi_phi,
            "obi_vol": ms_params.obi_vol,
            "aggressor_arrival_rate_per_s": ms_params.aggressor_arrival_rate_per_s,
            "touch_depth_usd": ms_params.touch_depth_usd,
            "markout_horizon_s": ms_params.markout_horizon_s,
        },
        "per_seed": per_seed_results,
        "aggregate": _aggregate(per_seed_results),
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
    out: dict = {}
    for k in keys_numeric:
        vals = [r[k] for r in per_seed]
        out[f"{k}_mean"] = sum(vals) / len(vals)
        out[f"{k}_min"] = min(vals)
        out[f"{k}_max"] = max(vals)

    if per_seed and "partial_fill_count" in per_seed[0]:
        vals = [r["partial_fill_count"] for r in per_seed]
        out["partial_fill_count_mean"] = sum(vals) / len(vals)
        out["partial_fill_count_min"] = min(vals)
        out["partial_fill_count_max"] = max(vals)

    if per_seed and "maker_markout_bps_mean" in per_seed[0]:
        maker_vals = [
            r["maker_markout_bps_mean"]
            for r in per_seed
            if r["maker_markout_bps_mean"] is not None
        ]
        out["maker_markout_bps_mean_mean"] = (
            sum(maker_vals) / len(maker_vals) if maker_vals else None
        )
        out["maker_markout_bps_seed_count"] = len(maker_vals)
        taker_vals = [
            r["taker_markout_bps_mean"]
            for r in per_seed
            if r["taker_markout_bps_mean"] is not None
        ]
        out["taker_markout_bps_mean_mean"] = (
            sum(taker_vals) / len(taker_vals) if taker_vals else None
        )

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


def evaluate_acceptance_v2(
    results: dict,
    initial_inventory: float,
    completion_tol_dollars: float,
    markout_floor_bps: float = -2.0,
) -> dict:
    """6+3 bar for microstructure_v1. Reuses the original 6, adds:
      7. maker markout not catastrophically negative in neutral/favorable
      8. toxic markout < favorable markout, AND quoter escalates more
      9. partial-fill tracking remains bounded without forced flush
    """
    base = evaluate_acceptance(results, initial_inventory, completion_tol_dollars)
    neutral = results["scenarios"]["neutral"]["aggregate"]
    toxic = results["scenarios"]["toxic"]["aggregate"]
    favorable = results["scenarios"]["favorable"]["aggregate"]

    n_mark = neutral.get("maker_markout_bps_mean_mean")
    f_mark = favorable.get("maker_markout_bps_mean_mean")
    t_mark = toxic.get("maker_markout_bps_mean_mean")

    crit_7 = (
        n_mark is not None
        and f_mark is not None
        and n_mark >= markout_floor_bps
        and f_mark >= markout_floor_bps
    )

    tox_escalation = (
        toxic["touch_step_fraction_mean"] + toxic["catchup_step_fraction_mean"]
    )
    fav_escalation = (
        favorable["touch_step_fraction_mean"] + favorable["catchup_step_fraction_mean"]
    )
    crit_8 = (
        t_mark is not None
        and f_mark is not None
        and t_mark < f_mark
        and tox_escalation > fav_escalation
    )

    bound = 0.50 * abs(initial_inventory)
    crit_9 = (
        neutral["max_behind_e_max"] <= bound
        and favorable["max_behind_e_max"] <= bound
        and neutral["forced_terminal_flush_count"] == 0
        and favorable["forced_terminal_flush_count"] == 0
        and neutral.get("partial_fill_count_mean", 0) > 0
    )

    out = dict(base)
    out["crit_7_markout_not_catastrophic"] = crit_7
    out["crit_8_toxic_worse_markout_and_more_escalation"] = crit_8
    out["crit_9_partial_fill_tracking_bounded"] = crit_9
    out["all_pass"] = all(
        [
            base["crit_1_tracking_bounded"],
            base["crit_2_no_sign_crossing"],
            base["crit_3_terminal_completion"],
            base["crit_4_toxic_faster_than_favorable"],
            base["crit_5_maker_share_in_non_catchup"],
            base["crit_6_no_forced_flush_normal"],
            crit_7,
            crit_8,
            crit_9,
        ]
    )
    out["details"]["markout_floor_bps"] = markout_floor_bps
    out["details"]["neutral_maker_markout_bps"] = n_mark
    out["details"]["favorable_maker_markout_bps"] = f_mark
    out["details"]["toxic_maker_markout_bps"] = t_mark
    out["details"]["toxic_escalation_share"] = tox_escalation
    out["details"]["favorable_escalation_share"] = fav_escalation
    return out


# ── Driver ────────────────────────────────────────────────────────────────


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def _run_family(
    *,
    scheduler_name: str,
    scheduler_kwargs: dict,
    params: QuoterParams,
    initial_inventory: float,
    horizon_s: float,
    dt_s: float,
    mid0: float,
    completion_tol_dollars: float,
    seeds: list[int],
    fill_model: str = "simple",
    obi_calibration_path: Optional[Path] = None,
    replay_mid_path: Optional[Path] = None,
) -> dict:
    """Run all three OFI scenarios for one scheduler family. Dispatches to
    the simple Bernoulli fill model (Tasks 19/20) or microstructure_v1
    (Task 21). When obi_calibration_path is set under microstructure_v1,
    OBI AR(1) parameters are loaded from the calibration JSON (Task 23)."""

    if fill_model == "simple":
        common = dict(
            scheduler_name=scheduler_name,
            scheduler_kwargs=scheduler_kwargs,
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
        record = {
            "scheduler_family": scheduler_name,
            "scheduler_kwargs": scheduler_kwargs,
            "fill_model": "simple",
            "scenarios": scenarios,
        }
        record["acceptance"] = evaluate_acceptance(
            {"scenarios": scenarios}, initial_inventory, completion_tol_dollars
        )
        return record

    if fill_model == "microstructure_v1":
        ms_common = dict(
            scheduler_name=scheduler_name,
            scheduler_kwargs=scheduler_kwargs,
            initial_inventory=initial_inventory,
            horizon_s=horizon_s,
            dt_s=dt_s,
            mid0=mid0,
            seeds=seeds,
            params=params,
            completion_tol_dollars=completion_tol_dollars,
        )
        replay_payload: Optional[dict] = None
        if replay_mid_path is not None:
            replay_payload = json.loads(replay_mid_path.read_text())

        def _ms_params_for(target: float) -> MicrostructureParams:
            base = MicrostructureParams(
                obi_target=target,
                mid_drift_y_coupling_bps=1.0,
            )
            if obi_calibration_path is not None:
                base = load_obi_ar1_calibration(
                    base, obi_calibration_path, obi_target=target
                )
            if replay_payload is not None:
                base = replace(
                    base,
                    mid_path_mode="replay",
                    replay_mid_path=tuple(replay_payload["mid_path"]),
                    replay_dt_s=float(replay_payload["dt_s"]),
                )
            return base

        if replay_mid_path is not None:
            label_suffix = " replay"
        elif obi_calibration_path is not None:
            label_suffix = " ar1-calibrated"
        else:
            label_suffix = ""
        scenarios = {
            "neutral": run_scenario_microstructure(
                f"neutral OFI (μstruct v1{label_suffix})",
                ms_params=_ms_params_for(0.0),
                **ms_common,
            ),
            "toxic": run_scenario_microstructure(
                f"toxic OFI (μstruct v1{label_suffix})",
                ms_params=_ms_params_for(0.6),
                **ms_common,
            ),
            "favorable": run_scenario_microstructure(
                f"favorable OFI (μstruct v1{label_suffix})",
                ms_params=_ms_params_for(-0.4),
                **ms_common,
            ),
        }
        record = {
            "scheduler_family": scheduler_name,
            "scheduler_kwargs": scheduler_kwargs,
            "fill_model": "microstructure_v1",
            "obi_calibration_used": (
                str(obi_calibration_path) if obi_calibration_path else None
            ),
            "replay_mid_used": (
                str(replay_mid_path) if replay_mid_path else None
            ),
            "scenarios": scenarios,
        }
        record["acceptance"] = evaluate_acceptance_v2(
            {"scenarios": scenarios}, initial_inventory, completion_tol_dollars
        )
        return record

    raise ValueError(f"unknown fill_model {fill_model!r}")


def _print_family(name: str, record: dict) -> None:
    print("\n" + "=" * 80)
    print(f"FAMILY: {name}   kwargs={record['scheduler_kwargs']}")
    print("=" * 80)
    for sname, scen in record["scenarios"].items():
        agg = scen["aggregate"]
        print(f"\n[{sname}] {scen['label']}")
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
        print(
            f"  sign_crossings_max={agg['sign_crossings_max']}  "
            f"forced_flushes={agg['forced_terminal_flush_count']}"
        )
        if "partial_fill_count_mean" in agg:
            print(
                f"  partial_fills (mean/max): "
                f"{agg['partial_fill_count_mean']:.1f} / "
                f"{agg['partial_fill_count_max']}"
            )
        if "maker_markout_bps_mean_mean" in agg:
            mm = agg["maker_markout_bps_mean_mean"]
            tm = agg.get("taker_markout_bps_mean_mean")
            mm_str = f"{mm:+.3f}" if mm is not None else "n/a"
            tm_str = f"{tm:+.3f}" if tm is not None else "n/a"
            print(f"  markout_bps (maker/taker): {mm_str} / {tm_str}")
    a = record["acceptance"]
    print("\n  ACCEPTANCE:")
    crit_keys = [
        "crit_1_tracking_bounded",
        "crit_2_no_sign_crossing",
        "crit_3_terminal_completion",
        "crit_4_toxic_faster_than_favorable",
        "crit_5_maker_share_in_non_catchup",
        "crit_6_no_forced_flush_normal",
    ]
    for k in (
        "crit_7_markout_not_catastrophic",
        "crit_8_toxic_worse_markout_and_more_escalation",
        "crit_9_partial_fill_tracking_bounded",
    ):
        if k in a:
            crit_keys.append(k)
    for k in crit_keys:
        mark = "PASS" if a[k] else "FAIL"
        print(f"    [{mark}] {k}")
    print(f"    ALL_PASS = {a['all_pass']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--scheduler",
        choices=sorted(SCHEDULERS),
        default="twap",
        help="single scheduler family (ignored when --all-families is set)",
    )
    ap.add_argument(
        "--all-families",
        action="store_true",
        help="sweep twap → exponential → sinh_ratio; stop at first failed family",
    )
    ap.add_argument("--rho", type=float, default=2.0, help="exponential urgency")
    ap.add_argument("--kappa", type=float, default=2.0, help="sinh-ratio shape")
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument(
        "--fill-model",
        choices=("simple", "microstructure_v1"),
        default="simple",
        help="fill simulation model (Tasks 19/20 default = simple; Task 21 = microstructure_v1)",
    )
    ap.add_argument(
        "--calibrated-obi",
        action="store_true",
        help="load OBI AR(1) parameters from config/obi_ar1.json (Task 23)",
    )
    ap.add_argument(
        "--obi-calibration-path",
        type=Path,
        default=None,
        help="explicit path to OBI calibration JSON (overrides --calibrated-obi default)",
    )
    ap.add_argument(
        "--replay-mid",
        type=Path,
        default=None,
        help="path to replay-mid window JSON (Task 24); when set, mid_path_mode=replay",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    params = QuoterParams()
    initial_inventory = 10_000.0
    horizon_s = 3600.0
    dt_s = 10.0
    mid0 = 100.0
    completion_tol_dollars = 50.0
    seeds = list(range(args.seeds))

    family_kwargs = {
        "twap": {},
        "exponential": {"rho": args.rho},
        "sinh_ratio": {"kappa": args.kappa},
    }

    obi_cal_path: Optional[Path] = None
    if args.fill_model == "microstructure_v1":
        if args.obi_calibration_path is not None:
            obi_cal_path = args.obi_calibration_path
        elif args.calibrated_obi:
            obi_cal_path = ROOT / "config/obi_ar1.json"

    replay_path: Optional[Path] = (
        args.replay_mid if args.fill_model == "microstructure_v1" else None
    )

    common = dict(
        params=params,
        initial_inventory=initial_inventory,
        horizon_s=horizon_s,
        dt_s=dt_s,
        mid0=mid0,
        completion_tol_dollars=completion_tol_dollars,
        seeds=seeds,
        fill_model=args.fill_model,
        obi_calibration_path=obi_cal_path,
        replay_mid_path=replay_path,
    )

    git_sha = _git_sha()
    base_meta = {
        "git_sha": git_sha,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "params": {
            "quoter_params": asdict(params),
            "initial_inventory": initial_inventory,
            "horizon_s": horizon_s,
            "dt_s": dt_s,
            "mid0": mid0,
            "completion_tol_dollars": completion_tol_dollars,
            "n_seeds": len(seeds),
        },
    }

    if args.all_families:
        if args.out is not None:
            out_path = args.out
        elif args.fill_model == "microstructure_v1" and replay_path is not None:
            out_path = ROOT / "autoresearch_gated/quoter_family_replay_markout_matrix.json"
        elif args.fill_model == "microstructure_v1" and obi_cal_path is not None:
            out_path = ROOT / "autoresearch_gated/quoter_family_microstructure_ar1_matrix.json"
        elif args.fill_model == "microstructure_v1":
            out_path = ROOT / "autoresearch_gated/quoter_family_microstructure_matrix.json"
        else:
            out_path = ROOT / "autoresearch_gated/quoter_family_matrix.json"
        order = ["twap", "exponential", "sinh_ratio"]
        families: dict[str, dict] = {}
        first_failure: Optional[str] = None

        print(f"Git SHA: {git_sha}")
        print(f"Seeds per scenario: {len(seeds)}")
        print(f"Family order: {order}  (decision rule: stop at first FAIL)\n")

        for fam in order:
            print(f"--- running {fam} (kwargs={family_kwargs[fam]}) ---")
            record = _run_family(
                scheduler_name=fam,
                scheduler_kwargs=family_kwargs[fam],
                **common,
            )
            families[fam] = record
            _print_family(fam, record)
            if not record["acceptance"]["all_pass"]:
                first_failure = fam
                print(f"\n>>> {fam} FAILED — stopping sweep per Task 20 decision rule.")
                break

        decision = {
            "order_attempted": list(families.keys()),
            "first_failure": first_failure,
            "all_families_passed": first_failure is None
            and len(families) == len(order),
        }

        if args.fill_model == "microstructure_v1" and replay_path is not None:
            task_tag = "task_24_family_replay_markout"
        elif args.fill_model == "microstructure_v1" and obi_cal_path is not None:
            task_tag = "task_23_family_microstructure_ar1"
        elif args.fill_model == "microstructure_v1":
            task_tag = "task_22_family_microstructure_v1"
        else:
            task_tag = "task_20_family_sweep"
        results = {
            "kind": "quoter_family_matrix",
            "task": task_tag,
            "fill_model": args.fill_model,
            "obi_calibration_used": str(obi_cal_path) if obi_cal_path else None,
            "replay_mid_used": str(replay_path) if replay_path else None,
            **base_meta,
            "family_kwargs": family_kwargs,
            "families": families,
            "decision": decision,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, indent=2, default=str))
        print(f"\nFamily matrix written: {out_path}")
        print(
            f"Decision: all_families_passed={decision['all_families_passed']}  "
            f"first_failure={decision['first_failure']}"
        )
        return 0 if decision["all_families_passed"] else 1

    # single-family path
    if args.out is not None:
        out_path = args.out
    elif args.fill_model == "microstructure_v1" and obi_cal_path is not None:
        out_path = ROOT / "autoresearch_gated/quoter_microstructure_ar1_matrix.json"
    elif args.fill_model == "microstructure_v1":
        out_path = ROOT / "autoresearch_gated/quoter_microstructure_matrix.json"
    else:
        out_path = ROOT / "autoresearch_gated/quoter_tracking_matrix.json"
    record = _run_family(
        scheduler_name=args.scheduler,
        scheduler_kwargs=family_kwargs[args.scheduler],
        **common,
    )
    task_tag = (
        f"task_21_microstructure_{args.scheduler}"
        if args.fill_model == "microstructure_v1"
        else f"task_19_smoke_{args.scheduler}"
    )
    results = {
        "kind": "quoter_tracking_matrix",
        "task": task_tag,
        "fill_model": args.fill_model,
        **base_meta,
        **record,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, default=str))

    print(f"Quoter tracking matrix written: {out_path}")
    print(f"Git SHA: {git_sha}")
    print(f"Seeds: {len(seeds)}")
    _print_family(args.scheduler, record)
    return 0 if record["acceptance"]["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
