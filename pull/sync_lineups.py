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
        team_id = (team.get("team") or {}).get("id")
        players = team.get("players", {})
        # team["battingOrder"] is a convenience list of whoever CURRENTLY
        # occupies each of the 9 slots -- once a pinch-hitter, pinch-runner,
        # or defensive replacement enters, that list silently starts
        # reporting the substitute instead of who actually started, with no
        # sign anything changed. Each player's OWN battingOrder field is more
        # precise: "P00" (an exact multiple of 100) marks slot P's original
        # starter, while "P01"/"P02"/... marks the first/second player to
        # later take over that same slot. Filtering to exact multiples of
        # 100 recovers the true starting lineup regardless of whether this
        # sync happens to run before, during, or well after the game --
        # this bit us for real: a late run relabeled a Royals game's
        # confirmed 9th-place hitter as a 0-at-bat late substitute, silently
        # dropping the actual starter (2 at-bats) from the lineup entirely.
        for info in players.values():
            bo = info.get("battingOrder")
            if bo is None or int(bo) % 100 != 0:
                continue
            person = info.get("person", {})
            position = (info.get("position") or {}).get("abbreviation")
            rows.append((game_pk, team_id, person.get("id"), int(bo) // 100, position, now))

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
