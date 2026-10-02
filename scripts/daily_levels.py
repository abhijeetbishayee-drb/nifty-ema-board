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
sys.path.insert(0, str(ROOT / "scripts"))

# PnF comes from a self-contained port, NOT from the private pnf-charts repo --
# a public runner cannot clone that ("Repository not found" at checkout), which
# is what left this column null on 2026-09-28. The port is proved identical to
# the real engine by tests/test_pnf_parity.py (216/216 cached symbols, same
# direction and box count), and that test also asserts the box preset has not
# drifted and demonstrates it is capable of failing. See scripts/pnf_column.py
# for why porting a fixed algorithm is safe where duplicating a curated
# taxonomy would not be.
import pnf_column                            # noqa: E402
import yfinance as yf                        # noqa: E402

PNF_AVAILABLE = True

IST = timezone(timedelta(hours=5, minutes=30))

EMA_SPANS = [9, 14, 25, 44, 50, 100, 200]
PNF_BOX_PCT = pnf_column.BOX_PCT   # 0.25 -- pinned to pnf-charts "short"
PNF_REVERSAL = pnf_column.REVERSAL # 3    -- daily 0.25% x 3, same as PnF board

# TWO SCALES, NOT ONE
# -------------------
# "short" reverses on 0.25% x 3 = 0.75%, which is well inside a normal daily
# range. Measured over the 216 cached symbols (53,545 columns): MEDIAN COLUMN
# LIFE IS 2 BARS and 48.3% of columns last a single bar -- so the short column
# is a 1-2 day flag, not a trend read, and saying only "X demand" oversells it.
# pnf-charts' own "medium" preset (1% x 3 = 3% reversal) is emitted alongside
# it so the board can show the fast and slow reading side by side. Both come
# from the same ported state machine; only (pct, reversal) differ, which is
# exactly the parameterisation the parity test pins.
PNF_MED_BOX_PCT = 1.0
PNF_MED_REVERSAL = 3

# How many columns of chart to publish per symbol per scale.
# The board draws the grid itself rather than linking out, because pnf-charts
# and pnf-board are both PRIVATE with Pages disabled -- there is no URL to link
# to. Measured on live data: a 2y 0.25%x3 chart runs to ~250 columns, which is
# ~1.2 MB raw across 749 symbols; the last 25 is ~118 KB raw / ~51 KB gzip and
# is about what fits on screen anyway. It goes in its OWN file, fetched only
# when a chart is first opened, so loading the board itself costs nothing.
PNF_CHART_COLUMNS = 25
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


def fetch_many(symbols: list[str], period: str = HISTORY,
               chunk: int = 40) -> dict[str, pd.DataFrame]:
    """OHLC for many symbols, batched.

    505 separate downloads is 505 round-trips and Yahoo throttles well before
    the end of that (HTTP 429, measured 2026-09-28). yfinance batches a list
    into far fewer calls and handles the cookie/crumb dance itself.
    """
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        print(f"  [data] {i + 1}-{i + len(batch)} of {len(symbols)}", flush=True)
        try:
            raw = yf.download([f"{s}.NS" for s in batch], period=period,
                              interval="1d", progress=False, auto_adjust=False,
                              group_by="ticker", threads=True)
        except Exception as e:
            print(f"  [data] batch failed: {e}", flush=True)
            continue
        for s in batch:
            tkr = f"{s}.NS"
            try:
                df = raw[tkr] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df[[c for c in ("Open", "High", "Low", "Close", "Volume")
                         if c in df.columns]].dropna(how="all")
                if not df.empty:
                    out[s] = df
            except Exception:
                pass
    return out


def pnf_col(df: pd.DataFrame, symbol: str, pct: float = PNF_BOX_PCT,
            rev: int = PNF_REVERSAL, key: str = "pnf") -> dict:
    """Current PnF column: X = demand, O = supply.

    `pct`/`rev` default to the scale the PnF board draws with, so a name reads
    identically here and there -- which is the point of pinning the preset
    rather than choosing one. `key` names the output fields, so the same engine
    serves both the fast and the slow column with no second code path.
    """
    null = {key: None, f"{key}_boxes": None}
    if "High" not in df or "Low" not in df:
        return null
    try:
        res = pnf_column.last_column(
            df["High"].astype(float).tolist(),
            df["Low"].astype(float).tolist(),
            pct, rev)
        if res is None:
            return null
        direction, boxes = res
        return {key: direction, f"{key}_boxes": boxes}
    except Exception:
        return null


