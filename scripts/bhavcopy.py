#!/usr/bin/env python3
"""Fill sessions that Yahoo is missing, from NSE's own equity bhavcopy.

WHY A SECOND SOURCE AT ALL
--------------------------
Yahoo's NSE daily series has HOLES, not just lag. On 2026-10-06, KOTAKBANK's
frame ran ... 2026-10-01, 2026-10-06 ... -- it carried TODAY's bar while the
2026-10-05 session was simply absent, so no amount of waiting would have
filled it. 333 of 747 names were affected, and the board showed a mixed wall
of "01 Oct" and "05 Oct" rows. Checked three ways (period=, start/end=,
Ticker.history) before concluding it was the data and not the request.

NSE's own bhavcopy has every one of them. It is the authoritative print, it
needs no key, and `build_universe.py` already proves nsearchives.nseindia.com
serves this account's Actions runners with a plain browser User-Agent -- the
Akamai wall is on www.nseindia.com/api/*, not here.

THE 404 IS THE TRADING CALENDAR
-------------------------------
A bhavcopy exists only for a day the market actually traded: 2026-10-02
(Gandhi Jayanti) returns 404. So walking back day by day and keeping what
returns 200 yields the real session list, with no holiday table to maintain
and nothing to go stale -- the bug that fired an armed recovery on a closed
exchange came from exactly such a hand-kept calendar.

SCOPE
-----
This fills gaps INSIDE the window it scans. It never prepends history before
a symbol's first Yahoo bar (a recent listing is not a gap), never writes past
the caller's completed-session cutoff, and never overwrites a bar Yahoo
already has -- Yahoo stays the source of record, this is strictly repair.
"""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import timedelta

import pandas as pd
import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
URL = ("https://nsearchives.nseindia.com/content/cm/"
       "BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")

# One symbol can appear under several series; take the ordinary ones first.
# RR is REITs and InvITs -- BIRET and EMBASSY are in the NIFTY Total Market and
# were the last two names left behind when this list was equities-only.
SERIES_PREFERENCE = ("EQ", "BE", "SM", "ST", "RR", "IV")


def _fetch_one(day, timeout: int = 45) -> dict[str, tuple] | None:
    """{symbol: (open, high, low, close, volume)} for `day`, or None if the
    market did not trade (HTTP 404) or the archive could not be read."""
    try:
        r = requests.get(URL.format(d=day), headers={"User-Agent": UA},
                         timeout=timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        rows = csv.DictReader(
            io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8"))
        best: dict[str, tuple[int, tuple]] = {}
        for x in rows:
            if x.get("FinInstrmTp") != "STK":
                continue
            ser = (x.get("SctySrs") or "").strip()
            if ser not in SERIES_PREFERENCE:
                continue
            sym = (x.get("TckrSymb") or "").strip()
            if not sym:
                continue
            rank = SERIES_PREFERENCE.index(ser)
            if sym in best and best[sym][0] <= rank:
                continue
            try:
                bar = (float(x["OpnPric"]), float(x["HghPric"]),
                       float(x["LwPric"]), float(x["ClsPric"]),
                       float(x.get("TtlTradgVol") or 0))
            except (ValueError, KeyError, TypeError):
                continue
            if bar[1] <= 0:
                continue
            best[sym] = (rank, bar)
        return {s: b for s, (_, b) in best.items()} or None
    except Exception:
        return None


class Filler:
    """Scans back `lookback` calendar days from `cutoff` and caches each
    trading session's bhavcopy once, for the whole universe."""

    def __init__(self, cutoff: pd.Timestamp, lookback: int = 15):
        self.sessions: dict[pd.Timestamp, dict[str, tuple]] = {}
        self.filled: dict[str, list[str]] = {}
        self.not_in_bhavcopy = 0
        self.phantoms: dict[str, list[str]] = {}
        day = cutoff
        for _ in range(lookback):
            if day.weekday() < 5:              # bhavcopy only exists Mon-Fri
                bars = _fetch_one(day)
                if bars:
                    self.sessions[pd.Timestamp(day.date())] = bars
            day -= timedelta(days=1)

    @property
    def session_dates(self) -> list[pd.Timestamp]:
        return sorted(self.sessions)

    def fill_df(self, sym: str, df):
        if df is None or df.empty or not self.sessions:
            return df
        try:
            idx = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
        except TypeError:
            idx = pd.DatetimeIndex(df.index).normalize()
        have = set(idx)
        first = idx.min()

        # DROP YAHOO'S PHANTOM HOLIDAY BARS. Yahoo invents a flat zero-volume
        # bar on some closed days -- RELIANCE carries 2026-10-02 (Gandhi
        # Jayanti) at O=H=L=C=1167.70, volume 0. Inside the scanned window the
        # bhavcopy 404s ARE the holiday list, so a dated bar that matches no
        # session is provably not a session. A flat bar is not harmless: it
        # feeds the EMAs and reads as a real box to the PnF state machine.
        lo, hi = self.session_dates[0], self.session_dates[-1]
        real = set(self.session_dates)
        ghosts = [d for d in idx if lo <= d <= hi and d not in real]

        add = {}
        for d in self.session_dates:
            # never prepend: a session before this symbol's first Yahoo bar is
            # a listing date, not a hole
            if d in have or d < first:
                continue
            bar = self.sessions[d].get(sym)
            if bar is None:
                self.not_in_bhavcopy += 1
                continue
            add[d] = bar
        if not add and not ghosts:
            return df

        out = df.copy()
        out.index = idx
        for d, (o, h, l, c, v) in add.items():
            for col, val in (("Open", o), ("High", h), ("Low", l),
                             ("Close", c), ("Adj Close", c), ("Volume", v)):
                if col in out.columns:
                    out.loc[d, col] = val
        if ghosts:
            out = out.drop(index=ghosts, errors="ignore")
            self.phantoms.setdefault(sym, []).extend(
                str(d.date()) for d in sorted(ghosts))
        out = out.sort_index()
        if add:
            self.filled.setdefault(sym, []).extend(
                str(d.date()) for d in sorted(add))
        return out
