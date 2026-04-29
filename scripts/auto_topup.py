#!/usr/bin/env python3
"""
Auto-topup daemon v3 — unified trigger + decaying margin.

Single fire condition (replaces v2's DIP/BREAKOUT split):
    z_4h >= Z4H_MIN  AND  obi >= OBI_MIN
Sign-agnostic on z; trend + liquidity drive timing. Cooldown + cap do the
rate-limiting. When trend reverses, z_4h flips negative and firing stops.

Margin schedule (linear decay, floor):
    margin_n = max(MARGIN_START - (n-1) * MARGIN_STEP, MARGIN_FLOOR)
    fires:   1   2   3   4   5   6   7   8   9   10
    $:       40  35  30  25  20  15  10  10  10  10

State recovery: parses this log at startup to recover fire_count and
last_fire per coin. Restart mid-run preserves decay + cooldown.
"""

import json
import os
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone

# --- config ---
WATCH_LONG = {"ZEC/USD"}
REGIME = os.environ.get("REGIME", "bull").lower()

# Unified trigger
Z4H_MIN = 0.30
OBI_MIN = 0.30

# Decaying margin
MARGIN_START = 40
MARGIN_STEP = 5
MARGIN_FLOOR = 10
LEV = 2

COOLDOWN_S = 10 * 60 if REGIME == "bull" else 20 * 60
_default_max = 10 if REGIME == "bull" else 4
MAX_FIRES = int(os.environ.get("MAX_FIRES", str(_default_max)))

CWD = "/Users/aurascoper/Developer/live_trading"
LOG = "/tmp/auto_topup.log"
LOGF = open(LOG, "a", buffering=1)

# Pause coordination with scripts/shock_ratchet.py: when ratchet recently
# sold a coin, we wait RATCHET_PAUSE_HOURS before re-entering. Breaks the
# "auto_topup builds stack -> ratchet sells stack -> auto_topup rebuilds" loop
# that has cost ~$3-4/day on ZEC across the last two days.
RATCHET_LOG = f"{CWD}/logs/shock_ratchet.log"
RATCHET_PAUSE_HOURS = float(os.environ.get("RATCHET_PAUSE_HOURS", "24"))
_RATCHET_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s.*hl_order_submitted.*symbol=(\S+).*tag=shock_ratchet_"
)
_RATCHET_CACHE_TTL_S = 60.0
_ratchet_cache: dict = {"ts": 0.0, "data": {}}
_pause_warned: set[str] = set()

last_fire = defaultdict(float)
fire_count = 0


def log(msg):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    LOGF.write(f"{ts} {msg}\n")
    print(f"{ts} {msg}", flush=True)


def margin_for_fire(n: int) -> int:
    """Fire count n (1-indexed) -> margin in USD."""
    return max(MARGIN_START - (n - 1) * MARGIN_STEP, MARGIN_FLOOR)


def recently_ratcheted_coins() -> dict[str, float]:
    """Return {coin: latest_ratchet_sell_ts_epoch} for shock_ratchet fills in
    the last RATCHET_PAUSE_HOURS. Cached for _RATCHET_CACHE_TTL_S to avoid
    re-scanning the log on every signal_tick.

    shock_ratchet.log timestamps are in local time (matches `time.time()`
    epoch when parsed via datetime.timestamp()).
    """
    now = time.time()
    if now - _ratchet_cache["ts"] < _RATCHET_CACHE_TTL_S:
        return _ratchet_cache["data"]
    cutoff = now - RATCHET_PAUSE_HOURS * 3600
    out: dict[str, float] = {}
    try:
        with open(RATCHET_LOG) as f:
            for line in f:
                m = _RATCHET_RE.search(line)
                if not m:
                    continue
                ts_str, coin = m.group(1), m.group(2)
                try:
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                if ts > out.get(coin, 0.0):
                    out[coin] = ts
    except OSError:
        pass
    _ratchet_cache["ts"] = now
    _ratchet_cache["data"] = out
    return out


def ratchet_pause_active(coin: str) -> tuple[bool, float | None]:
    """(is_paused, hours_since_last_ratchet) — pause active if the coin was
    sold by shock_ratchet within RATCHET_PAUSE_HOURS."""
    recent = recently_ratcheted_coins()
    ts = recent.get(coin)
    if ts is None:
        return (False, None)
    return (True, (time.time() - ts) / 3600.0)


