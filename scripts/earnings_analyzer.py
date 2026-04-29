#!/usr/bin/env python3
"""
scripts/earnings_analyzer.py — eligibility scorer for next-day earnings names.

Reads a name-list JSON file (default: config/earnings/<date>.json), maps each
name to a tradable venue symbol in our HL native + HIP-3 universe, and emits
one structured `earnings_eligibility` event per name with decision in
{allow, warn, block} plus reasons.

Default mode: REPORT-ONLY. The analyzer never mutates engine state, never
submits orders, never edits the live universe. It only reads and emits events.

Universe-addition behavior is gated by config/earnings_addition.json:
  {"enabled": false, ...}    → analyzer marks would_add=false on every name
  {"enabled": true, "mode": "shadow", ...} → would_add=true on allow tier;
                                              still no engine state change
                                              tonight (engine integration is
                                              a separate authorized PR)
  {"mode": "live"}           → reserved; engine-side hook is NOT yet wired

Default on ambiguity: BLOCK.

Schema of input file (config/earnings/<date>.json):
  {
    "date": "YYYY-MM-DD",
    "schema_version": 1,
    "names": [
      {"company": "Apple Inc",   "ticker": "AAPL", "session": "after_close"},
      {"company": "Some Co",     "ticker": "XYZ",  "session": "before_open"},
      ...
    ]
  }

  - `session` is informational ("before_open" | "after_close" | "during" | "unknown").
  - Only `ticker` is used for venue mapping; `company` is carried through for
    auditability.

Schema of emitted event (per name):
  {
    "event": "earnings_eligibility",
    "ts": "<UTC ISO>",
    "date": "<YYYY-MM-DD>",
    "company": "...",
    "source_ticker": "...",
    "ticker": "<UPPER>",
    "venue_symbol": "xyz:AAPL" | null,
    "session": "...",
    "decision": "allow" | "warn" | "block",
    "reasons": ["..."],
    "would_add": bool,
    "add_reason": "...",
    "addition_config": {"enabled": ..., "mode": ..., "allow_warn_tier": ...}
  }

CLI:
  scripts/earnings_analyzer.py --date 2026-04-29
  scripts/earnings_analyzer.py --date 2026-04-29 --out logs/earnings_2026-04-29.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Venue maps. Sourced from scripts/relaunch_hl_engine_stage2_full_LIVE.sh
# (HIP3_UNIVERSE) and the canonical HL_UNIVERSE list.
#
# Keep these in sync by hand for now — a future PR can derive them from the
# launch script or settings module. Manual is fine tonight; this is observation.
# ---------------------------------------------------------------------------

HIP3_XYZ_TRADABLE: frozenset[str] = frozenset(
    {
        "HIMS",
        "HOOD",
        "ORCL",
        "EWY",
        "XYZ100",
        "CRWV",
        "TSLA",
        "CL",
        "SNDK",
        "SKHX",
        "MSFT",
        "MU",
        "SP500",
        "AMD",
        "PLTR",
        "BRENTOIL",
        "GOLD",
        "SILVER",
        "NATGAS",
        "COPPER",
        "PLATINUM",
        "TSM",
        "GOOGL",
        "META",
        "AAPL",
        "LLY",
        "NFLX",
        "COST",
        "BABA",
        "RKLB",
        "MRVL",
        "EUR",
        "JP225",
        "XLE",
        "PALLADIUM",
        "URANIUM",
        "RIVN",
        "MSTR",
    }
)

HIP3_OTHER_EQUITY: dict[str, str] = {
    "BMNR": "km",
    "USTECH": "km",
    "USOIL": "km",
    "SMALL2000": "km",
    "TENCENT": "km",
    "XIAOMI": "km",
    "RTX": "km",
    "KWEB": "cash",
    "WTI": "cash",
}

NATIVE_CRYPTO: frozenset[str] = frozenset(
    {
        "BTC",
        "ETH",
        "SOL",
        "AAVE",
        "XRP",
        "DOGE",
        "PAXG",
        "ARB",
        "CRV",
        "LINK",
        "ADA",
        "AVAX",
        "LTC",
        "BCH",
        "DOT",
        "UNI",
        "LDO",
        "POL",
        "RENDER",
        "FIL",
        "HYPE",
        "BNB",
        "SUI",
        "TAO",
        "NEAR",
        "ENA",
        "ZEC",
    }
)

EXCLUDED_TICKERS: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Mapping logic. Returns (venue_symbol_or_None, decision, reasons_list).
# Conservative: any ambiguity → block. No silent guesses.
# ---------------------------------------------------------------------------


def _normalize(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip().upper()


def map_to_venue(ticker: str) -> tuple[str | None, str, list[str]]:
    if not ticker:
        return None, "block", ["empty_ticker"]
    if ticker in EXCLUDED_TICKERS:
        return None, "block", ["explicit_exclusion"]
    if ticker in NATIVE_CRYPTO:
        return ticker, "block", ["native_crypto_no_earnings_concept"]
    if ticker in HIP3_XYZ_TRADABLE:
        return f"xyz:{ticker}", "allow", ["hip3_xyz_tradable"]
    if ticker in HIP3_OTHER_EQUITY:
        dex = HIP3_OTHER_EQUITY[ticker]
        return (
            f"{dex}:{ticker}",
            "warn",
            [f"hip3_{dex}_dex", "verify_liquidity_before_inclusion"],
        )
    return None, "block", ["ticker_not_in_configured_universe"]


def _decide_addition(decision: str, cfg: dict) -> tuple[bool, str]:
    if not cfg.get("enabled", False):
        return False, "feature_flag_disabled"
    mode = cfg.get("mode", "shadow")
    if mode not in ("shadow", "live"):
        return False, f"invalid_mode_{mode}"
    if decision == "allow":
        return True, f"allow_tier_mode_{mode}"
    if decision == "warn" and cfg.get("allow_warn_tier", False):
        return True, f"warn_tier_permitted_mode_{mode}"
    return False, f"decision_{decision}_not_eligible"


def analyze(payload: dict, addition_cfg: dict, date_str: str) -> list[dict]:
    out: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for entry in payload.get("names", []) or []:
        if not isinstance(entry, dict):
            out.append(
                {
                    "event": "earnings_eligibility",
                    "ts": now_iso,
                    "date": date_str,
                    "company": None,
                    "source_ticker": None,
                    "ticker": "",
                    "venue_symbol": None,
                    "session": "unknown",
                    "decision": "block",
                    "reasons": ["malformed_entry_not_object"],
                    "would_add": False,
                    "add_reason": "malformed_entry",
                    "addition_config": addition_cfg,
                }
            )
            continue
        company = entry.get("company") or entry.get("name")
        source_ticker = entry.get("ticker") or entry.get("symbol")
        ticker = _normalize(source_ticker)
        session = entry.get("session") or "unknown"
        venue_symbol, decision, reasons = map_to_venue(ticker)
        would_add, add_reason = _decide_addition(decision, addition_cfg)
        out.append(
            {
                "event": "earnings_eligibility",
                "ts": now_iso,
                "date": date_str,
                "company": company,
                "source_ticker": source_ticker,
                "ticker": ticker,
                "venue_symbol": venue_symbol,
                "session": session,
                "decision": decision,
                "reasons": reasons,
                "would_add": would_add,
                "add_reason": add_reason,
                "addition_config": {
                    "enabled": bool(addition_cfg.get("enabled", False)),
                    "mode": addition_cfg.get("mode", "shadow"),
                    "allow_warn_tier": bool(addition_cfg.get("allow_warn_tier", False)),
                },
            }
        )
    return out


def _summarize(decisions: list[dict], cfg: dict) -> dict:
    n = len(decisions)
    allow = sum(1 for d in decisions if d["decision"] == "allow")
    warn = sum(1 for d in decisions if d["decision"] == "warn")
    block = sum(1 for d in decisions if d["decision"] == "block")
    would_add = [d for d in decisions if d["would_add"]]
    block_reasons: dict[str, int] = {}
    for d in decisions:
        if d["decision"] == "block":
            for r in d["reasons"]:
                block_reasons[r] = block_reasons.get(r, 0) + 1
    return {
        "event": "earnings_readiness_summary",
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_total": n,
        "n_allow": allow,
        "n_warn": warn,
        "n_block": block,
        "n_would_add": len(would_add),
        "would_add_symbols": [d["venue_symbol"] for d in would_add],
        "block_reasons": block_reasons,
        "feature_flag_enabled": bool(cfg.get("enabled", False)),
        "addition_mode": cfg.get("mode", "shadow"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Earnings eligibility analyzer")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (input lookup)")
    ap.add_argument(
        "--names-file",
        default=None,
        help="Override input path (default: config/earnings/<date>.json)",
    )
    ap.add_argument(
        "--addition-config",
        default="config/earnings_addition.json",
        help="Feature-flag config path",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Append JSONL events to this path in addition to stdout",
    )
    ap.add_argument(
        "--summary-only",
        action="store_true",
        help="Print only the readiness summary, not per-name events",
    )
    args = ap.parse_args()

    names_path = Path(args.names_file or f"config/earnings/{args.date}.json")
    if not names_path.exists():
        print(
            f"earnings_analyzer: input file not found: {names_path}",
            file=sys.stderr,
        )
        return 2
    try:
        payload = json.loads(names_path.read_text())
    except json.JSONDecodeError as e:
        print(f"earnings_analyzer: invalid JSON in {names_path}: {e}", file=sys.stderr)
        return 2

    cfg_path = Path(args.addition_config)
    if cfg_path.exists():
        try:
            addition_cfg = json.loads(cfg_path.read_text())
        except json.JSONDecodeError as e:
            print(
                f"earnings_analyzer: invalid JSON in {cfg_path}: {e}",
                file=sys.stderr,
            )
            return 2
    else:
        addition_cfg = {"enabled": False, "mode": "shadow"}

    decisions = analyze(payload, addition_cfg, args.date)
    summary = _summarize(decisions, addition_cfg)

    out_lines = []
    if not args.summary_only:
        out_lines.extend(json.dumps(d) for d in decisions)
    out_lines.append(json.dumps(summary))
    body = "\n".join(out_lines)
    print(body)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a") as f:
            f.write(body + "\n")

    print(
        f"earnings_analyzer: n={summary['n_total']} "
        f"allow={summary['n_allow']} warn={summary['n_warn']} "
        f"block={summary['n_block']} would_add={summary['n_would_add']} "
        f"flag_enabled={summary['feature_flag_enabled']} "
        f"mode={summary['addition_mode']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
