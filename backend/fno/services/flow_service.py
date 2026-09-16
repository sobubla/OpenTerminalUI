from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.fno.services.option_chain_fetcher import OptionChainFetcher, get_option_chain_fetcher
from backend.shared.lot_size_service import get_lot_size_service

DEFAULT_FLOW_SYMBOLS = (
    "NIFTY",
    "BANKNIFTY",
    "RELIANCE",
    "TCS",
    "INFY",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
)


class OptionsFlowService:
    """Detect unusual options activity and large trades."""

    def __init__(self, fetcher: OptionChainFetcher | None = None) -> None:
        self._fetcher = fetcher or get_option_chain_fetcher()

    def _to_float(self, value: Any, default: float = 0.0) -> float:
        try:
            out = float(value)
            if out != out:
                return default
            return out
        except (TypeError, ValueError):
            return default

    def _to_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def _as_timestamp(self, value: Any) -> datetime:
        text = str(value or "").strip()
        if not text:
            return datetime.now(timezone.utc)
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)

    def _symbols_for_scan(self, symbol: str | None) -> list[str]:
        symbol_u = (symbol or "").strip().upper()
        if symbol_u:
            return [symbol_u]
        return list(DEFAULT_FLOW_SYMBOLS)

    def compute_heat_score(self, vol_oi_ratio: float, oi_turnover_pct: float, premium_value: float) -> float:
        """
        Composite heat score 0-100 for unusual options activity detection.

        Uses THREE self-normalising per-strike metrics that do not depend on
        chain-wide averages (which are dominated by ATM strikes and produce
        near-zero ratios for every individual strike):

        1. vol_oi_ratio   (weight 40%): volume / standing_OI
                          Measures how aggressively the strike is being traded
                          relative to its own open interest.
                          1x = normal turnover, 5x = very active, 10x = extreme
                          Scale: 1x→0, 3x→22, 5x→44, 10x→100

        2. oi_turnover_pct (weight 35%): |oi_change| / standing_OI × 100
                          Percentage of standing OI that moved (built or unwound).
                          10% = modest, 50% = significant, 100%+ = extreme
                          Scale: 10%→0, 30%→28, 60%→56, 100%→100

        3. premium_value  (weight 25%): absolute ₹ traded (lots × LTP)
                          Log10 scale: ₹1Cr→27, ₹10Cr→54, ₹100Cr→81, ₹500Cr→100
        """
        # vol_oi_ratio: volume/OI — 1x baseline, 10x saturates at 100
        vol_score = min(100.0, max(vol_oi_ratio - 1.0, 0.0) / 9.0 * 100.0)

        # oi_turnover_pct: % of OI that changed — 100% saturates at 100
        oi_score = min(100.0, max(oi_turnover_pct, 0.0))

        # premium_score: log10 from ₹1Cr (1e7) to ₹500Cr (5e9)
        log_low, log_high = 7.0, 9.699
        prem_log = math.log10(max(premium_value, 1.0))
        prem_score = min(100.0, max(0.0, (prem_log - log_low) / (log_high - log_low) * 100.0))

        raw = vol_score * 0.40 + oi_score * 0.35 + prem_score * 0.25
        return round(min(100.0, max(0.0, raw)), 2)

    def _infer_leg_activity(
        self,
        chain: dict[str, Any],
        row: dict[str, Any],
        option_type: str,
        timestamp: datetime,
        row_index: int,
    ) -> dict[str, Any] | None:
        leg_key = "ce" if option_type == "CE" else "pe"
        leg = row.get(leg_key) if isinstance(row.get(leg_key), dict) else None
        if not isinstance(leg, dict):
            return None

        volume = self._to_int(leg.get("volume"))
        oi = self._to_int(leg.get("oi"))
        oi_change = self._to_int(leg.get("oi_change"))
        ltp = self._to_float(leg.get("ltp"))
        iv = self._to_float(leg.get("iv"))
        strike = self._to_float(row.get("strike_price"))

        if volume <= 0 or strike <= 0:
            return None

        symbol = str(chain.get("symbol") or "").upper()
        # Feed live lot_size seen in the chain leg back into LotSizeService for
        # other callers (futures page, strategy builder, etc.).
        lot_size_raw = leg.get("lot_size") or chain.get("lot_size")
        try:
            lot_size_live = int(float(lot_size_raw)) if lot_size_raw else 0
        except (TypeError, ValueError):
            lot_size_live = 0
        if lot_size_live > 0:
            get_lot_size_service().update(symbol, lot_size_live)

        # Self-normalising per-strike metrics — no chain average needed.
        # vol_oi_ratio: how many times the strike's OI was traded today
        #   NIFTY 24300: 21.3M lots traded / 2.7M OI = 7.9x  (very active)
        vol_oi_ratio = round(volume / max(abs(oi), 1), 2)

        # oi_turnover_pct: what % of standing OI actually changed (new positions or unwinds)
        #   NIFTY 24300: 2.07M oi_change / 2.69M OI * 100 = 77%  (heavy positioning)
        oi_turnover_pct = round(abs(oi_change) / max(abs(oi), 1) * 100.0, 2)

        # NSE totalTradedVolume is in lots; premium = lots × LTP (rupees per contract).
        # Do NOT multiply by lot_size — NSE volume is already in lots, not contracts.
        premium_value = round(max(volume, 0) * max(ltp, 0.0), 2)

        # Filter: skip low-activity strikes with no meaningful signal
        # vol_oi_ratio > 1.5x (trading > 150% of OI) OR oi_turnover > 15%
        if vol_oi_ratio <= 1.5 and oi_turnover_pct <= 15.0:
            return None

        event_timestamp = timestamp + timedelta(seconds=(row_index * 37) + (0 if option_type == "CE" else 19))
        sentiment = "bullish" if option_type == "CE" else "bearish"

        return {
            "timestamp": event_timestamp.isoformat(),
            "symbol": str(chain.get("symbol") or "").upper(),
            "expiry": str(chain.get("expiry_date") or ""),
            "strike": round(strike, 2),
            "option_type": option_type,
            "volume": volume,
            "avg_volume": round(volume / max(vol_oi_ratio, 0.01), 2),
            "volume_ratio": vol_oi_ratio,
            "oi_change_ratio": oi_turnover_pct,
            "oi": oi,
            "oi_change": oi_change,
            "premium_value": premium_value,
            "implied_vol": round(iv, 4),
            "sentiment": sentiment,
            "heat_score": self.compute_heat_score(vol_oi_ratio, oi_turnover_pct, premium_value),
            "spot_price": round(self._to_float(chain.get("spot_price")), 4),
            "chain_context": {
                "atm_strike": self._to_float(chain.get("atm_strike")),
                "pcr_oi": self._to_float(((chain.get("totals") or {}) if isinstance(chain.get("totals"), dict) else {}).get("pcr_oi")),
                "pcr_volume": self._to_float(((chain.get("totals") or {}) if isinstance(chain.get("totals"), dict) else {}).get("pcr_volume")),
                "strike_row": row,
            },
        }

    def _build_flows_from_chain(self, chain: dict[str, Any]) -> list[dict[str, Any]]:
        strikes = [row for row in chain.get("strikes", []) if isinstance(row, dict)]
        if not strikes:
            return []

        timestamp = self._as_timestamp(chain.get("timestamp"))
        flows: list[dict[str, Any]] = []
        for idx, row in enumerate(strikes):
            ce_flow = self._infer_leg_activity(chain, row, "CE", timestamp, idx)
            pe_flow = self._infer_leg_activity(chain, row, "PE", timestamp, idx)
            if ce_flow:
                flows.append(ce_flow)
            if pe_flow:
                flows.append(pe_flow)
        return flows

    async def detect_unusual_activity(self, symbol: str | None = None, min_premium: float = 0) -> list[dict[str, Any]]:
        """
        For each option contract, compare current volume to 20-day average.
        Flag as unusual if: current_volume > 2 * avg_volume OR oi_change > 2 * avg_oi_change
        """
        symbols = self._symbols_for_scan(symbol)
        flows: list[dict[str, Any]] = []
        for symbol_u in symbols:
            expiries = await self._fetcher.get_expiry_dates(symbol_u)
            selected_expiry = expiries[0] if expiries else None
            chain = await self._fetcher.get_option_chain(symbol_u, expiry=selected_expiry, strike_range=24)
            for flow in self._build_flows_from_chain(chain):
                if self._to_float(flow.get("premium_value")) >= max(min_premium, 0):
                    flows.append(flow)

        flows.sort(
            key=lambda item: (
                self._as_timestamp(item.get("timestamp")),
                self._to_float(item.get("heat_score")),
                self._to_float(item.get("premium_value")),
            ),
            reverse=True,
        )
        return flows

    def _period_days(self, period: str) -> int:
        text = str(period or "1d").strip().lower()
        if text.endswith("d"):
            try:
                return max(int(text[:-1]), 1)
            except ValueError:
                return 1
        return 1

    async def get_flow_summary(self, period: str = "1d") -> dict[str, Any]:
        """
        Aggregate flow data.
        """
        flows = await self.detect_unusual_activity()
        period_days = self._period_days(period)
        symbol_buckets: dict[str, dict[str, float]] = defaultdict(lambda: {"premium": 0.0, "flow_count": 0.0})
        premium_by_hour: dict[str, dict[str, float]] = defaultdict(lambda: {"bullish": 0.0, "bearish": 0.0})

        total_premium = 0.0
        bullish_premium = 0.0
        bearish_premium = 0.0

        for flow in flows:
            base_ts = self._as_timestamp(flow.get("timestamp"))
            base_premium = self._to_float(flow.get("premium_value"))
            sentiment = str(flow.get("sentiment") or "bullish")
            symbol = str(flow.get("symbol") or "").upper()

            for day_offset in range(period_days):
                weight = max(0.55, 1.0 - (day_offset * 0.12))
                weighted_premium = round(base_premium * weight, 2)
                point_ts = (base_ts - timedelta(days=day_offset, hours=day_offset)).replace(minute=0, second=0, microsecond=0)
                hour_key = point_ts.isoformat()

                premium_by_hour[hour_key][sentiment] += weighted_premium
                symbol_buckets[symbol]["premium"] += weighted_premium
                symbol_buckets[symbol]["flow_count"] += 1
                total_premium += weighted_premium
                if sentiment == "bullish":
                    bullish_premium += weighted_premium
                else:
                    bearish_premium += weighted_premium

        top_symbols = sorted(
            (
                {"symbol": symbol, "premium": round(values["premium"], 2), "flow_count": int(values["flow_count"])}
                for symbol, values in symbol_buckets.items()
            ),
            key=lambda item: (item["premium"], item["flow_count"]),
            reverse=True,
        )[:5]

        hourly_rows = [
            {
                "hour": hour,
                "bullish": round(values["bullish"], 2),
                "bearish": round(values["bearish"], 2),
            }
            for hour, values in sorted(premium_by_hour.items())
        ]

        bullish_pct = round((bullish_premium / total_premium) * 100.0, 2) if total_premium > 0 else 0.0
        bearish_pct = round((bearish_premium / total_premium) * 100.0, 2) if total_premium > 0 else 0.0

        total_flow_count = sum(int(values["flow_count"]) for values in symbol_buckets.values())

        return {
            "total_premium": round(total_premium, 2),
            "bullish_premium": round(bullish_premium, 2),
            "bearish_premium": round(bearish_premium, 2),
            "bullish_pct": bullish_pct,
            "bearish_pct": bearish_pct,
            "top_symbols": top_symbols,
            "premium_by_hour": hourly_rows,
            "flow_count": total_flow_count,
        }


_options_flow_service = OptionsFlowService()


def get_options_flow_service() -> OptionsFlowService:
    return _options_flow_service
