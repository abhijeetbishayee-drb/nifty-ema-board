# EMA Board — NSE ranks 251–750

A self-updating table of the 500 NSE stocks ranked 251st to 750th by market
cap, tracking how close each one is to its **44 EMA**, which side of it price
sits on, the full EMA ladder, 52-week extremes, and the current Point & Figure
column (X = demand, O = supply).

Live: `https://abhijeetbishayee-drb.github.io/nifty-ema-board/`

---

## The universe is derived, not curated

NSE's index family partitions the full market-cap ranking exactly:

| index | ranks |
|---|---|
| NIFTY 100 | 1–100 |
| NIFTY Midcap 150 | 101–250 |
| **NIFTY Smallcap 250** | **251–500** |
| **NIFTY Microcap 250** | **501–750** |

so **ranks 251–750 *is* Smallcap 250 ∪ Microcap 250**, by definition rather
than by estimate. `scripts/build_universe.py` downloads NSE's own constituent
CSVs and asserts all five partition relations on every run:

```
Nifty100 | Midcap150 | Smallcap250 == Nifty500      (symmetric difference 0)
Smallcap250 ⊆ Nifty500
Microcap250 ∩ Nifty500 == ∅
Nifty500 | Microcap250 == NIFTY Total Market        (symmetric difference 0)
TotalMarket − band == Nifty100 | Midcap150
```

If NSE changes methodology the build **fails** rather than quietly emitting a
list that is no longer ranks 251–750.

**The count is exactly 500, and the arithmetic is worth knowing.** Smallcap250
carries 251 rows and Microcap250 carries 254 — 505 raw. Five of those are NSE
**`DUMMY*` placeholder scrips**: corporate-action stubs (`Dummy HEG Ltd.`,
`Dummy India Glycols ltd. 1` and `2`, `Dummy Inox Green Ltd.`, `Dummy Triveni
Ltd.`) that are not tradeable and have no price history anywhere. Excluding
them takes 505 → **500 real names**. Nothing is trimmed arbitrarily; the build
records exactly which symbols it dropped and why.

Raw constituent counts still drift between semi-annual reconstitutions, so the
total may not stay at 500 forever — `data/universe.json` always reports
`raw_band`, `dummy_excluded` and `tradeable_total` so a change is visible.

Sector labels come from NSE's own `Industry` column (22 industries), so they
stay current automatically — unlike a hand-curated map.

**Akamai note:** these archive CSVs download with a plain browser User-Agent.
The Playwright route in `nse-akamai-spike` is needed for `nseindia.com/api/*`
but **not** for `nsearchives.nseindia.com/content/indices/*`.

---

## Two cadences, because only one thing moves

Every column except spot is a **daily** value. Re-deriving EMAs every minute
for 500 names would be ~190k Yahoo requests/day and earns HTTP 429 — measured,
not assumed.

| job | when | output | cost |
|---|---|---|---|
| `scripts/daily_levels.py` | once, 15:45 IST | `data/levels.json` | 500 histories |
| `scripts/refresh_spot.py` | every minute | `data/spot.json` | ~6 requests |

The page joins them in the browser: **Dist %** and **Side** are recomputed from
(live spot, daily 44 EMA) on every poll, so they move every minute without
anyone refetching a candle.

### Spot fetch has two paths

* **primary** `v7/finance/quote`, 100 symbols per call → ~6 calls for the universe
* **fallback** `v8/finance/chart`, 1 symbol per call → ~500 calls, threaded

v7 needs a cookie+crumb pair and is ~80× cheaper when it works. The fallback is
the path `nifty-heatmap-core` already proves in production. Whichever one ran is
reported in the page header, so you can see which path you are on.

### Staleness is reported, never hidden

A symbol that fails keeps its previous price, is marked `stale`, and the page
greys that row. The header shows `fresh/total`. A board that shows a ten-minute
-old price as if it were live is worse than one that admits it is behind.

---

## The 1-minute cadence does not come from `schedule`

GitHub throttles scheduled workflows and will not honour a 1-minute cron. The
real cadence comes from an **external pinger (cron-job.org) calling the
`workflow_dispatch` API every minute** — the same arrangement
`nifty-heatmap-web` already runs. The `*/5` cron in `refresh.yml` is only a
safety net for when the pinger dies.

To wire it up: create a cron-job.org job hitting
`POST https://api.github.com/repos/abhijeetbishayee-drb/nifty-ema-board/actions/workflows/refresh.yml/dispatches`
with `{"ref":"main"}` and a fine-grained PAT with Actions: write.

**Honest limit:** each run costs ~40–60s of checkout + setup + install before
it fetches anything. On the v7 batch path a sweep finishes comfortably inside a
minute; if Yahoo forces the v8 fallback the effective cadence stretches toward
~90s. `cancel-in-progress: true` means a superseded run is dropped rather than
queued, and the page's freshness counter shows the truth either way.

---

## Columns

| column | source | notes |
|---|---|---|
| Spot | 1-min job | falls back to previous close if the sweep failed |
| 44 EMA | daily | mean-seeded, verified identical to pandas `ewm` |
| Dist % | computed live | `(spot − ema44) / ema44`; sorted by **absolute** distance |
| Side | computed live | Above / Below the 44 EMA |
| Col | daily | PnF column: **X = demand**, **O = supply** |
| 9 / 14 / 25 / 50 / 100 / 200 | daily | same EMA convention |
| 52w High / Low | daily | 252-session window; suppressed under 200 bars |
| % off H / % off L | computed live | distance from each extreme |

PnF uses **0.25% × 3 on daily bars** — the same box scale `pnf-charts` draws
with, so a name reads identically on this board and on the PnF board. Anything
else would be two sources of truth for one fact.

`ema_ok` in `levels.json` records which spans had enough history to be
trustworthy. A 200 EMA needs >200 sessions; 2 years (~500) is pulled. A value
that is present but unreliable is worse than one that is absent.

---

## Filters

Search (symbol or company), band (Smallcap/Microcap), industry, Above/Below the
44 EMA, PnF X/O, and a max-absolute-distance cutoff. Every column sorts. They
compose — e.g. *Above 44 + O supply* isolates names holding above the average
while their PnF column has already turned down.

---

## Layout

```
scripts/build_universe.py   NSE constituents -> data/universe.json  (+ partition assertions)
scripts/daily_levels.py     2y history       -> data/levels.json    (EMAs, 52w, PnF)
scripts/refresh_spot.py     last price       -> data/spot.json      (1-min)
index.html                  joins levels + spot, sorts, filters
core/                       pnf-charts submodule (BoxScale, PnFChart)
```

Clone with `--recurse-submodules`; `core/` is the PnF engine.
