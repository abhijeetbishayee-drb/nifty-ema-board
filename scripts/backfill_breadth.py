#!/usr/bin/env python3
"""Rebuild a day's advance/decline series from the committed spot snapshots.

WHY THIS EXISTS
---------------
The live recorder in refresh_spot.py only captures a minute it is actually
running for, so a session that ends before the recorder ships -- or any gap in
the refresh chain -- leaves a hole that nothing can fill afterwards. It shipped
on 2026-10-09 after that day's close, and the board sat on "no readings yet
today" for a full session whose prices were all sitting in git the whole time.

They were, because data/spot.json is COMMITTED on every refresh: the repo
already holds one snapshot of all 750 prices per minute. Replaying those
commits reconstructs the series exactly as the live recorder would have
written it, so the history is recoverable rather than lost.

Each snapshot is scored against the levels.json FROM THE SAME COMMIT, not
today's. prev_close rolls over at the daily build, and scoring an old session
against a newer close would compare a price to a close that had not happened
yet.

    python3 scripts/backfill_breadth.py              # today, IST
    python3 scripts/backfill_breadth.py 2026-10-09   # a specific IST date
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IST = timezone(timedelta(hours=5, minutes=30))
OPEN_MIN, CLOSE_MIN = 9 * 60 + 15, 15 * 60 + 30


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=True).stdout


def commits_for(day: str) -> list[str]:
    """Every spot.json commit whose IST day is `day`, oldest first.

    The window is given in UTC with a day of slack at each end and then
    filtered on each snapshot's own IST timestamp, because a commit's time and
    the moment its prices were read are not the same instant.
    """
    lo = f"{day}T00:00:00+05:30"
    hi = f"{day}T23:59:59+05:30"
    out = git("log", "--reverse", f"--since={lo}", f"--until={hi}",
              "--format=%H", "--", "data/spot.json")
    return [l for l in out.splitlines() if l.strip()]


def prev_closes(sha: str, cache: dict) -> dict:
    """{symbol: prev_close} from the levels.json at `sha`, cached by BLOB id.

    levels.json changes once a day but is read once per commit, and it is the
    big file in this repo -- keying the cache on the blob means it is parsed
    once per day's worth of snapshots instead of 300-odd times.
    """
    try:
        blob = git("rev-parse", f"{sha}:data/levels.json").strip()
    except subprocess.CalledProcessError:
        return {}
    if blob not in cache:
        try:
            doc = json.loads(git("cat-file", "-p", blob))
            cache[blob] = {r["symbol"]: r.get("prev_close")
                           for r in doc.get("rows", [])}
        except (subprocess.CalledProcessError, ValueError):
            cache[blob] = {}
    return cache[blob]


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(IST).strftime("%Y-%m-%d")
    shas = commits_for(day)
    print(f"{day}: {len(shas)} spot commits")

    cache: dict = {}
    by_min: dict[int, list[int]] = {}
    for sha in shas:
        try:
            spot = json.loads(git("show", f"{sha}:data/spot.json"))
        except (subprocess.CalledProcessError, ValueError):
            continue
        try:
            at = datetime.fromisoformat(spot["generated_at"]).astimezone(IST)
        except (KeyError, ValueError):
            continue
        if at.strftime("%Y-%m-%d") != day:
            continue
        mins = at.hour * 60 + at.minute
        if not (OPEN_MIN <= mins <= CLOSE_MIN):
            continue

        prev = prev_closes(sha, cache)
        if not prev:
            continue
        adv = dec = 0
        for r in spot.get("rows", []):
            if r.get("stale") or r.get("ltp") is None:
                continue
            pc = prev.get(r["symbol"])
            if not pc:
                continue
            if r["ltp"] > pc:
                adv += 1
            elif r["ltp"] < pc:
                dec += 1
        if adv or dec:
            by_min[mins] = [adv, dec]      # a later snapshot wins the minute

    points = [[m, *by_min[m]] for m in sorted(by_min)]
    if not points:
        print("no usable snapshots in the session window — nothing written")
        return 1

    out = DATA / "breadth_today.json"
    doc = {"date": day, "total": points[-1][1] + points[-1][2], "points": points}
    out.write_text(json.dumps(doc, separators=(",", ":")))
    print(f"wrote {len(points)} points {points[0]} .. {points[-1]} -> {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
