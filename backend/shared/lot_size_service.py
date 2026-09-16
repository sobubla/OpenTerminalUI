"""
backend/shared/lot_size_service.py

Dynamic lot-size registry for NSE F&O instruments.

Priority resolution order (highest → lowest):
  1. Fyers symbol master (NSE_FO.csv) — column index [3], refreshed daily
  2. NSE website /api/equity-stockIndices or /api/marketStatus (lot sizes appear
     in the option-chain records themselves) — pulled fresh from each chain fetch
  3. In-memory fallback table populated from real chain data at runtime
  4. Hard-coded last-resort defaults (updated as of SEBI circular Nov 2024):
       NIFTY → 75, BANKNIFTY → 30, FINNIFTY → 40, MIDCPNIFTY → 120,
       NIFTYNXT50 → 10, all others → 1 (signals "unknown")

The service exposes:
  get(symbol) -> int          non-blocking, returns best known value
  update(symbol, lot_size)    called by option-chain fetcher when it sees live data
  refresh()                   async, re-reads the Fyers master CSV (called at startup
                              and by InstrumentsLoader every 24 h)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# ── hard-coded last-resort defaults (SEBI circular Nov 2024) ────────
_DEFAULTS: dict[str, int] = {
    "NIFTY":       75,
    "NIFTY50":     75,
    "BANKNIFTY":   30,
    "FINNIFTY":    40,
    "MIDCPNIFTY":  120,
    "NIFTYNXT50":  10,
}


class LotSizeService:
    """
    Thread-safe, async-compatible store for NSE F&O lot sizes.

    Sources are tried in priority order; the highest-priority value for each
    symbol wins and is cached in-memory with a per-source TTL.
    """

    # Source priority levels (lower number = higher priority)
    _PRI_FYERS  = 1   # from Fyers NSE_FO master CSV  (daily)
    _PRI_LIVE   = 2   # from live option-chain records  (real-time, per fetch)
    _PRI_KITE   = 3   # from Kite instruments dump       (daily)
    _PRI_DEFAULT = 99 # hard-coded fallback

    def __init__(self) -> None:
        # {symbol: (lot_size, priority, expires_at)}
        self._store: dict[str, tuple[int, int, float]] = {}
        self._lock = asyncio.Lock()
        self._fyers_ts: float = 0.0   # last successful Fyers master load

    # ── public API ────────────────────────────────────────────────────

    def get(self, symbol: str) -> int:
        """Return the best-known lot size for *symbol* (non-blocking)."""
        sym = (symbol or "").strip().upper()
        entry = self._store.get(sym)
        if entry and time.monotonic() < entry[2]:
            return entry[0]
        # Expired or missing — fall through to defaults
        return _DEFAULTS.get(sym, 1)

    def update(self, symbol: str, lot_size: int, priority: int = _PRI_LIVE, ttl: float = 3_600) -> None:
        """
        Store a lot size for *symbol*.  Only overwrites an existing entry if
        the new source has equal or higher priority (lower number).
        """
        if lot_size <= 0:
            return
        sym = (symbol or "").strip().upper()
        existing = self._store.get(sym)
        if existing:
            _, ex_pri, ex_exp = existing
            # Don't replace a higher-priority non-expired entry with a lower one
            if ex_pri < priority and time.monotonic() < ex_exp:
                return
        self._store[sym] = (lot_size, priority, time.monotonic() + ttl)

    async def refresh(self) -> None:
        """
        Re-read the Fyers NSE_FO master CSV to populate lot sizes for all
        F&O underlyings.  Safe to call concurrently — uses an asyncio Lock.
        """
        async with self._lock:
            await asyncio.to_thread(self._refresh_from_fyers)

    # ── internal helpers ──────────────────────────────────────────────

    def _refresh_from_fyers(self) -> None:
        """Blocking: download Fyers NSE_FO.csv and extract lot sizes."""
        try:
            import csv
            import io
            import requests

            url = "https://public.fyers.in/sym_details/NSE_FO.csv"
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            reader = csv.reader(io.StringIO(resp.text))
            seen: dict[str, int] = {}
            for row in reader:
                if len(row) <= 13:
                    continue
                try:
                    underlying = row[13].strip().upper()
                    lot = int(float(row[3]))
                except (ValueError, IndexError):
                    continue
                if underlying and lot > 0:
                    # Keep the first (smallest) lot size seen per underlying — NSE uses
                    # consistent lot sizes within an expiry series.
                    if underlying not in seen:
                        seen[underlying] = lot

            for sym, lot in seen.items():
                self.update(sym, lot, priority=self._PRI_FYERS, ttl=86_400)

            self._fyers_ts = time.monotonic()
            log.info("LotSizeService: refreshed %d symbols from Fyers NSE_FO master", len(seen))

        except Exception as exc:
            log.warning("LotSizeService: Fyers master refresh failed: %s", exc)
            # On failure, seed defaults so we never return 1 for known indices
            self._seed_defaults()

    def _seed_defaults(self) -> None:
        for sym, lot in _DEFAULTS.items():
            # Only seed if not already in store with higher-priority data
            if sym not in self._store:
                self._store[sym] = (lot, self._PRI_DEFAULT, time.monotonic() + 86_400)


# ── module-level singleton ─────────────────────────────────────────────
_lot_size_service = LotSizeService()
# Pre-seed hard-coded defaults immediately so first calls never return 1
_lot_size_service._seed_defaults()


def get_lot_size_service() -> LotSizeService:
    return _lot_size_service


def get_lot_size(symbol: str) -> int:
    """Convenience helper — returns best known lot size for *symbol*."""
    return _lot_size_service.get(symbol)
