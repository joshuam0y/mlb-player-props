"""
sync_results.py

Backfills final scores for already-played games this season by re-running
sync_schedule's sync_range() over a historical window. This is the ground
truth the game-simulation calibration and backtest need -- sync_schedule.py
itself only looks forward, so past scores never get filled in without this.
"""

import argparse
from datetime import datetime, timezone

from db import get_conn, init_db, mlb_today
from sync_schedule import sync_range

CURRENT_SEASON = datetime.now(timezone.utc).year


def run(start=None, end=None):
    init_db()
    conn = get_conn()
    start = start or f"{CURRENT_SEASON}-03-01"
    end = end or mlb_today()
    n = sync_range(conn, start, end)
    final_count = conn.execute(
        "SELECT COUNT(*) FROM games WHERE official_date BETWEEN ? AND ? AND home_score IS NOT NULL", (start, end)
    ).fetchone()[0]
    conn.close()
    print(f"Synced {n} games from {start} to {end}; {final_count} have final scores.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", help="YYYY-MM-DD, defaults to March 1 of the current season")
    p.add_argument("--end", help="YYYY-MM-DD, defaults to today")
    args = p.parse_args()
    run(start=args.start, end=args.end)
