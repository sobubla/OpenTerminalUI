"""
backend/adapters/fyers.py — Fyers API v3 adapter for OpenTerminalUI (implements adapters/base.py).

Config (env / .env):
    FYERS_APP_ID        = XXXXXXXX-100
    FYERS_ACCESS_TOKEN  = <daily token>          # or leave unset and keep it in ~/.fyers_token
    FYERS_TOKEN_FILE    = ~/.fyers_token          # optional override

Notes
- Fyers has no search endpoint: search uses the public symbol-master CSVs (NSE_CM / NSE_FO),
  downloaded once a day and cached in memory.
- Fyers has no fundamentals endpoint: get_fundamentals() returns {} (the platform's
  Yahoo/NSEPython providers cover that; the failover chain handles it).
- Option chain uses Fyers' optionchain endpoint; IV/greeks are computed locally with py_vollib
  when installed (pip install py_vollib), otherwise left at 0.
- Streaming (WebSocket ticks) is not wired in this version -> supports_streaming() = False.
"""
from __future__ import annotations

import asyncio, csv, io, logging, math, os, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.adapters.base import (DataAdapter, QuoteResponse, OHLCV, Instrument,
                                   OptionContract, OptionChain, FuturesContract)

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

def _f(val, default: float = 0.0) -> float:
    """Safely coerce Fyers API values to float — handles str, None, '-', and empty string."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

# ── symbol translation ──────────────────────────────────────────────
_INDEX = {"NIFTY": "NSE:NIFTY50-INDEX", "NIFTY50": "NSE:NIFTY50-INDEX",
          "BANKNIFTY": "NSE:NIFTYBANK-INDEX", "FINNIFTY": "NSE:FINNIFTY-INDEX",
          "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX", "INDIAVIX": "NSE:INDIAVIX-INDEX"}

def to_fyers(symbol: str) -> str:
    """'RELIANCE' / 'NSE:RELIANCE' -> 'NSE:RELIANCE-EQ'; indices and F&O symbols pass through."""
    s = symbol.strip().upper()
    if s.startswith("NSE:") or s.startswith("BSE:"):
        exch, s = s.split(":", 1)
    else:
        exch = "NSE"
    if s in _INDEX:
        return _INDEX[s]
    if s.endswith(("-EQ", "-INDEX", "FUT")) or s[-2:] in ("CE", "PE") and any(ch.isdigit() for ch in s):
        return f"{exch}:{s}"
    return f"{exch}:{s}-EQ"

def from_fyers(fsym: str) -> str:
    s = fsym.split(":", 1)[-1]
    return s[:-3] if s.endswith("-EQ") else s

_TF = {"1m": "1", "2m": "2", "3m": "3", "5m": "5", "10m": "10", "15m": "15", "30m": "30",
       "45m": "45", "1h": "60", "60m": "60", "2h": "120", "4h": "240", "1d": "D", "d": "D", "day": "D",
       "1wk": "W", "1mo": "M"}      # W/M are resampled locally from daily bars


def _resample(bars: list[OHLCV], rule: str) -> list[OHLCV]:
    """Aggregate daily bars into weekly (Mon-Fri) or monthly bars."""
    out: list[OHLCV] = []
    key_of = (lambda d: d.isocalendar()[:2]) if rule == "W" else (lambda d: (d.year, d.month))
    cur_key, cur = None, None
    for b in bars:
        d = datetime.fromtimestamp(b.t, IST).date()
        k = key_of(d)
        if k != cur_key:
            if cur: out.append(cur)
            cur_key, cur = k, OHLCV(t=b.t, o=b.o, h=b.h, l=b.l, c=b.c, v=b.v)
        else:
            cur.h, cur.l, cur.c, cur.v = max(cur.h, b.h), min(cur.l, b.l), b.c, cur.v + b.v
    if cur: out.append(cur)
    return out


# ── symbol master (search / F&O metadata) ───────────────────────────
_MASTER_URLS = {"CM": "https://public.fyers.in/sym_details/NSE_CM.csv",
                "FO": "https://public.fyers.in/sym_details/NSE_FO.csv"}

class _Master:
    def __init__(self):
        self.rows: dict[str, list[list[str]]] = {}
        self.ts = 0.0

    def load(self):
        if time.time() - self.ts < 86400 and self.rows:
            return
        import requests
        for seg, url in _MASTER_URLS.items():
            try:
                txt = requests.get(url, timeout=20).text
                self.rows[seg] = [r for r in csv.reader(io.StringIO(txt)) if len(r) > 13]
            except Exception as e:
                log.warning("fyers master %s failed: %s", seg, e)
        self.ts = time.time()

    # column layout of Fyers masters: [1]=description, [3]=lot, [8]=expiry epoch,
    # [9]=fyers ticker, [13]=underlying, [15]=strike, [16]=option type
    def search(self, q: str, limit=25) -> list[Instrument]:
        self.load(); q = q.upper(); out = []
        for r in self.rows.get("CM", []):
            tick, desc = r[9], r[1]
            if not tick.endswith("-EQ"):
                continue
            if q in tick.upper() or q in desc.upper():
                out.append(Instrument(symbol=from_fyers(tick), name=desc.strip(), exchange="NSE", currency="INR"))
                if len(out) >= limit: break
        return out

    def fo_rows(self, underlying: str) -> list[list[str]]:
        self.load(); u = underlying.upper()
        return [r for r in self.rows.get("FO", []) if r[13].upper() == u]

_master = _Master()


class FyersAdapter(DataAdapter):
    name = "fyers"

    def __init__(self):
        self.app_id = os.getenv("FYERS_APP_ID", "")
        self._fy = None

    # ── client ──
    def _client(self):
        if self._fy is None:
            from fyers_apiv3 import fyersModel
            tok = os.getenv("FYERS_ACCESS_TOKEN") or ""
            if not tok:
                p = Path(os.path.expanduser(os.getenv("FYERS_TOKEN_FILE", "~/.fyers_token")))
                tok = p.read_text().strip() if p.exists() else ""
            if not tok or not self.app_id:
                raise RuntimeError("Fyers not configured: set FYERS_APP_ID and FYERS_ACCESS_TOKEN (or ~/.fyers_token)")
            self._fy = fyersModel.FyersModel(client_id=self.app_id, token=tok, is_async=False, log_path="")
        return self._fy

    async def _run(self, fn, *a):
        return await asyncio.to_thread(fn, *a)

    # ── quotes ──
    async def get_quote(self, symbol: str) -> QuoteResponse | None:
        fsym = to_fyers(symbol)
        try:
            resp = await self._run(self._client().quotes, {"symbols": fsym})
            d = resp.get("d", [])
            if resp.get("s") != "ok" or not d:
                return None
            v = d[0].get("v", {})
            ts = datetime.fromtimestamp(_f(v.get("tt"), time.time()), IST).isoformat() if v.get("tt") else None
            return QuoteResponse(symbol=symbol, price=_f(v.get("lp")), change=_f(v.get("ch")),
                                 change_pct=_f(v.get("chp")), currency="INR", ts=ts)
        except Exception as e:
            log.warning("fyers quote %s: %s", symbol, e); raise   # let call_with_failover try the fallback

    # ── history ──
    async def get_history(self, symbol: str, timeframe: str, start: date, end: date) -> list[OHLCV]:
        res = _TF.get((timeframe or "1d").lower(), timeframe)
        agg = res if res in ("W", "M") else None
        if agg: res = "D"
        fsym = to_fyers(symbol)
        # Fyers caps intraday ranges (~100 days per call) — chunk if needed
        out: list[OHLCV] = []
        chunk = timedelta(days=365 if res == "D" else 90)
        s = start
        try:
            while s <= end:
                e = min(end, s + chunk)
                resp = await self._run(self._client().history, {
                    "symbol": fsym, "resolution": res, "date_format": "1",
                    "range_from": s.isoformat(), "range_to": e.isoformat(), "cont_flag": "1"})
                if resp.get("s") == "ok":
                    out.extend(OHLCV(t=int(_f(c[0])), o=_f(c[1]), h=_f(c[2]), l=_f(c[3]),
                                     c=_f(c[4]), v=_f(c[5])) for c in resp.get("candles", []))
                else:
                    log.warning("fyers history %s: %s", fsym, resp.get("message", resp))
                s = e + timedelta(days=1)
        except Exception as e:
            log.warning("fyers history %s: %s", symbol, e); raise
        out.sort(key=lambda x: x.t)
        return _resample(out, agg) if agg else out

    # ── search / fundamentals / streaming ──
    async def search_instruments(self, query: str) -> list[Instrument]:
        return await self._run(_master.search, query)

    async def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        # Fyers has no fundamentals API; reuse the platform's NSE client exactly as KiteAdapter does
        try:
            from backend.core.nse_client import NSEClient
            row = await NSEClient().get_quote_equity(symbol.strip().upper())
            return row if isinstance(row, dict) else {}
        except Exception as e:
            log.warning("fundamentals %s: %s", symbol, e); return {}

    async def supports_streaming(self) -> bool:
        return False

    # ── option chain ──
    async def get_option_chain(self, underlying: str, expiry: date) -> OptionChain | None:
        fsym = to_fyers(underlying)
        try:
            resp = await self._run(self._client().optionchain, {"symbol": fsym, "strikecount": 30, "timestamp": ""})
            data = resp.get("data", {})
            if resp.get("s") != "ok" or not data:
                return None
            # choose the requested expiry (Fyers expiries come as epoch + dd-mm-yyyy)
            want = expiry.strftime("%d-%m-%Y")
            exp = next((x for x in data.get("expiryData", []) if x.get("date") == want), None)
            if exp and exp.get("expiry") and str(exp["expiry"]) != str(data.get("expiryData", [{}])[0].get("expiry")):
                resp = await self._run(self._client().optionchain, {"symbol": fsym, "strikecount": 30, "timestamp": exp["expiry"]})
                data = resp.get("data", {})
            spot = _f(next((r.get("ltp", 0) for r in data.get("optionsChain", []) if r.get("option_type") == ""), 0)) \
                   or _f((await self.get_quote(underlying) or QuoteResponse(underlying, 0)).price)
            lot = self._lot_size(underlying)
            contracts, ce_oi, pe_oi, ce_v, pe_v = [], 0, 0, 0, 0
            tte = max((datetime.combine(expiry, datetime.min.time(), IST).replace(hour=15, minute=30)
                       - datetime.now(IST)).total_seconds() / (365 * 86400), 1e-6)
            for r in data.get("optionsChain", []):
                ot = r.get("option_type", "")
                if ot not in ("CE", "PE"):
                    continue
                ltp, K = _f(r.get("ltp", 0)), _f(r.get("strike_price", 0))
                iv, greeks = _iv_greeks(spot, K, tte, ltp, ot)
                oi, vol = int(_f(r.get("oi", 0))), int(_f(r.get("volume", 0)))
                if ot == "CE": ce_oi += oi; ce_v += vol
                else:          pe_oi += oi; pe_v += vol
                contracts.append(OptionContract(
                    symbol=r.get("symbol", ""), underlying=underlying, expiry=expiry.isoformat(), strike=K,
                    option_type=ot, ltp=ltp, bid=_f(r.get("bid", 0)), ask=_f(r.get("ask", 0)),
                    iv=iv, oi=oi, oi_change=int(_f(r.get("oich", 0))), volume=vol, lot_size=lot, **greeks))
            return OptionChain(underlying=underlying, spot_price=spot, expiry=expiry.isoformat(),
                               contracts=contracts,
                               pcr_oi=round(pe_oi / ce_oi, 3) if ce_oi else 0.0,
                               pcr_volume=round(pe_v / ce_v, 3) if ce_v else 0.0,
                               max_pain=_max_pain(contracts), timestamp=datetime.now(IST).isoformat())
        except Exception as e:
            log.warning("fyers option chain %s: %s", underlying, e); raise

    # ── futures chain ──
    async def get_futures_chain(self, underlying: str) -> list[FuturesContract]:
        rows = await self._run(_master.fo_rows, underlying)
        futs = [r for r in rows if r[9].endswith("FUT")]
        if not futs:
            return []
        spot_q = await self.get_quote(underlying)
        spot = spot_q.price if spot_q else 0.0
        try:
            resp = await self._run(self._client().quotes, {"symbols": ",".join(r[9] for r in futs[:3])})
            quotes = {d["n"]: d.get("v", {}) for d in resp.get("d", []) if d.get("s") == "ok"}
        except Exception as e:
            log.warning("fyers futures quotes %s: %s", underlying, e); quotes = {}
        out = []
        for r in futs[:3]:
            v = quotes.get(r[9], {})
            ltp = _f(v.get("lp", 0)); exp_dt = datetime.fromtimestamp(int(_f(r[8])), IST).date()
            basis = ltp - spot if spot else 0.0
            days = max((exp_dt - date.today()).days, 1)
            out.append(FuturesContract(symbol=r[9], underlying=underlying, expiry=exp_dt.isoformat(), ltp=ltp,
                                       basis=round(basis, 2), basis_pct=round(basis / spot * 100, 3) if spot else 0.0,
                                       annualized_basis=round(basis / spot * 365 / days * 100, 2) if spot else 0.0,
                                       oi=int(_f(v.get("oi", 0))), volume=int(_f(v.get("volume", 0))),
                                       lot_size=int(_f(r[3] or 1)), change=_f(v.get("ch", 0)),
                                       change_pct=_f(v.get("chp", 0))))
        return sorted(out, key=lambda f: f.expiry)

    def _lot_size(self, underlying: str) -> int:
        rows = _master.fo_rows(underlying)
        try: return int(float(rows[0][3])) if rows else 1
        except Exception: return 1


# ── local IV / greeks (optional py_vollib) ──────────────────────────
def _iv_greeks(S, K, t, price, ot, r=0.065):
    zero = dict(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)
    if not (S > 0 and K > 0 and price > 0 and t > 0):
        return 0.0, zero
    try:
        from py_vollib.black_scholes.implied_volatility import implied_volatility
        from py_vollib.black_scholes.greeks import analytical as g
        flag = "c" if ot == "CE" else "p"
        iv = implied_volatility(price, S, K, t, r, flag)
        return round(iv * 100, 2), dict(delta=round(g.delta(flag, S, K, t, r, iv), 4), gamma=round(g.gamma(flag, S, K, t, r, iv), 5),
                                        theta=round(g.theta(flag, S, K, t, r, iv), 4), vega=round(g.vega(flag, S, K, t, r, iv), 4),
                                        rho=round(g.rho(flag, S, K, t, r, iv), 4))
    except Exception:
        return 0.0, zero

def _max_pain(contracts: list[OptionContract]) -> float | None:
    strikes = sorted({c.strike for c in contracts})
    if not strikes: return None
    best, best_pain = None, math.inf
    for S in strikes:
        pain = sum(c.oi * max(S - c.strike, 0) for c in contracts if c.option_type == "CE") + \
               sum(c.oi * max(c.strike - S, 0) for c in contracts if c.option_type == "PE")
        if pain < best_pain: best, best_pain = S, pain
    return best