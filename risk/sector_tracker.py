"""
risk/sector_tracker.py — In-memory sector exposure tracker.

Maintains a running tally of open positions (long + short combined) per sector.
All operations are O(1) dict lookups — zero API calls, zero event-loop latency.

Usage in equities_engine.py:
    tracker = SectorExposureTracker(SECTOR_MAP, SECTOR_CAPS, MAX_SECTOR_EXPOSURE)

    # Pre-trade check (in EquitiesSignalEngine.evaluate):
    if not tracker.check(symbol):
        log.warning("signal_rejected_sector_cap", ...)
        return None

    # On confirmed order submission:
    tracker.open(symbol)    # entry fills

    # On confirmed close/cover:
    tracker.close(symbol)   # exit fills

Note on restart: tracker initialises at zero for all sectors. Positions already
open on Alpaca from prior sessions are not reflected until they exit (counts stay
at 0 until new entries come in). For paper trading with $15 notionals this is
acceptable. For live trading, add a _sync_from_broker() call at startup.
"""

from __future__ import annotations
from collections import defaultdict
import structlog

log = structlog.get_logger(__name__)


class SectorExposureTracker:
    """
    Thread-safety: single-threaded asyncio — no locks needed.

    _exposure: sector → count of currently open positions (long + short)
    """

    __slots__ = ("_sector_map", "_caps", "_default_cap", "_exposure")

    def __init__(
        self,
        sector_map:   dict[str, str],
        sector_caps:  dict[str, int],
        default_cap:  int,
    ) -> None:
        self._sector_map  = sector_map
        self._caps        = sector_caps
        self._default_cap = default_cap
        self._exposure: dict[str, int] = defaultdict(int)

    # ── Public API ─────────────────────────────────────────────────────────────

    def check(self, symbol: str) -> bool:
        """
        Returns True if opening a position in this symbol is within the sector cap.
        Called before emitting a signal — does NOT modify state.
        """
        sector = self._sector_of(symbol)
        cap    = self._caps.get(sector, self._default_cap)
        return self._exposure[sector] < cap

    def open(self, symbol: str) -> None:
        """Increment sector count when an entry order is confirmed submitted."""
        sector = self._sector_of(symbol)
        self._exposure[sector] += 1
        log.info(
            "sector_exposure_open",
            symbol=symbol,
            sector=sector,
            exposure=self._exposure[sector],
            cap=self._caps.get(sector, self._default_cap),
        )

    def close(self, symbol: str) -> None:
        """Decrement sector count when an exit/cover order is confirmed submitted."""
        sector = self._sector_of(symbol)
        self._exposure[sector] = max(0, self._exposure[sector] - 1)
        log.info(
            "sector_exposure_close",
            symbol=symbol,
            sector=sector,
            exposure=self._exposure[sector],
            cap=self._caps.get(sector, self._default_cap),
        )

    def snapshot(self) -> dict[str, int]:
        """Returns a copy of the current exposure tally — for logging/monitoring."""
        return {k: v for k, v in self._exposure.items() if v > 0}

    def sector_of(self, symbol: str) -> str:
        """Public accessor — used by engine for log enrichment."""
        return self._sector_of(symbol)

    # ── Private ────────────────────────────────────────────────────────────────

    def _sector_of(self, symbol: str) -> str:
        return self._sector_map.get(symbol, "Unknown")
