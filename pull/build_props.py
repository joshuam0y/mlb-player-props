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
from game_model import team_bullpen_fatigue
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

# Static, illustrative park-factor tiers by long-term reputation (altitude,
# dimensions, prevailing wind/marine layer) -- NOT a dynamically computed or
# year-adjusted index. Treat as directional context for HR/TB props, not a
# precise number. Anything not listed is treated as roughly neutral.
PARK_FACTORS = {
    "Coors Field": "hitter",
    "Great American Ball Park": "hitter",
    "Chase Field": "hitter",
    "Yankee Stadium": "hitter",
    "Globe Life Field": "hitter",
    "Citizens Bank Park": "hitter",
    "Oracle Park": "pitcher",
    "Petco Park": "pitcher",
    "T-Mobile Park": "pitcher",
    "loanDepot park": "pitcher",
    "Comerica Park": "pitcher",
    "Kauffman Stadium": "pitcher",
}


def park_factor_tier(venue_name):
    return PARK_FACTORS.get(venue_name, "neutral")


def batting_rolling(conn, player_id, n, as_of_date=None):
    """as_of_date, if given, restricts to games strictly before it -- for point-in-time backtesting."""
    date_frag = " AND date < ?" if as_of_date else ""
    params = (player_id, as_of_date, n) if as_of_date else (player_id, n)
    rows = conn.execute(
        f"SELECT * FROM batting_game_logs WHERE player_id = ?{date_frag} ORDER BY date DESC LIMIT ?",
        params,
    ).fetchall()
    if not rows:
        return None
    sums = {c: sum((r[c] or 0) for r in rows) for c in BATTING_COUNT_COLS}
    ab = sums["at_bats"]
    avg = round(sums["hits"] / ab, 3) if ab else None
    slg = round(sums["total_bases"] / ab, 3) if ab else None
    babip_denom = ab - sums["strike_outs"] - sums["home_runs"]
    return {
        "games": len(rows),
        **sums,
        "avg": avg,
        "slg": slg,
        # BABIP: strips out HR/K, isolating "did balls in play fall for hits" --
        # a stretch this far above the ~.290-.300 league-average band usually
        # means a hot streak is riding luck, not a real quality-of-contact
        # improvement (ignores sac flies, which this schema doesn't track).
        "babip": round((sums["hits"] - sums["home_runs"]) / babip_denom, 3) if babip_denom > 0 else None,
        # ISO = SLG - AVG: extra-base power isolated from empty-average bloop
        # hits. A hot streak with rising ISO is a power surge; flat/falling
        # ISO alongside a BABIP spike is the "getting lucky, not better" case.
        "iso": round(slg - avg, 3) if (slg is not None and avg is not None) else None,
    }


def hit_streak(conn, player_id, lookback=40):
    """Current consecutive-games-with-a-hit streak -- a prop market in its own right."""
    rows = conn.execute(
        "SELECT hits FROM batting_game_logs WHERE player_id = ? ORDER BY date DESC LIMIT ?",
        (player_id, lookback),
    ).fetchall()
    streak = 0
    for r in rows:
        if (r["hits"] or 0) > 0:
            streak += 1
        else:
            break
    return streak


# Standard FanDuel/Sleeper-style prop categories with common alt-lines.
# We don't pull real sportsbook lines (see README), so instead of guessing
# a single "the line is X" number, we show the plain hit-rate at each of
# these common thresholds over the player's actual recent games -- e.g.
# "went over 1.5 total bases in 8 of the last 10 games" -- so it can be
# compared directly against whatever number the sportsbook app shows.
# (column, plain-English label, common alt-lines to check the hit-rate at)
BATTER_PROP_CATEGORIES = [
    ("hits", "Hits", [0.5, 1.5]),
    ("total_bases", "Total Bases", [1.5, 2.5]),
    ("home_runs", "Home Runs", [0.5]),
    ("rbi", "RBIs", [0.5, 1.5]),
    ("runs", "Runs Scored", [0.5, 1.5]),
    ("base_on_balls", "Walks", [0.5]),
]
PITCHER_PROP_CATEGORIES = [
    ("strike_outs", "Strikeouts", [4.5, 5.5, 6.5]),
    ("earned_runs", "Runs Allowed", [1.5, 2.5]),
    ("hits", "Hits Allowed", [4.5]),
    ("base_on_balls", "Walks Allowed", [1.5]),
]


