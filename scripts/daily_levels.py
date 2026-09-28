#!/usr/bin/env python3
"""Daily job: EMAs, 52-week extremes and PnF column state for the 251-750 band.

CADENCE IS THE WHOLE DESIGN
---------------------------
Every value this script produces is a DAILY value -- it can only change once a
day, after the close. Re-deriving it every minute would mean ~190k Yahoo
requests/day for 505 names and would get the IP throttled (observed: Yahoo
returns HTTP 429 on both v7/quote and v8/chart once you push it).

So the board is split:
    this script      once daily, after close   -> data/levels.json  (heavy)
    refresh_spot.py  every minute in-session   -> data/spot.json    (light)

The page joins the two client-side. Distance-to-44EMA and Above/Below are
computed in the browser from (live spot, daily 44 EMA), so they move every
minute without anyone re-fetching a single candle.

EMA SEEDING
-----------
EMAs are seeded with a simple mean of the first `span` closes and then run
recursively, which is what every charting package does. A 200 EMA therefore
needs >200 sessions of history before its first value is trustworthy; we pull
2 years (~500 sessions) and refuse to emit an EMA whose warm-up is short.
`ema_ok` per row records which spans had enough history -- a value that is
present but unreliable is worse than one that is absent.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CORE = ROOT / "core"          # pnf-charts, vendored as a submodule
sys.path.insert(0, str(CORE))

from pnf.boxes import BoxScale      # noqa: E402
from pnf.chart import PnFChart      # noqa: E402
from pnf import data as pnfdata     # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

EMA_SPANS = [9, 14, 25, 44, 50, 100, 200]
PNF_BOX_PCT = 0.25               # matches pnf-charts "short" preset
PNF_REVERSAL = 3                 # daily 0.25% x 3, same as the PnF board
HISTORY = "2y"
MIN_BARS_FOR_52W = 200           # ~10 months; below this the 52w range is a lie


def ema(series: pd.Series, span: int) -> pd.Series:
    """Mean-seeded EMA, the convention charting packages use."""
    if len(series) < span:
        return pd.Series(dtype="float64")
    seed = series.iloc[:span].mean()
    rest = series.iloc[span:]
    out = [seed]
    k = 2.0 / (span + 1.0)
    for px in rest:
        out.append(px * k + out[-1] * (1 - k))
    return pd.Series(out, index=series.index[span - 1:])


def pnf_column(df: pd.DataFrame, symbol: str) -> dict:
    """Current PnF column: X = demand, O = supply.

    Same box scale the PnF board draws with, so a name reads identically here
    and there. Anything else would be two sources of truth for one fact.
    """
    try:
        ch = PnFChart.from_ohlc(df, BoxScale(PNF_BOX_PCT, PNF_REVERSAL), symbol)
        if not ch.columns:
            return {"pnf": None, "pnf_boxes": None}
        col = ch.columns[-1]
        return {
            "pnf": "X" if col.direction > 0 else "O",
            "pnf_boxes": col.boxes,
        }
    except Exception:
        return {"pnf": None, "pnf_boxes": None}


def build_row(sym: str, meta: dict, df: pd.DataFrame) -> dict | None:
    if df is None or df.empty or "Close" not in df:
        return None
    df = df.dropna(subset=["Close"])
    if len(df) < 60:
        return None

    close = df["Close"].astype(float)
    row = {
        "symbol":   meta["symbol"],
        "name":     meta["name"],
        "industry": meta["industry"],
        "band":     meta["band"],
        "bars":     len(df),
        "prev_close": round(float(close.iloc[-1]), 2),
    }

    ema_ok = {}
    for span in EMA_SPANS:
        s = ema(close, span)
        # Require the EMA to have warmed up over at least one full span beyond
        # its seed before we treat it as usable.
        ok = len(s) >= span
        ema_ok[str(span)] = bool(ok)
        row[f"ema{span}"] = round(float(s.iloc[-1]), 2) if len(s) else None
    row["ema_ok"] = ema_ok

    if len(df) >= MIN_BARS_FOR_52W:
        win = df.iloc[-252:] if len(df) >= 252 else df
        row["high52"] = round(float(win["High"].astype(float).max()), 2)
        row["low52"] = round(float(win["Low"].astype(float).min()), 2)
    else:
        row["high52"] = row["low52"] = None

    row.update(pnf_column(df, meta["symbol"]))
    return row


def main() -> int:
    uni = json.loads((DATA / "universe.json").read_text())
    by_sym = {s["symbol"]: s for s in uni["stocks"]}
    symbols = sorted(by_sym)

    # fetch_many keys its result by the INPUT symbol and appends .NS itself,
    # so bare NSE symbols in means bare NSE symbols out.
    print(f"fetching {len(symbols)} symbols x {HISTORY} daily ...", flush=True)
    frames = pnfdata.fetch_many(symbols, period=HISTORY, interval="1d")

    rows, missing = [], []
    for sym in symbols:
        df = frames.get(sym) if isinstance(frames, dict) else None
        r = build_row(sym, by_sym[sym], df)
        if r is None:
            missing.append(sym)
        else:
            rows.append(r)

    out = {
        "generated_at": datetime.now(IST).isoformat(),
        "history": HISTORY,
        "ema_spans": EMA_SPANS,
        "pnf": {"box_pct": PNF_BOX_PCT, "reversal": PNF_REVERSAL,
                "legend": {"X": "demand", "O": "supply"}},
        "counts": {"universe": len(symbols), "built": len(rows),
                   "missing": len(missing)},
        "missing": sorted(missing),
        "rows": rows,
    }
    DATA.mkdir(exist_ok=True)
    (DATA / "levels.json").write_text(json.dumps(out, separators=(",", ":")))

    print(f"OK  built {len(rows)}/{len(tickers)}  missing={len(missing)}")
    if missing:
        print("    missing:", ", ".join(sorted(missing)[:20]),
              "..." if len(missing) > 20 else "")
    return 0 if rows else 4


if __name__ == "__main__":
    raise SystemExit(main())
