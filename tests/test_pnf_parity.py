"""Prove scripts/pnf_column.py matches pnf-charts exactly.

This is the guard that makes porting the PnF algorithm safe instead of
reckless. It asserts two separate things:

  1. the CONSTANTS here still match pnf-charts' own PRESETS["short"], because
     those two numbers are the only part of PnF box arithmetic that can drift;
  2. the OUTPUT is identical to PnFChart.from_ohlc on every locally cached
     symbol -- same direction, same box count, no tolerance.

pnf-charts is private, so this cannot run on a public GitHub runner. It SKIPS
loudly there rather than passing vacuously -- a green gate that cannot go red
is not a gate. Run it on the Mac before trusting a change to pnf_column.py:

    python3 tests/test_pnf_parity.py
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# The private engine lives beside this repo on the Mac. Absent on a runner.
PNF_CHARTS = ROOT.parent / "pnf-charts"


def main() -> int:
    if not (PNF_CHARTS / "pnf" / "chart.py").exists():
        print(f"SKIP  pnf-charts not found at {PNF_CHARTS}")
        print("      This test needs the private engine to compare against.")
        print("      It cannot run on a public runner -- that is expected.")
        return 0

    sys.path.insert(0, str(PNF_CHARTS))
    import pandas as pd
    from pnf.boxes import BoxScale, PRESETS
    from pnf.chart import PnFChart

    import pnf_column as port

    # ---- 1. constants still agree ------------------------------------
    ref_pct, ref_rev = PRESETS["short"]
    if (ref_pct, ref_rev) != (port.BOX_PCT, port.REVERSAL):
        print(f"FAIL  preset drift: pnf-charts short = ({ref_pct}, {ref_rev}), "
              f"port = ({port.BOX_PCT}, {port.REVERSAL})")
        return 1
    print(f"OK    preset matches pnf-charts short = {ref_pct}% x {ref_rev}")

    # The board also emits a slower column. Pin it to pnf-charts' own "medium"
    # the same way, so a second scale cannot drift unnoticed either.
    import daily_levels as dl
    med_pct, med_rev = PRESETS["medium"]
    if (med_pct, med_rev) != (dl.PNF_MED_BOX_PCT, dl.PNF_MED_REVERSAL):
        print(f"FAIL  preset drift: pnf-charts medium = ({med_pct}, {med_rev}), "
              f"board = ({dl.PNF_MED_BOX_PCT}, {dl.PNF_MED_REVERSAL})")
        return 1
    print(f"OK    preset matches pnf-charts medium = {med_pct}% x {med_rev}")

    # ---- 2. output is identical on real data -------------------------
    cache = PNF_CHARTS / "data" / "ohlc"
    files = sorted(glob.glob(str(cache / "*_2y_1d.csv")))
    if not files:
        print(f"SKIP  no cached OHLC under {cache}")
        return 0

    checked = mismatched = 0
    for f in files:
        sym = os.path.basename(f).split(".NS_")[0]
        df = pd.read_csv(f, index_col=0, parse_dates=True)
        if df.empty or "High" not in df or "Low" not in df:
            continue
        try:
            ch = PnFChart.from_ohlc(df, BoxScale(ref_pct, ref_rev), sym)
        except Exception:
            continue
        if not ch.columns:
            continue
        col = ch.columns[-1]
        ref = ("X" if col.direction > 0 else "O", col.boxes)

        got = port.last_column(df["High"].tolist(), df["Low"].tolist(),
                               ref_pct, ref_rev)
        checked += 1
        if got != ref:
            mismatched += 1
            print(f"FAIL  {sym}: pnf-charts={ref}  port={got}")

    if mismatched:
        print(f"FAIL  {mismatched}/{checked} symbols differ")
        return 1

    print(f"OK    {checked}/{checked} symbols identical (direction + box count)")

    # ---- 3. the test can actually fail -------------------------------
    # A parity check that cannot go red proves nothing. Feed the port a WRONG
    # reversal and confirm the comparison notices.
    #
    # This must scan, not sample: for a name whose final column is one long
    # extension, widening the reversal does not move where that column began,
    # so a single symbol can legitimately agree under 3 and 5 boxes. The claim
    # being tested is that the comparison detects drift SOMEWHERE -- checking
    # one symbol found a false negative on the first run of this test.
    detected = None
    for f in files:
        sym = os.path.basename(f).split(".NS_")[0]
        df = pd.read_csv(f, index_col=0, parse_dates=True)
        if df.empty or "High" not in df or "Low" not in df:
            continue
        try:
            ch = PnFChart.from_ohlc(df, BoxScale(ref_pct, ref_rev), sym)
        except Exception:
            continue
        if not ch.columns:
            continue
        col = ch.columns[-1]
        ref = ("X" if col.direction > 0 else "O", col.boxes)
        broken = port.last_column(df["High"].tolist(), df["Low"].tolist(),
                                  ref_pct, ref_rev + 2)
        if broken != ref:
            detected = (sym, ref, broken)
            break

    if detected is None:
        print(f"FAIL  self-check: a wrong reversal matched on ALL {checked} "
              "symbols -- this comparison cannot detect drift and must not "
              "be trusted")
        return 1
    sym, ref, broken = detected
    print(f"OK    self-check: reversal {ref_rev}->{ref_rev + 2} changes {sym} "
          f"({ref} -> {broken}), so the comparison can fail")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
