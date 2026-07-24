"""
sync_lineups.py

Pulls confirmed starting lineups for today's (and tomorrow's, to get ahead
of very early games) scheduled games. A team's `battingOrder` in the live
boxscore feed is empty until the lineup is officially posted -- usually
1-2 hours before first pitch -- so this has to be re-run regularly (it's
part of the hourly job) to catch lineups as they drop. Bench players never
get a row here, which is the whole point: build_props.py can require a
confirmed lineups row before treating a hitter as a starter.
"""

from datetime import datetime, timedelta, timezone

import api
from db import get_conn, init_db


def sync_game_lineups(conn, game_pk):
    box = api.get_boxscore(game_pk)
    teams = box.get("teams", {})
    now = datetime.now(timezone.utc).isoformat()

    rows = []
    for side in ("home", "away"):
        team = teams.get(side, {})
        batting_order = team.get("battingOrder") or []
        if not batting_order:
            continue  # lineup not posted yet
        team_id = (team.get("team") or {}).get("id")
        players = team.get("players", {})
        for slot, player_id in enumerate(batting_order, start=1):
            info = players.get(f"ID{player_id}", {})
            position = (info.get("position") or {}).get("abbreviation")
            rows.append((game_pk, team_id, player_id, slot, position, now))

    if rows:
        conn.execute("DELETE FROM lineups WHERE game_pk = ?", (game_pk,))
        conn.executemany(
            """
            INSERT INTO lineups (game_pk, team_id, player_id, batting_order, position, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_pk, team_id, player_id) DO UPDATE SET
                batting_order=excluded.batting_order, position=excluded.position,
                confirmed_at=excluded.confirmed_at
            """,
            rows,
        )
        conn.commit()
    return len(rows)


def run(days_ahead=1):
    init_db()
    conn = get_conn()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    end = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    game_pks = [
        row["game_pk"]
        for row in conn.execute(
            "SELECT game_pk FROM games WHERE official_date BETWEEN ? AND ?", (today, end)
        )
    ]

    total = 0
    confirmed_games = 0
    for game_pk in game_pks:
        n = sync_game_lineups(conn, game_pk)
        if n:
            confirmed_games += 1
        total += n

    conn.close()
    print(f"Synced lineups for {confirmed_games}/{len(game_pks)} games ({total} player slots).")


if __name__ == "__main__":
    run()
