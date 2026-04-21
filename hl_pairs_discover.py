#!/usr/bin/env python3
"""
hl_pairs_discover.py — Nightly cointegration discovery for hl_pairs.

Pulls 48h of 1-min candles for a candidate universe, tests pairwise
cointegration (OLS + AR(1) half-life on residuals, statsmodels-free),
ranks survivors by expected spread tradability, and writes
`config/pairs_whitelist.json` for the runtime engine to consume.

Usage:
    source .env
    venv/bin/python hl_pairs_discover.py                    # default universe
    venv/bin/python hl_pairs_discover.py --candidates MSTR,COIN,NVDA,BTC
    venv/bin/python hl_pairs_discover.py --lookback-hours 72

Cointegration method (no statsmodels dependency):

    1. Fit OLS:       log(px_A_t) = α + β · log(px_B_t) + r_t
    2. AR(1) on r:    r_t = φ · r_{t-1} + ε_t
    3. Half-life:     h = -ln(2) / ln(φ)                (samples, 1-min each)
    4. Keep if:       0 < φ < 1
                  AND half_life_min ∈ [HL_MIN_MIN, HL_MAX_MIN]
                  AND |β| ∈ [BETA_MIN, BETA_MAX]
                  AND spread_sigma_bps ≥ SPREAD_SIGMA_MIN_BPS
                  AND R²(OLS) ≥ R2_MIN

This is NOT a proper ADF test — φ<1 is a necessary but not sufficient
stationarity condition at finite sample. It is however the cheap
version of the test that avoids adding statsmodels as a dependency and
is adequate for a whitelist that the operator reviews before going live.

Output shape (config/pairs_whitelist.json):
    {
      "generated_at": "2026-04-21T03:30:00Z",
      "lookback_hours": 48,
      "universe": [...],
      "pairs": [
        {
          "leg_a": "xyz:MSTR",
          "leg_b": "BTC",
          "beta": 2.18,
          "alpha": -3.45,
          "half_life_min": 92.4,
          "phi": 0.9925,
          "r2": 0.87,
          "spread_sigma_bps": 38.2,
          "score": 1.42,
          "samples": 2856
        },
        ...
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
WHITELIST_PATH = ROOT / "config" / "pairs_whitelist.json"
SUMMARY_PATH = ROOT / "logs" / "pairs_discovery.md"

# ── Candidate universe ───────────────────────────────────────────────────────
# HIP-3 equity perps on xyz + majors on native. Edit to taste, or override
# via --candidates. Coins prefixed with "dex:" are HIP-3; bare names are native.
DEFAULT_CANDIDATES = [
    # Native majors — liquid, always-quoting base legs.
    "BTC",
    "ETH",
    "SOL",
    # xyz HIP-3 equity perps — the interesting equity-crypto basis.
    "xyz:MSTR",
    "xyz:COIN",
    "xyz:HOOD",
    "xyz:NVDA",
    "xyz:INTC",
    "xyz:AMZN",
    "xyz:CRCL",
    # cash HIP-3 — MicroStrategy alt-venue; useful for cross-DEX sanity later.
    "cash:HOOD",
]

# ── Discovery thresholds ─────────────────────────────────────────────────────
LOOKBACK_HOURS_DEFAULT = 48
BAR_INTERVAL = "1m"
HL_MIN_MIN = 30  # half-life floor — faster = noise-trading, fee-eaten
HL_MAX_MIN = 24 * 60  # half-life ceiling — slower = too much funding drag
BETA_MIN = 0.15  # clamp: below = decoupled, above = levered beyond hedge utility
BETA_MAX = 4.0
SPREAD_SIGMA_MIN_BPS = 15  # below this the expected z-revert PnL < fees
R2_MIN = 0.50  # OLS fit must explain ≥50% of log-price variance
MIN_SAMPLES = 2000  # require ≥2000 aligned 1m bars out of ~2880 over 48h
TOP_N_DEFAULT = 12  # how many survivors to keep in whitelist
MAX_PAIRS_PER_SYMBOL = 3  # cap hub-concentration; no single symbol in > N pairs


@dataclass
class PairStats:
    leg_a: str
    leg_b: str
    beta: float
    alpha: float
    half_life_min: float
    phi: float
    r2: float
    spread_sigma_bps: float
    score: float
    samples: int


# ── Candle fetch ─────────────────────────────────────────────────────────────
def _fetch_candles(info, coin: str, start_ms: int, end_ms: int) -> list[dict]:
    """Resolve dex:coin → (dex, name) and pull 1-min candles.

    SDK: info.candles_snapshot(name, interval, startTime_ms, endTime_ms).
    For HIP-3 perps some SDK versions require passing the prefixed name
    directly ("xyz:MSTR"); others take (coin=name, dex=dex). We try the
    prefixed-name form first since it's what the public /info endpoint
    accepts.
    """
    try:
        return info.candles_snapshot(coin, BAR_INTERVAL, start_ms, end_ms) or []
    except Exception as exc:
        # Retry with the HTTP endpoint directly — SDK method signatures
        # drift on HIP-3 support between releases.
        try:
            return (
                info.post(
                    "/info",
                    {
                        "type": "candleSnapshot",
                        "req": {
                            "coin": coin,
                            "interval": BAR_INTERVAL,
                            "startTime": start_ms,
                            "endTime": end_ms,
                        },
                    },
                )
                or []
            )
        except Exception as exc2:
            print(f"[warn] candles fetch failed for {coin}: {exc2} (primary: {exc})")
            return []


def _candles_to_series(candles: list[dict]) -> dict[int, float]:
    """{close_timestamp_ms: close_price}. HL candles have keys t (open ms),
    T (close ms), o/h/l/c/v. We key on close-time so pair alignment is on
    completed bars."""
    out: dict[int, float] = {}
    for c in candles:
        try:
            t = int(c["T"])
            px = float(c["c"])
            if px > 0:
                out[t] = px
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ── Cointegration math ───────────────────────────────────────────────────────
def _ols(y: np.ndarray, x: np.ndarray) -> tuple[float, float, float]:
    """Simple OLS y = α + β·x + ε. Returns (β, α, R²)."""
    n = len(x)
    if n < 30:
        return float("nan"), float("nan"), 0.0
    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    sx2 = float(np.sum((x - x_mean) ** 2))
    if sx2 <= 1e-12:
        return float("nan"), float("nan"), 0.0
    beta = float(np.sum((x - x_mean) * (y - y_mean)) / sx2)
    alpha = y_mean - beta * x_mean
    y_hat = alpha + beta * x
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - y_mean) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    return beta, alpha, r2


def _ar1_phi(r: np.ndarray) -> float:
    """AR(1) coefficient of residual series: r_t = φ · r_{t-1} + ε."""
    if len(r) < 30:
        return float("nan")
    r_lag = r[:-1]
    r_now = r[1:]
    denom = float(np.sum(r_lag * r_lag))
    if denom <= 1e-12:
        return float("nan")
    return float(np.sum(r_lag * r_now) / denom)


def _analyze_pair(leg_a: str, leg_b: str, s_a: dict, s_b: dict) -> PairStats | None:
    """Align timestamps, fit OLS on log prices, compute AR(1) half-life."""
    common = sorted(set(s_a.keys()) & set(s_b.keys()))
    if len(common) < MIN_SAMPLES:
        return None
    px_a = np.array([s_a[t] for t in common], dtype=float)
    px_b = np.array([s_b[t] for t in common], dtype=float)
    if (px_a <= 0).any() or (px_b <= 0).any():
        return None

    la = np.log(px_a)
    lb = np.log(px_b)
    beta, alpha, r2 = _ols(la, lb)
    if not np.isfinite(beta) or not np.isfinite(alpha):
        return None
    if not (BETA_MIN <= abs(beta) <= BETA_MAX):
        return None
    if r2 < R2_MIN:
        return None

    resid = la - (alpha + beta * lb)
    phi = _ar1_phi(resid)
    if not np.isfinite(phi) or not (0.0 < phi < 1.0):
        return None

    half_life_samples = -np.log(2.0) / np.log(phi)
    half_life_min = float(half_life_samples)  # 1 sample = 1 min
    if not (HL_MIN_MIN <= half_life_min <= HL_MAX_MIN):
        return None

    spread_mean = float(np.mean(resid))
    spread_sigma = float(np.std(resid, ddof=0))
    # Residual is in log-price units → σ·1e4 ≈ bps (for small moves).
    spread_sigma_bps = spread_sigma * 1e4
    if spread_sigma_bps < SPREAD_SIGMA_MIN_BPS:
        return None

    # Score: prefer high σ (more PnL per revert) + short half-life (more reverts/day).
    # Penalize extreme β (less dollar-efficient hedging).
    score = (
        float(np.log(spread_sigma_bps))
        - 0.5 * float(np.log(half_life_min / 60.0))
        - 0.1 * abs(float(np.log(abs(beta))))
    )
    _ = spread_mean  # kept in scope for future funding-cost enrichment

    return PairStats(
        leg_a=leg_a,
        leg_b=leg_b,
        beta=round(beta, 4),
        alpha=round(alpha, 4),
        half_life_min=round(half_life_min, 2),
        phi=round(phi, 6),
        r2=round(r2, 4),
        spread_sigma_bps=round(spread_sigma_bps, 2),
        score=round(score, 4),
        samples=len(common),
    )


# ── Orchestration ────────────────────────────────────────────────────────────
def discover(candidates: list[str], lookback_hours: int, top_n: int) -> dict:
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    url = getattr(
        constants,
        "MAINNET_API_URL",
        getattr(constants, "MAINNET", "https://api.hyperliquid.xyz"),
    )

    # Register every used builder DEX with the Info client so it can resolve
    # HIP-3 coins. Native perps work without perp_dexs.
    used_dexs = sorted({c.split(":", 1)[0] for c in candidates if ":" in c})
    info_kwargs = {"skip_ws": True}
    if used_dexs:
        info_kwargs["perp_dexs"] = [""] + used_dexs
    info = Info(url, **info_kwargs)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - lookback_hours * 3600 * 1000

    print(
        f"[discover] pulling {lookback_hours}h of 1m candles for {len(candidates)} coins"
    )
    series: dict[str, dict[int, float]] = {}
    for coin in candidates:
        candles = _fetch_candles(info, coin, start_ms, end_ms)
        s = _candles_to_series(candles)
        print(f"  {coin:>14s}: {len(s):>4d} bars")
        if len(s) >= MIN_SAMPLES:
            series[coin] = s
        # Be polite; HL rate limit is generous but not infinite.
        time.sleep(0.15)

    # Test every ordered pair (leg_a ≠ leg_b). Order matters because
    # spread = log(A) − β·log(B) is asymmetric in A and B.
    results: list[PairStats] = []
    coins = sorted(series.keys())
    tested = 0
    for a in coins:
        for b in coins:
            if a == b:
                continue
            tested += 1
            st = _analyze_pair(a, b, series[a], series[b])
            if st is not None:
                results.append(st)

    # Deduplicate asymmetric variants by keeping whichever direction scores higher.
    best: dict[tuple[str, str], PairStats] = {}
    for r in results:
        key = tuple(sorted([r.leg_a, r.leg_b]))
        if key not in best or r.score > best[key].score:
            best[key] = r
    survivors = sorted(best.values(), key=lambda p: -p.score)

    # Hub-cap: greedy pick so no single symbol appears in > MAX_PAIRS_PER_SYMBOL.
    # Prevents one noisy symbol from dominating the whitelist as sole hub.
    used: dict[str, int] = {}
    diversified: list[PairStats] = []
    for p in survivors:
        if len(diversified) >= top_n:
            break
        if (used.get(p.leg_a, 0) >= MAX_PAIRS_PER_SYMBOL
                or used.get(p.leg_b, 0) >= MAX_PAIRS_PER_SYMBOL):
            continue
        diversified.append(p)
        used[p.leg_a] = used.get(p.leg_a, 0) + 1
        used[p.leg_b] = used.get(p.leg_b, 0) + 1

    print(
        f"[discover] tested {tested} ordered pairs → "
        f"{len(results)} passed gates → {len(survivors)} unique → "
        f"keeping top {len(diversified)} after hub-cap (≤{MAX_PAIRS_PER_SYMBOL}/symbol)"
    )

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lookback_hours": lookback_hours,
        "universe": candidates,
        "pairs": [asdict(p) for p in diversified],
    }


def _write_summary(result: dict) -> None:
    SUMMARY_PATH.parent.mkdir(exist_ok=True)
    lines = [
        "# Pairs Discovery — Cointegration Scan",
        "",
        f"**Generated:** {result['generated_at']}  |  "
        f"**Lookback:** {result['lookback_hours']}h  |  "
        f"**Universe:** {len(result['universe'])} coins  |  "
        f"**Survivors:** {len(result['pairs'])}",
        "",
        f"Gates: `β∈[0.15, 4.0]`, `R²≥{R2_MIN:.2f}`, "
        f"`half-life∈[{HL_MIN_MIN}min, {HL_MAX_MIN // 60}h]`, "
        f"`σ_spread≥{SPREAD_SIGMA_MIN_BPS}bps`, `φ<1`",
        "",
        "| # | Pair | β | R² | Half-life (min) | φ | σ (bps) | Score | N |",
        "|---|------|---|----|------|---|---------|-------|---|",
    ]
    for i, p in enumerate(result["pairs"], 1):
        lines.append(
            f"| {i} | `{p['leg_a']}` vs `{p['leg_b']}` | "
            f"{p['beta']:.3f} | {p['r2']:.3f} | "
            f"{p['half_life_min']:.1f} | {p['phi']:.4f} | "
            f"{p['spread_sigma_bps']:.1f} | {p['score']:.3f} | {p['samples']} |"
        )
    SUMMARY_PATH.write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="HL pairs cointegration discovery")
    ap.add_argument(
        "--candidates",
        default=None,
        help="Comma-separated coin list (dex:NAME for HIP-3). "
        f"Default: built-in {len(DEFAULT_CANDIDATES)}-coin universe.",
    )
    ap.add_argument("--lookback-hours", type=int, default=LOOKBACK_HOURS_DEFAULT)
    ap.add_argument("--top-n", type=int, default=TOP_N_DEFAULT)
    ap.add_argument(
        "--out",
        default=str(WHITELIST_PATH),
        help=f"Output path (default: {WHITELIST_PATH.relative_to(ROOT)})",
    )
    args = ap.parse_args()

    # Load .env if present so HL_WALLET_ADDRESS etc. are available to SDK.
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    if args.candidates:
        candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
    else:
        candidates = list(DEFAULT_CANDIDATES)
        # Let operator extend via env without editing source.
        extra = os.environ.get("HL_PAIRS_EXTRA_CANDIDATES", "")
        for c in extra.split(","):
            c = c.strip()
            if c and c not in candidates:
                candidates.append(c)

    result = discover(candidates, args.lookback_hours, args.top_n)
    out_path = Path(args.out)
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2) + "\n")
    _write_summary(result)

    print(f"[discover] wrote {out_path}")
    print(f"[discover] wrote {SUMMARY_PATH}")
    for p in result["pairs"][:5]:
        print(
            f"  {p['leg_a']:>12s} vs {p['leg_b']:<10s} "
            f"β={p['beta']:+.3f} hl={p['half_life_min']:6.1f}min "
            f"σ={p['spread_sigma_bps']:5.1f}bps score={p['score']:+.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