def recover_state() -> None:
    """Seed fire_count + last_fire from existing FIRE log lines."""
    global fire_count
    try:
        with open(LOG) as f:
            lines = f.readlines()
    except OSError:
        return
    pat = re.compile(r"^(\S+) FIRE (\S+) ")
    counted = 0
    latest_ts_per_coin: dict[str, float] = {}
    for line in lines:
        m = pat.match(line)
        if not m:
            continue
        ts_str, coin = m.group(1), m.group(2)
        try:
            ts = (
                datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except ValueError:
            continue
        latest_ts_per_coin[coin] = ts
        counted += 1
    fire_count = counted
    for coin, ts in latest_ts_per_coin.items():
        last_fire[coin] = ts


def fire(coin, side, z, z4, obi):
    global fire_count
    if fire_count >= MAX_FIRES:
        log(f"CAP reached ({MAX_FIRES}) — skip {coin} {side}")
        return
    paused, hrs_ago = ratchet_pause_active(coin)
    if paused:
        # Log once per pause-window per coin to avoid spam (one line per
        # signal_tick would flood). _pause_warned is cleared when pause clears.
        if coin not in _pause_warned:
            remaining = RATCHET_PAUSE_HOURS - hrs_ago
            log(
                f"PAUSE {coin} {side} — ratchet sold {hrs_ago:.1f}h ago; "
                f"resumes in {remaining:.1f}h (RATCHET_PAUSE_HOURS={RATCHET_PAUSE_HOURS})"
            )
            _pause_warned.add(coin)
        return
    _pause_warned.discard(coin)  # pause cleared; allow re-warning if it returns
    if time.time() - last_fire[coin] < COOLDOWN_S:
        return
    last_fire[coin] = time.time()
    fire_count += 1
    margin = margin_for_fire(fire_count)
    log(
        f"FIRE {coin} {side} UNIFIED margin=${margin} lev={LEV}x  "
        f"z={z:+.2f} z_4h={z4:+.2f} obi={obi:+.3f}  count={fire_count}/{MAX_FIRES}"
    )
    cmd = (
        f"set -a; source .env 2>/dev/null; set +a; "
        f"export HL_WALLET_ADDRESS=0x32D178fc6BC4CCC7AFBDB7Db78317cF2Bbd6C048; "
        f"venv/bin/python3 scripts/manual_topup.py --symbol {coin} --margin {margin} "
        f"--side {side} --lev {LEV} --slippage 0.003 --tif Ioc --skip-news"
    )
    r = subprocess.run(
        ["bash", "-c", cmd], cwd=CWD, capture_output=True, text=True, timeout=30
    )
    tail = r.stdout.strip().split(chr(10))[-1][:200] if r.stdout else ""
    log(f"  result: {tail}")
    if r.returncode != 0:
        log(f"  STDERR: {r.stderr.strip()[:200]}")


def main():
    recover_state()
    next_m = margin_for_fire(fire_count + 1) if fire_count < MAX_FIRES else None
    # Show ratchet-paused coins on boot so we can see the coordination working.
    paused_now = recently_ratcheted_coins()
    paused_summary = (
        ",".join(
            f"{c}({(time.time() - ts) / 3600:.1f}h)" for c, ts in paused_now.items()
        )
        or "none"
    )
    log(
        f"auto_topup v3 started REGIME={REGIME} watching={','.join(WATCH_LONG)} "
        f"cap={MAX_FIRES} cooldown={COOLDOWN_S}s z4h_min={Z4H_MIN} obi_min={OBI_MIN} "
        f"recovered_count={fire_count} next_margin=${next_m} "
        f"ratchet_pause={RATCHET_PAUSE_HOURS}h paused_now={paused_summary}"
    )
    p = subprocess.Popen(
        ["tail", "-F", "-n", "0", "logs/hl_engine.jsonl"],
        cwd=CWD,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        for line in p.stdout:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("event") != "signal_tick":
                continue
            sym = d.get("symbol", "")
            if sym not in WATCH_LONG:
                continue
            z = d.get("z")
            z4 = d.get("z_4h")
            obi = d.get("obi")
            if z is None or obi is None:
                continue
            z4v = z4 if z4 is not None else 0.0
            coin = sym.replace("/USD", "")

            # Unified trigger: trend + liquidity, sign-agnostic on z.
            if z4v >= Z4H_MIN and obi >= OBI_MIN:
                fire(coin, "long", z, z4v, obi)

            if fire_count >= MAX_FIRES:
                log("cap reached — exiting")
                return
    finally:
        p.terminate()


if __name__ == "__main__":
    main()
