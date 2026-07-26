"""
sync_stats.py

The heavy lifter: for a given set of players, pulls every season of game
logs from their MLB debut through the current season (skipping seasons
already fully synced -- checkpointed in `sync_state`), plus refreshes
career and current-season splits vs LHP/RHP (batters) or vs LHB/RHB
(pitchers). This is the daily job.

Resumable by design: if this script is interrupted mid-run, rerunning it
picks back up rather than re-pulling completed seasons, mirroring the
"resume from latest row" pattern in eia_grid_pull.py but per player+season
since seasons are immutable once the season has ended.
"""

import argparse
import random
import time
from datetime import datetime, timedelta, timezone

import api
from db import CAREER_SEASON, get_conn, init_db

CURRENT_SEASON = datetime.now(timezone.utc).year


def groups_for_player(row):
    if row["primary_position"] == "TWP":
        return ["hitting", "pitching"]
    return ["pitching"] if row["is_pitcher"] else ["hitting"]


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def upsert_batting_logs(conn, player_id, season, logs):
    conn.execute("DELETE FROM batting_game_logs WHERE player_id = ? AND season = ?", (player_id, season))
    rows = []
    for log in logs:
        stat = log.get("stat", {})
        rows.append(
            (
                player_id,
                log["game"]["gamePk"],
                season,
                log["date"],
                log["team"]["id"],
                (log.get("opponent") or {}).get("id"),
                1 if log.get("isHome") else 0,
                _to_int(stat.get("atBats")), _to_int(stat.get("hits")),
                _to_int(stat.get("doubles")), _to_int(stat.get("triples")),
                _to_int(stat.get("homeRuns")), _to_int(stat.get("rbi")),
                _to_int(stat.get("runs")), _to_int(stat.get("baseOnBalls")),
                _to_int(stat.get("strikeOuts")), _to_int(stat.get("totalBases")),
                _to_int(stat.get("hitByPitch")), _to_int(stat.get("stolenBases")),
            )
        )
    if rows:
        conn.executemany(
            """
            INSERT INTO batting_game_logs
                (player_id, game_pk, season, date, team_id, opponent_id, is_home,
                 at_bats, hits, doubles, triples, home_runs, rbi, runs,
                 base_on_balls, strike_outs, total_bases, hit_by_pitch, stolen_bases)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def upsert_pitching_logs(conn, player_id, season, logs):
    conn.execute("DELETE FROM pitching_game_logs WHERE player_id = ? AND season = ?", (player_id, season))
    rows = []
    for log in logs:
        stat = log.get("stat", {})
        rows.append(
            (
                player_id,
                log["game"]["gamePk"],
                season,
                log["date"],
                log["team"]["id"],
                (log.get("opponent") or {}).get("id"),
                1 if log.get("isHome") else 0,
                stat.get("inningsPitched"),
                _to_int(stat.get("outs")), _to_int(stat.get("hits")),
                _to_int(stat.get("earnedRuns")), _to_int(stat.get("runs")),
                _to_int(stat.get("baseOnBalls")), _to_int(stat.get("strikeOuts")),
                _to_int(stat.get("homeRuns")), _to_int(stat.get("battersFaced")),
                _to_int(stat.get("wins")), _to_int(stat.get("losses")),
                _to_int(stat.get("gamesStarted")),
            )
        )
    if rows:
        conn.executemany(
            """
            INSERT INTO pitching_game_logs
                (player_id, game_pk, season, date, team_id, opponent_id, is_home,
                 innings_pitched, outs, hits, earned_runs, runs, base_on_balls,
                 strike_outs, home_runs, batters_faced, wins, losses, games_started)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def upsert_splits(conn, table, player_id, season, splits):
    stat_cols = (
        ["games", "plate_appearances", "at_bats", "hits", "doubles", "triples", "home_runs",
         "rbi", "runs", "base_on_balls", "strike_outs", "total_bases", "avg", "obp", "slg", "ops"]
        if table == "batting_splits"
        else ["games", "innings_pitched", "outs", "hits", "earned_runs", "runs", "base_on_balls",
              "strike_outs", "home_runs", "batters_faced", "era", "whip", "avg_against"]
    )
    key_map = {
        "games": "gamesPlayed", "plate_appearances": "plateAppearances", "at_bats": "atBats",
        "hits": "hits", "doubles": "doubles", "triples": "triples", "home_runs": "homeRuns",
        "rbi": "rbi", "runs": "runs", "base_on_balls": "baseOnBalls", "strike_outs": "strikeOuts",
        "total_bases": "totalBases", "avg": "avg", "obp": "obp", "slg": "slg", "ops": "ops",
        "innings_pitched": "inningsPitched", "outs": "outs", "earned_runs": "earnedRuns",
        "era": "era", "whip": "whip", "avg_against": "avg", "batters_faced": "battersFaced",
    }
    for code in ("vl", "vr"):
        stat = splits.get(code)
        if not stat:
            continue
        values = []
        for col in stat_cols:
            raw = stat.get(key_map[col])
            if col in ("avg", "obp", "slg", "ops", "era", "whip", "avg_against"):
                values.append(_to_float(raw))
            elif col == "innings_pitched":
                values.append(raw)
            else:
                values.append(_to_int(raw))
        placeholders = ", ".join("?" for _ in stat_cols)
        col_names = ", ".join(stat_cols)
        update_clause = ", ".join(f"{c}=excluded.{c}" for c in stat_cols)
        conn.execute(
            f"""
            INSERT INTO {table} (player_id, split_code, season, {col_names})
            VALUES (?, ?, ?, {placeholders})
            ON CONFLICT(player_id, split_code, season) DO UPDATE SET {update_clause}
            """,
            [player_id, code, season] + values,
        )


def sync_player(conn, row):
    player_id = row["player_id"]
    debut_season = row["mlb_debut_season"] or CURRENT_SEASON
    groups = groups_for_player(row)
    total_new_rows = 0

    for group in groups:
        # current season first -- that's what recent-form (L7/L15) needs;
        # older immutable seasons backfill after, in reverse-chronological order
        for season in range(CURRENT_SEASON, debut_season - 1, -1):
            state = conn.execute(
                "SELECT complete FROM sync_state WHERE player_id=? AND season=? AND stat_group=?",
                (player_id, season, group),
            ).fetchone()
            if state and state["complete"] and season != CURRENT_SEASON:
                continue  # already fully synced, immutable past season

            logs = api.get_game_log(player_id, season, group)
            if group == "hitting":
                n = upsert_batting_logs(conn, player_id, season, logs)
            else:
                n = upsert_pitching_logs(conn, player_id, season, logs)
            total_new_rows += n

            is_complete = 1 if season != CURRENT_SEASON else 0
            conn.execute(
                """
                INSERT INTO sync_state (player_id, season, stat_group, complete) VALUES (?, ?, ?, ?)
                ON CONFLICT(player_id, season, stat_group) DO UPDATE SET complete=excluded.complete
                """,
                (player_id, season, group, is_complete),
            )

        # splits: career + current season
        career = api.get_splits_vs_hand(player_id, group, season=None)
        table = "batting_splits" if group == "hitting" else "pitching_splits"
        upsert_splits(conn, table, player_id, CAREER_SEASON, career)
        season_splits = api.get_splits_vs_hand(player_id, group, season=CURRENT_SEASON)
        upsert_splits(conn, table, player_id, CURRENT_SEASON, season_splits)

    conn.commit()
    return total_new_rows


def teams_active_since(conn, cutoff_date):
    rows = conn.execute(
        "SELECT DISTINCT home_team_id as team_id FROM games WHERE official_date >= ? "
        "UNION SELECT DISTINCT away_team_id as team_id FROM games WHERE official_date >= ?",
        (cutoff_date, cutoff_date),
    ).fetchall()
    return {r["team_id"] for r in rows}


FINAL_STATUSES = ("Final", "Game Over", "Completed Early")


def teams_just_finished(conn, cutoff_date):
    """
    Teams whose game went Final since `cutoff_date` -- these need a
    priority resync, not just an eventual one. sync_player() gets called on
    a shuffled, time-budgeted pass over every active player, so a player
    whose game finishes can otherwise sit on a stale mid-game snapshot
    (whatever the last sync happened to catch, e.g. "4 innings, 2 K")
    labeled FINAL on the site for hours, until the shuffle happens to pick
    them again -- confirmed on a real game where two starters' final lines
    were each still their 4th-inning stat line well after the game ended.
    """
    rows = conn.execute(
        f"""
        SELECT DISTINCT home_team_id as team_id FROM games
        WHERE official_date >= ? AND status IN {FINAL_STATUSES}
        UNION
        SELECT DISTINCT away_team_id as team_id FROM games
        WHERE official_date >= ? AND status IN {FINAL_STATUSES}
        """,
        (cutoff_date, cutoff_date),
    ).fetchall()
    return {r["team_id"] for r in rows}


def players_with_current_season_data(conn):
    rows = conn.execute(
        "SELECT DISTINCT player_id FROM batting_game_logs WHERE season = ? "
        "UNION SELECT DISTINCT player_id FROM pitching_game_logs WHERE season = ?",
        (CURRENT_SEASON, CURRENT_SEASON),
    ).fetchall()
    return {r["player_id"] for r in rows}


def run(player_ids=None, only_active=True, time_budget_seconds=None):
    init_db()
    conn = get_conn()

    query = "SELECT * FROM players"
    conditions = []
    params = []
    if only_active:
        conditions.append("active = 1")
    if player_ids:
        conditions.append(f"player_id IN ({','.join('?' for _ in player_ids)})")
        params.extend(player_ids)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    players = list(conn.execute(query, params).fetchall())

    # A player's current-season stats can't have changed since the last sync
    # unless their team has actually played -- re-fetching all ~776 active
    # players every single hour regardless is pure waste once each has been
    # bootstrapped at least once. Skipped only when explicit --player-id was
    # NOT given (a manual single-player run should always process it).
    if not player_ids:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        active_teams = teams_active_since(conn, cutoff)
        bootstrapped = players_with_current_season_data(conn)
        before = len(players)
        players = [
            p for p in players
            if p["player_id"] not in bootstrapped or p["current_team_id"] in active_teams
        ]
        print(f"Skipping {before - len(players)} players whose team hasn't played in the last day.")

    # Shuffled so a time-budget cutoff doesn't always starve the same tail of
    # players -- progress rotates across different players run to run instead
    # of always stalling on whoever sorts last. Stable-sorted afterward so
    # players from a team whose game JUST went final jump to the front of
    # the queue (still shuffled amongst themselves) -- otherwise their box
    # score can sit on a stale mid-game snapshot, mislabeled FINAL on the
    # site, until the shuffle happens to land on them again.
    random.shuffle(players)
    if not player_ids:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        priority_teams = teams_just_finished(conn, cutoff)
        players.sort(key=lambda p: p["current_team_id"] not in priority_teams)

    start = time.monotonic()
    print(f"Syncing stats for {len(players)} players...")
    for i, row in enumerate(players, 1):
        if time_budget_seconds is not None and time.monotonic() - start > time_budget_seconds:
            print(f"Time budget ({time_budget_seconds}s) reached after {i - 1}/{len(players)} players -- stopping early, resumable next run.")
            break
        n = sync_player(conn, row)
        print(f"  [{i}/{len(players)}] {row['full_name']}: {n} game-log rows synced")

    conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--player-id", type=int, action="append", help="Limit to specific player id(s)")
    p.add_argument(
        "--time-budget-seconds", type=int, default=None,
        help="Stop (resumably) after this many seconds, so a scheduled job always leaves time for the rest of the pipeline",
    )
    args = p.parse_args()
    run(player_ids=args.player_id, time_budget_seconds=args.time_budget_seconds)
