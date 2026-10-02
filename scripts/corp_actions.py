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

# Days either side of a gap to look for an NSE action, and how close a ratio
# has to come to the observed gap before it is believed.
WINDOW = 5
RATIO_TOL = 0.08

SKIP_PURPOSE = ("DIV",)
GAP_KINDS = (
    ("DEMERGER", ("DEMERGER", "SPIN OFF", "SPIN-OFF", "SPINOFF")),
    ("SPLIT", ("SPLIT", "SUB-DIVISION", "SUBDIVISION", "SUB DIVISION")),
    ("BONUS", ("BONUS",)),
)
BONUS_RE = re.compile(r"BONUS\s*(\d+)\s*:\s*(\d+)")
SPLIT_RE = re.compile(r"FROM\s*RS?\.?\s*([\d.]+).*?TO\s*RS?\.?\s*([\d.]+)")

CACHE = Path(__file__).resolve().parent.parent / "data" / "corp_actions.json"

# Bump when the cached SHAPE changes. v1 stored {SYMBOL: PURPOSE}; v2 stores
# {SYMBOL: [[EX_DT, PURPOSE], ...]} so a forward-dated action can be found.
# Without this the new code read the old file and died on `for ex, pur in rows`
# with a bare string - which no local test caught, because every local run used
# a fresh cache path and never met the artefact CI actually had on disk.
CACHE_SCHEMA = 2


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
    """{SYMBOL: [[EX_DT, PURPOSE], ...]} from one PR file, or None if missing.

    EVERY row is kept, not only the ones whose ex-date is this file's date: a
    PR file is a FORWARD register, so the file for day D already lists actions
    dated weeks ahead. That is what makes a misdated gap findable at all.
    """
    try:
        r = session.get(PR_URL.format(day.strftime("%d%m%y")),
                        headers=HEADERS, timeout=45)
        if r.status_code != 200:
            return None
        z = zipfile.ZipFile(io.BytesIO(r.content))
        name = next(n for n in z.namelist() if n.lower().startswith("bc"))
    except Exception:
        return None
    out: dict[str, list] = {}
    for row in csv.DictReader(io.StringIO(z.read(name).decode("utf-8", "replace"))):
        sym = (row.get("SYMBOL") or "").strip()
        ex = (row.get("EX_DT") or "").strip()
        pur = (row.get("PURPOSE") or "").strip()
        if not sym or not ex or not pur:
            continue
        pair = [ex, pur]
        if pair not in out.setdefault(sym, []):
            out[sym].append(pair)
    return out


