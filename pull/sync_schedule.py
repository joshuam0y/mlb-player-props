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

from datetime import datetime, timedelta

import api
from db import get_conn, init_db, mlb_today

# home.get("score")/away.get("score") come back as 0 (not None/absent) from
# the moment a game's linescore object exists -- which MLB's API populates
# well before first pitch, not just once the game is actually over. Treating
# any numeric score as "the final score" (as the rest of this codebase does
# via `home_score IS NOT NULL`) would show a false "Final: 0-0" for a game
# that hasn't started yet. Only a truly finished game's score should ever
# make it into home_score/away_score.
FINAL_STATUSES = {"Final", "Game Over", "Completed Early"}


def sync_range(conn, start, end):
    dates = api.get_schedule(start, end)

    rows = []
    for date_block in dates:
        for game in date_block.get("games", []):
            if game.get("gameType") != "R":  # regular season only
                continue
            home = game["teams"]["home"]
            away = game["teams"]["away"]
            status = (game.get("status") or {}).get("detailedState")
            is_final = status in FINAL_STATUSES
            rows.append(
                (
                    game["gamePk"],
                    game["officialDate"],
                    game["gameDate"],
                    status,
                    home["team"]["id"],
                    away["team"]["id"],
                    (home.get("probablePitcher") or {}).get("id"),
                    (away.get("probablePitcher") or {}).get("id"),
                    (game.get("venue") or {}).get("name"),
                    home.get("score") if is_final else None,
                    away.get("score") if is_final else None,
                )
            )

    # official_date/game_date_utc ARE updated on conflict, not just inserted
    # once -- a postponed/rained-out game keeps the same game_pk but MLB
    # reassigns it a new date (often a same-day doubleheader with the
    # originally-scheduled game), and this sync's own date window
    # (mlb_today() forward) is what decides which games show up as "today."
    # Without this, a rescheduled game stays permanently stuck under its
    # old date and silently never appears once the calendar moves past it.
    conn.executemany(
        """
        INSERT INTO games (game_pk, official_date, game_date_utc, status, home_team_id,
                            away_team_id, home_probable_pitcher_id, away_probable_pitcher_id,
                            venue_name, home_score, away_score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_pk) DO UPDATE SET
            official_date=excluded.official_date,
            game_date_utc=excluded.game_date_utc,
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
    start = mlb_today()
    end = (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    n = sync_range(conn, start, end)
    conn.close()
    print(f"Synced {n} scheduled games from {start} to {end}.")


if __name__ == "__main__":
    run()
