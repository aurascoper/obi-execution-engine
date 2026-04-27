#!/usr/bin/env python3
"""Breakout watcher — observation-only. NO ORDER PATH.

Polls HL `info.all_mids()` on a short cadence and detects when an
allowlisted native symbol's mid crosses a configured level (in either
direction). Enriches each alert with the most recent z / obi / z_4h
from the engine's signal_tick log so an operator can decide whether
the cross is supported by mean-rev / momentum context.

Reads:
  config/breakout_levels.json    (level set, poll cadence, paths)
  logs/hl_engine.jsonl           (latest signal_tick context per sym)

Writes:
  logs/breakout_alerts.jsonl     (one event per crossing)
  stdout                          (human-readable)

This module DOES NOT:
  - place any orders
  - touch the engine's risk gates
  - touch signals.py
  - touch any production state

It is independent from hl_engine and can run in the same process or
a separate one. Default is a separate process via:

    venv/bin/python3 scripts/breakout_watcher.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import sys
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CFG = ROOT / "config" / "breakout_levels.json"


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def _parse_iso_ms(s: str) -> int:
    if not isinstance(s, str):
        return 0
    s = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0


def load_cfg(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    cfg["levels"] = {_norm(k): list(v) for k, v in (cfg.get("levels") or {}).items()}
    return cfg


def latest_signal_context(log_path: Path, syms: set[str], max_age_s: int) -> dict:
    """Walk the engine log backwards to grab the most recent signal_tick
    per symbol. Returns sym -> {ts_ms, z, obi, z_4h, in_position}.

    Uses tail-and-scan since jsonl can be large. We bound by age so
    stale context isn't attached to fresh crosses.
    """
    if not log_path.exists():
        return {}
    cutoff_ms = int(time.time() * 1000) - max_age_s * 1000
    found: dict[str, dict] = {}
    # Read last N MB only — bounded scan
    file_size = log_path.stat().st_size
    chunk = min(file_size, 32 * 1024 * 1024)  # last 32 MB max
    with log_path.open("rb") as fh:
        if file_size > chunk:
            fh.seek(file_size - chunk)
            fh.readline()  # discard partial
        for line in fh:
            try:
                line_s = line.decode("utf-8", errors="ignore")
            except Exception:
                continue
            if '"signal_tick"' not in line_s:
                continue
            try:
                o = json.loads(line_s)
            except Exception:
                continue
            if o.get("event") != "signal_tick":
                continue
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            if sym not in syms:
                continue
            ts_ms = _parse_iso_ms(o.get("timestamp", ""))
            if ts_ms < cutoff_ms:
                continue
            # Keep the latest per symbol (we may overwrite with a later one)
            cur = found.get(sym)
            if cur is None or ts_ms >= cur.get("ts_ms", 0):
                found[sym] = {
                    "ts_ms": ts_ms,
                    "z": o.get("z"),
                    "obi": o.get("obi"),
                    "z_4h": o.get("z_4h"),
                    "in_position": o.get("in_position"),
                }
    return found


def pause_new_entries_active() -> bool:
    """Best-effort: are any running engine processes setting PAUSE_NEW_ENTRIES=1?
    Not authoritative — used only to enrich the alert payload."""
    try:
        import subprocess
        out = subprocess.run(
            ["ps", "-eEw"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return "PAUSE_NEW_ENTRIES=1" in out
    except Exception:
        return False


def main():
    cfg_path = Path(os.environ.get("BREAKOUT_LEVELS", DEFAULT_CFG))
    if not cfg_path.exists():
        print(f"# config not found: {cfg_path}", file=sys.stderr)
        return 1
    cfg = load_cfg(cfg_path)
    levels: dict[str, list[float]] = cfg["levels"]
    poll_s = float(cfg.get("poll_interval_s", 5))
    alert_log = ROOT / cfg.get("alert_log_path", "logs/breakout_alerts.jsonl")
    engine_log = ROOT / cfg.get("engine_log_path", "logs/hl_engine.jsonl")
    max_age_s = int(cfg.get("max_signal_tick_age_s", 60))

    syms = set(levels.keys())
    if not syms:
        print("# no levels configured", file=sys.stderr)
        return 1

    addr = os.environ.get("HL_WALLET_ADDRESS")
    if not addr:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("HL_WALLET_ADDRESS="):
                    addr = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    alert_log.parent.mkdir(parents=True, exist_ok=True)
    alert_fh = alert_log.open("a", buffering=1)

    print(
        f"# breakout watcher started — poll {poll_s}s, "
        f"symbols={sorted(syms)}, levels={sum(len(v) for v in levels.values())} total",
        flush=True,
    )
    print(f"# alert log: {alert_log}", flush=True)

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    last_mid: dict[str, float] = {}
    n_alerts = 0
    while not stop["flag"]:
        try:
            mids = info.all_mids() or {}
        except Exception as e:
            print(f"# warn: all_mids failed: {e}", file=sys.stderr)
            time.sleep(poll_s)
            continue
        # Refresh signal context — once per poll. Cheap: bounded log scan.
        ctx_by_sym = latest_signal_context(engine_log, syms, max_age_s)
        crosses_this_tick: list[dict] = []
        for sym, level_list in levels.items():
            raw = mids.get(sym)
            if raw is None:
                continue
            try:
                mid = float(raw)
            except (TypeError, ValueError):
                continue
            prev = last_mid.get(sym)
            last_mid[sym] = mid
            if prev is None:
                continue
            for level in level_list:
                cross_up = prev < level <= mid
                cross_down = prev > level >= mid
                if not (cross_up or cross_down):
                    continue
                ctx = ctx_by_sym.get(sym, {})
                ev = {
                    "event": "breakout_level_crossed",
                    "ts": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
                    "symbol": sym,
                    "level": level,
                    "mid": mid,
                    "prev_mid": prev,
                    "direction": "cross_up" if cross_up else "cross_down",
                    "z": ctx.get("z"),
                    "obi": ctx.get("obi"),
                    "z_4h": ctx.get("z_4h"),
                    "in_position": ctx.get("in_position"),
                    "signal_age_s": (
                        round((time.time() * 1000 - ctx["ts_ms"]) / 1000, 1)
                        if "ts_ms" in ctx else None
                    ),
                    "pause_new_entries_active": pause_new_entries_active(),
                }
                alert_fh.write(json.dumps(ev) + "\n")
                crosses_this_tick.append(ev)
                n_alerts += 1
        for ev in crosses_this_tick:
            arrow = "↑" if ev["direction"] == "cross_up" else "↓"
            print(
                f"  [{ev['ts'][:19]}]  {arrow}  {ev['symbol']:6s}  "
                f"level=${ev['level']}  mid=${ev['mid']}  prev=${ev['prev_mid']}  "
                f"z={ev['z']}  obi={ev['obi']}  z_4h={ev['z_4h']}  "
                f"paused={ev['pause_new_entries_active']}",
                flush=True,
            )
        # Allow KeyboardInterrupt promptly
        for _ in range(int(poll_s * 10)):
            if stop["flag"]:
                break
            time.sleep(0.1)

    print(f"\n# breakout watcher stopped — emitted {n_alerts} alerts", flush=True)
    alert_fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
