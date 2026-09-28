#!/usr/bin/env python3
"""1-minute job: last traded price only, for all 505 names.

WHY THIS IS SEPARATE FROM daily_levels.py
-----------------------------------------
EMAs, 52w extremes and the PnF column are daily values. Only spot moves
intraday, so only spot is fetched here. The page recomputes distance-to-44EMA
and Above/Below in the browser from (spot, daily EMA), which is why a 1-minute
board costs one small request set instead of 505 full histories.

TWO FETCH PATHS, AND WHY BOTH EXIST
-----------------------------------
  primary   v7/finance/quote, 100 symbols per call -> ~6 calls for the universe
  fallback  v8/finance/chart, 1 symbol per call    -> ~505 calls, threaded

v7 needs a cookie+crumb pair. When it works it is ~80x fewer requests, which is
the difference between a board that survives a 1-minute cadence and one that
gets rate limited. Measured 2026-09-28 from a residential IP: sustained polling
earns HTTP 429 on BOTH endpoints, so the fallback is not theoretical -- it is
what runs whenever Yahoo is unhappy, and it is still rate-limited itself.

STALENESS IS REPORTED, NEVER HIDDEN
-----------------------------------
A symbol that fails keeps its previous price and gets `stale: true` with the
age in seconds. The page greys those rows. A board that silently shows a
10-minute-old price as live is worse than one that admits it is behind --
the same reasoning as the reconciliation report's `fuzzy` confidence.
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IST = timezone(timedelta(hours=5, minutes=30))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

CHUNK = 100          # v7 accepts comfortably more than this; 100 keeps URLs sane
WORKERS = 16         # fallback concurrency -- deliberately below core's 20
TIMEOUT = 12


# ── primary: batched v7 quote ────────────────────────────────────────────────
def _crumb(sess: requests.Session) -> str | None:
    try:
        sess.get("https://fc.yahoo.com", headers=HEADERS, timeout=TIMEOUT)
        r = sess.get("https://query1.finance.yahoo.com/v1/test/getcrumb",
                     headers=HEADERS, timeout=TIMEOUT)
        c = (r.text or "").strip()
        # A throttled crumb endpoint returns prose, not a token.
        if r.status_code == 200 and c and len(c) < 32 and " " not in c:
            return c
    except Exception:
        pass
    return None


def fetch_v7(tickers: list[str]) -> dict[str, float]:
    sess = requests.Session()
    crumb = _crumb(sess)
    if not crumb:
        return {}
    out: dict[str, float] = {}
    for i in range(0, len(tickers), CHUNK):
        batch = tickers[i:i + CHUNK]
        try:
            r = sess.get("https://query1.finance.yahoo.com/v7/finance/quote",
                         params={"symbols": ",".join(batch), "crumb": crumb},
                         headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                return out if out else {}
            for q in r.json().get("quoteResponse", {}).get("result", []):
                px = q.get("regularMarketPrice")
                if q.get("symbol") and px:
                    out[q["symbol"]] = float(px)
        except Exception:
            return out if out else {}
    return out


# ── fallback: per-ticker v8 chart (the path nifty-heatmap-core proved) ───────
def _one(tkr: str) -> tuple[str, float | None]:
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{tkr}",
            params={"interval": "1d", "range": "1d"},
            headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return tkr, None
        meta = r.json()["chart"]["result"][0]["meta"]
        px = meta.get("regularMarketPrice")
        return tkr, float(px) if px else None
    except Exception:
        return tkr, None


def fetch_v8(tickers: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for tkr, px in ex.map(_one, tickers):
            if px:
                out[tkr] = px
    return out


def main() -> int:
    uni = json.loads((DATA / "universe.json").read_text())
    by_yahoo = {s["yahoo"]: s["symbol"] for s in uni["stocks"]}
    tickers = sorted(by_yahoo)

    prev = {}
    spot_path = DATA / "spot.json"
    if spot_path.exists():
        try:
            prev = {r["symbol"]: r for r in
                    json.loads(spot_path.read_text()).get("rows", [])}
        except Exception:
            prev = {}

    t0 = time.time()
    prices = fetch_v7(tickers)
    path = "v7-batch"
    if len(prices) < len(tickers) * 0.5:
        got = fetch_v8([t for t in tickers if t not in prices])
        prices.update(got)
        path = "v8-fallback" if not prices else ("v7+v8" if path == "v7-batch" and got else "v8-fallback")
    elapsed = round(time.time() - t0, 1)

    now = datetime.now(IST)
    rows, stale = [], 0
    for tkr in tickers:
        sym = by_yahoo[tkr]
        px = prices.get(tkr)
        if px:
            rows.append({"symbol": sym, "ltp": round(px, 2),
                         "at": now.isoformat(), "stale": False})
        else:
            p = prev.get(sym)
            if p:
                age = None
                try:
                    age = int((now - datetime.fromisoformat(p["at"])).total_seconds())
                except Exception:
                    pass
                rows.append({**p, "stale": True, "stale_secs": age})
            else:
                rows.append({"symbol": sym, "ltp": None,
                             "at": now.isoformat(), "stale": True})
            stale += 1

    out = {
        "generated_at": now.isoformat(),
        "fetch_path": path,
        "elapsed_secs": elapsed,
        "counts": {"universe": len(tickers), "fresh": len(tickers) - stale,
                   "stale": stale},
        "rows": rows,
    }
    DATA.mkdir(exist_ok=True)
    spot_path.write_text(json.dumps(out, separators=(",", ":")))
    print(f"OK  {path}  fresh={len(tickers)-stale}/{len(tickers)}  "
          f"stale={stale}  {elapsed}s")
    # A totally dead sweep is a failure; a partial one is reported, not fatal.
    return 0 if stale < len(tickers) else 5


if __name__ == "__main__":
    raise SystemExit(main())
