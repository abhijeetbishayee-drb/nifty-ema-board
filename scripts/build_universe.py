#!/usr/bin/env python3
"""Build the rank 251-750 universe from NSE's own published index constituents.

WHY THIS IS NOT A MARKET-CAP RANKING WE COMPUTE OURSELVES
---------------------------------------------------------
NSE's index family partitions the full market-cap ranking exactly:

    NIFTY 100        ranks   1-100
    NIFTY Midcap 150 ranks 101-250
    NIFTY Smallcap 250 ranks 251-500
    NIFTY Microcap 250 ranks 501-750

so "ranks 251-750" IS, by definition, Smallcap250 union Microcap250. Verified
2026-09-28 by set algebra against NSE's own CSVs, all four relations exact:

    Nifty100 | Midcap150 | Smallcap250 == Nifty500      (symmetric diff 0)
    Smallcap250 subset of Nifty500                       True
    Microcap250 intersect Nifty500 == empty              True
    Nifty500 | Microcap250 == NIFTY Total Market         (symmetric diff 0)
    TotalMarket - band == Nifty100 | Midcap150           True

`verify_bands()` below re-runs those assertions on every build. If NSE changes
its methodology the build FAILS rather than silently emitting a wrong universe.

COUNT IS NOT EXACTLY 500 AND THAT IS CORRECT
--------------------------------------------
Today the band is 505 names (Smallcap250 carries 251, Microcap250 carries 254).
NSE constituent counts drift between semi-annual reconstitutions. The CSVs carry
no market-cap column, so trimming to exactly 500 would mean inventing a ranking
NSE does not publish. We emit the true band and record its size.

AKAMAI
------
These archive CSVs download with a plain browser User-Agent. The Playwright
route in `nse-akamai-spike` is needed for www.nseindia.com/api/* but NOT here.
"""
from __future__ import annotations

import csv
import io
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

BASE = "https://nsearchives.nseindia.com/content/indices"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/csv,*/*"}

# The two lists that make up the band, plus the three used only to prove the
# partition still holds.
LISTS = {
    "smallcap250": "ind_niftysmallcap250list.csv",
    "microcap250": "ind_niftymicrocap250_list.csv",
    "nifty100":    "ind_nifty100list.csv",
    "midcap150":   "ind_niftymidcap150list.csv",
    "nifty500":    "ind_nifty500list.csv",
    "totalmarket": "ind_niftytotalmarket_list.csv",
}


def fetch_list(fname: str, timeout: int = 30) -> list[dict]:
    r = requests.get(f"{BASE}/{fname}", headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def symbols(rows: list[dict]) -> set[str]:
    key = next(c for c in rows[0] if "Symbol" in c)
    return {r[key].strip() for r in rows if r[key].strip()}


def verify_bands(S: dict[str, set[str]]) -> list[str]:
    """Return a list of failures; empty means NSE's partition still holds."""
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    check("Nifty100|Midcap150|Smallcap250 == Nifty500",
          (S["nifty100"] | S["midcap150"] | S["smallcap250"]) == S["nifty500"])
    check("Smallcap250 subset of Nifty500",
          S["smallcap250"] <= S["nifty500"])
    check("Microcap250 disjoint from Nifty500",
          not (S["microcap250"] & S["nifty500"]))
    check("Nifty500|Microcap250 == TotalMarket",
          (S["nifty500"] | S["microcap250"]) == S["totalmarket"])
    check("TotalMarket - band == Nifty100|Midcap150",
          (S["totalmarket"] - (S["smallcap250"] | S["microcap250"]))
          == (S["nifty100"] | S["midcap150"]))
    return fails


def main() -> int:
    raw = {}
    for key, fname in LISTS.items():
        try:
            raw[key] = fetch_list(fname)
        except Exception as e:
            print(f"FATAL: could not fetch {fname}: {e}", file=sys.stderr)
            return 2

    S = {k: symbols(v) for k, v in raw.items()}

    fails = verify_bands(S)
    if fails:
        print("FATAL: NSE index partition no longer holds -- refusing to emit a "
              "universe that may not be ranks 251-750:", file=sys.stderr)
        for f in fails:
            print(f"  FAILED: {f}", file=sys.stderr)
        return 3

    # Company names + industry, straight from NSE's own CSV.
    meta: dict[str, dict] = {}
    for band_name in ("smallcap250", "microcap250"):
        rows = raw[band_name]
        skey = next(c for c in rows[0] if "Symbol" in c)
        nkey = next((c for c in rows[0] if "Company" in c), None)
        ikey = next((c for c in rows[0] if "Industry" in c), None)
        for r in rows:
            sym = r[skey].strip()
            if not sym:
                continue
            meta[sym] = {
                "symbol":   sym,
                "yahoo":    f"{sym}.NS",
                "name":     (r.get(nkey) or "").strip() if nkey else "",
                "industry": (r.get(ikey) or "").strip() if ikey else "",
                "band":     "Smallcap250" if band_name == "smallcap250" else "Microcap250",
            }

    universe = sorted(meta.values(), key=lambda d: d["symbol"])
    out = {
        "generated_at": datetime.now(IST).isoformat(),
        "definition": "NSE ranks 251-750 by full market cap = NIFTY Smallcap 250 "
                      "UNION NIFTY Microcap 250",
        "partition_verified": True,
        "counts": {
            "smallcap250": len(S["smallcap250"]),
            "microcap250": len(S["microcap250"]),
            "band_total":  len(universe),
        },
        "stocks": universe,
    }

    DATA.mkdir(exist_ok=True)
    (DATA / "universe.json").write_text(json.dumps(out, indent=2))

    print(f"OK  partition verified (5/5 relations)")
    print(f"    Smallcap250 {len(S['smallcap250'])}  + Microcap250 "
          f"{len(S['microcap250'])}  = band {len(universe)}")
    print(f"    wrote {DATA / 'universe.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