class Repairer:
    """Candidate gaps in, NSE verdicts out, with the answers cached on disk."""

    def __init__(self, cache_path: Path = CACHE):
        self.path = cache_path
        self.by_day: dict[str, dict] = {}
        self.unexplained: list[tuple[str, str, float]] = []
        self.applied: list[dict] = []
        try:
            blob = json.loads(self.path.read_text())
            if int(blob.get("schema", 1)) == CACHE_SCHEMA:
                self.by_day = blob.get("by_day", {})
            else:
                print(f"corp-action cache is schema {blob.get('schema', 1)}, "
                      f"need {CACHE_SCHEMA} - refetching")
        except Exception:
            self.by_day = {}
        self._session = requests.Session()
        self._aliases = _symbol_aliases(self._session)
        self._split_cache: dict[str, list] = {}

    def names_for(self, symbol: str) -> list[str]:
        """Today's symbol first, then anything it used to be called."""
        return [symbol] + sorted(self._aliases.get(symbol, ()))

    def actions_on(self, iso: str) -> dict:
        if iso not in self.by_day:
            got = _pr_actions(dt.date.fromisoformat(iso), self._session)
            # None means the archive had nothing for that date (weekend, holiday
            # or not published). Cached as {} either way so one bad day is not
            # refetched every run; a missing file simply explains no gap.
            self.by_day[iso] = got or {}
        return self.by_day[iso]

    def actions_for(self, symbol: str, iso: str, window: int = WINDOW):
        """Every action NSE lists for this name with an ex-date near `iso`.

        Walks PR files around the gap rather than trusting its date, because
        YAHOO MISDATES SPLIT SEAMS. Measured on the sector board's universe:
        MOTILALOFS (3:1 bonus, ex 2024-06-10), PARAS (1:2 split, ex 2025-07-04)
        and TRENT (1:2 bonus, ex 2026-06-04) ALL show their discontinuity on
        1 January - a day the exchange is shut. A date-exact match cannot see
        any of them, which is why the sector board matches on RATIO.
        """
        names = set(self.names_for(symbol))
        target = dt.date.fromisoformat(iso)
        out = []
        for off in range(-window, window + 1):
            day = target + dt.timedelta(days=off)
            if day.weekday() >= 5:                 # no PR file on a weekend
                continue
            for sym, rows in self.actions_on(day.isoformat()).items():
                if sym not in names:
                    continue
                for pair in rows:
                    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                        continue
                    ex, pur = pair
                    try:
                        ex_d = dt.date.fromisoformat(ex)
                    except ValueError:
                        continue
                    if abs((ex_d - target).days) <= window and (ex, pur) not in out:
                        out.append((ex, pur))
        return out

    def save(self):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(
            {"schema": CACHE_SCHEMA,
             "updated": dt.datetime.now(dt.timezone.utc).isoformat(),
             "note": "NSE PR corporate actions, keyed by ex-date. {} = asked, "
                     "nothing on that date.",
             "by_day": self.by_day}, separators=(",", ":"), sort_keys=True))

    def _split_seam(self, symbol: str, iso: str, seen: float):
        """(factor, label) if this gap is Yahoo's split seam, else None.

        YAHOO SPLITS THE SERIES AT NEW YEAR, NOT AT THE EX-DATE. Measured:
        MOTILALOFS closes 1,240.85 on 2023-12-29 and 315.01 on 2024-01-01, a
        x0.254 step - while its 4:1 split is dated 2024-06-10. PARAS and TRENT
        do the same thing in their own years. The split RECORD is accurate; the
        PRICES are rebased from 1 January of the split's year.

        So three things must agree before a gap is called a split seam:
          * the ratio reproduces an actual split Yahoo reports for this symbol,
          * the seam is in the SAME CALENDAR YEAR as that split,
          * and it is at or before the ex-date.
        All three are needed. RECLTD fell 25.2% on 2024-06-04 with the election
        result - a x0.748 step that matches its 4:3 split factor of 0.75 almost
        exactly - and is left alone only because that split was in 2022.
        """
        for when, label, factor in self._splits(symbol):
            if not factor or factor <= 0:
                continue
            if abs(seen - factor) > RATIO_TOL * factor:
                continue
            if when[:4] != iso[:4] or iso > when:
                continue
            return factor, f"Yahoo split {label} ex {when}"
        return None

    def _splits(self, symbol: str):
        if symbol in self._split_cache:
            return self._split_cache[symbol]
        out = []
        try:
            r = self._session.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
                "?interval=1d&range=10y&events=split", headers=HEADERS, timeout=30)
            ev = r.json()["chart"]["result"][0].get("events", {}).get("splits", {}) or {}
            for v in ev.values():
                num, den = float(v["numerator"]), float(v["denominator"])
                out.append((dt.date.fromtimestamp(v["date"]).isoformat(),
                            v.get("splitRatio") or f"{num:g}:{den:g}",
                            den / num if num else None))
        except Exception:
            out = []
        self._split_cache[symbol] = out
        return out

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
            kind = ratio = purpose = None
            matched_on = None
            for ex, pur in self.actions_for(symbol, iso):
                if ex != iso:
                    continue
                k, r = classify(pur)
                if k is not None:
                    kind, ratio, purpose, matched_on = k, r, pur, "nse-date"
                    break
            if kind is None:
                hit = self._split_seam(symbol, iso, seen)
                if hit:
                    ratio, purpose = hit
                    kind, matched_on = "SPLIT", "yahoo-split-seam"
            if kind is None:
                self.unexplained.append((symbol, iso, round((seen - 1) * 100, 1)))
                continue
            if kind in ("SPLIT", "BONUS") and ratio:
                for j in range(i):
                    factor[j] *= ratio
                self.applied.append({"symbol": symbol, "ex_date": iso, "kind": kind,
                                     "ratio": round(ratio, 6), "purpose": purpose,
                                     "matched_on": matched_on})
            else:
                # economic, or terms we cannot parse - no ratio is defensible
                start = max(start, i)
                self.applied.append({"symbol": symbol, "ex_date": iso,
                                     "kind": kind or "UNPARSED", "ratio": None,
                                     "purpose": purpose, "matched_on": matched_on})

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
