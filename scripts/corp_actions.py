"""Repair corporate-action cliffs in a daily price series, against NSE.

WHY THIS IS NOT A SIZE DETECTOR
-------------------------------
A 78% fall in one bar looks like a split and may be a crash. Trying to tell
them apart by SIZE has already been tested to failure on the sector board: a
threshold-only detector "repaired" ten real crashes (ADANIENT -28.2%,
INDUSINDBK -27.2%, IEX -29.6%). Worse, the obvious ratios collide - a 3:4
split is 0.75 and a genuine -25% day is 0.75, and nothing in the price can
separate them.

Yahoo cannot settle it either. Its `adjclose` gaps identically to `close`
(checked on INDIAGLYCO: 1111.70 -> 236.20 in both), and its split records
carry the wrong dates - for INDIAGLYCO it lists a 2:1 on 2025-08-12 and says
nothing at all about the 2026-09-02 event that actually moved the price.

So the size test is only a CANDIDATE filter, and the verdict comes from NSE:
the corporate-action file inside the daily PR archive, which carries SYMBOL,
EX_DT and the announced PURPOSE for every listed equity. A gap is repaired
only where NSE names an action on that exact date. INDIAGLYCO's 2026-09-02
cliff resolves to PURPOSE=DEMERGER, which is also why guessing "1:5 split"
from the 0.21 ratio would have been wrong.

COSMETIC vs ECONOMIC, the distinction that decides the repair:

  * SPLIT / BONUS are COSMETIC. The share count changed and the business did
    not, so the announced ratio back-adjusts the older bars onto the new basis
    and the series carries on.
  * A DEMERGER is ECONOMIC. The company itself changed, so no arithmetic makes
    the older bars describe the business now being plotted. History is
    TRUNCATED at the ex-date and the name carries less of it.

Dividends are excluded on purpose - see the sector board's note; the short
version is that they are tiny next to this threshold and the board quotes the
price change a holder actually sees.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import zipfile
from pathlib import Path

import requests

PR_URL = "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{}.zip"
SYMCHG_URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}

# Candidate only. Set where no ordinary session reaches: the sector board
# measured 112,164 sessions to calibrate its own floor, and a 1-bar move past
# 25% is rare enough that asking NSE about each one costs a handful of fetches.
JUMP = 0.25

SKIP_PURPOSE = ("DIV",)
GAP_KINDS = (
    ("DEMERGER", ("DEMERGER", "SPIN OFF", "SPIN-OFF", "SPINOFF")),
    ("SPLIT", ("SPLIT", "SUB-DIVISION", "SUBDIVISION", "SUB DIVISION")),
    ("BONUS", ("BONUS",)),
)
BONUS_RE = re.compile(r"BONUS\s*(\d+)\s*:\s*(\d+)")
SPLIT_RE = re.compile(r"FROM\s*RS?\.?\s*([\d.]+).*?TO\s*RS?\.?\s*([\d.]+)")

CACHE = Path(__file__).resolve().parent.parent / "data" / "corp_actions.json"


def classify(purpose: str):
    """(kind, ratio) for a gap-making action, else (None, None).

    `ratio` is the factor the price is multiplied by on the ex-date, taken from
    the ANNOUNCED TERMS - the only ratio worth dividing out. A demerger has
    none: its ex-value is discovered across the resulting entities.
    """
    p = (purpose or "").upper().strip()
    if p.startswith(SKIP_PURPOSE):
        return None, None
    kind = next((k for k, words in GAP_KINDS if any(w in p for w in words)), None)
    if kind is None:
        return None, None
    if kind == "BONUS":
        m = BONUS_RE.search(p)                      # a new for every b held
        return kind, (int(m.group(2)) / (int(m.group(1)) + int(m.group(2)))) if m else None
    if kind == "SPLIT":
        m = SPLIT_RE.search(p)                      # face value FROM x TO y
        return kind, (float(m.group(2)) / float(m.group(1))) if m else None
    return kind, None


def _symbol_aliases(session) -> dict[str, set[str]]:
    """{current symbol: every name it has traded under}.

    NSE files a corporate action under the symbol OF THAT DAY, and a demerger
    often renames the company at the same time - so the action and the ticker
    disagree exactly when it matters most. HEG's 2026-09-07 demerger is filed
    under HEG; the board carries HEGAM, because NSE renamed it on 22-Sep-2026.
    Matching on today's ticker alone silently misses those, and a prefix guess
    would match the wrong company. This is NSE's own rename register.
    """
    try:
        r = session.get(SYMCHG_URL, headers=HEADERS, timeout=45)
        if r.status_code != 200:
            return {}
        rows = list(csv.reader(io.StringIO(r.content.decode("utf-8", "replace"))))
    except Exception:
        return {}
    nxt: dict[str, str] = {}
    for row in rows:
        if len(row) >= 3:
            old_s, new_s = row[1].strip(), row[2].strip()
            if old_s and new_s:
                nxt[old_s] = new_s
    back: dict[str, set[str]] = {}
    for old_s in nxt:                       # follow the chain to today's symbol
        cur, seen = old_s, {old_s}
        while cur in nxt and nxt[cur] not in seen:
            cur = nxt[cur]
            seen.add(cur)
        back.setdefault(cur, set()).update(seen - {cur})
    return back


def _pr_actions(day: dt.date, session) -> dict | None:
    """{SYMBOL: PURPOSE} with EX_DT == `day`, or None if the file is missing."""
    try:
        r = session.get(PR_URL.format(day.strftime("%d%m%y")),
                        headers=HEADERS, timeout=45)
        if r.status_code != 200:
            return None
        z = zipfile.ZipFile(io.BytesIO(r.content))
        name = next(n for n in z.namelist() if n.lower().startswith("bc"))
    except Exception:
        return None
    iso = day.isoformat()
    out = {}
    for row in csv.DictReader(io.StringIO(z.read(name).decode("utf-8", "replace"))):
        if (row.get("EX_DT") or "").strip() == iso:
            out.setdefault((row.get("SYMBOL") or "").strip(), row.get("PURPOSE") or "")
    return out


class Repairer:
    """Candidate gaps in, NSE verdicts out, with the answers cached on disk."""

    def __init__(self, cache_path: Path = CACHE):
        self.path = cache_path
        self.by_day: dict[str, dict] = {}
        self.unexplained: list[tuple[str, str, float]] = []
        self.applied: list[dict] = []
        try:
            self.by_day = json.loads(self.path.read_text()).get("by_day", {})
        except Exception:
            self.by_day = {}
        self._session = requests.Session()
        self._aliases = _symbol_aliases(self._session)

    def names_for(self, symbol: str) -> list[str]:
        """Today's symbol first, then anything it used to be called."""
        return [symbol] + sorted(self._aliases.get(symbol, ()))

    def actions_on(self, iso: str) -> dict:
        if iso not in self.by_day:
            got = _pr_actions(dt.date.fromisoformat(iso), self._session)
            # None means the archive had nothing for that date (holiday, or not
            # published). Cached as {} either way so one bad day is not refetched
            # on every run; a missing file simply explains no gap.
            self.by_day[iso] = got or {}
        return self.by_day[iso]

    def save(self):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(
            {"updated": dt.datetime.now(dt.timezone.utc).isoformat(),
             "note": "NSE PR corporate actions, keyed by ex-date. {} = asked, "
                     "nothing on that date.",
             "by_day": self.by_day}, separators=(",", ":"), sort_keys=True))

    def repair(self, symbol: str, dates: list[str], ohlc: dict[str, list]):
        """Return (dates, ohlc) with cliffs removed. Inputs are not mutated.

        `ohlc` maps a column name to a list the same length as `dates`.
        """
        closes = ohlc.get("Close") or []
        n = min(len(dates), len(closes))
        if n < 3:
            return dates, ohlc

        cuts = []                               # (index, iso, ratio_seen)
        for i in range(1, n):
            a, b = closes[i - 1], closes[i]
            if not a or not b or a <= 0 or b <= 0:
                continue
            if abs(b / a - 1.0) >= JUMP:
                cuts.append((i, dates[i], b / a))
        if not cuts:
            return dates, ohlc

        start = 0                               # truncate point after economics
        factor = [1.0] * n                      # multiplier for bars BEFORE a cut
        for i, iso, seen in cuts:
            day_actions = self.actions_on(iso)
            purpose = next((day_actions[nm] for nm in self.names_for(symbol)
                            if nm in day_actions), None)
            kind, ratio = classify(purpose) if purpose else (None, None)
            if kind is None:
                self.unexplained.append((symbol, iso, round((seen - 1) * 100, 1)))
                continue
            if kind in ("SPLIT", "BONUS") and ratio:
                for j in range(i):
                    factor[j] *= ratio
                self.applied.append({"symbol": symbol, "ex_date": iso, "kind": kind,
                                     "ratio": round(ratio, 6), "purpose": purpose})
            else:
                # economic, or terms we cannot parse - no ratio is defensible
                start = max(start, i)
                self.applied.append({"symbol": symbol, "ex_date": iso,
                                     "kind": kind or "UNPARSED", "ratio": None,
                                     "purpose": purpose})

        out = {}
        for col, vals in ohlc.items():
            v = list(vals[:n])
            if col in ("Open", "High", "Low", "Close", "Adj Close"):
                v = [x * factor[j] if x else x for j, x in enumerate(v)]
            out[col] = v[start:]
        return dates[start:n], out


    def repair_df(self, symbol: str, df):
        """DataFrame adapter: same repair, applied to an OHLC frame in place of
        lists. Returns a NEW frame; the caller's is untouched."""
        if df is None or getattr(df, "empty", True) or "Close" not in df:
            return df
        dates = [d.date().isoformat() if hasattr(d, "date") else str(d)[:10]
                 for d in df.index]
        cols = {c: [None if v != v else float(v) for v in df[c].tolist()]
                for c in df.columns if c in
                ("Open", "High", "Low", "Close", "Adj Close")}
        if not cols.get("Close"):
            return df
        keep_dates, fixed = self.repair(symbol, dates, cols)
        if len(keep_dates) == len(dates) and all(
                (a is None and b is None) or (a == b)
                for a, b in zip(cols["Close"], fixed["Close"])):
            return df                                   # nothing to do
        out = df.iloc[len(dates) - len(keep_dates):].copy()
        for c, vals in fixed.items():
            out[c] = vals
        return out
