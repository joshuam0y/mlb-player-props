"""
build_props.py

The output layer: turns the raw synced data (game logs, splits, injuries,
headlines, lineups) into a per-game context report for upcoming matchups --
one entry per probable/confirmed starter with everything relevant to a
player-prop decision (Hits/HR/RBI/TB/BB/K style markets):

  * L7 / L15 rolling counting stats (recent form)
  * season and career splits vs the *opposing* probable pitcher's throwing
    hand (or, for pitchers, their own splits vs LHB/RHB)
  * current injury status
  * recent headlines mentioning the player

Lineup handling is deliberately conservative: a batter is only marked
"confirmed" once `lineups` has a row for that game (i.e. MLB has posted the
actual batting order). Until then we fall back to "likely starters" --
the 9 hitters who've appeared most often in that team's last 15 games --
and label them clearly as unconfirmed so a hot bench bat never gets
mistaken for a real starter.

Run this *after* sync_schedule / sync_lineups / sync_stats / sync_news so
it reflects the latest data.
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from db import CAREER_SEASON, get_conn, init_db
from render_dashboard import render_html

CURRENT_SEASON = datetime.now(timezone.utc).year
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")

BATTING_COUNT_COLS = [
    "at_bats", "hits", "doubles", "triples", "home_runs", "rbi", "runs",
    "base_on_balls", "strike_outs", "total_bases", "hit_by_pitch", "stolen_bases",
]
PITCHING_COUNT_COLS = [
    "outs", "hits", "earned_runs", "runs", "base_on_balls", "strike_outs",
    "home_runs", "batters_faced",
]


def batting_rolling(conn, player_id, n):
    rows = conn.execute(
        "SELECT * FROM batting_game_logs WHERE player_id = ? ORDER BY date DESC LIMIT ?",
        (player_id, n),
    ).fetchall()
    if not rows:
        return None
    sums = {c: sum((r[c] or 0) for r in rows) for c in BATTING_COUNT_COLS}
    ab = sums["at_bats"]
    return {
        "games": len(rows),
        **sums,
        "avg": round(sums["hits"] / ab, 3) if ab else None,
        "slg": round(sums["total_bases"] / ab, 3) if ab else None,
    }


def pitching_rolling(conn, player_id, n):
    rows = conn.execute(
        "SELECT * FROM pitching_game_logs WHERE player_id = ? ORDER BY date DESC LIMIT ?",
        (player_id, n),
    ).fetchall()
    if not rows:
        return None
    sums = {c: sum((r[c] or 0) for r in rows) for c in PITCHING_COUNT_COLS}
    innings = sums["outs"] / 3 if sums["outs"] else 0
    return {
        "games": len(rows),
        **sums,
        "innings_pitched": round(innings, 1),
        "era": round(sums["earned_runs"] * 9 / innings, 2) if innings else None,
        "whip": round((sums["hits"] + sums["base_on_balls"]) / innings, 2) if innings else None,
    }


def hand_splits(conn, table, player_id, hand):
    """
    `hand` is the *opponent's* throwing/batting hand: for batting_splits it's
    the opposing pitcher's hand, for pitching_splits it's the batter's hand.
    Both tables use the same split_code convention -- 'vl' means "situation
    vs a lefty", 'vr' means "situation vs a righty" -- so the L->vl / R->vr
    mapping is identical either way.
    """
    code = "vl" if hand == "L" else "vr"
    season = conn.execute(
        f"SELECT * FROM {table} WHERE player_id = ? AND split_code = ? AND season = ?",
        (player_id, code, CURRENT_SEASON),
    ).fetchone()
    career = conn.execute(
        f"SELECT * FROM {table} WHERE player_id = ? AND split_code = ? AND season = ?",
        (player_id, code, CAREER_SEASON),
    ).fetchone()
    return {"vs_hand": code, "season": dict(season) if season else None, "career": dict(career) if career else None}


def injury_status(conn, player_id):
    row = conn.execute(
        "SELECT status, date, description FROM injuries WHERE player_id = ? ORDER BY date DESC LIMIT 1",
        (player_id,),
    ).fetchone()
    if row and row["status"] == "IL":
        return dict(row)
    return None


def recent_headlines(conn, player_id, limit=3):
    pid = str(player_id)
    rows = conn.execute(
        "SELECT title, link, pub_date FROM headlines WHERE matched_player_ids != '' ORDER BY pub_date DESC"
    ).fetchall()
    matched = [dict(r) for r in rows if pid in (r["matched_player_ids"] or "").split(",")]
    return matched[:limit]


def likely_starters(conn, team_id, n_dates=15, top_n=9):
    """
    Fallback for when the real lineup isn't posted yet: the players who've
    appeared most for this team in its last N game dates. Restricted to the
    *current season* and to players still on that team's *current* active
    roster -- a player's own historical logs stick with whatever team_id
    they played for at the time, so without both filters a since-traded or
    since-released player's old games could outrank an actual current
    teammate, especially early in a backfill when few current players have
    synced data yet.
    """
    dates = [
        r["date"]
        for r in conn.execute(
            "SELECT DISTINCT date FROM batting_game_logs WHERE team_id = ? AND season = ? ORDER BY date DESC LIMIT ?",
            (team_id, CURRENT_SEASON, n_dates),
        )
    ]
    if not dates:
        return []
    placeholders = ",".join("?" for _ in dates)
    rows = conn.execute(
        f"""
        SELECT bgl.player_id, COUNT(*) as starts
        FROM batting_game_logs bgl
        JOIN players p ON p.player_id = bgl.player_id
        WHERE bgl.team_id = ? AND bgl.season = ? AND bgl.date IN ({placeholders})
              AND p.active = 1 AND p.current_team_id = ?
        GROUP BY bgl.player_id
        ORDER BY starts DESC
        LIMIT ?
        """,
        [team_id, CURRENT_SEASON] + dates + [team_id, top_n],
    ).fetchall()
    return [r["player_id"] for r in rows]


HOT_COLD_AVG_THRESHOLD = 0.075  # L7 AVG this far above/below season AVG => hot/cold
HOT_COLD_MIN_GAMES = 5  # need at least this many L7 games before calling a trend


def form_trend(l_short, season, min_games=HOT_COLD_MIN_GAMES, threshold=HOT_COLD_AVG_THRESHOLD):
    if not l_short or not season or l_short["games"] < min_games or season["avg"] is None or l_short["avg"] is None:
        return None
    diff = l_short["avg"] - season["avg"]
    if diff >= threshold:
        return "hot"
    if diff <= -threshold:
        return "cold"
    return None


PITCHER_WEAK_AVG_AGAINST = 0.260  # opposing avg this high or above => pitcher struggles vs that batter hand


def effective_bat_side(bat_side, pitcher_hand):
    """A switch hitter always takes the platoon side, batting opposite whichever hand the pitcher throws."""
    if bat_side == "S":
        return "L" if pitcher_hand == "R" else "R"
    return bat_side


def matchup_edge(conn, bat_side, pitcher_hand, opp_pitcher_id):
    """
    The signal FanDuel/Sleeper prop lines don't surface directly: is this
    specific opposing pitcher unusually hittable by this batter's hand,
    not just "batter has the platoon advantage" in the generic sense.
    """
    if not opp_pitcher_id or not pitcher_hand:
        return {"platoon": None, "pitcher_avg_against": None, "pitcher_era_vs_hand": None, "favorable": None}

    eff_side = effective_bat_side(bat_side, pitcher_hand)
    platoon = "opposite-hand" if eff_side != pitcher_hand else "same-hand"
    code = "vl" if eff_side == "L" else "vr"

    split = conn.execute(
        "SELECT * FROM pitching_splits WHERE player_id = ? AND split_code = ? AND season = ?",
        (opp_pitcher_id, code, CURRENT_SEASON),
    ).fetchone()
    if not split or split["avg_against"] is None:
        split = conn.execute(
            "SELECT * FROM pitching_splits WHERE player_id = ? AND split_code = ? AND season = ?",
            (opp_pitcher_id, code, CAREER_SEASON),
        ).fetchone()

    pitcher_weak = split is not None and split["avg_against"] is not None and split["avg_against"] >= PITCHER_WEAK_AVG_AGAINST
    return {
        "platoon": platoon,
        "pitcher_avg_against": split["avg_against"] if split else None,
        "pitcher_era_vs_hand": split["era"] if split else None,
        "favorable": platoon == "opposite-hand" and pitcher_weak,
    }


def build_batter_entry(conn, player_id, opp_hand, opp_pitcher_id, batting_order=None):
    player = conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
    if not player:
        return None
    l7 = batting_rolling(conn, player_id, 7)
    season = batting_rolling(conn, player_id, 162)
    return {
        "player_id": player_id,
        "name": player["full_name"],
        "bat_side": player["bat_side"],
        "batting_order": batting_order,
        "injury": injury_status(conn, player_id),
        "l7": l7,
        "l15": batting_rolling(conn, player_id, 15),
        "season": season,
        "trend": form_trend(l7, season),
        "splits_vs_opp_hand": hand_splits(conn, "batting_splits", player_id, opp_hand),
        "matchup": matchup_edge(conn, player["bat_side"], opp_hand, opp_pitcher_id),
        "headlines": recent_headlines(conn, player_id),
    }


def build_pitcher_entry(conn, player_id):
    if not player_id:
        return None
    player = conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
    if not player:
        return None
    return {
        "player_id": player_id,
        "name": player["full_name"],
        "pitch_hand": player["pitch_hand"],
        "injury": injury_status(conn, player_id),
        "l3": pitching_rolling(conn, player_id, 3),
        "l5": pitching_rolling(conn, player_id, 5),
        "splits_vs_lhb": hand_splits(conn, "pitching_splits", player_id, "L"),
        "splits_vs_rhb": hand_splits(conn, "pitching_splits", player_id, "R"),
        "headlines": recent_headlines(conn, player_id),
    }


def build_team_side(conn, game, side):
    opp_side = "away" if side == "home" else "home"
    team_id = game[f"{side}_team_id"]
    opp_pitcher_id = game[f"{opp_side}_probable_pitcher_id"]
    team = conn.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()

    opp_pitcher_row = (
        conn.execute("SELECT pitch_hand FROM players WHERE player_id = ?", (opp_pitcher_id,)).fetchone()
        if opp_pitcher_id
        else None
    )
    opp_hand = (opp_pitcher_row["pitch_hand"] if opp_pitcher_row else None) or "R"

    confirmed = conn.execute(
        "SELECT player_id, batting_order FROM lineups WHERE game_pk = ? AND team_id = ? ORDER BY batting_order",
        (game["game_pk"], team_id),
    ).fetchall()

    if confirmed:
        lineup_confirmed = True
        batters = [
            build_batter_entry(conn, r["player_id"], opp_hand, opp_pitcher_id, r["batting_order"]) for r in confirmed
        ]
    else:
        lineup_confirmed = False
        batters = [build_batter_entry(conn, pid, opp_hand, opp_pitcher_id) for pid in likely_starters(conn, team_id)]

    return {
        "team_id": team_id,
        "team_name": team["name"] if team else None,
        "lineup_confirmed": lineup_confirmed,
        "probable_pitcher": build_pitcher_entry(conn, game[f"{side}_probable_pitcher_id"]),
        "batters": [b for b in batters if b],
    }


def build_report(conn, days_ahead=2):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    end = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    games = conn.execute(
        "SELECT * FROM games WHERE official_date BETWEEN ? AND ? ORDER BY official_date, game_date_utc",
        (today, end),
    ).fetchall()

    report_games = []
    for game in games:
        report_games.append(
            {
                "game_pk": game["game_pk"],
                "date": game["official_date"],
                "game_time_utc": game["game_date_utc"],
                "status": game["status"],
                "venue": game["venue_name"],
                "home": build_team_side(conn, game, "home"),
                "away": build_team_side(conn, game, "away"),
            }
        )

    return {"generated_at": datetime.now(timezone.utc).isoformat(), "range": [today, end], "games": report_games}


def render_markdown(report):
    lines = [f"# MLB Player Props Context Report", f"_Generated {report['generated_at']}_", ""]
    for g in report["games"]:
        lines.append(f"## {g['date']} - {g['away']['team_name']} @ {g['home']['team_name']} ({g['status']})")
        lines.append(f"_{g['venue']}_")
        lines.append("")
        for side_key in ("away", "home"):
            side = g[side_key]
            tag = "CONFIRMED" if side["lineup_confirmed"] else "PROJECTED (unconfirmed)"
            lines.append(f"### {side['team_name']} lineup -- {tag}")
            p = side["probable_pitcher"]
            if p:
                inj = f" [INJURY: {p['injury']['status']}]" if p["injury"] else ""
                l5 = p["l5"]
                l5_txt = f"L5: {l5['innings_pitched']} IP, {l5['strike_outs']} K, {l5['earned_runs']} ER, {l5['era']} ERA" if l5 else "L5: no data"
                lines.append(f"**Probable P: {p['name']} ({p['pitch_hand']})**{inj} -- {l5_txt}")
            for b in side["batters"]:
                order = f"#{b['batting_order']} " if b["batting_order"] else ""
                inj = f" [INJURY: {b['injury']['status']}]" if b["injury"] else ""
                l7 = b["l7"]
                l7_txt = (
                    f"L7: {l7['hits']}H {l7['home_runs']}HR {l7['rbi']}RBI {l7['total_bases']}TB ({l7['avg']} avg)"
                    if l7
                    else "L7: no data"
                )
                matchup_txt = ""
                if b["matchup"]["favorable"]:
                    matchup_txt = f" [MATCHUP EDGE: pitcher hits {b['matchup']['pitcher_avg_against']} avg-against vs this hand]"
                headline_txt = f" -- news: {b['headlines'][0]['title']}" if b["headlines"] else ""
                lines.append(f"- {order}{b['name']} ({b['bat_side']}){inj}{matchup_txt} -- {l7_txt}{headline_txt}")
            lines.append("")
    return "\n".join(lines)


def run(days_ahead=2):
    init_db()
    conn = get_conn()
    report = build_report(conn, days_ahead=days_ahead)
    conn.close()

    os.makedirs(OUT_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    json_path = os.path.join(OUT_DIR, "latest.json")
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    md_path = os.path.join(OUT_DIR, "latest.md")
    with open(md_path, "w") as f:
        f.write(render_markdown(report))

    archive_path = os.path.join(OUT_DIR, f"props_{today}.json")
    with open(archive_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    html_path = os.path.join(OUT_DIR, "index.html")
    with open(html_path, "w") as f:
        f.write(render_html(report))

    print(f"Wrote report for {len(report['games'])} games to {json_path}, {md_path}, {html_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days-ahead", type=int, default=2)
    args = p.parse_args()
    run(days_ahead=args.days_ahead)
