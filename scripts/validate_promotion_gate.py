#!/usr/bin/env python3
"""Promotion Gate v2 runner (Gates A/B/C/D/E).

Read-only, verdict-oriented. Does NOT change config or trading state.

Gates implemented:
  A — HL truth loader integrity      (PASS / FAIL)
  B — Mode 2A accounting replay       (PASS / FAIL)
  C — Predictive replay diagnostic    (VALUE_ONLY — never blocks)
  D — Forward live/paper soak         (PASS / PENDING / FAIL via gate_d_eval.py)
  E — Intervention mask                (PASS / PENDING / FAIL)

Gate D backend: scripts/gate_d_eval.py reading config/expectation_bands.json
(declared per-symbol bands sourced from Stage 2.5 live-first / class-default
fallback per docs/stage3_promotion_design.md). Soak window is read from the
bands config; override with --gate-d-window-start / --gate-d-window-end.

Default mode reads existing artifacts under autoresearch_gated/. Pass
--refresh to re-run the underlying chain-integrity + ledger-audit checks.

Recommendation logic (Phase 1):
  Gate A FAIL                 → block (canonical truth broken)
  Gate B FAIL                 → block (accounting replay broken)
  Gates A+B PASS, D/E pending → paper-soak (forward proof needed)
  Gate C never blocks alone.

Usage:
    venv/bin/python3 scripts/validate_promotion_gate.py
    venv/bin/python3 scripts/validate_promotion_gate.py --refresh
    venv/bin/python3 scripts/validate_promotion_gate.py --window-days 14 \\
        --short-window-days 7 \\
        --output autoresearch_gated/promotion_gate_status.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ARTIFACTS = ROOT / "autoresearch_gated"
DEFAULT_OUT = ARTIFACTS / "promotion_gate_status.json"

ARTIFACT_CHAIN = ARTIFACTS / "fill_chain_integrity.json"
ARTIFACT_LEDGER = ARTIFACTS / "audit_fill_ledger_sessions.json"
ARTIFACT_PARTIAL_REDUCE = ARTIFACTS / "session_policy_partial_reduce_replay.json"
ARTIFACT_RATCHET_SWEEP = ARTIFACTS / "session_policy_ratchet_sweep.json"
ARTIFACT_REDUCTION_PATTERNS = ARTIFACTS / "live_reduction_patterns.json"
SIDECAR_MANUAL = ROOT / "logs" / "manual_orders.jsonl"

# Canonical post-#2 baselines (from validate_replay_fit, current as of 2026-04-27)
POST2_BASELINE_14D = 0.2570
POST2_BASELINE_7D = -0.0672

# Known cluster used for tid-dedup verification — ZEC sub-fill cluster
# at this timestamp has 5 distinct sub-fills with same hash/oid/px/sz
# but distinct tid. If fetch_user_fills_all keeps all 5, dedup is honored.
ZEC_CLUSTER_TS_MS = 1776582280507
ZEC_CLUSTER_EXPECTED_FILLS = 5


# ── Gate B thresholds (per design doc) ─────────────────────────────────
B_RHO_PER_FILL_MIN = 0.99
B_RHO_PER_SYM_MIN = 0.95
B_GROSS_14D_MAX = 5.0
B_GROSS_7D_MAX = 10.0
B_ABS_7D_MAX = 10.0

# ── Gate A thresholds ──────────────────────────────────────────────────
A_RECONCILE_MAX_PCT = 0.5

# ── Gate E thresholds ──────────────────────────────────────────────────
E_INTERVENTION_SHARE_MAX_PCT = 30.0  # matches β.0 audit baseline


def fmt_status(s: str) -> str:
    return {
        "PASS": "PASS",
        "FAIL": "FAIL",
        "PENDING": "PENDING",
        "VALUE_ONLY": "VALUE_ONLY",
    }.get(s, s)


# ── Gate A ─────────────────────────────────────────────────────────────
def run_chain_integrity_check(refresh: bool):
    """A1 — startPosition chain integrity."""
    if refresh or not ARTIFACT_CHAIN.exists():
        cmd = ["venv/bin/python3", "scripts/audit_fill_chain_integrity.py"]
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        if proc.returncode not in (0, 1):
            return {"status": "FAIL", "reason": f"chain audit subprocess failed: {proc.stderr[:300]}"}
    if not ARTIFACT_CHAIN.exists():
        return {"status": "FAIL", "reason": "fill_chain_integrity.json missing"}
    chain = json.loads(ARTIFACT_CHAIN.read_text())
    n_syms = chain.get("n_symbols_checked", 0)
    n_gaps = chain.get("total_gaps", -1)
    verdict = chain.get("verdict", "?")
    return {
        "status": "PASS" if verdict == "PASS" else "FAIL",
        "n_symbols_checked": n_syms,
        "total_gaps": n_gaps,
        "verdict_source": str(ARTIFACT_CHAIN.relative_to(ROOT)),
    }


def verify_tid_dedup():
    """A2 — verify tid-dedup is honored on a known sub-fill cluster.

    Issues a narrow-window query around a ZEC sub-fill cluster known to
    have 5 fills sharing (hash, oid, px, sz) but distinct tid. Old (oid)
    dedup would drop one; tid-dedup retains all 5.
    """
    try:
        from scripts.validate_replay_fit import fetch_user_fills_all
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
    except Exception as e:
        return {"status": "FAIL", "reason": f"import failed: {e}"}

    addr = os.environ.get("HL_WALLET_ADDRESS")
    if not addr:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("HL_WALLET_ADDRESS="):
                    addr = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not addr:
        return {"status": "FAIL", "reason": "HL_WALLET_ADDRESS unavailable"}

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    t = ZEC_CLUSTER_TS_MS
    fills = fetch_user_fills_all(info, addr, t - 5000, t + 5000)
    zec_at_cluster = [f for f in fills
                      if f.get("coin") == "ZEC" and int(f.get("time", 0)) == t]
    n = len(zec_at_cluster)
    return {
        "status": "PASS" if n >= ZEC_CLUSTER_EXPECTED_FILLS else "FAIL",
        "expected_fills_at_cluster": ZEC_CLUSTER_EXPECTED_FILLS,
        "actual_fills_at_cluster": n,
        "cluster_ts_ms": t,
        "note": "ZEC sub-fill cluster — tid-dedup must retain all 5",
    }


def verify_closed_pnl_reconciliation(window_days: int):
    """A3 — sum closedPnl from in-window fills matches parse_hl_closed_pnl
    within A_RECONCILE_MAX_PCT (0.5%)."""
    try:
        from scripts.validate_replay_fit import (
            fetch_user_fills_all,
            parse_hl_closed_pnl,
        )
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
    except Exception as e:
        return {"status": "FAIL", "reason": f"import failed: {e}"}

    addr = os.environ.get("HL_WALLET_ADDRESS")
    if not addr:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("HL_WALLET_ADDRESS="):
                    addr = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - window_days * 86_400_000
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    raw_fills = fetch_user_fills_all(info, addr, from_ms, to_ms)
    raw_sum = sum(float(f.get("closedPnl", 0) or 0) for f in raw_fills
                  if from_ms <= int(f.get("time", 0)) < to_ms)
    canonical_pnl, _per_day, _fees = parse_hl_closed_pnl(from_ms, to_ms)
    canonical_sum = sum(canonical_pnl.values())
    if abs(canonical_sum) < 1e-6:
        diff_pct = 0.0
    else:
        diff_pct = abs(raw_sum - canonical_sum) / abs(canonical_sum) * 100
    return {
        "status": "PASS" if diff_pct <= A_RECONCILE_MAX_PCT else "FAIL",
        "raw_in_window_sum": raw_sum,
        "canonical_sum": canonical_sum,
        "diff_pct": diff_pct,
        "max_pct_allowed": A_RECONCILE_MAX_PCT,
    }


def gate_a(refresh: bool, window_days: int):
    a1 = run_chain_integrity_check(refresh)
    a2 = verify_tid_dedup()
    a3 = verify_closed_pnl_reconciliation(window_days)
    overall = "PASS" if (
        a1["status"] == "PASS" and a2["status"] == "PASS"
        and a3["status"] == "PASS"
    ) else "FAIL"
    return {
        "status": overall,
        "checks": {
            "start_position_chain": a1,
            "tid_dedup": a2,
            "closed_pnl_reconciliation": a3,
        },
    }


# ── Gate B ─────────────────────────────────────────────────────────────
def run_ledger_audit(refresh: bool):
    """Ensure audit_fill_ledger_sessions.json exists and is current."""
    if refresh or not ARTIFACT_LEDGER.exists():
        cmd = [
            "venv/bin/python3", "scripts/audit_fill_ledger_sessions.py",
            "--prewindow-lookback-days", "90",
            "--seed-window-start-state", "reconstructed",
        ]
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        if proc.returncode not in (0, 1):
            return None
    if not ARTIFACT_LEDGER.exists():
        return None
    return json.loads(ARTIFACT_LEDGER.read_text())


def gate_b(refresh: bool):
    data = run_ledger_audit(refresh)
    if data is None:
        return {
            "status": "FAIL",
            "reason": "audit_fill_ledger_sessions.json missing or unreadable",
        }
    try:
        s14 = next(s for s in data if s["window_days"] == 14)
        s7 = next(s for s in data if s["window_days"] == 7)
    except StopIteration:
        return {"status": "FAIL", "reason": "ledger audit lacks 14d/7d windows"}

    rho_fill = s14.get("rho_per_fill_ledger_vs_hl_field") or 0.0
    rho_sym_14 = s14.get("rho_per_symbol_vs_hl_api") or 0.0
    rho_sym_7 = s7.get("rho_per_symbol_vs_hl_api") or 0.0
    gross_14 = s14.get("gross_mismatch_pct_vs_api") or 999.0
    gross_7 = s7.get("gross_mismatch_pct_vs_api") or 999.0
    abs_7 = abs(s7.get("sum_ledger", 0) - s7.get("sum_hl_api", 0))

    b1 = rho_fill >= B_RHO_PER_FILL_MIN
    b2_14 = rho_sym_14 >= B_RHO_PER_SYM_MIN
    b2_7 = rho_sym_7 >= B_RHO_PER_SYM_MIN
    b3 = gross_14 <= B_GROSS_14D_MAX
    b4 = (abs_7 <= B_ABS_7D_MAX) or (gross_7 <= B_GROSS_7D_MAX)

    overall = "PASS" if (b1 and b2_14 and b2_7 and b3 and b4) else "FAIL"
    return {
        "status": overall,
        "per_fill_rho": rho_fill,
        "per_symbol_rho_14d": rho_sym_14,
        "per_symbol_rho_7d": rho_sym_7,
        "gross_mismatch_14d_pct": gross_14,
        "gross_mismatch_7d_pct": gross_7,
        "abs_mismatch_7d_usd": abs_7,
        "checks": {
            "B1_per_fill_rho_ge_0.99": b1,
            "B2_per_sym_rho_14d_ge_0.95": b2_14,
            "B2_per_sym_rho_7d_ge_0.95": b2_7,
            "B3_gross_14d_le_5pct": b3,
            "B4_7d_abs_le_$10_or_gross_le_10pct": b4,
        },
        "thresholds": {
            "per_fill_rho_min": B_RHO_PER_FILL_MIN,
            "per_sym_rho_min": B_RHO_PER_SYM_MIN,
            "gross_14d_max_pct": B_GROSS_14D_MAX,
            "gross_7d_max_pct": B_GROSS_7D_MAX,
            "abs_7d_max_usd": B_ABS_7D_MAX,
        },
    }


# ── Gate C ─────────────────────────────────────────────────────────────
def gate_c():
    """Predictive replay — VALUE ONLY. Reads the most recent partial-reduce
    matrix (β.1) artifact. Falls back to ratchet sweep if missing."""
    out = {
        "status": "VALUE_ONLY",
        "baseline_rho_14d": POST2_BASELINE_14D,
        "baseline_rho_7d": POST2_BASELINE_7D,
        "note": "Predictive replay is diagnostic only; never sole promotion blocker.",
    }
    art = ARTIFACT_PARTIAL_REDUCE
    if not art.exists():
        art = ARTIFACT_RATCHET_SWEEP
    if not art.exists():
        out["note"] = ("No β.1 / ratchet-sweep artifact present. "
                       "Baseline values reported only.")
        return out
    try:
        data = json.loads(art.read_text())
    except Exception as e:
        out["error"] = f"could not parse {art.name}: {e}"
        return out

    # Look for "best" v0/β.1 config
    matrix = data.get("matrix") or data.get("configs") or []
    best_name = None
    best_rho_14 = None
    best_rho_7 = None
    best_top10 = None
    best_focus = None
    def _bw(cfg, w):
        bw = cfg.get("by_window") or {}
        return bw.get(w, bw.get(str(w), bw.get(int(w), {})))

    for cfg in matrix:
        s14 = _bw(cfg, 14) or cfg
        rho = s14.get("rho") if "rho" in s14 else s14.get("rho_per_symbol_vs_hl_api")
        if rho is None:
            continue
        if best_rho_14 is None or rho > best_rho_14:
            best_rho_14 = rho
            best_name = cfg.get("name")
            s7 = _bw(cfg, 7)
            best_rho_7 = s7.get("rho") or s7.get("rho_per_symbol_vs_hl_api")
            rows = s14.get("rows_top10") or (s14.get("rows", [])[:10])
            best_top10 = sum(abs(r["residual"]) for r in rows) if rows else None
            # focus may be a dict (oracle-style) or derivable from rows (β.1 style)
            best_focus = s14.get("focus") or {}
            if not best_focus:
                full_rows = s14.get("rows") or rows
                for r in full_rows:
                    if r.get("sym") in ("ZEC", "AAVE", "BTC", "ETH", "xyz:MSTR"):
                        best_focus[r["sym"]] = {
                            "replay": r.get("replay"),
                            "hl": r.get("hl"),
                        }
    out.update({
        "best_exit_policy_config": best_name,
        "best_exit_policy_rho_14d": best_rho_14,
        "best_exit_policy_rho_7d": best_rho_7,
        "top_10_abs_residual_usd": best_top10,
        "focus_signs": _focus_signs(best_focus) if best_focus else {},
        "source_artifact": str(art.relative_to(ROOT)),
    })
    return out


def _focus_signs(focus: dict) -> dict:
    out = {}
    for sym in ["ZEC", "AAVE", "BTC", "ETH", "xyz:MSTR"]:
        f = focus.get(sym)
        if not f:
            continue
        replay = f.get("replay")
        hl = f.get("hl") if "hl" in f else f.get("hl_api")
        if replay is None or hl is None:
            continue
        if abs(hl) < 0.01:
            out[sym] = "near_zero"
        else:
            out[sym] = "correct" if (replay >= 0) == (hl >= 0) else "wrong"
    return out


# ── Gate D ─────────────────────────────────────────────────────────────
def run_gate_d(
    bands_path: str,
    jsonl_path: str,
    soak_start: str | None,
    soak_end: str | None,
):
    """Forward-soak attribution via scripts/gate_d_eval.py.

    Returns the JSON decision payload directly so callers can include it in
    the consolidated promotion-gate status. Falls back to PENDING with an
    explanatory note if the bands or jsonl artifacts are missing.
    """
    from pathlib import Path as _Path
    if not _Path(bands_path).exists():
        return {
            "status": "PENDING",
            "decision": "pending",
            "note": f"bands file not found at {bands_path}",
        }
    if not _Path(jsonl_path).exists():
        return {
            "status": "PENDING",
            "decision": "pending",
            "note": f"engine log not found at {jsonl_path}",
        }
    cmd = [
        "venv/bin/python3",
        "scripts/gate_d_eval.py",
        "--bands", bands_path,
        "--jsonl", jsonl_path,
        "--summary-only",
    ]
    if soak_start is not None:
        cmd.extend(["--soak-start", soak_start])
    if soak_end is not None:
        cmd.extend(["--soak-end", soak_end])
    import subprocess
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        return {
            "status": "PENDING",
            "decision": "pending",
            "note": f"gate_d_eval invocation failed: {exc.stderr.strip()[:240]}",
        }
    try:
        d = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "status": "PENDING",
            "decision": "pending",
            "note": "gate_d_eval produced non-JSON output",
        }
    # Map gate_d_eval's lower-case decision into the PASS/PENDING/FAIL shape
    # used by the rest of this runner.
    decision = (d.get("decision") or "pending").lower()
    status_map = {"pass": "PASS", "pending": "PENDING", "fail": "FAIL"}
    out = dict(d)
    out["status"] = status_map.get(decision, "PENDING")
    summary = d.get("summary", {})
    out["note"] = (
        f"in_band={summary.get('n_in_band',0)} "
        f"outliers={summary.get('n_outliers',0)} "
        f"pending={summary.get('n_pending_thin_sample',0)} "
        f"agg_pnl=${summary.get('aggregate_pnl',0)}"
    )
    return out


# ── Gate E ─────────────────────────────────────────────────────────────
def gate_e(window_days: int):
    """Intervention mask — count tagged manual cloids + heuristic unlabeled.

    PASS:
      - sidecar log present, has entries
      - intervention share (tagged + unlabeled-likely-manual) ≤ threshold
      - no anomalous manual signature in symbols expected to be policy-only

    PENDING:
      - sidecar log exists but no fills in any window have been compared
        against it (no soak window active yet — Phase 3 territory), OR
      - no recent manual orders to verify tagging is wired correctly

    FAIL:
      - intervention share > threshold
      - heuristic likely_manual share much higher than tagged share, suggesting
        scripts are still issuing untagged orders
    """
    try:
        from scripts.validate_replay_fit import fetch_user_fills_all
        from scripts.lib.manual_order import is_manual_cloid
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
    except Exception as e:
        return {"status": "FAIL", "reason": f"import failed: {e}"}

    addr = os.environ.get("HL_WALLET_ADDRESS")
    if not addr:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("HL_WALLET_ADDRESS="):
                    addr = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    mode = None
    sidecar_present = SIDECAR_MANUAL.exists()
    sidecar_n_entries = 0
    if sidecar_present:
        try:
            with open(SIDECAR_MANUAL) as f:
                sidecar_n_entries = sum(1 for _ in f if _.strip())
        except Exception:
            pass

    # Pull window fills and count tagged manual cloids
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - window_days * 86_400_000
    fills = fetch_user_fills_all(info, addr, from_ms, to_ms)
    in_window = [f for f in fills
                 if from_ms <= int(f.get("time", 0)) < to_ms]
    n_total = len(in_window)
    n_tagged = sum(1 for f in in_window if is_manual_cloid(f.get("cloid")))

    # Heuristic likely_manual count from β.0 audit if available
    heuristic_share = None
    if ARTIFACT_REDUCTION_PATTERNS.exists():
        try:
            patt = json.loads(ARTIFACT_REDUCTION_PATTERNS.read_text())
            o = patt.get("overall", {})
            heuristic_share = o.get("share_likely_manual")
        except Exception:
            pass

    tagged_share_pct = (n_tagged / n_total * 100) if n_total else 0.0
    heuristic_share_pct = (heuristic_share * 100) if heuristic_share is not None else None

    # Decision
    rules = []
    if not sidecar_present:
        rules.append(("sidecar log present", False, str(SIDECAR_MANUAL.relative_to(ROOT))))
        status = "PENDING"
        reason = "sidecar log not yet created (no manual orders issued via tagged helper)"
    elif sidecar_n_entries == 0:
        rules.append(("sidecar log has entries", False, "0 entries"))
        status = "PENDING"
        reason = "sidecar exists but empty — tagging not yet exercised"
    elif n_total == 0:
        rules.append(("in-window fills present", False, "0 fills"))
        status = "PENDING"
        reason = "no in-window fills (manage-only-idle); cannot evaluate intervention share"
    else:
        rules.append(("sidecar log present", True, f"{sidecar_n_entries} entries"))
        rules.append(("in-window fills present", True, f"{n_total} fills"))
        # Intervention share check (tagged + heuristic-flagged unlabeled)
        # Use the tagged share as the primary signal; heuristic as secondary.
        if heuristic_share is not None:
            combined_share = max(tagged_share_pct, heuristic_share_pct)
        else:
            combined_share = tagged_share_pct
        within_threshold = combined_share <= E_INTERVENTION_SHARE_MAX_PCT
        rules.append((
            f"intervention share ≤ {E_INTERVENTION_SHARE_MAX_PCT}%",
            within_threshold,
            f"{combined_share:.1f}% (tagged {tagged_share_pct:.1f}%, "
            f"heuristic {heuristic_share_pct or 0:.1f}%)",
        ))
        status = "PASS" if within_threshold else "FAIL"
        reason = None
        # Mode disambiguation: a PASS with no tagged fills means the
        # instrumentation exists and contamination is below threshold,
        # but Phase 2 hasn't been exercised on real fills yet. A PASS
        # with tagged fills means real manual orders have flowed through
        # the helper.
        if status == "PASS":
            if n_tagged == 0:
                mode = "instrumented_not_yet_exercised"
            else:
                mode = "exercised_within_threshold"
        else:
            mode = "contamination_breach"

    return {
        "status": status,
        "mode": mode,
        "reason": reason,
        "sidecar_path": str(SIDECAR_MANUAL.relative_to(ROOT)),
        "sidecar_present": sidecar_present,
        "sidecar_n_entries": sidecar_n_entries,
        "in_window_fills": n_total,
        "tagged_manual_fills": n_tagged,
        "tagged_share_pct": tagged_share_pct,
        "heuristic_share_pct": heuristic_share_pct,
        "threshold_pct": E_INTERVENTION_SHARE_MAX_PCT,
        "checks": rules,
    }


# ── Decision ───────────────────────────────────────────────────────────
def decide(a, b, c, d_status="PENDING", e_status="PENDING"):
    reasons = []
    if a["status"] == "FAIL":
        reasons.append("Gate A FAIL — canonical truth source broken; "
                       "no downstream verdict is interpretable.")
        return "block", reasons
    if b["status"] == "FAIL":
        reasons.append("Gate B FAIL — Mode 2A accounting replay broken; "
                       "fix the ledger before promotion.")
        return "block", reasons
    if d_status == "FAIL":
        reasons.append("Gate D FAIL — forward soak failed.")
        return "block", reasons
    if e_status == "FAIL":
        reasons.append("Gate E FAIL — intervention contamination breach.")
        return "block", reasons
    if d_status == "PENDING" or e_status == "PENDING":
        reasons.append("Gates A+B PASS; predictive replay reported as value-only.")
        if d_status == "PENDING":
            reasons.append("Gate D PENDING — forward soak required before promotion.")
        if e_status == "PENDING":
            reasons.append("Gate E PENDING — intervention labeling not yet "
                           "implemented at ingestion (Phase 2).")
        return "paper-soak", reasons
    if d_status == "PASS" and e_status == "PASS":
        reasons.append("All required gates green; eligible pending operator sign-off.")
        return "eligible", reasons
    return "pending", reasons


# ── Main ───────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--short-window-days", type=int, default=7)
    ap.add_argument("--output", default=str(DEFAULT_OUT))
    ap.add_argument("--refresh", action="store_true",
                    help="re-run chain integrity + ledger audit (slower; "
                         "default reads existing artifacts)")
    ap.add_argument("--use-existing-artifacts", action="store_true",
                    default=True, help="read existing artifacts (default)")
    ap.add_argument("--candidate-name", default="(none — baseline only)")
    ap.add_argument("--gate-d-bands", default="config/expectation_bands.json",
                    help="Path to declared per-symbol bands for Gate D")
    ap.add_argument("--gate-d-jsonl", default="logs/hl_engine.jsonl",
                    help="Engine fill log for Gate D round-trip extraction")
    ap.add_argument("--gate-d-window-start", default=None,
                    help="Override soak window start for Gate D "
                         "(default: from bands config)")
    ap.add_argument("--gate-d-window-end", default=None,
                    help="Override soak window end for Gate D "
                         "(default: from bands config)")
    args = ap.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("# Promotion Gate v2 — Phase 1 (A/B/C)", file=sys.stderr)
    print(f"# candidate: {args.candidate_name}", file=sys.stderr)
    print(f"# refresh: {args.refresh}", file=sys.stderr)

    print("# running Gate A — HL truth loader integrity ...", file=sys.stderr)
    a = gate_a(refresh=args.refresh, window_days=args.window_days)

    print("# running Gate B — Mode 2A accounting replay ...", file=sys.stderr)
    b = gate_b(refresh=args.refresh)

    print("# running Gate C — predictive replay (value only) ...", file=sys.stderr)
    c = gate_c()

    print("# running Gate D — forward-soak attribution ...", file=sys.stderr)
    d = run_gate_d(
        bands_path=args.gate_d_bands,
        jsonl_path=args.gate_d_jsonl,
        soak_start=args.gate_d_window_start,
        soak_end=args.gate_d_window_end,
    )

    print("# running Gate E — intervention mask ...", file=sys.stderr)
    e = gate_e(window_days=args.window_days)
    if "note" not in e:
        if e["status"] == "PENDING":
            e["note"] = (e.get("reason") or
                         "Sidecar / fill data not yet available for evaluation.")
        elif e["status"] == "PASS":
            e["note"] = "Tagged manual cloids + heuristic share within threshold."
        elif e["status"] == "FAIL":
            e["note"] = (f"Intervention share exceeds "
                         f"{e.get('threshold_pct', '?')}% threshold.")

    recommendation, reasons = decide(a, b, c, d["status"], e["status"])

    payload = {
        "generated_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "candidate_name": args.candidate_name,
        "window_days": args.window_days,
        "short_window_days": args.short_window_days,
        "canonical_truth": {
            "source": "HL Info.user_fills_by_time",
            "single_info": True,
            "paginated": True,
            "tid_deduped": True,
            "chain_validated": True,
        },
        "canonical_baseline": {
            "rho_14d": POST2_BASELINE_14D,
            "rho_7d": POST2_BASELINE_7D,
        },
        "gate_a_loader_integrity": a,
        "gate_b_accounting": b,
        "gate_c_predictive_replay": c,
        "gate_d_forward_soak": d,
        "gate_e_intervention_mask": e,
        "recommendation": recommendation,
        "reasons": reasons,
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str))

    # ── Console summary ────────────────────────────────────────────────
    print()
    print("=" * 70)
    print(f"Promotion Gate v2 — candidate: {args.candidate_name}")
    print("=" * 70)

    print(f"\nGate A: {a['status']:<10s} HL truth loader integrity")
    for k, v in a.get("checks", {}).items():
        s = v.get("status", "?")
        detail = ""
        if k == "start_position_chain":
            detail = f"({v.get('total_gaps', '?')} gaps across {v.get('n_symbols_checked', '?')} syms)"
        elif k == "tid_dedup":
            detail = f"({v.get('actual_fills_at_cluster', '?')}/{v.get('expected_fills_at_cluster', '?')} fills at known cluster)"
        elif k == "closed_pnl_reconciliation":
            detail = f"(diff {v.get('diff_pct', 0):.3f}% of canonical $-{abs(v.get('canonical_sum', 0)):.2f})"
        print(f"  {k:<32s} {s:<10s} {detail}")

    print(f"\nGate B: {b['status']:<10s} Mode 2A accounting replay")
    if b["status"] != "FAIL" or "per_fill_rho" in b:
        print(f"  per-fill ρ                       {b.get('per_fill_rho', 0):+.4f}")
        print(f"  per-symbol ρ 14d / 7d            {b.get('per_symbol_rho_14d', 0):+.4f} / {b.get('per_symbol_rho_7d', 0):+.4f}")
        print(f"  gross mismatch 14d / 7d          {b.get('gross_mismatch_14d_pct', 0):.2f}% / {b.get('gross_mismatch_7d_pct', 0):.2f}%")
        print(f"  abs mismatch 7d                  ${b.get('abs_mismatch_7d_usd', 0):.2f}")
    else:
        print(f"  reason: {b.get('reason', '?')}")

    print(f"\nGate C: {c['status']:<10s} predictive replay (diagnostic only)")
    print(f"  baseline ρ 14d / 7d              {c['baseline_rho_14d']:+.4f} / {c['baseline_rho_7d']:+.4f}")
    if "best_exit_policy_rho_14d" in c and c["best_exit_policy_rho_14d"] is not None:
        print(f"  best exit-policy config          {c.get('best_exit_policy_config')}")
        print(f"  best exit-policy ρ 14d / 7d      "
              f"{c['best_exit_policy_rho_14d']:+.4f} / "
              f"{c.get('best_exit_policy_rho_7d') or 0:+.4f}")
        if c.get("top_10_abs_residual_usd") is not None:
            print(f"  top-10 |residual|                ${c['top_10_abs_residual_usd']:.2f}")
        signs = c.get("focus_signs", {})
        if signs:
            sign_str = "  ".join(f"{k}:{v}" for k, v in signs.items())
            print(f"  focus signs                      {sign_str}")
    print(f"  note: {c.get('note', '')}")

    print(f"\nGate D: {d['status']:<10s} forward live/paper soak")
    print(f"  note: {d.get('note', '')}")

    e_status_label = e["status"]
    if e.get("mode"):
        e_status_label = f"{e['status']} ({e['mode']})"
    print(f"\nGate E: {e_status_label:<46s} intervention mask")
    print(f"  sidecar log                      "
          f"{e.get('sidecar_path', '?')}  "
          f"(present={e.get('sidecar_present')}, entries={e.get('sidecar_n_entries', 0)})")
    if e.get("in_window_fills") is not None:
        print(f"  in-window fills                  {e['in_window_fills']}")
        print(f"  tagged manual cloids             {e.get('tagged_manual_fills', 0)} "
              f"({e.get('tagged_share_pct', 0):.1f}%)")
        if e.get("heuristic_share_pct") is not None:
            print(f"  heuristic likely_manual share    "
                  f"{e['heuristic_share_pct']:.1f}%  (β.0 audit baseline)")
        print(f"  threshold                        "
              f"{e.get('threshold_pct', E_INTERVENTION_SHARE_MAX_PCT)}%")
    print(f"  note: {e.get('note', '')}")

    print()
    print("=" * 70)
    print(f"Recommendation: {recommendation.upper()}")
    print("=" * 70)
    for r in reasons:
        print(f"  - {r}")

    print(f"\n# wrote {out_path}")
    return 0 if recommendation != "block" else 1


if __name__ == "__main__":
    sys.exit(main())
