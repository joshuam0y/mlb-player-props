"""
sync_teams_and_roster.py

Refreshes the 30 MLB teams and each team's active roster, upserting bios
(bat side / pitch hand / debut season) for any player not already known.
Cheap (30 team calls + 30 roster calls + one bio call per *new* player), so
this is safe to run on every hourly/daily job.
"""

from datetime import datetime, timezone

import api
from db import get_conn, init_db

PITCHER_POSITION_CODES = {"1"}  # position.code '1' == Pitcher
PITCHER_POSITION_ABBREVS = {"P", "TWP"}  # TWP = two-way player (e.g. Ohtani) -- pitches AND hits


def upsert_teams(conn, teams):
    rows = [
        (t["id"], t["name"], t.get("abbreviation"), (t.get("league") or {}).get("name"), (t.get("division") or {}).get("name"))
        for t in teams
    ]
    conn.executemany(
        "INSERT INTO teams (team_id, name, abbrev, league, division) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(team_id) DO UPDATE SET name=excluded.name, abbrev=excluded.abbrev, "
        "league=excluded.league, division=excluded.division",
        rows,
    )


def upsert_player_bio(conn, person, team_id, position_code):
    primary_abbrev = (person.get("primaryPosition") or {}).get("abbreviation")
    is_pitcher = 1 if (position_code in PITCHER_POSITION_CODES or primary_abbrev in PITCHER_POSITION_ABBREVS) else 0
    debut = person.get("mlbDebutDate")
    debut_season = int(debut[:4]) if debut else None
    conn.execute(
        """
        INSERT INTO players (player_id, full_name, bat_side, pitch_hand, primary_position,
                              is_pitcher, active, mlb_debut_season, current_team_id, last_synced_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(player_id) DO UPDATE SET
            full_name=excluded.full_name, bat_side=excluded.bat_side,
            pitch_hand=excluded.pitch_hand, primary_position=excluded.primary_position,
            is_pitcher=excluded.is_pitcher, active=1, current_team_id=excluded.current_team_id,
            last_synced_at=excluded.last_synced_at
        """,
        (
            person["id"],
            person.get("fullName"),
            (person.get("batSide") or {}).get("code"),
            (person.get("pitchHand") or {}).get("code"),
            (person.get("primaryPosition") or {}).get("abbreviation"),
            is_pitcher,
            debut_season,
            team_id,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def deactivate_players_not_in(conn, active_ids):
    if not active_ids:
        return
    placeholders = ",".join("?" for _ in active_ids)
    conn.execute(f"UPDATE players SET active = 0 WHERE player_id NOT IN ({placeholders})", list(active_ids))


def run(team_limit=None):
    init_db()
    conn = get_conn()

    teams = api.get_teams()
    if team_limit:
        teams = teams[:team_limit]
    upsert_teams(conn, teams)
    conn.commit()

    active_player_ids = set()
    known_ids = {row["player_id"] for row in conn.execute("SELECT player_id FROM players")}

    for team in teams:
        roster = api.get_roster(team["id"])
        new_person_ids = [
            entry["person"]["id"] for entry in roster if entry["person"]["id"] not in known_ids
        ]
        bios_by_id = {}
        if new_person_ids:
            for person in api.get_people(new_person_ids):
                bios_by_id[person["id"]] = person

        for entry in roster:
            pid = entry["person"]["id"]
            active_player_ids.add(pid)
            position_code = (entry.get("position") or {}).get("code")
            if pid in bios_by_id:
                upsert_player_bio(conn, bios_by_id[pid], team["id"], position_code)
                known_ids.add(pid)
            else:
                conn.execute(
                    "UPDATE players SET active = 1, current_team_id = ?, last_synced_at = ? WHERE player_id = ?",
                    (team["id"], datetime.now(timezone.utc).isoformat(), pid),
                )
        conn.commit()
        print(f"  {team['name']}: {len(roster)} active roster spots ({len(new_person_ids)} new players)")

    if not team_limit:  # only clean up "active" flag on a full run across all 30 teams
        deactivate_players_not_in(conn, active_player_ids)
        conn.commit()

    conn.close()
    print(f"Synced {len(teams)} teams, {len(active_player_ids)} active roster spots.")


if __name__ == "__main__":
    run()
