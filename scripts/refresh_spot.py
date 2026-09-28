#!/usr/bin/env python3
"""1-minute job: last traded price only, for all 500 names.

WHY THIS IS SEPARATE FROM daily_levels.py
-----------------------------------------
EMAs, 52w extremes and the PnF column are daily values. Only spot moves
intraday, so only spot is fetched here. The page recomputes distance-to-44EMA
and Above/Below in the browser from (spot, daily EMA), which is why a 1-minute
board costs one small sweep instead of 500 full histories.

WHY yfinance AND NOT RAW HTTP -- THIS WAS MEASURED, NOT ASSUMED
---------------------------------------------------------------
The first version of this script called Yahoo's JSON endpoints directly:
`v7/finance/quote` in batches of 100 with a cookie+crumb pair, falling back to
`v8/finance/chart` per symbol. On a GitHub Actions runner that scored
**fresh=0/500 in 7.5 seconds** -- every request refused outright. Actions IP
ranges are heavily used for Yahoo scraping and are blocked at the edge. The
same endpoints also return HTTP 429 from a residential IP once polled.

In the same workflow, on the same runners, `yf.download()` pulled 500 symbols
x 2 years successfully (500/500, 0 missing). yfinance maintains the cookie and
crumb session that the raw calls could not establish, so it is the path that
actually works here. Speed was never the deciding factor; reachability was.

STALENESS IS REPORTED, NEVER HIDDEN
-----------------------------------
A symbol that fails keeps its previous price and is marked `stale`. The page
greys those rows and the header shows fresh/total. A board that shows a
ten-minute-old price as if it were live is worse than one that admits it is
behind -- the same reasoning as the reconciliation report's `fuzzy` confidence.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IST = timezone(timedelta(hours=5, minutes=30))

CHUNK = 50          # symbols per yf.download call
PERIOD = "1d"       # just today's bar; its Close is the live price intraday


def fetch_spot(symbols: list[str]) -> dict[str, float]:
    """{bare symbol: last price}. Missing symbols are simply absent."""
    out: dict[str, float] = {}
    for i in range(0, len(symbols), CHUNK):
        batch = symbols[i:i + CHUNK]
        try:
            raw = yf.download([f"{s}.NS" for s in batch], period=PERIOD,
                              interval="1d", progress=False, auto_adjust=False,
                              group_by="ticker", threads=True)
        except Exception as e:
            print(f"  batch {i // CHUNK}: {e}", flush=True)
            continue
        if raw is None or len(raw) == 0:
            continue
        for s in batch:
            tkr = f"{s}.NS"
            try:
                df = raw[tkr] if isinstance(raw.columns, pd.MultiIndex) else raw
                close = df["Close"].dropna()
                if len(close):
                    out[s] = float(close.iloc[-1])
            except Exception:
                pass
    return out


def main() -> int:
    uni = json.loads((DATA / "universe.json").read_text())
    symbols = sorted(s["symbol"] for s in uni["stocks"])

    prev = {}
    spot_path = DATA / "spot.json"
    if spot_path.exists():
        try:
            prev = {r["symbol"]: r for r in
                    json.loads(spot_path.read_text()).get("rows", [])}
        except Exception:
            prev = {}

    t0 = time.time()
    prices = fetch_spot(symbols)
    elapsed = round(time.time() - t0, 1)

    now = datetime.now(IST)
    rows, stale = [], 0
    for sym in symbols:
        px = prices.get(sym)
        if px:
            rows.append({"symbol": sym, "ltp": round(px, 2),
                         "at": now.isoformat(), "stale": False})
            continue
        stale += 1
        p = prev.get(sym)
        if p and p.get("ltp"):
            age = None
            try:
                age = int((now - datetime.fromisoformat(p["at"])).total_seconds())
            except Exception:
                pass
            rows.append({**p, "stale": True, "stale_secs": age})
        else:
            rows.append({"symbol": sym, "ltp": None,
                         "at": now.isoformat(), "stale": True})

    out = {
        "generated_at": now.isoformat(),
        "fetch_path": "yfinance-batch",
        "elapsed_secs": elapsed,
        "counts": {"universe": len(symbols), "fresh": len(symbols) - stale,
                   "stale": stale},
        "rows": rows,
    }
    DATA.mkdir(exist_ok=True)
    spot_path.write_text(json.dumps(out, separators=(",", ":")))
    print(f"OK  fresh={len(symbols) - stale}/{len(symbols)}  stale={stale}  {elapsed}s")
    # A totally dead sweep is a failure; a partial one is reported, not fatal.
    return 0 if stale < len(symbols) else 5


if __name__ == "__main__":
    raise SystemExit(main())
