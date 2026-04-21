#!/usr/bin/env python3
"""
feedback_loop.py — single iteration of the 16h feedback loop.

Runs screener + mark_to_market + analyzer, diffs against current HIP3_UNIVERSE,
appends one JSONL line + rewrites the human dashboard, emits macOS notification
on alerts. Invoked on a schedule by scripts/feedback_loop.sh.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/Users/aurascoper/Developer/live_trading")
LOG_JSONL = ROOT / "logs" / "feedback_loop.jsonl"
DASHBOARD = ROOT / "logs" / "feedback_dashboard.md"
SUGGESTIONS = ROOT / "logs" / "feedback_suggestions.md"
PY = ROOT / "venv" / "bin" / "python"


def run(cmd: list[str], timeout: int = 300) -> str:
    try:
        r = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return f"TIMEOUT after {timeout}s"
    except Exception as e:
        return f"ERROR: {e}"


def parse_nav(mtm_out: str) -> dict:
    nav = {
        "nav": 0.0,
        "free": 0.0,
        "hold": 0.0,
        "positions": 0,
        "phantoms": 0,
        "upnl": 0.0,
    }
    m = re.search(r"portfolio NAV\s+\$\s*([\d.]+)", mtm_out)
    if m:
        nav["nav"] = float(m.group(1))
    m = re.search(r"spot USDC free\s+\$\s*([\d.]+)", mtm_out)
    if m:
        nav["free"] = float(m.group(1))
    m = re.search(r"spot USDC hold\s+\$\s*([\d.]+)", mtm_out)
    if m:
        nav["hold"] = float(m.group(1))
    m = re.search(r"TOTAL UNREALIZED\s+\$([+-]?[\d.]+)", mtm_out)
    if m:
        nav["upnl"] = float(m.group(1))
    phantom_section = re.search(
        r"Phantom entries.*?:\n(.*?)(?:\n\n|\Z)", mtm_out, re.DOTALL
    )
    if phantom_section:
        nav["phantoms"] = len(
            [
                ln
                for ln in phantom_section.group(1).splitlines()
                if ln.strip().startswith(
                    (
                        "BTC",
                        "ETH",
                        "AAVE",
                        "xyz:",
                        "para:",
                        "ZEC",
                        "SOL",
                        "XRP",
                        "DOGE",
                        "LINK",
                        "UNI",
                    )
                )
            ]
        )
    nav["positions"] = len(re.findall(r"szi=\s*[+-][\d.]+\s+entry=", mtm_out))
    return nav


def parse_pnl(an_out: str) -> dict:
    pnl = {"gross": 0.0, "fees": 0.0, "net": 0.0, "wins": 0, "losses": 0}
    m = re.search(r"gross\s*:\s*\$([+-]?[\d.]+)", an_out)
    if m:
        pnl["gross"] = float(m.group(1))
    m = re.search(r"fees\s*:\s*\$([+-]?[\d.]+)", an_out)
    if m:
        pnl["fees"] = float(m.group(1))
    m = re.search(r"NET\s*:\s*\$([+-]?[\d.]+)", an_out)
    if m:
        pnl["net"] = float(m.group(1))
    m = re.search(r"(\d+)W\s*/\s*(\d+)L", an_out)
    if m:
        pnl["wins"] = int(m.group(1))
        pnl["losses"] = int(m.group(2))
    return pnl


def get_current_universe() -> set[str]:
    env_sh = (ROOT / "env.sh").read_text()
    m = re.search(r'HIP3_UNIVERSE=([^\n"]+)"?', env_sh)
    if not m:
        return set()
    return {s.strip() for s in m.group(1).strip('"').split(",") if s.strip()}


def run_screener() -> list[dict]:
    out = run(
        [
            str(PY),
            "screener_hip3.py",
            "--dex",
            "xyz,para,hyna,flx,vntl,km,cash",
            "--min-oi",
            "1",
            "--min-vol",
            "0.5",
            "--top",
            "40",
            "--json",
        ],
        timeout=180,
    )
    # Extract JSON array — screener emits a DEX-list header like
    # "DEXs: ['xyz', ...]" before the real JSON, so find("[") is wrong.
    # Locate the array by its own-line "[" marker instead.
    idx = out.find("\n[\n")
    if idx == -1:
        return []
    end = out.rfind("]")
    if end == -1:
        return []
    try:
        return json.loads(out[idx + 1 : end + 1])
    except json.JSONDecodeError:
        return []


def run_alpaca_screener() -> dict:
    """R3k/SP500/NDX z-score screener as cross-venue check against HIP-3."""
    out = run([str(PY), "screener.py", "--min-z", "1.5", "--json"], timeout=240)
    idx = out.find("\n{")
    if idx == -1:
        return {"longs": [], "shorts": []}
    end = out.rfind("}")
    if end == -1:
        return {"longs": [], "shorts": []}
    try:
        return json.loads(out[idx + 1 : end + 1])
    except json.JSONDecodeError:
        return {"longs": [], "shorts": []}


def cross_venue_hits(alpaca: dict, hip3_candidates: list[dict]) -> list[dict]:
    """Alpaca-flagged names that also appear as HIP-3 perps, in either direction."""
    hip3_bases = {c["coin"].split(":")[-1]: c for c in hip3_candidates}
    hits = []
    for side, rows in (("long", alpaca.get("longs", [])), ("short", alpaca.get("shorts", []))):
        for r in rows:
            sym = r["symbol"]
            if sym in hip3_bases:
                hits.append({
                    "symbol": sym,
                    "alpaca_side": side,
                    "alpaca_z": round(r["z"], 3),
                    "hip3_coin": hip3_bases[sym]["coin"],
                    "hip3_composite": round(hip3_bases[sym]["composite_score"], 3),
                })
    return hits


def notify(title: str, body: str) -> None:
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{body}" with title "{title}"',
            ],
            timeout=5,
        )
    except Exception:
        pass


def main() -> int:
    iteration = int(os.environ.get("LOOP_ITER", "0"))
    ts = datetime.now(timezone.utc).isoformat()

    # 1. mark-to-market
    mtm = run([str(PY), "mark_to_market.py"], timeout=120)
    nav = parse_nav(mtm)

    # 2. analyzer
    an = run([str(PY), "analyze_session.py"], timeout=120)
    pnl = parse_pnl(an)

    # 3. screener (HIP-3 primary + Alpaca R3k cross-check)
    candidates = run_screener()
    current_uni = get_current_universe()
    new_candidates = [
        c
        for c in candidates
        if c["coin"] not in current_uni and c["composite_score"] >= 0.55
    ]
    alpaca_screen = run_alpaca_screener()
    cross_hits = cross_venue_hits(alpaca_screen, candidates)

    # Alerts
    alerts = []
    if nav["phantoms"] > 0:
        alerts.append(f"{nav['phantoms']} phantom positions")
    if cross_hits:
        alerts.append(f"{len(cross_hits)} cross-venue hits")

    # Prev iteration delta
    prev_net = None
    if LOG_JSONL.exists():
        try:
            lines = LOG_JSONL.read_text().strip().splitlines()
            if lines:
                prev = json.loads(lines[-1])
                prev_net = prev.get("pnl", {}).get("net")
        except Exception:
            pass
    net_delta = (pnl["net"] - prev_net) if prev_net is not None else None
    if net_delta is not None and abs(net_delta) >= 50:
        alerts.append(f"PnL Δ ${net_delta:+.0f}")
    if new_candidates:
        alerts.append(f"{len(new_candidates)} new screener candidates")

    # JSONL entry
    entry = {
        "type": "iter",
        "iteration": iteration,
        "timestamp": ts,
        "nav": nav,
        "pnl": pnl,
        "new_candidates": [c["coin"] for c in new_candidates[:10]],
        "cross_venue_hits": cross_hits[:10],
        "alerts": alerts,
    }
    LOG_JSONL.parent.mkdir(exist_ok=True)
    with LOG_JSONL.open("a") as f:
        f.write(json.dumps(entry) + "\n")

    # Suggestions append-only
    if new_candidates:
        with SUGGESTIONS.open("a") as f:
            f.write(f"\n## {ts} — iter {iteration}\n")
            for c in new_candidates[:10]:
                f.write(
                    f"- `{c['coin']}` composite={c['composite_score']:.3f} "
                    f"rmsd={c['rmsd_pct']:.2f}% lev={c['leverage']}x "
                    f"z_entry={c['z_entry']}\n"
                )

    # Dashboard (overwrite each iter — user reads the live view)
    lines_all = LOG_JSONL.read_text().strip().splitlines()
    recent = [json.loads(ln) for ln in lines_all[-16:]]
    dash = [
        "# Feedback Loop Dashboard",
        "",
        f"**Last update:** {ts}  |  **Iteration:** {iteration}",
        f"**NAV:** ${nav['nav']:.2f}  |  free ${nav['free']:.2f}  |  hold ${nav['hold']:.2f}  |  upnl ${nav['upnl']:+.2f}",
        f"**PnL (log-based):** net ${pnl['net']:+.2f}  gross ${pnl['gross']:+.2f}  fees ${pnl['fees']:.2f}  {pnl['wins']}W/{pnl['losses']}L",
        f"**Positions on-chain:** {nav['positions']}  |  **Phantoms:** {nav['phantoms']}",
        "",
        "## Alerts this iter",
    ]
    dash.append("- " + "\n- ".join(alerts) if alerts else "- (none)")
    dash += [
        "",
        "## Recent iterations",
        "",
        "| # | UTC | NAV | Net PnL | Upnl | Phantoms | Alerts |",
        "|---|-----|-----|---------|------|----------|--------|",
    ]
    for e in recent:
        dash.append(
            f"| {e['iteration']} | {e['timestamp'][11:19]} "
            f"| ${e['nav']['nav']:.0f} | ${e['pnl']['net']:+.1f} "
            f"| ${e['nav']['upnl']:+.1f} | {e['nav']['phantoms']} "
            f"| {', '.join(e['alerts']) if e['alerts'] else '—'} |"
        )
    dash += [
        "",
        "## Top new candidates (not in HIP3_UNIVERSE)",
        "",
    ]
    if new_candidates:
        for c in new_candidates[:10]:
            dash.append(
                f"- **{c['coin']}** composite={c['composite_score']:.3f} "
                f"rmsd={c['rmsd_pct']:.2f}% lev={c['leverage']}x"
            )
    else:
        dash.append("- (none above composite 0.55)")

    DASHBOARD.write_text("\n".join(dash) + "\n")

    # macOS notification (only if alerts)
    if alerts:
        notify("Trading Feedback Loop", " | ".join(alerts))

    print(
        f"iter {iteration}: NAV=${nav['nav']:.2f} net=${pnl['net']:+.2f} alerts={len(alerts)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