def _prop_categories(rows, categories, include_values=True):
    if not rows:
        return []
    rows = list(reversed(rows))  # oldest -> newest, so a game-log bar chart reads left to right
    out = []
    for stat, label, lines in categories:
        values = [r[stat] or 0 for r in rows]
        n = len(values)
        entry = {
            "label": label,
            "average": round(sum(values) / n, 2),
            "hit_rates": [
                {"line": line, "pct": round(sum(1 for v in values if v > line) / n * 100), "n": n} for line in lines
            ],
        }
        if include_values:
            entry["values"] = values
        out.append(entry)
    return out


def batter_prop_categories(conn, player_id, n=10, include_values=True):
    cols = [c for c, _, _ in BATTER_PROP_CATEGORIES]
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM batting_game_logs WHERE player_id = ? ORDER BY date DESC LIMIT ?",
        (player_id, n),
    ).fetchall()
    return _prop_categories(rows, BATTER_PROP_CATEGORIES, include_values=include_values)


def pitcher_prop_categories(conn, player_id, n=5, include_values=True):
    cols = [c for c, _, _ in PITCHER_PROP_CATEGORIES]
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM pitching_game_logs WHERE player_id = ? ORDER BY date DESC LIMIT ?",
        (player_id, n),
    ).fetchall()
    return _prop_categories(rows, PITCHER_PROP_CATEGORIES, include_values=include_values)


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


