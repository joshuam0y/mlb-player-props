"""
backtest_props.py

Point-in-time backtest of the player-props HOT/COLD and BABIP-luck-caveat
signals: for every real batting_game_logs row this season, reconstructs
what build_props.py would have flagged using only games strictly before
that date, then checks the *actual* result of that game (the one a hits/TB
prop would resolve on) against players who weren't flagged.

Known, deliberate scope limit: this does NOT backtest the platoon
matchup-edge flag. That relies on batting_splits/pitching_splits, which
this project only stores as a current snapshot (each sync overwrites the
prior one) -- there's no historical "splits as they stood on date X" to
reconstruct from, so any attempt to backtest it would silently leak
current/end-of-season split data into a "past" evaluation. Backtesting it
properly would need dated split snapshots, which isn't built yet.

Reads bucket-level rates (sum hits / sum at-bats across all games in a
bucket), not an average of per-game averages -- a single game's AVG is a
0/0.5/1.0-type number and averaging those directly would be dominated by
small-sample noise.

SUPERSEDED, for the HOT/COLD question specifically. This script's own
finding -- that HOT and COLD are "barely different, and not in the obvious
direction" -- was correct but under-read: grading nine real days against the
prop lines those flags actually feed showed the trend is INVERTED, not
merely weak (a hot batter clears his OVER 27.1% of the time against 32.9%
for a cold one, z = -4.75 on 13,398 graded player-game-categories), and the
BABIP-luck caveat this script also checks has no discriminating power at all
(z = -0.45). Neither now feeds any pick score. Prefer grade_picks.py's
running record over this script for anything the graded history covers: it
measures the flags against the actual lines and directions the model
publishes, rather than against raw AVG/TB, and it can't leak. See
docs/MODEL_NOTES.md. This script stays useful for the longer season-wide
sample and for signals grading hasn't accumulated enough days on yet.
"""

import argparse

from build_props import HOT_COLD_MIN_GAMES, batting_rolling, form_trend, trend_caveat
from db import get_conn, init_db


def bucket_rate(games):
    hits = sum(g["hits"] for g in games)
    at_bats = sum(g["at_bats"] for g in games)
    total_bases = sum(g["total_bases"] for g in games)
    n = len(games)
    return {
        "games": n,
        "avg": round(hits / at_bats, 3) if at_bats else None,
        "tb_per_game": round(total_bases / n, 2) if n else None,
    }


def run(start=None, end=None):
    init_db()
    conn = get_conn()

    query = "SELECT * FROM batting_game_logs WHERE 1=1"
    params = []
    if start:
        query += " AND date >= ?"
        params.append(start)
    if end:
        query += " AND date <= ?"
        params.append(end)
    query += " ORDER BY date"
    logs = conn.execute(query, params).fetchall()

    buckets = {"hot": [], "cold": [], "neutral": [], "hot_babip_luck": [], "hot_real": []}
    skipped = 0

    for log in logs:
        l7 = batting_rolling(conn, log["player_id"], 7, as_of_date=log["date"])
        season = batting_rolling(conn, log["player_id"], 162, as_of_date=log["date"])
        if not l7 or not season or l7["games"] < HOT_COLD_MIN_GAMES:
            skipped += 1
            continue

        trend = form_trend(l7, season)
        caveat = trend_caveat(trend, l7, season)

        actual = {"hits": log["hits"] or 0, "at_bats": log["at_bats"] or 0, "total_bases": log["total_bases"] or 0}
        bucket_name = trend if trend else "neutral"
        buckets[bucket_name].append(actual)
        if trend == "hot":
            buckets["hot_babip_luck" if caveat == "babip_driven" else "hot_real"].append(actual)

    conn.close()

    print(f"Backtested {len(logs) - skipped} player-games ({skipped} skipped -- not enough prior data yet).\n")
    print("What actually happened THAT game, by what the dashboard would have flagged beforehand:")
    for name in ("hot", "cold", "neutral"):
        r = bucket_rate(buckets[name])
        print(f"  {name.upper():8s} n={r['games']:<6d} AVG={r['avg']}  TB/G={r['tb_per_game']}")

    print("\nWithin HOT: does the BABIP-luck caveat actually predict a worse follow-through?")
    for name in ("hot_real", "hot_babip_luck"):
        r = bucket_rate(buckets[name])
        print(f"  {name:15s} n={r['games']:<6d} AVG={r['avg']}  TB/G={r['tb_per_game']}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", help="YYYY-MM-DD")
    p.add_argument("--end", help="YYYY-MM-DD")
    args = p.parse_args()
    run(start=args.start, end=args.end)
