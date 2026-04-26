#!/usr/bin/env python3
"""Re-baseline the residual map UNDER the accepted bucketed_3600 flag.

The previous residual decomposition (scripts/diagnose_replay_hl_residual.py)
ran against the no-cooldown baseline. Now that bucketed_3600 is the
accepted flagged candidate, the residual landscape has shifted:
xyz: equity churn is largely suppressed, leaving the structurally-harder
buckets exposed.

Output one row per shared symbol:
  symbol
  bucket_label              hip3_equity / longhold_native / churn_native_exception / other
  hl_closed_pnl
  replay_pnl_under_bucketed
  residual
  abs_residual_rank
  n_replay_opens
  n_live_sessions
  mean_replay_hold_h
  mean_live_session_h
  ratchet_count
  auto_topup_count
  manual_close_script_evidence    nearby close_*.py invocation timestamps
  exit_reason_distribution        replay-side per symbol

Then group totals to identify the next concrete lever.
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
LOG = ROOT / "logs" / "hl_engine.jsonl"
RATCHET_LOG = ROOT / "logs" / "shock_ratchet.log"
TOPUP_LOG = Path("/tmp/auto_topup.log")
COOLDOWN_CFG = ROOT / "config" / "gates" / "reentry_cooldown_by_symbol.json"

sys.path.insert(0, str(ROOT))
from scripts.validate_replay_fit import parse_hl_closed_pnl  # noqa: E402


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def load_buckets(path: Path) -> dict[str, str]:
    cfg = json.loads(path.read_text())
    out: dict[str, str] = {}
    for bucket_name, grp in (cfg.get("groups") or {}).items():
        for sym in grp.get("symbols") or []:
            out[_norm(sym)] = bucket_name
    return out


def run_replay_under_bucketed(window_days: int):
    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - window_days * 86_400_000
    env = os.environ.copy()
    env["REPLAY_FROM_MS"] = str(from_ms)
    env["REPLAY_TO_MS"] = str(to_ms)
    env["REENTRY_COOLDOWN_BY_SYMBOL"] = str(COOLDOWN_CFG)
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tmp_p:
        env["REPLAY_PERSYM_OUT"] = tmp_p.name
        pnl_path = tmp_p.name
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tmp_t:
        env["REPLAY_TRADES_OUT"] = tmp_t.name
        trades_path = tmp_t.name
    cmd = [sys.executable, str(ROOT / "scripts" / "z_entry_replay_gated.py")]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print(r.stderr[-1000:], file=sys.stderr)
        raise SystemExit(f"replay failed rc={r.returncode}")
    pnl = json.loads(Path(pnl_path).read_text())
    trades_by_sym: dict[str, list[dict]] = defaultdict(list)
    with open(trades_path) as fh:
        for line in fh:
            try:
                t = json.loads(line)
            except Exception:
                continue
            sym = _norm(t.get("symbol", ""))
            if sym:
                trades_by_sym[sym].append(t)
    Path(pnl_path).unlink(missing_ok=True)
    Path(trades_path).unlink(missing_ok=True)
    return pnl, dict(trades_by_sym), from_ms, to_ms


def live_session_stats(from_ms: int, to_ms: int):
    """Per-symbol (n_sessions, mean_session_s) from hl_fill_received."""
    fills_by_sym: dict[str, list[tuple[int, str, float]]] = defaultdict(list)
    with LOG.open() as fh:
        for line in fh:
            if '"hl_fill_received"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("event") != "hl_fill_received":
                continue
            ts_raw = o.get("timestamp", "")
            try:
                if isinstance(ts_raw, str):
                    s = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
                    ts_ms = int(dt.datetime.fromisoformat(s).timestamp() * 1000)
                else:
                    ts_ms = int(ts_raw * 1000) if ts_raw < 1e12 else int(ts_raw)
            except Exception:
                continue
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            if not sym:
                continue
            try:
                sz = float(o.get("sz", 0) or 0)
                side = (o.get("side") or "").lower()
            except (TypeError, ValueError):
                continue
            if sz <= 0 or side not in ("buy", "sell"):
                continue
            fills_by_sym[sym].append((ts_ms, side, sz))

    out: dict[str, dict] = {}
    for sym, fills in fills_by_sym.items():
        fills.sort()
        pos = 0.0
        sessions = []
        cur_open = None
        for ts_ms, side, sz in fills:
            d = sz if side == "buy" else -sz
            new_pos = pos + d
            if abs(pos) < 1e-9 and abs(new_pos) > 1e-9 and from_ms <= ts_ms < to_ms:
                cur_open = ts_ms
            if abs(pos) > 1e-9 and abs(new_pos) < 1e-9 and cur_open is not None:
                sessions.append((cur_open, ts_ms))
                cur_open = None
            pos = new_pos
        if cur_open is not None:
            sessions.append((cur_open, to_ms))
        n_sess = len(sessions)
        mean_s = sum((c - o) / 1000 for o, c in sessions) / n_sess if n_sess > 0 else 0.0
        out[sym] = {"n_sessions": n_sess, "mean_session_s": mean_s}
    return out


def count_ratchet(from_ms: int, to_ms: int) -> dict[str, int]:
    cnt = defaultdict(int)
    if not RATCHET_LOG.exists():
        return dict(cnt)
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
                        dt.datetime.fromisoformat(m_ts.group(1).replace(" ", "T") + "+00:00").timestamp() * 1000
                    )
                except Exception:
                    ts_ms = 0
                if ts_ms < from_ms or ts_ms >= to_ms:
                    continue
            m_sym = sym_re.search(line)
            if m_sym:
                cnt[_norm(m_sym.group(1))] += 1
    return dict(cnt)


def count_topup(from_ms: int, to_ms: int) -> dict[str, int]:
    cnt = defaultdict(int)
    if not TOPUP_LOG.exists():
        return dict(cnt)
    fire_re = re.compile(r"^(\S+)\s+FIRE\s+([A-Za-z0-9:_/]+)\s")
    with TOPUP_LOG.open() as fh:
        for line in fh:
            m = fire_re.match(line)
            if not m:
                continue
            try:
                ts_ms = int(
                    dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp() * 1000
                )
            except Exception:
                continue
            if ts_ms < from_ms or ts_ms >= to_ms:
                continue
            cnt[_norm(m.group(2))] += 1
    return dict(cnt)


def manual_close_script_evidence() -> dict[str, list[str]]:
    """Crude: look for close_*.py files in repo root and infer target sym."""
    out: dict[str, list[str]] = defaultdict(list)
    for f in ROOT.glob("close_*.py"):
        # close_cl_half.py → xyz:CL ; close_mstr.py → xyz:MSTR ; close_sui.py → SUI
        name = f.stem.replace("close_", "")
        name = name.replace("_half", "")
        upper = name.upper()
        # heuristic mapping
        candidates = {upper, f"xyz:{upper}"}
        for c in candidates:
            out[c].append(f.name)
    return dict(out)


def main():
    print("# residual decomposition under bucketed_3600 flag", file=sys.stderr)
    pnl, trades_by_sym, from_ms, to_ms = run_replay_under_bucketed(14)
    hl_pnl, _per_day, _hl_fees = parse_hl_closed_pnl(from_ms, to_ms)
    buckets = load_buckets(COOLDOWN_CFG)
    live_stats = live_session_stats(from_ms, to_ms)
    ratchet = count_ratchet(from_ms, to_ms)
    topup = count_topup(from_ms, to_ms)
    manual_evidence = manual_close_script_evidence()

    shared = sorted(set(pnl) & set(hl_pnl))
    rows = []
    for s in shared:
        ts = trades_by_sym.get(s, [])
        n_replay = len(ts)
        replay_holds = [(t["exit_ts"] - t["entry_ts"]) / 3_600_000 for t in ts]
        mean_replay_hold_h = sum(replay_holds) / n_replay if n_replay > 0 else 0.0
        ls = live_stats.get(s, {"n_sessions": 0, "mean_session_s": 0.0})
        exit_reasons = defaultdict(int)
        for t in ts:
            exit_reasons[t.get("reason", "?")] += 1
        rows.append({
            "sym": s,
            "bucket": buckets.get(s, "other"),
            "hl": hl_pnl[s],
            "sim": pnl[s],
            "residual": hl_pnl[s] - pnl[s],
            "abs_residual": abs(hl_pnl[s] - pnl[s]),
            "n_replay_opens": n_replay,
            "n_live_sessions": ls["n_sessions"],
            "mean_replay_hold_h": mean_replay_hold_h,
            "mean_live_session_h": ls["mean_session_s"] / 3600,
            "ratchet_count": ratchet.get(s, 0),
            "auto_topup_count": topup.get(s, 0),
            "manual_close_evidence": manual_evidence.get(s, []),
            "exit_reasons": dict(exit_reasons),
        })
    rows.sort(key=lambda r: -r["abs_residual"])
    for i, r in enumerate(rows):
        r["abs_residual_rank"] = i + 1

    # Bucket totals
    bucket_abs = defaultdict(float)
    bucket_n = defaultdict(int)
    for r in rows:
        bucket_abs[r["bucket"]] += r["abs_residual"]
        bucket_n[r["bucket"]] += 1
    total_abs = sum(bucket_abs.values())

    print()
    print("=== bucket residual breakdown (under bucketed_3600) ===")
    print(f"  {'bucket':32s}  {'n':>3s}  {'abs|res|':>10s}  {'%':>5s}")
    for b in sorted(bucket_abs, key=lambda b: -bucket_abs[b]):
        pct = (bucket_abs[b] / total_abs * 100) if total_abs > 0 else 0
        print(f"  {b:32s}  {bucket_n[b]:>3d}  ${bucket_abs[b]:>9.2f}  {pct:>4.1f}%")
    print(f"  {'TOTAL':32s}  {sum(bucket_n.values()):>3d}  ${total_abs:>9.2f}")

    print()
    print("=== top-20 |residual| under bucketed_3600 ===")
    print(
        f"  {'#':>2s}  {'sym':<14s}  {'bucket':<26s}  "
        f"{'|res|':>7s}  {'rOpens':>6s}  {'lSess':>5s}  "
        f"{'rHold h':>7s}  {'lSess h':>8s}  {'rat':>3s}  {'top':>3s}  "
        f"{'manual?':>7s}  exits"
    )
    for r in rows[:20]:
        manual = "Y" if r["manual_close_evidence"] else "n"
        exits_compact = "+".join(
            f"{k[:4]}={v}" for k, v in sorted(r["exit_reasons"].items(), key=lambda kv: -kv[1])[:3]
        )
        print(
            f"  {r['abs_residual_rank']:>2d}  {r['sym']:<14s}  {r['bucket']:<26s}  "
            f"${r['abs_residual']:>6.0f}  {r['n_replay_opens']:>6d}  {r['n_live_sessions']:>5d}  "
            f"{r['mean_replay_hold_h']:>6.2f}  {r['mean_live_session_h']:>7.2f}  "
            f"{r['ratchet_count']:>3d}  {r['auto_topup_count']:>3d}  "
            f"{manual:>7s}  {exits_compact}"
        )

    out = ROOT / "autoresearch_gated" / "residual_after_bucketed_cooldown.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "buckets": dict(bucket_abs),
        "rows": rows,
    }, indent=2, default=str))
    print(f"\n# wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
