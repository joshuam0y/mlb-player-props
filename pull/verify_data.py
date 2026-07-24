"""
verify_data.py

Data-integrity check, not a projection accuracy check: confirms our summed
batting_game_logs / pitching_game_logs rows exactly match MLB's own official
season totals (stats='season'), for every player+season marked complete in
sync_state. Since both numbers come from the same source of record (MLB
Stats API), any mismatch means a bug in our ingestion/aggregation -- not
league data being wrong. Run after a sync_stats.py pass to sanity-check it.

This does NOT verify that our hot/cold reads or projections are predictive
of anything -- that's a separate, inherently probabilistic question the
backtest addresses. This only verifies the numbers we store are correct.
"""

import argparse
import sys

import api
from db import get_conn, init_db

BATTING_CHECK_COLS = {
    "hits": "hits", "home_runs": "homeRuns", "rbi": "rbi", "runs": "runs",
    "at_bats": "atBats", "base_on_balls": "baseOnBalls", "strike_outs": "strikeOuts",
    "doubles": "doubles", "triples": "triples", "total_bases": "totalBases",
    "stolen_bases": "stolenBases", "hit_by_pitch": "hitByPitch",
}
PITCHING_CHECK_COLS = {
    "outs": "outs", "hits": "hits", "earned_runs": "earnedRuns", "runs": "runs",
    "base_on_balls": "baseOnBalls", "strike_outs": "strikeOuts", "home_runs": "homeRuns",
    "batters_faced": "battersFaced", "wins": "wins", "losses": "losses",
}


def verify_player_season(conn, player_id, season, group):
    table = "batting_game_logs" if group == "hitting" else "pitching_game_logs"
    cols = BATTING_CHECK_COLS if group == "hitting" else PITCHING_CHECK_COLS

    row = conn.execute(
        f"SELECT {', '.join(f'SUM({c}) as {c}' for c in cols)} FROM {table} WHERE player_id = ? AND season = ?",
        (player_id, season),
    ).fetchone()
    if row is None or all(row[c] is None for c in cols):
        return []  # no local rows for this season (e.g. injured all year) -- nothing to check

    official = api.get_season_stats(player_id, season, group)
    if official is None:
        return []  # MLB has no official season stat line either (e.g. didn't play) -- nothing to check

    mismatches = []
    for local_col, api_key in cols.items():
        ours = row[local_col] or 0
        theirs = official.get(api_key)
        if theirs is None:
            continue
        if int(ours) != int(theirs):
            mismatches.append((local_col, ours, theirs))
    return mismatches


def run(limit=None):
    init_db()
    conn = get_conn()

    pairs = conn.execute(
        "SELECT DISTINCT player_id, season, stat_group FROM sync_state WHERE complete = 1 ORDER BY player_id, season"
    ).fetchall()
    if limit:
        pairs = pairs[:limit]

    total_checked = 0
    total_mismatched = 0
    for row in pairs:
        player_id, season, group = row["player_id"], row["season"], row["stat_group"]
        mismatches = verify_player_season(conn, player_id, season, group)
        total_checked += 1
        if mismatches:
            total_mismatched += 1
            name = conn.execute("SELECT full_name FROM players WHERE player_id = ?", (player_id,)).fetchone()
            print(f"MISMATCH {name['full_name'] if name else player_id} {season} {group}: {mismatches}")

    conn.close()
    print(f"Checked {total_checked} player-seasons, {total_mismatched} mismatched.")
    return total_mismatched


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None, help="Only check the first N player-seasons")
    args = p.parse_args()
    sys.exit(1 if run(limit=args.limit) else 0)
