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

### Spot fetch uses yfinance, and that was measured

The first version called Yahoo's JSON endpoints directly (`v7/finance/quote`
batched with a cookie+crumb, falling back to `v8/finance/chart`). On a GitHub
Actions runner that scored **fresh=0/500 in 7.5 seconds** — every request
refused. Actions IP ranges are blocked at Yahoo's edge for the plain endpoints,
and the same endpoints return HTTP 429 from a residential IP once polled.

In the same workflow on the same runners, `yf.download()` pulled 500 symbols ×
2 years successfully. yfinance maintains the cookie/crumb session the raw calls
could not establish. Reachability decided this, not speed.

### Staleness is reported, never hidden

A symbol that fails keeps its previous price and is marked `stale`. The page
greys those rows; the header shows `fresh/total`. A board that shows a
ten-minute-old price as if it were live is worse than one that admits it is
behind.

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

### The DAILY build needs the same treatment, for a different reason

`daily.yml` only asks for one run a day, so `schedule` does fire it -- but it
fires it LATE and at an unpredictable hour. On 2026-10-05 the cron asked for
10:15 UTC and GitHub ran it at **19:18 UTC, nine hours adrift**. That matters
because Yahoo posts NSE daily bars per-symbol over many hours: the 19:18 run
found no 2026-10-05 bars at all and committed a board still dated 10-01, and
by 09:30 the next morning only 414 of 747 names had caught up. The run
succeeded; the data did not move.

Second job, same shape as the one above:

| field | value |
|---|---|
| URL | `https://api.github.com/repos/abhijeetbishayee-drb/nifty-ema-board/actions/workflows/daily.yml/dispatches` |
| Method | `POST` |
| Body | `{"ref":"main"}` |
| Schedule | minute **30**, hours **8** and **20**, every day, timezone **Asia/Kolkata** |

Headers:

    Accept: application/vnd.github+json
    Authorization: Bearer <PAT>
    X-GitHub-Api-Version: 2022-11-28
    Content-Type: application/json

A success is **HTTP 204 with an empty body** -- not 200, and cron-job.org will
show it as an empty response. The PAT is the same fine-grained token the
1-minute job uses (repo `nifty-ema-board`, **Actions: Read and write**); no
extra scope is needed.

Why 08:30 and 20:30, and why running on weekends is harmless:

* **20:30 IST** is five hours after the close -- the primary build.
* **08:30 IST** the next morning is the catch-up for the names Yahoo had not
  posted yet, and it is safely before the open. It does not need to be: since
  2026-10-06 `build_row` takes a cutoff from `complete_through()` and ignores
  any bar for a session that has not finished, so a dispatch at ANY hour is
  safe. Before that fix a mid-session run would have reported the forming
  candle as a completed session.
* A dispatch on a holiday or a weekend rebuilds identical data, and the commit
  step is `git diff --staged --quiet || git commit` -- so it pushes nothing.

**Honest limit, measured on this repo:**

| stage | time |
|---|---|
| fetch 500 symbols (yfinance, chunks of 50) | **23s** |
| checkout + setup-python + cached pip + commit/push | ~40s |
| **total run** | **~63s** |

So a full cycle is a little *over* a minute, not under it. At a 1-minute ping
cadence `cancel-in-progress: true` will cancel some in-flight runs, making the
effective refresh roughly **60–70s** rather than a clean 60. Pip caching already
took the fetch from 32.7s to 23.0s; the remaining cost is runner startup, which
cannot be removed. The page's freshness clock shows the truth either way.

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
with, so a name reads identically on this board and on the PnF board.

`pnf-charts` is private, so a public runner cannot clone it as a submodule and
the column came back empty on the first build. `scripts/pnf_column.py` is a
self-contained port of its box arithmetic instead. Porting a **fixed algorithm**
is not the same risk as duplicating a **curated taxonomy** — PnF box maths does
not drift, only the two constants `(box_pct, reversal)` can — so those are
pinned and guarded:

```
python3 tests/test_pnf_parity.py
OK    preset matches pnf-charts short = 0.25% x 3
OK    216/216 symbols identical (direction + box count)
OK    self-check: reversal 3->5 changes BAJAJ-AUTO (('X', 4) -> ('O', 8)), so the comparison can fail
```

The test needs the private repo, so it runs on the Mac and **skips loudly** on a
runner rather than passing vacuously. Run it before trusting any change to
`pnf_column.py`. Its third assertion exists because the first version of the
self-check sampled one symbol and produced a false negative — a name whose final
column is one long extension legitimately agrees under 3 and 5 boxes.

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