def pnf_series(df: pd.DataFrame, pct: float, rev: int,
               keep: int = PNF_CHART_COLUMNS) -> list[int] | None:
    """The last `keep` columns, packed flat for the browser.

    [dir_of_first, base_box, then two ints per column: bottom-base, height]

    Directions strictly alternate, so only the first one is carried. Box
    INDICES, never prices -- the page rebuilds the price axis from (base, pct)
    with the same geometric formula, so the two sides cannot round differently.
    """
    if "High" not in df or "Low" not in df:
        return None
    try:
        cols = pnf_column.columns(
            df["High"].astype(float).tolist(),
            df["Low"].astype(float).tolist(), pct, rev)
    except Exception:
        return None
    if not cols:
        return None
    cols = cols[-keep:]
    base = cols[0][1]
    out = [1 if cols[0][0] == pnf_column.X else 0, base]
    for _d, bottom, top in cols:
        out += [bottom - base, top - bottom + 1]
    return out


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
        # The last COMPLETED daily bar and its own move -- i.e. the last
        # working day, not today. Dated explicitly because the board is read
        # on holidays and before the open, when "latest bar" and "today" are
        # different days and an undated % change silently misleads.
        "last_date": str(df.index[-1])[:10],
        "chg_pct":  (round((float(close.iloc[-1]) / float(close.iloc[-2]) - 1) * 100, 2)
                     if len(close) >= 2 and float(close.iloc[-2]) else None),
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

    row.update(pnf_col(df, meta["symbol"]))
    row.update(pnf_col(df, meta["symbol"], PNF_MED_BOX_PCT,
                       PNF_MED_REVERSAL, "pnfm"))
    # carried out of here under a private key and split into its own file by
    # main(), so levels.json keeps exactly the shape it had
    row["_cols"] = {"pnf": pnf_series(df, PNF_BOX_PCT, PNF_REVERSAL),
                    "pnfm": pnf_series(df, PNF_MED_BOX_PCT, PNF_MED_REVERSAL)}
    return row


def main() -> int:
    uni = json.loads((DATA / "universe.json").read_text())
    by_sym = {s["symbol"]: s for s in uni["stocks"]}
    symbols = sorted(by_sym)

    # fetch_many keys its result by the INPUT symbol and appends .NS itself,
    # so bare NSE symbols in means bare NSE symbols out.
    print(f"fetching {len(symbols)} symbols x {HISTORY} daily ...", flush=True)
    frames = fetch_many(symbols, period=HISTORY)

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
        "pnf": {"available": PNF_AVAILABLE, "box_pct": PNF_BOX_PCT,
                "reversal": PNF_REVERSAL,
                "medium_box_pct": PNF_MED_BOX_PCT,
                "medium_reversal": PNF_MED_REVERSAL,
                "legend": {"X": "demand", "O": "supply"}},
        "counts": {"universe": len(symbols), "built": len(rows),
                   "missing": len(missing)},
        "missing": sorted(missing),
        "rows": rows,
    }
    charts = {}
    for r in rows:
        c = r.pop("_cols", None)
        if c and (c.get("pnf") or c.get("pnfm")):
            charts[r["symbol"]] = {k: v for k, v in c.items() if v}

    DATA.mkdir(exist_ok=True)
    (DATA / "levels.json").write_text(json.dumps(out, separators=(",", ":")))
    (DATA / "pnf.json").write_text(json.dumps({
        "generated_at": out["generated_at"],
        "base": pnf_column.BASE,
        "max_columns": PNF_CHART_COLUMNS,
        "presets": {"pnf": {"box_pct": PNF_BOX_PCT, "reversal": PNF_REVERSAL},
                    "pnfm": {"box_pct": PNF_MED_BOX_PCT,
                             "reversal": PNF_MED_REVERSAL}},
        "format": "[dir_of_first(1=X,0=O), base_box, (bottom-base, height) per column]",
        "cols": charts,
    }, separators=(",", ":")))

    print(f"OK  built {len(rows)}/{len(symbols)}  missing={len(missing)}")
    if missing:
        print("    missing:", ", ".join(sorted(missing)[:20]),
              "..." if len(missing) > 20 else "")
    return 0 if rows else 4


if __name__ == "__main__":
    raise SystemExit(main())
