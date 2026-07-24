"""
sync_schedule.py

Pulls the next N days of the schedule (regular-season games only) with
probable pitchers and upserts into `games`. This is the hourly job --
probable pitchers get confirmed/changed right up to game time.

`sync_range` also captures final scores for games that have already been
played (the schedule API returns `score`/`isWinner` directly, no extra
boxscore call needed) -- sync_results.py reuses it for historical
backfills, since that's what the game-simulation calibration needs as
ground truth.
"""

from datetime import datetime, timedelta, timezone

import api
from db import get_conn, init_db


def sync_range(conn, start, end):
    dates = api.get_schedule(start, end)

    rows = []
    for date_block in dates:
        for game in date_block.get("games", []):
            if game.get("gameType") != "R":  # regular season only
                continue
            home = game["teams"]["home"]
            away = game["teams"]["away"]
            rows.append(
                (
                    game["gamePk"],
                    game["officialDate"],
                    game["gameDate"],
                    (game.get("status") or {}).get("detailedState"),
                    home["team"]["id"],
                    away["team"]["id"],
                    (home.get("probablePitcher") or {}).get("id"),
                    (away.get("probablePitcher") or {}).get("id"),
                    (game.get("venue") or {}).get("name"),
                    home.get("score"),
                    away.get("score"),
                )
            )

    conn.executemany(
        """
        INSERT INTO games (game_pk, official_date, game_date_utc, status, home_team_id,
                            away_team_id, home_probable_pitcher_id, away_probable_pitcher_id,
                            venue_name, home_score, away_score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_pk) DO UPDATE SET
            status=excluded.status,
            home_probable_pitcher_id=excluded.home_probable_pitcher_id,
            away_probable_pitcher_id=excluded.away_probable_pitcher_id,
            venue_name=excluded.venue_name,
            home_score=excluded.home_score,
            away_score=excluded.away_score
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def run(days_ahead=5):
    init_db()
    conn = get_conn()
    start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    end = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    n = sync_range(conn, start, end)
    conn.close()
    print(f"Synced {n} scheduled games from {start} to {end}.")


if __name__ == "__main__":
    run()
