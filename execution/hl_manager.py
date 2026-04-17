"""
execution/hl_manager.py — Hyperliquid order execution wrapper (Phase 4 scaffold).

Parallel to execution/order_manager.py (Alpaca) but deliberately separate —
the Hyperliquid path must not share state, auth, or failure modes with the
Alpaca path.

Keys are loaded from Settings (populated by config/settings.load() from the
HL_WALLET_ADDRESS and HL_PRIVATE_KEY env vars). Nothing is hardcoded.

Public surface:
    mgr = HyperliquidOrderManager(cfg)
    result = await mgr.submit_order({
        "symbol":    "BTC",
        "side":      "buy",          # "buy" | "sell"
        "qty":       0.01,
        "limit_px":  50_000.0,
        "tif":       "Gtc",          # "Gtc" | "Ioc" | "Alo"
        "reduce_only": False,
    })
    positions = await mgr.get_positions()

Phase 4 state: scaffold. Not wired into any engine. No circuit breaker
integration yet — that will come when we decide how HL exposure participates
in the unified risk budget.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from config.settings import ExecutionMode, Settings

log = structlog.get_logger(__name__)


class HyperliquidOrderManager:
    """
    Thin async facade over the synchronous hyperliquid-python-sdk Exchange/Info
    clients. All blocking SDK calls are offloaded to a thread via
    asyncio.to_thread so we don't stall the event loop.
    """

    def __init__(
        self,
        cfg:               Settings,
        strategy_tag:      str               = "hl_taker",
        default_leverage:  int               = 2,
        coins:             list[str] | None  = None,
        is_cross:          bool              = True,
        perp_dexs:         list[str] | None  = None,
        leverage_map:      dict[str, int] | None = None,
    ):
        if not cfg.hl_wallet_address or not cfg.hl_private_key:
            raise RuntimeError(
                "Hyperliquid credentials missing. "
                "Set HL_WALLET_ADDRESS and HL_PRIVATE_KEY in the environment."
            )

        # Imports are local so the Alpaca-only boot path does not require the
        # hyperliquid package to be installed.
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info     import Info
        from hyperliquid.utils    import constants
        from hyperliquid.utils.types import Cloid as _Cloid

        # Stash on the instance so submit_order / cancel_by_cloid can reuse
        # without re-importing. Keeps the HL dependency lazy.
        self._Cloid = _Cloid

        # constants.MAINNET_API_URL is the canonical string; the user spec
        # referred to this as "constants.MAINNET" — we resolve whichever the
        # installed SDK version exposes.
        mainnet_url = getattr(
            constants, "MAINNET_API_URL",
            getattr(constants, "MAINNET", "https://api.hyperliquid.xyz"),
        )

        wallet = Account.from_key(cfg.hl_private_key)
        self._wallet_address = cfg.hl_wallet_address
        self._mode           = cfg.execution_mode
        self.strategy_tag    = strategy_tag

        exchange_kwargs: dict[str, Any] = {
            "account_address": cfg.hl_wallet_address,
        }
        info_kwargs: dict[str, Any] = {"skip_ws": True}
        if perp_dexs:
            # SDK convention: "" = native HyperCore perps, must be included
            # alongside builder DEX names to keep BTC/ETH/etc. resolvable.
            full_dexs = [""] + [d for d in perp_dexs if d != ""]
            exchange_kwargs["perp_dexs"] = full_dexs
            info_kwargs["perp_dexs"] = full_dexs

        self._exchange = Exchange(wallet, mainnet_url, **exchange_kwargs)
        self._info = Info(mainnet_url, **info_kwargs)

        log.info(
            "hl_manager_initialized",
            wallet=cfg.hl_wallet_address,
            url=mainnet_url,
            mode=self._mode.value,
            tag=self.strategy_tag,
        )

        # Per-coin leverage pin. Fail-loud in PAPER/LIVE so the operator sees
        # bridge/auth issues before any real order attempt. In SHADOW the
        # contract is "strategy runs fully; orders logged but never submitted"
        # — orders cannot reach the exchange, so an unfunded perp account is a
        # valid burn-in state; demote to a warning.
        self._leverage_map = leverage_map or {}
        if coins:
            for coin in coins:
                lev = self._leverage_map.get(coin, default_leverage)
                # HIP-3 builder-deployed assets (dex:NAME) only support isolated margin.
                coin_cross = is_cross and ":" not in coin
                resp   = self._exchange.update_leverage(
                    lev, coin, coin_cross
                )
                status = (resp or {}).get("status")
                resp_msg = str((resp or {}).get("response", ""))
                if status == "ok":
                    log.info(
                        "hl_leverage_set",
                        coin=coin,
                        leverage=lev,
                        is_cross=coin_cross,
                    )
                elif "sufficient margin" in resp_msg.lower():
                    log.warning(
                        "hl_leverage_pin_margin_constraint",
                        coin=coin,
                        leverage=lev,
                        is_cross=coin_cross,
                        response=resp,
                    )
                elif self._mode == ExecutionMode.SHADOW:
                    log.warning(
                        "hl_leverage_pin_skipped_shadow",
                        coin=coin,
                        leverage=lev,
                        response=resp,
                    )
                else:
                    raise RuntimeError(
                        f"hl update_leverage failed for {coin} "
                        f"(leverage={lev}, cross={coin_cross}, "
                        f"mode={self._mode.value}): {resp}"
                    )

    # ── Order submission ─────────────────────────────────────────────────────

    async def submit_order(self, order: dict[str, Any]) -> dict[str, Any] | None:
        """
        Translate an internal order dict into a hyperliquid exchange.order() call.

        Internal order schema:
          symbol       : str   — coin name, e.g. "BTC"
          side         : str   — "buy" | "sell"
          qty          : float — coin-denominated size
          limit_px     : float — limit price
          tif          : str   — "Gtc" | "Ioc" | "Alo"   (default "Gtc")
          reduce_only  : bool  — default False

        Returns the SDK response dict, or None in SHADOW mode (logs a mock fill).
        """
        try:
            symbol       = str(order["symbol"])
            side         = str(order["side"]).lower()
            qty          = float(order["qty"])
            limit_px     = float(order["limit_px"])
            tif          = str(order.get("tif", "Gtc"))
            reduce_only  = bool(order.get("reduce_only", False))
            cloid_raw    = order.get("cloid")  # optional; maker path sets it
        except (KeyError, TypeError, ValueError) as exc:
            log.error("hl_order_malformed", order=order, error=str(exc))
            return None

        if side not in ("buy", "sell"):
            log.error("hl_order_bad_side", side=side)
            return None

        is_buy = (side == "buy")

        # Build the SDK Cloid object once — passed through both the SHADOW
        # short-circuit response and the real submit. Invalid strings are
        # caught here rather than in the signer.
        cloid_obj = None
        if cloid_raw:
            try:
                cloid_obj = self._Cloid.from_str(str(cloid_raw))
            except Exception as exc:
                log.error("hl_order_bad_cloid", cloid=cloid_raw, error=str(exc))
                return None

        if self._mode == ExecutionMode.SHADOW:
            log.warning(
                "[SHADOW EXECUTION] hl mock order",
                symbol=symbol, side=side, qty=qty, limit_px=limit_px,
                tif=tif, reduce_only=reduce_only,
                cloid=cloid_raw, tag=self.strategy_tag,
            )
            return {
                "status":   "shadow_filled",
                "symbol":   symbol,
                "side":     side,
                "qty":      qty,
                "limit_px": limit_px,
                "cloid":    cloid_raw,
                "mode":     "SHADOW",
            }

        order_type = {"limit": {"tif": tif}}

        t0 = time.perf_counter_ns()
        try:
            resp = await asyncio.to_thread(
                self._exchange.order,
                symbol,          # coin / name
                is_buy,
                qty,
                limit_px,
                order_type,
                reduce_only,
                cloid_obj,
            )
        except Exception as exc:
            log.warning(
                "hl_order_rejected",
                symbol=symbol, side=side, qty=qty, limit_px=limit_px,
                error=str(exc), tag=self.strategy_tag,
            )
            return None

        lat_ms = (time.perf_counter_ns() - t0) / 1e6
        log.info(
            "hl_order_submitted",
            symbol=symbol, side=side, qty=qty, limit_px=limit_px,
            tif=tif, reduce_only=reduce_only,
            cloid=cloid_raw,
            latency_ms=round(lat_ms, 3),
            resp_status=(resp or {}).get("status"),
            tag=self.strategy_tag,
            mode=self._mode.value,
        )
        return resp

    # ── Order cancellation ───────────────────────────────────────────────────

    async def cancel_order(
        self, symbol: str, oid: int
    ) -> dict[str, Any] | None:
        """
        Cancel a resting order by its on-chain oid (returned by HL inside the
        submit response `statuses[i].resting.oid`). Spike C uses this to walk
        a maker quote when the book moves away from us.
        """
        if self._mode == ExecutionMode.SHADOW:
            log.warning(
                "[SHADOW EXECUTION] hl mock cancel",
                symbol=symbol, oid=oid, tag=self.strategy_tag,
            )
            return {"status": "shadow_cancelled", "symbol": symbol, "oid": oid}
        try:
            resp = await asyncio.to_thread(
                self._exchange.cancel, symbol, int(oid)
            )
        except Exception as exc:
            log.warning(
                "hl_cancel_rejected",
                symbol=symbol, oid=oid, error=str(exc),
            )
            return None
        log.info(
            "hl_order_cancelled",
            symbol=symbol, oid=oid,
            resp_status=(resp or {}).get("status"),
            tag=self.strategy_tag,
        )
        return resp

    async def cancel_by_cloid(
        self, symbol: str, cloid: str
    ) -> dict[str, Any] | None:
        """
        Cancel by client order id — matches the cloid we stamped at submit.
        Preferred in the maker loop because we track orders by cloid locally
        (the oid only appears after the rest response).
        """
        if self._mode == ExecutionMode.SHADOW:
            log.warning(
                "[SHADOW EXECUTION] hl mock cancel_by_cloid",
                symbol=symbol, cloid=cloid, tag=self.strategy_tag,
            )
            return {"status": "shadow_cancelled", "symbol": symbol, "cloid": cloid}
        try:
            cloid_obj = self._Cloid.from_str(str(cloid))
        except Exception as exc:
            log.error("hl_cancel_bad_cloid", cloid=cloid, error=str(exc))
            return None
        try:
            resp = await asyncio.to_thread(
                self._exchange.cancel_by_cloid, symbol, cloid_obj
            )
        except Exception as exc:
            log.warning(
                "hl_cancel_by_cloid_rejected",
                symbol=symbol, cloid=cloid, error=str(exc),
            )
            return None
        log.info(
            "hl_order_cancelled_by_cloid",
            symbol=symbol, cloid=cloid,
            resp_status=(resp or {}).get("status"),
            tag=self.strategy_tag,
        )
        return resp

    # ── State sync ───────────────────────────────────────────────────────────

    async def get_user_state(self) -> dict[str, Any]:
        """
        Raw user_state payload from Hyperliquid. Includes margin summary,
        open positions, withdrawable balance, and funding info.
        """
        return await asyncio.to_thread(
            self._info.user_state, self._wallet_address
        )

    async def get_positions(self) -> list[dict[str, Any]]:
        """
        Flattened list of open positions, one entry per coin with nonzero size.

        Each entry:
          { "coin": "BTC", "szi": float, "entry_px": float,
            "unrealized_pnl": float, "leverage": dict }

        szi is signed: positive = long, negative = short.
        """
        state = await self.get_user_state()
        asset_positions = state.get("assetPositions", []) or []

        out: list[dict[str, Any]] = []
        for ap in asset_positions:
            pos = ap.get("position", {}) if isinstance(ap, dict) else {}
            try:
                szi = float(pos.get("szi", 0))
            except (TypeError, ValueError):
                continue
            if szi == 0:
                continue
            out.append({
                "coin":            pos.get("coin", ""),
                "szi":             szi,
                "entry_px":        float(pos.get("entryPx", 0) or 0),
                "unrealized_pnl":  float(pos.get("unrealizedPnl", 0) or 0),
                "leverage":        pos.get("leverage", {}),
            })
        return out