def home_away_split(conn, table, player_id, is_home):
    cols = BATTING_COUNT_COLS if table == "batting_game_logs" else PITCHING_COUNT_COLS
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE player_id = ? AND season = ? AND is_home = ?",
        (player_id, CURRENT_SEASON, 1 if is_home else 0),
    ).fetchall()
    if not rows:
        return None
    sums = {c: sum((r[c] or 0) for r in rows) for c in cols}
    if table == "batting_game_logs":
        ab = sums["at_bats"]
        return {
            "games": len(rows), **sums,
            "avg": round(sums["hits"] / ab, 3) if ab else None,
            "slg": round(sums["total_bases"] / ab, 3) if ab else None,
        }
    innings = sums["outs"] / 3 if sums["outs"] else 0
    return {
        "games": len(rows), **sums,
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
        "SELECT title, link, pub_date, matched_player_ids FROM headlines WHERE matched_player_ids != '' ORDER BY pub_date DESC"
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


BABIP_LUCK_THRESHOLD = 0.340  # well above the ~.290-.300 league-average band
ISO_POWER_THRESHOLD = 0.060  # ISO uptick at least this large signals a real power surge


def trend_caveat(trend, l_short, season):
    """
    Separates a real hot streak (power/contact quality actually up) from a
    lucky one (BABIP spiked, ISO didn't move) -- turns "hot streaks are often
    just BABIP noise" from an asserted caveat into a checked one.
    """
    if trend != "hot" or not l_short or l_short["babip"] is None:
        return None
    iso_delta = (
        l_short["iso"] - season["iso"] if (l_short["iso"] is not None and season["iso"] is not None) else None
    )
    if l_short["babip"] >= BABIP_LUCK_THRESHOLD and (iso_delta is None or iso_delta < ISO_POWER_THRESHOLD):
        return "babip_driven"
    return None


PITCHER_WEAK_AVG_AGAINST = 0.260  # opposing avg this high or above => pitcher struggles vs that batter hand
PITCHER_TOUGH_AVG_AGAINST = 0.210  # opposing avg this low or below => pitcher dominates that batter hand


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

    avg_against = split["avg_against"] if split and split["avg_against"] is not None else None
    pitcher_weak = avg_against is not None and avg_against >= PITCHER_WEAK_AVG_AGAINST
    pitcher_tough = avg_against is not None and avg_against <= PITCHER_TOUGH_AVG_AGAINST
    return {
        "platoon": platoon,
        "pitcher_avg_against": avg_against,
        "pitcher_era_vs_hand": split["era"] if split else None,
        "favorable": platoon == "opposite-hand" and pitcher_weak,
        "unfavorable": platoon == "same-hand" and pitcher_tough,
    }


def build_batter_entry(conn, player_id, opp_hand, opp_pitcher_id, is_home_game, batting_order=None):
    player = conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
    if not player:
        return None
    l7 = batting_rolling(conn, player_id, 7)
    season = batting_rolling(conn, player_id, 162)
    trend = form_trend(l7, season)
    return {
        "player_id": player_id,
        "name": player["full_name"],
        "bat_side": player["bat_side"],
        "batting_order": batting_order,
        "injury": injury_status(conn, player_id),
        "l7": l7,
        "l15": batting_rolling(conn, player_id, 15),
        "season": season,
        "trend": trend,
        "trend_caveat": trend_caveat(trend, l7, season),
        "hit_streak": hit_streak(conn, player_id),
        "splits_vs_opp_hand": hand_splits(conn, "batting_splits", player_id, opp_hand),
        "matchup": matchup_edge(conn, player["bat_side"], opp_hand, opp_pitcher_id),
        "home_away": {
            "this_game": "home" if is_home_game else "away",
            "home": home_away_split(conn, "batting_game_logs", player_id, True),
            "away": home_away_split(conn, "batting_game_logs", player_id, False),
        },
        "headlines": recent_headlines(conn, player_id),
        "prop_categories": batter_prop_categories(conn, player_id),
        "prop_categories_season": batter_prop_categories(conn, player_id, n=200, include_values=False),
    }


def build_pitcher_entry(conn, player_id, is_home_game=None):
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
        "home_away": {
            "this_game": "home" if is_home_game else "away",
            "home": home_away_split(conn, "pitching_game_logs", player_id, True),
            "away": home_away_split(conn, "pitching_game_logs", player_id, False),
        },
        "splits_vs_lhb": hand_splits(conn, "pitching_splits", player_id, "L"),
        "splits_vs_rhb": hand_splits(conn, "pitching_splits", player_id, "R"),
        "headlines": recent_headlines(conn, player_id),
        "prop_categories": pitcher_prop_categories(conn, player_id),
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
    is_home_game = side == "home"

    confirmed = conn.execute(
        "SELECT player_id, batting_order FROM lineups WHERE game_pk = ? AND team_id = ? ORDER BY batting_order",
        (game["game_pk"], team_id),
    ).fetchall()

    if confirmed:
        lineup_confirmed = True
        batters = [
            build_batter_entry(conn, r["player_id"], opp_hand, opp_pitcher_id, is_home_game, r["batting_order"])
            for r in confirmed
        ]
    else:
        lineup_confirmed = False
        batters = [
            build_batter_entry(conn, pid, opp_hand, opp_pitcher_id, is_home_game)
            for pid in likely_starters(conn, team_id)
        ]

    opp_team_id = game[f"{opp_side}_team_id"]
    return {
        "team_id": team_id,
        "team_name": team["name"] if team else None,
        "lineup_confirmed": lineup_confirmed,
        "probable_pitcher": build_pitcher_entry(conn, game[f"{side}_probable_pitcher_id"], is_home_game),
        # bullpen fatigue is about who these batters face in relief innings,
        # so it's the *opponent's* pen -- unrelated to (and doesn't touch)
        # the platoon/vs-hand matchup logic on the starter above.
        "opponent_bullpen_fatigue": team_bullpen_fatigue(conn, opp_team_id),
        "batters": [b for b in batters if b],
    }


def batter_over_score(b):
    """
    Signals that point toward this player OUTPERFORMING their normal --
    weighted by how much we've actually validated each one (our own
    backtest showed HOT alone barely predicts anything, so a BABIP-luck
    hot streak counts for very little; a real hot streak and a favorable
    matchup count for more).
    """
    score = 0.0
    reasons = []
    if b["trend"] == "hot" and b.get("trend_caveat") != "babip_driven":
        score += 2.0
        reasons.append("real hot streak (not just lucky bloops)")
    if b.get("matchup") and b["matchup"].get("favorable"):
        score += 2.0
        reasons.append("favorable matchup vs. tonight's pitcher")
    streak = b.get("hit_streak") or 0
    if streak >= 5:
        score += 1.0
        reasons.append(f"{streak}-game hit streak")
    elif streak >= 3:
        score += 0.5
    return score, reasons


def batter_under_score(b):
    """The mirror image: signals pointing toward this player UNDERPERFORMING their normal."""
    score = 0.0
    reasons = []
    if b["trend"] == "cold":
        score += 1.5
        reasons.append("cold recent stretch (well below season average)")
    if b.get("matchup") and b["matchup"].get("unfavorable"):
        score += 2.0
        reasons.append("tough matchup vs. tonight's pitcher")
    return score, reasons


def prop_category_delta(recent_categories, season_categories, min_games=8):
    """
    The category where recent performance deviates most from this player's
    OWN season norm, in each direction -- not the category with the
    highest raw hit-rate. Comparing raw rates across categories always
    picks "1+ hits" (the easiest bar to clear for almost any hitter), which
    isn't a meaningful "best angle," just an artifact of it being the
    lowest threshold. Returns (most_over, most_under), either possibly None.
    """
    if not recent_categories or not season_categories:
        return None, None
    season_by_label = {c["label"]: c for c in season_categories}
    deltas = []
    for rc in recent_categories:
        sc = season_by_label.get(rc["label"])
        if not sc or not rc["hit_rates"] or not sc["hit_rates"]:
            continue
        r_line = rc["hit_rates"][0]
        if r_line["n"] < min_games:
            continue
        s_line = next((x for x in sc["hit_rates"] if x["line"] == r_line["line"]), None)
        if not s_line:
            continue
        deltas.append((r_line["pct"] - s_line["pct"], rc["label"], r_line))
    if not deltas:
        return None, None
    deltas.sort(key=lambda d: d[0])
    most_under_delta, most_under_label, most_under_line = deltas[0]
    most_over_delta, most_over_label, most_over_line = deltas[-1]
    most_over = (
        {"label": most_over_label, "line": most_over_line["line"], "pct": most_over_line["pct"], "n": most_over_line["n"]}
        if most_over_delta > 0
        else None
    )
    most_under = (
        {"label": most_under_label, "line": most_under_line["line"], "pct": most_under_line["pct"], "n": most_under_line["n"]}
        if most_under_delta < 0
        else None
    )
    return most_over, most_under


def build_top_picks(report_games, limit=12):
    """
    Cross-game leaderboards: the best OVER and UNDER candidates across the
    *entire* day/date range, not buried inside each game's card. Excludes
    injured players from both (can't bet on someone who might not play at
    all, in either direction). Unconfirmed-lineup players are still
    included -- lineups usually don't post until 1-3 hours before game
    time, so requiring CONFIRMED here would leave both lists empty most of
    the day -- but they're scored slightly lower and clearly labeled,
    since "projected" is a real guess.
    """
    overs, unders = [], []
    for g in report_games:
        for side_key in ("home", "away"):
            side = g[side_key]
            opp_side = g["away"] if side_key == "home" else g["home"]
            for b in side["batters"]:
                if b["injury"]:
                    continue
                confirmed_penalty = 0 if side["lineup_confirmed"] else 0.5
                best_over_cat, best_under_cat = prop_category_delta(
                    b.get("prop_categories"), b.get("prop_categories_season")
                )
                base = {
                    "player_id": b["player_id"],
                    "name": b["name"],
                    "team": side["team_name"],
                    "opponent": opp_side["team_name"],
                    "date": g["date"],
                    "lineup_confirmed": side["lineup_confirmed"],
                }

                over_score, over_reasons = batter_over_score(b)
                over_score -= confirmed_penalty
                if over_reasons and over_score > 0:
                    overs.append({**base, "score": over_score, "reasons": over_reasons, "best_category": best_over_cat})

                under_score, under_reasons = batter_under_score(b)
                under_score -= confirmed_penalty
                if under_reasons and under_score > 0:
                    unders.append({**base, "score": under_score, "reasons": under_reasons, "best_category": best_under_cat})

    overs.sort(key=lambda c: c["score"], reverse=True)
    unders.sort(key=lambda c: c["score"], reverse=True)
    return overs[:limit], unders[:limit]


def latest_projection(conn, game_pk):
    """Most recent game_projections snapshot for this game, if simulate_games.py has run."""
    row = conn.execute(
        "SELECT * FROM game_projections WHERE game_pk = ? ORDER BY generated_at DESC LIMIT 1", (game_pk,)
    ).fetchone()
    return dict(row) if row else None


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
                "park_factor": park_factor_tier(game["venue_name"]),
                "home_score": game["home_score"],
                "away_score": game["away_score"],
                "projection": latest_projection(conn, game["game_pk"]),
                "home": build_team_side(conn, game, "home"),
                "away": build_team_side(conn, game, "away"),
            }
        )

    top_overs, top_unders = build_top_picks(report_games)

    # prop_categories_season only exists to feed build_top_picks' recent-vs-
    # season delta above; it's never rendered, so drop it before this report
    # gets serialized to JSON/HTML -- otherwise 800+ batters x 6 categories x
    # up to 200 games each bloats the output by tens of MB for no reason.
    for g in report_games:
        for side in (g["home"], g["away"]):
            for b in side["batters"]:
                b.pop("prop_categories_season", None)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "range": [today, end],
        "games": report_games,
        "top_overs": top_overs,
        "top_unders": top_unders,
    }


