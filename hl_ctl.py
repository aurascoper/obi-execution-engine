#!/usr/bin/env python3
"""
hl_ctl.py — CLI for the Hyperliquid engine control plane.

Phase 1 (read-only):
  python hl_ctl.py get BTC          # one coin's thresholds + positions
  python hl_ctl.py get-all          # z-thresholds for all coins
  python hl_ctl.py snapshot         # full engine state dump

Phase 2 will add write subcommands (set-z, set-notional).
"""

from __future__ import annotations

import argparse
import json
import sys

from control.client import ControlClient


def _fmt_value(v, width: int = 10) -> str:
    """Format a value for aligned display."""
    if v is None:
        return "n/a".rjust(width)
    if isinstance(v, float):
        return f"{v:>{width}.4f}"
    if isinstance(v, dict):
        if not v:
            return "{}".rjust(width)
        return json.dumps(v, default=str)
    return str(v).rjust(width)


def _print_symbol(sym: str, data: dict) -> None:
    """Pretty-print one symbol's data block."""
    print(f"\n  {sym}")
    print(f"  {'─' * 40}")
    for key in (
        "z_entry",
        "z_exit",
        "z_short_entry",
        "z_exit_short",
        "obi",
        "best_bid",
        "best_ask",
    ):
        if key in data:
            print(f"    {key:<18} {_fmt_value(data[key])}")
    if "positions" in data:
        pos = data["positions"]
        if pos:
            for tag, qty in pos.items():
                px = data.get("entry_prices", {}).get(tag, None)
                print(
                    f"    position/{tag:<12} qty={_fmt_value(qty, 12)}  entry={_fmt_value(px)}"
                )
        else:
            print("    positions          (flat)")


def cmd_get(args: argparse.Namespace) -> int:
    client = ControlClient(args.sock)
    resp = client.get(args.coin)
    if not resp.get("ok"):
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    _print_symbol(args.coin.upper(), resp["data"])
    return 0


def cmd_get_all(args: argparse.Namespace) -> int:
    client = ControlClient(args.sock)
    resp = client.get_all()
    if not resp.get("ok"):
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    print("\n  Z-Thresholds")
    print(
        f"  {'Symbol':<12} {'z_entry':>10} {'z_exit':>10} {'z_short':>10} {'z_xs':>10}"
    )
    print(f"  {'─' * 54}")
    for sym, d in resp["data"].items():
        print(
            f"  {sym:<12} "
            f"{_fmt_value(d['z_entry'])} "
            f"{_fmt_value(d['z_exit'])} "
            f"{_fmt_value(d['z_short_entry'])} "
            f"{_fmt_value(d['z_exit_short'])}"
        )
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    client = ControlClient(args.sock)
    resp = client.snapshot()
    if not resp.get("ok"):
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    data = resp["data"]

    # Engine meta
    meta = data.get("engine", {})
    print("\n  Engine")
    print(f"  {'─' * 40}")
    for k, v in meta.items():
        print(f"    {k:<18} {v}")

    # Per-symbol detail
    for sym, sd in data.get("symbols", {}).items():
        _print_symbol(sym, sd)

    ts = data.get("ts")
    if ts:
        from datetime import datetime, timezone

        t = datetime.fromtimestamp(ts, tz=timezone.utc)
        print(f"\n  snapshot at {t.isoformat()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="hl_ctl",
        description="Control plane CLI for the Hyperliquid trading engine.",
        epilog=(
            "examples:\n"
            "  python hl_ctl.py get BTC\n"
            "  python hl_ctl.py get-all\n"
            "  python hl_ctl.py snapshot\n"
            "  python hl_ctl.py --sock /tmp/custom.sock get ETH"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    from util.platform_compat import control_socket_path

    _default_sock = control_socket_path("hl_engine")
    parser.add_argument(
        "--sock",
        default=_default_sock,
        help=f"Path to the engine control socket (default: {_default_sock})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # get <COIN>
    p_get = sub.add_parser("get", help="Get one coin's thresholds and position state")
    p_get.add_argument("coin", help="Coin name, e.g. BTC or ETH")
    p_get.set_defaults(func=cmd_get)

    # get-all
    p_all = sub.add_parser("get-all", help="Get z-thresholds for all coins")
    p_all.set_defaults(func=cmd_get_all)

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Full engine state snapshot")
    p_snap.set_defaults(func=cmd_snapshot)

    args = parser.parse_args()
    try:
        return args.func(args)
    except ConnectionError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
