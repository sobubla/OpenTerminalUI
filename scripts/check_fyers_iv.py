import asyncio
import os
import sys
sys.path.insert(0, ".")

from backend.config.env import load_local_env
load_local_env()

async def main():
    from backend.adapters.fyers import FyersAdapter, to_fyers, _f, _iv_greeks
    from datetime import date, datetime, timedelta, timezone

    IST = timezone(timedelta(hours=5, minutes=30))
    a = FyersAdapter()

    from backend.adapters.fyers import _master
    _master.load()
    fo_rows = _master.fo_rows("NIFTY")
    epochs = sorted({int(float(r[8])) for r in fo_rows if r[8] and r[8] != "0"})
    if not epochs:
        print("No FO rows found for NIFTY")
        return

    exp_date = datetime.fromtimestamp(epochs[0], IST).date()
    print(f"Next expiry: {exp_date}")

    client = a._client()
    fsym = to_fyers("NIFTY")
    print(f"Fyers symbol: {fsym}")

    resp = await asyncio.to_thread(client.optionchain, {"symbol": fsym, "strikecount": 5, "timestamp": ""})
    data = resp.get("data", {})
    status = resp.get("s")
    print(f"Status: {status}")

    chain = data.get("optionsChain", [])[:6]
    spot = 0.0
    for r in chain:
        if r.get("option_type") == "":
            spot = _f(r.get("ltp", 0))
    if not spot:
        spot = _f(data.get("expiryData", [{}])[0].get("underlying_price", 0)) if data.get("expiryData") else 0.0
    print(f"Spot: {spot}")

    print("\nSample contracts:")
    for r in chain[:6]:
        ot = r.get("option_type", "")
        if ot not in ("CE", "PE"):
            continue
        ltp = _f(r.get("ltp", 0))
        strike = _f(r.get("strike_price", 0))
        print(f"  strike={strike}  ot={ot}  ltp={ltp}  type={type(r.get('ltp')).__name__}")

    # Compute tte and test IV
    now_ist = datetime.now(IST)
    expiry_dt = datetime.combine(exp_date, datetime.min.time()).replace(
        hour=15, minute=30, tzinfo=IST
    )
    tte = max((expiry_dt - now_ist).total_seconds() / (365 * 86400), 1e-6)
    print(f"\ntte (years): {tte:.6f}")

    # Test IV on first CE
    for r in chain:
        ot = r.get("option_type", "")
        if ot != "CE":
            continue
        ltp = _f(r.get("ltp", 0))
        strike = _f(r.get("strike_price", 0))
        if ltp > 0 and spot > 0:
            iv, greeks = _iv_greeks(spot, strike, tte, ltp, ot)
            print(f"  IV test: strike={strike} ltp={ltp} -> IV={iv}  delta={greeks.get('delta')}")
            break

if __name__ == "__main__":
    asyncio.run(main())