def _render_picks_section(lines, heading, picks):
    if not picks:
        return
    lines.append(f"## {heading}")
    for pick in picks:
        cat_txt = ""
        if pick["best_category"]:
            c = pick["best_category"]
            cat_txt = f" -- try {c['label']}: {c['pct']}% over {c['line']} recently (vs. {c['n']}-game sample)"
        lines.append(f"- **{pick['name']}** ({pick['team']} vs {pick['opponent']}): {', '.join(pick['reasons'])}{cat_txt}")
    lines.append("")


def render_markdown(report):
    lines = [f"# MLB Player Props Context Report", f"_Generated {report['generated_at']}_", ""]
    _render_picks_section(lines, "Today's Top Overs", report.get("top_overs"))
    _render_picks_section(lines, "Today's Top Unders", report.get("top_unders"))
    for g in report["games"]:
        lines.append(f"## {g['date']} - {g['away']['team_name']} @ {g['home']['team_name']} ({g['status']})")
        park_note = f" [{g['park_factor']}-friendly park]" if g["park_factor"] != "neutral" else ""
        lines.append(f"_{g['venue']}{park_note}_")
        if g["home_score"] is not None:
            lines.append(f"Final: {g['away']['team_name']} {g['away_score']} - {g['home']['team_name']} {g['home_score']}")
        elif g["projection"]:
            p = g["projection"]
            lines.append(
                f"Projected: {g['away']['team_name']} {p['away_exp_runs']} - {g['home']['team_name']} {p['home_exp_runs']} "
                f"(home win {p['home_win_prob']:.0%}, total {p['total_line']} runs, over {p['over_prob']:.0%})"
            )
        lines.append("")
        for side_key in ("away", "home"):
            side = g[side_key]
            tag = "CONFIRMED" if side["lineup_confirmed"] else "PROJECTED (unconfirmed)"
            lines.append(f"### {side['team_name']} lineup -- {tag}")
            fatigue = side["opponent_bullpen_fatigue"]
            if fatigue and fatigue["fatigue_ratio"] >= 1.2:
                lines.append(f"_Facing a taxed bullpen: {fatigue['recent_innings']} relief IP in last 2 days (ratio {fatigue['fatigue_ratio']})_")
            elif fatigue and fatigue["fatigue_ratio"] <= 0.7:
                lines.append(f"_Facing a rested bullpen: {fatigue['recent_innings']} relief IP in last 2 days (ratio {fatigue['fatigue_ratio']})_")
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
                streak_txt = f" [{b['hit_streak']}-game hit streak]" if b["hit_streak"] >= 3 else ""
                caveat_txt = " [likely BABIP-driven, not a real power uptick]" if b["trend_caveat"] == "babip_driven" else ""
                ha = b["home_away"][b["home_away"]["this_game"]]
                ha_txt = f" -- {b['home_away']['this_game']} split: {ha['avg']} avg" if ha and ha["avg"] is not None else ""
                headline_txt = f" -- news: {b['headlines'][0]['title']}" if b["headlines"] else ""
                lines.append(
                    f"- {order}{b['name']} ({b['bat_side']}){inj}{matchup_txt}{streak_txt}{caveat_txt}"
                    f" -- {l7_txt}{ha_txt}{headline_txt}"
                )
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
