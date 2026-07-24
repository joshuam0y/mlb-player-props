"""
sync_schedule.py

Pulls the next N days of the schedule (regular-season games only) with
probable pitchers and upserts into `games`. This is the hourly job --
probable pitchers get confirmed/changed right up to game time.
"""

from datetime import datetime, timedelta, timezone

import api
from db import get_conn, init_db


def run(days_ahead=5):
    init_db()
    conn = get_conn()

    start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    end = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
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
                )
            )

    conn.executemany(
        """
        INSERT INTO games (game_pk, official_date, game_date_utc, status, home_team_id,
                            away_team_id, home_probable_pitcher_id, away_probable_pitcher_id, venue_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_pk) DO UPDATE SET
            status=excluded.status,
            home_probable_pitcher_id=excluded.home_probable_pitcher_id,
            away_probable_pitcher_id=excluded.away_probable_pitcher_id,
            venue_name=excluded.venue_name
        """,
        rows,
    )
    conn.commit()
    conn.close()
    print(f"Synced {len(rows)} scheduled games from {start} to {end}.")


if __name__ == "__main__":
    run()
