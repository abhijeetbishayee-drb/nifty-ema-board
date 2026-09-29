"""Current Point & Figure column: X = demand, O = supply.

WHY THIS IS A PORT AND NOT A SUBMODULE
--------------------------------------
`pnf-charts` is a PRIVATE repo. A public repo's Actions token cannot clone it,
so the submodule route fails at checkout ("Repository not found") and the whole
column came back null.

The first instinct was to refuse to duplicate it -- two sources of truth for one
fact is what `parity_check.py` exists to prevent in `fno-rollover`. On reflection
that conflates two different kinds of sharing:

  * fno-rollover shares a CURATED TAXONOMY -- a dataset that drifts as names are
    added and removed. Duplicating that is genuinely unsafe.
  * this shares a FIXED ALGORITHM parameterised by (box_pct, reversal). Point &
    Figure box arithmetic does not drift; only those two constants can.

So the constants are pinned here, asserted against pnf-charts' own `PRESETS`
by `tests/test_pnf_parity.py`, and that test also proves this port produces
IDENTICAL output to `PnFChart.from_ohlc` on every locally cached symbol. The
test runs on the Mac, where the private repo is checked out; it cannot run on a
public runner, and it says so rather than passing vacuously.

Ported from pnf/boxes.py + pnf/chart.py @ pnf-charts, read 2026-09-29.
Only the LAST column is needed here, so fill timestamps and the level map --
which exist in the original to stop look-ahead in backtests -- are omitted.
"""
from __future__ import annotations

import math

# Must match pnf-charts PRESETS["short"] -- asserted by the parity test.
BOX_PCT = 0.25
REVERSAL = 3

# A base far below any traded price, so every box index is positive and the
# grid depends only on (base, pct) -- never on the data window. Two charts of
# the same symbol over different ranges therefore share a grid.
BASE = 1.0

X, O = 1, -1


def box_index(price: float, pct: float = BOX_PCT) -> int:
    """Box index containing `price`. Geometric, not arithmetic."""
    if price <= 0:
        raise ValueError(f"non-positive price: {price}")
    return math.floor(math.log(price / BASE) / math.log1p(pct / 100.0) + 1e-9)


def last_column(highs, lows, pct: float = BOX_PCT,
                rev: int = REVERSAL) -> tuple[str, int] | None:
    """('X'|'O', box count) for the final column, or None if unbuildable.

    Mirrors PnFChart.from_ohlc's state machine exactly: seed from the first
    bar's own range (X is the conventional default for a flat bar), extend on a
    new extreme, reverse only on a `rev`-box move against the column.
    """
    bars = [(h, l) for h, l in zip(highs, lows)
            if h is not None and l is not None
            and h == h and l == l and h > 0 and l > 0]
    if not bars:
        return None

    direction = top = bottom = None
    for h, l in bars:
        hb, lb = box_index(h, pct), box_index(l, pct)

        if direction is None:
            direction, top = X, hb
            bottom = lb if hb > lb else hb
            continue

        if direction == X:
            if hb > top:                        # extend
                top = hb
            elif lb <= top - rev:               # reverse into O
                direction, top, bottom = O, top - 1, lb
        else:
            if lb < bottom:                     # extend
                bottom = lb
            elif hb >= bottom + rev:            # reverse into X
                direction, top, bottom = X, hb, bottom + 1

    return ("X" if direction == X else "O"), (top - bottom + 1)
