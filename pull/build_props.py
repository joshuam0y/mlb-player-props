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

import api
from db import CAREER_SEASON, get_conn, init_db
from game_model import team_bullpen_fatigue
import render_track_record
from render_dashboard import render_html
from sync_teams_and_roster import upsert_player_bio

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


def team_streak_and_form(conn, team_id, lookback=10):
    """
    Current consecutive win/loss streak (positive = win streak, negative =
    losing streak) plus last-`lookback`-games record and run differential --
    the team-level mirror of hit_streak()/form_trend() above. Shown as
    context, not folded into the game projection: team_season_run_rates()
    already blends in a recency-weighted rate from real runs scored/allowed
    (verified by backtest to help), which is the rigorous version of "is
    this team hot" -- a separate win/loss streak counter is a noisier,
    already-covered restatement of the same underlying games, not an
    additional independent signal.
    """
    home_rows = conn.execute(
        "SELECT official_date, home_score as scored, away_score as allowed FROM games "
        "WHERE home_team_id = ? AND home_score IS NOT NULL ORDER BY official_date DESC LIMIT ?",
        (team_id, lookback),
    ).fetchall()
    away_rows = conn.execute(
        "SELECT official_date, away_score as scored, home_score as allowed FROM games "
        "WHERE away_team_id = ? AND home_score IS NOT NULL ORDER BY official_date DESC LIMIT ?",
        (team_id, lookback),
    ).fetchall()
    combined = sorted(home_rows + away_rows, key=lambda r: r["official_date"], reverse=True)[:lookback]
    if not combined:
        return None

    streak = 0
    direction = None
    for r in combined:
        won = r["scored"] > r["allowed"]
        if direction is None:
            direction = won
        if won != direction:
            break
        streak += 1

    wins = sum(1 for r in combined if r["scored"] > r["allowed"])
    return {
        "streak": streak if direction else -streak,
        "record_games": len(combined),
        "wins": wins,
        "losses": len(combined) - wins,
        "run_diff": sum(r["scored"] - r["allowed"] for r in combined),
    }


def team_recent_k_rate(conn, team_id, recent_games=2):
    """
    This team's actual strikeout rate (batting side) across its last
    `recent_games` games, vs. its own season rate -- a team that just got
    struck out at an elevated clip (e.g. run through by a tough starter the
    game before) plausibly carries some of that into the very next game of
    the same series against another good arm, which a per-batter platoon
    read alone wouldn't capture. Returns None if there's not enough of
    either window yet.
    """
    recent_pks = conn.execute(
        "SELECT DISTINCT game_pk, MAX(date) as d FROM batting_game_logs WHERE team_id = ? "
        "GROUP BY game_pk ORDER BY d DESC LIMIT ?",
        (team_id, recent_games),
    ).fetchall()
    if len(recent_pks) < recent_games:
        return None
    pks = [r["game_pk"] for r in recent_pks]
    recent = conn.execute(
        f"SELECT SUM(strike_outs) as k, SUM(at_bats) as ab FROM batting_game_logs "
        f"WHERE team_id = ? AND game_pk IN ({','.join('?' for _ in pks)})",
        (team_id, *pks),
    ).fetchone()
    season = conn.execute(
        "SELECT SUM(strike_outs) as k, SUM(at_bats) as ab FROM batting_game_logs WHERE team_id = ? AND season = ?",
        (team_id, CURRENT_SEASON),
    ).fetchone()
    if not recent["ab"] or not season["ab"]:
        return None
    recent_rate = recent["k"] / recent["ab"]
    season_rate = season["k"] / season["ab"]
    return {
        "games": recent_games, "recent_k_rate": round(recent_rate, 3), "season_k_rate": round(season_rate, 3),
        "elevated": recent_rate - season_rate >= 0.06,  # 6+ points above their own norm -- a real, not marginal, jump
    }


# FanDuel/Sleeper-style prop categories. We don't pull real sportsbook
# lines (see README), so instead of a fixed common threshold applied to
# every player alike (which is meaningless for someone like Tarik Skubal --
# a flat 4.5-strikeout line is trivial for an ace, but would be a stretch
# for a back-of-rotation innings-eater), the line for each category is a
# per-player PROJECTION: a blend of this player's own recent and
# season-long per-game rate, rounded to the nearest half (never a whole
# number, same "no pushes" convention real player-prop lines use). See
# category_baselines() below. (column, plain-English label)
BATTER_PROP_CATEGORIES = [
    ("hits", "Hits"),
    ("total_bases", "Total Bases"),
    ("home_runs", "Home Runs"),
    ("rbi", "RBIs"),
    ("runs", "Runs Scored"),
    ("base_on_balls", "Walks"),
]
PITCHER_PROP_CATEGORIES = [
    ("strike_outs", "Strikeouts"),
    ("earned_runs", "Runs Allowed"),
    ("hits", "Hits Allowed"),
    ("base_on_balls", "Walks Allowed"),
]

RECENT_WEIGHT = 0.4  # how much of the projected line comes from recent form vs. full-season rate


def round_to_half(x):
    """Sportsbook-style line: always ends in .5 (never a whole number), so a push is impossible."""
    return round(x - 0.5) + 0.5


def _per_game_avg(rolling, stat):
    if not rolling or not rolling.get("games"):
        return None
    return rolling[stat] / rolling["games"]


def season_stat_averages(conn, table, player_id, stats, season=CURRENT_SEASON):
    """
    Per-game average for each stat, strictly within `season`. Deliberately
    separate from batting_rolling()/pitching_rolling()'s "season" rollup
    (which is really just "last up to 162/162 games regardless of year" --
    fine for that function's own purposes, since backtest.py relies on that
    exact behavior, but wrong for a projected line: an established
    veteran's last 162 games can span several years, and calling that "this
    season" would be a false claim in the UI text).
    """
    cols = ", ".join(f"SUM({stat}) as {stat}" for stat, _ in stats)
    row = conn.execute(
        f"SELECT {cols}, COUNT(*) as games FROM {table} WHERE player_id = ? AND season = ?",
        (player_id, season),
    ).fetchone()
    if not row or not row["games"]:
        return {stat: None for stat, _ in stats}
    games = row["games"]
    return {stat: (row[stat] / games) if row[stat] is not None else None for stat, _ in stats}


def category_baselines(recent_rolling, season_avgs, stats):
    """
    This player's own projected per-game line for each stat -- a blend of
    their recent-form rate and their actual current-season rate, rounded to
    the nearest half. This is the number the bar-chart baseline and hit-rate
    are measured against (their own normal expectation), NOT adjusted for
    tonight's specific opponent -- that adjustment is a separate "today's
    projection" layered on top in _prop_categories(), so the historical
    bars stay an apples-to-apples read of "did they clear their own line",
    unaffected by who they happened to be facing on any given past night.
    Returns {stat: (avg, line)}, or {stat: None} if there's no data yet.
    """
    out = {}
    for stat, _label in stats:
        recent_avg = _per_game_avg(recent_rolling, stat)
        season_avg = season_avgs.get(stat)
        if recent_avg is None and season_avg is None:
            out[stat] = None
            continue
        if season_avg is None:
            avg = recent_avg
        elif recent_avg is None:
            avg = season_avg
        else:
            avg = RECENT_WEIGHT * recent_avg + (1 - RECENT_WEIGHT) * season_avg
        # No floor here: round_to_half() already naturally maps any average
        # in [0, 1.0) to a 0.5 line on its own (round_to_half(0) == 0.5), so
        # a floor was never needed for the LINE -- but it WAS silently
        # inflating `avg` itself for genuinely cold hitters (e.g. a real
        # 0.08 hits/game average was getting bumped to 0.5 before the
        # matchup-adjusted "today" projection used it), which is exactly
        # backwards: a truly cold bat should show a truly low projection,
        # not get rounded up to a coin flip.
        out[stat] = (avg, round_to_half(avg))
    return out


LEAN_EPSILON = 0.15  # today's projection has to clear the line by more than this to call a lean -- otherwise it's a near-coin-flip and shouldn't be shown as confidently over/under


def _prop_categories(rows, stats, baselines, factor_fn=None, include_values=True):
    if not rows:
        return []
    rows = list(reversed(rows))  # oldest -> newest, so a game-log bar chart reads left to right
    factor_fn = factor_fn or (lambda label: 1.0)
    out = []
    for stat, label in stats:
        baseline = baselines.get(stat)
        if baseline is None:
            continue  # not enough data yet to project a line for this category
        avg, line = baseline
        values = [r[stat] or 0 for r in rows]
        n = len(values)
        today_projection = round(avg * factor_fn(label), 2)
        diff = today_projection - line
        if diff > LEAN_EPSILON:
            lean = "over"
        elif diff < -LEAN_EPSILON:
            lean = "under"
        else:
            lean = None
        entry = {
            "label": label,
            "average": round(sum(values) / n, 2),
            "primary_line": line,
            "today_projection": today_projection,
            "lean": lean,
            "hit_rates": [{"line": line, "pct": round(sum(1 for v in values if v > line) / n * 100), "n": n}],
        }
        if include_values:
            entry["values"] = values
            # dates line up 1:1 with values (both oldest->newest) so the bar-chart
            # tooltip can say "2 total bases on Jul 20" instead of a bare number
            entry["dates"] = [r["date"] for r in rows] if "date" in rows[0].keys() else None
        out.append(entry)
    return out


def batter_prop_categories(conn, player_id, baselines, factor_fn=None, n=10, include_values=True):
    cols = [c for c, _ in BATTER_PROP_CATEGORIES]
    rows = conn.execute(
        f"SELECT {', '.join(cols)}, date FROM batting_game_logs WHERE player_id = ? ORDER BY date DESC LIMIT ?",
        (player_id, n),
    ).fetchall()
    return _prop_categories(rows, BATTER_PROP_CATEGORIES, baselines, factor_fn=factor_fn, include_values=include_values)


def pitcher_prop_categories(conn, player_id, baselines, factor_fn=None, n=5, include_values=True):
    cols = [c for c, _ in PITCHER_PROP_CATEGORIES]
    rows = conn.execute(
        f"SELECT {', '.join(cols)}, date FROM pitching_game_logs WHERE player_id = ? ORDER BY date DESC LIMIT ?",
        (player_id, n),
    ).fetchall()
    return _prop_categories(rows, PITCHER_PROP_CATEGORIES, baselines, factor_fn=factor_fn, include_values=include_values)


BATTER_MATCHUP_FAVORABLE_FACTOR = 1.15
BATTER_MATCHUP_UNFAVORABLE_FACTOR = 0.85


def batter_matchup_factor(matchup):
    """Applied uniformly across every batter category for 'today's projection' -- a coarse but honest single adjustment, not a per-stat model."""
    if not matchup:
        return 1.0
    if matchup.get("favorable"):
        return BATTER_MATCHUP_FAVORABLE_FACTOR
    if matchup.get("unfavorable"):
        return BATTER_MATCHUP_UNFAVORABLE_FACTOR
    return 1.0


PITCHER_FORM_PROJECTION_FACTOR = 0.10  # +/-10% swing to a category's projection from recent form


def pitcher_category_factor(label, form_trend):
    """
    Unlike batters, 'pitching well' doesn't push every category the same
    direction: a dominant stretch means MORE strikeouts but FEWER runs/
    hits/walks allowed, so the sign flips depending on the category.
    """
    if form_trend == "dominant":
        return 1 + PITCHER_FORM_PROJECTION_FACTOR if label == "Strikeouts" else 1 - PITCHER_FORM_PROJECTION_FACTOR
    if form_trend == "rough":
        return 1 - PITCHER_FORM_PROJECTION_FACTOR if label == "Strikeouts" else 1 + PITCHER_FORM_PROJECTION_FACTOR
    return 1.0


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


def batter_game_result(conn, player_id, game_pk):
    """This player's own actual box-score line for this specific game, once it's been played and synced -- None beforehand, so callers can show it only once there's something real to show."""
    row = conn.execute(
        "SELECT at_bats, hits, doubles, triples, home_runs, rbi, runs, base_on_balls, strike_outs, total_bases, stolen_bases "
        "FROM batting_game_logs WHERE player_id = ? AND game_pk = ?",
        (player_id, game_pk),
    ).fetchone()
    return dict(row) if row else None


def pitcher_game_result(conn, player_id, game_pk):
    row = conn.execute(
        "SELECT innings_pitched, hits, earned_runs, runs, base_on_balls, strike_outs, home_runs "
        "FROM pitching_game_logs WHERE player_id = ? AND game_pk = ?",
        (player_id, game_pk),
    ).fetchone()
    return dict(row) if row else None


def live_score(conn, game_pk, home_team_id, away_team_id):
    """
    Runs scored so far in a game that's still in progress -- summed from
    each team's own batters' individual runs-scored column for this game_pk
    (the same near-real-time data the per-player LIVE box score already
    uses), NOT from games.home_score/away_score, which is deliberately
    never set until the game is truly Final (see sync_schedule.py) to
    avoid a false "0-0 final" for a game that hasn't started. Without this,
    the game summary line just kept showing the stale pre-game projection
    for the entire multi-hour duration of a game, with no visible sign
    anything was even happening. None if there's no in-progress data yet.
    """
    home = conn.execute(
        "SELECT SUM(runs) as r FROM batting_game_logs WHERE game_pk = ? AND team_id = ?", (game_pk, home_team_id)
    ).fetchone()
    away = conn.execute(
        "SELECT SUM(runs) as r FROM batting_game_logs WHERE game_pk = ? AND team_id = ?", (game_pk, away_team_id)
    ).fetchone()
    if home["r"] is None and away["r"] is None:
        return None
    return {"home_runs": home["r"] or 0, "away_runs": away["r"] or 0}


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


def team_injury_report(conn, team_id):
    """
    Every player on this team's injury-transaction history whose MOST
    RECENT transaction is still 'IL' (not superseded by a later
    'activated' entry) -- i.e. actually out right now, regardless of
    whether they'd otherwise be a starter. This is deliberately separate
    from injury_status() being checked against the handful of players who
    made it into a lineup/probable-starter list -- an injured player is by
    definition NOT one of those, so a report limited to that list would
    almost always be empty. Uses the exact same "latest row wins" rule as
    injury_status() so the two can never disagree about a given player.
    """
    player_ids = {
        r["player_id"] for r in conn.execute("SELECT DISTINCT player_id FROM injuries WHERE team_id = ?", (team_id,))
    }
    out = []
    for player_id in player_ids:
        latest = conn.execute(
            "SELECT player_name, date, status, description FROM injuries WHERE player_id = ? ORDER BY date DESC LIMIT 1",
            (player_id,),
        ).fetchone()
        if latest and latest["status"] == "IL":
            out.append(dict(latest))
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


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


PITCHER_FORM_ERA_THRESHOLD = 1.00  # L5-vs-season ERA diff this large => pitcher trending dominant/rough
PITCHER_FORM_MIN_STARTS = 3


def pitcher_form_trend(l5, season, min_starts=PITCHER_FORM_MIN_STARTS, threshold=PITCHER_FORM_ERA_THRESHOLD):
    """
    Mirror of form_trend() for pitchers, on ERA instead of AVG -- lower is
    better, so the sign is flipped vs. the batter version (an ERA well BELOW
    the season rate is the good/"dominant" direction, not "cold").
    """
    if not l5 or not season or l5["games"] < min_starts or l5.get("era") is None or season.get("era") is None:
        return None
    diff = l5["era"] - season["era"]
    if diff <= -threshold:
        return "dominant"
    if diff >= threshold:
        return "rough"
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


def _ensure_player(conn, player_id, team_id):
    """
    A player can show up in an officially CONFIRMED lineup before our own
    roster sync has caught up to a recent trade/call-up (sync_teams_and_
    roster.py's "active" roster type runs hourly; the transaction can land
    in between) -- rather than silently dropping a real, lineup-confirmed
    batter, fetch just their bio on demand and upsert them so a confirmed
    lineup shown here is always complete instead of missing whoever's newest.
    """
    person = api.get_person(player_id)
    if not person:
        return None
    position_code = (person.get("primaryPosition") or {}).get("code")
    upsert_player_bio(conn, person, team_id, position_code)
    conn.commit()
    return conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()


def build_batter_entry(conn, player_id, opp_hand, opp_pitcher_id, is_home_game, team_id, batting_order=None, game_pk=None):
    player = conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
    if not player:
        player = _ensure_player(conn, player_id, team_id)
        if not player:
            return None
    l7 = batting_rolling(conn, player_id, 7)
    l15 = batting_rolling(conn, player_id, 15)
    season = batting_rolling(conn, player_id, 162)
    trend = form_trend(l7, season)
    matchup = matchup_edge(conn, player["bat_side"], opp_hand, opp_pitcher_id)

    season_avgs = season_stat_averages(conn, "batting_game_logs", player_id, BATTER_PROP_CATEGORIES)
    baselines = category_baselines(l15, season_avgs, BATTER_PROP_CATEGORIES)
    factor_fn = lambda label: batter_matchup_factor(matchup)  # noqa: E731 -- same factor for every batting category
    recent_categories = batter_prop_categories(conn, player_id, baselines, factor_fn=factor_fn)
    season_categories = batter_prop_categories(conn, player_id, baselines, factor_fn=factor_fn, n=200, include_values=False)
    best_over, best_under = prop_category_delta(recent_categories, season_categories)
    best_prop, best_prop_direction = headline_prop(best_over, best_under)
    if best_prop is None:
        best_prop, best_prop_direction = fallback_best_prop(recent_categories, season_categories)

    return {
        "player_id": player_id,
        "name": player["full_name"],
        "bat_side": player["bat_side"],
        "batting_order": batting_order,
        "injury": injury_status(conn, player_id),
        "l7": l7,
        "l15": l15,
        "season": season,
        "trend": trend,
        "trend_caveat": trend_caveat(trend, l7, season),
        "hit_streak": hit_streak(conn, player_id),
        "splits_vs_opp_hand": hand_splits(conn, "batting_splits", player_id, opp_hand),
        "matchup": matchup,
        "home_away": {
            "this_game": "home" if is_home_game else "away",
            "home": home_away_split(conn, "batting_game_logs", player_id, True),
            "away": home_away_split(conn, "batting_game_logs", player_id, False),
        },
        "headlines": recent_headlines(conn, player_id),
        "prop_categories": recent_categories,
        "best_over": best_over,
        "best_under": best_under,
        "best_prop": best_prop,
        "best_prop_direction": best_prop_direction,
        "matchup_lean": best_matchup_lean(recent_categories),
        "game_result": batter_game_result(conn, player_id, game_pk) if game_pk else None,
    }


def build_pitcher_entry(conn, player_id, team_id, is_home_game=None, game_pk=None):
    if not player_id:
        return None
    player = conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
    if not player:
        player = _ensure_player(conn, player_id, team_id)
        if not player:
            return None
    l5 = pitching_rolling(conn, player_id, 5)
    season = pitching_rolling(conn, player_id, 162)
    form_trend_value = pitcher_form_trend(l5, season)

    season_avgs = season_stat_averages(conn, "pitching_game_logs", player_id, PITCHER_PROP_CATEGORIES)
    baselines = category_baselines(l5, season_avgs, PITCHER_PROP_CATEGORIES)
    factor_fn = lambda label: pitcher_category_factor(label, form_trend_value)  # noqa: E731
    recent_categories = pitcher_prop_categories(conn, player_id, baselines, factor_fn=factor_fn)
    season_categories = pitcher_prop_categories(conn, player_id, baselines, factor_fn=factor_fn, n=200, include_values=False)
    best_over, best_under = prop_category_delta(recent_categories, season_categories, min_games=4)
    best_prop, best_prop_direction = headline_prop(best_over, best_under)
    if best_prop is None:
        best_prop, best_prop_direction = fallback_best_prop(recent_categories, season_categories)

    return {
        "player_id": player_id,
        "name": player["full_name"],
        "pitch_hand": player["pitch_hand"],
        "injury": injury_status(conn, player_id),
        "l3": pitching_rolling(conn, player_id, 3),
        "l5": l5,
        "season": season,
        "form_trend": form_trend_value,
        "home_away": {
            "this_game": "home" if is_home_game else "away",
            "home": home_away_split(conn, "pitching_game_logs", player_id, True),
            "away": home_away_split(conn, "pitching_game_logs", player_id, False),
        },
        "splits_vs_lhb": hand_splits(conn, "pitching_splits", player_id, "L"),
        "splits_vs_rhb": hand_splits(conn, "pitching_splits", player_id, "R"),
        "headlines": recent_headlines(conn, player_id),
        "prop_categories": recent_categories,
        "best_over": best_over,
        "best_under": best_under,
        "best_prop": best_prop,
        "best_prop_direction": best_prop_direction,
        "matchup_lean": best_matchup_lean(recent_categories),
        # filled in by build_report() once the opposing lineup for this game
        # is known -- a pitcher's own entry has no visibility into the other
        # team's batters at the point build_team_side() constructs it.
        "opponent_matchup": None,
        "game_result": pitcher_game_result(conn, player_id, game_pk) if game_pk else None,
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
            build_batter_entry(conn, r["player_id"], opp_hand, opp_pitcher_id, is_home_game, team_id, r["batting_order"], game["game_pk"])
            for r in confirmed
        ]
    else:
        lineup_confirmed = False
        batters = [
            build_batter_entry(conn, pid, opp_hand, opp_pitcher_id, is_home_game, team_id, game_pk=game["game_pk"])
            for pid in likely_starters(conn, team_id)
        ]

    opp_team_id = game[f"{opp_side}_team_id"]
    pitcher = build_pitcher_entry(conn, game[f"{side}_probable_pitcher_id"], team_id, is_home_game, game["game_pk"])
    if pitcher:
        # Unlike opponent_matchup (needs the opposing BATTERS list, so it's
        # filled in later by build_report() once both sides exist), this
        # only needs the opposing team_id, already in scope here.
        pitcher["opponent_recent_k_rate"] = team_recent_k_rate(conn, opp_team_id)
    return {
        "team_id": team_id,
        "team_name": team["name"] if team else None,
        "lineup_confirmed": lineup_confirmed,
        "probable_pitcher": pitcher,
        # bullpen fatigue is about who these batters face in relief innings,
        # so it's the *opponent's* pen -- unrelated to (and doesn't touch)
        # the platoon/vs-hand matchup logic on the starter above.
        "opponent_bullpen_fatigue": team_bullpen_fatigue(conn, opp_team_id),
        "form": team_streak_and_form(conn, team_id),
        "batters": [b for b in batters if b],
        "injuries": team_injury_report(conn, team_id),
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


PITCHER_TOUGH_MATCHUP_COUNT = 3  # this many opposing batters in a bad spot => a real pitcher edge


def pitcher_matchup_summary(opp_batters):
    """
    Aggregates the *batters'* own per-matchup edge (already computed while
    building the opposing lineup) into a pitcher-facing summary: how many of
    tonight's actual lineup are in a tough spot against this arm, vs. how
    many have the platoon edge on him. Reuses matchup_edge() output instead
    of re-deriving it, so "batter X has a tough matchup vs. pitcher Y" and
    "pitcher Y has a good matchup vs. batter X" can never disagree.
    """
    tough_for_batters = [b["name"] for b in opp_batters if b.get("matchup") and b["matchup"].get("unfavorable")]
    exploitable_by_batters = [b["name"] for b in opp_batters if b.get("matchup") and b["matchup"].get("favorable")]
    return {"tough_matchups": tough_for_batters, "exploitable_matchups": exploitable_by_batters}


PITCHER_ROUGH_FORM_PENALTY = 1.5  # a pitcher actively getting hit hard lately shouldn't score into a Top Over/Under on matchup alone


def pitcher_strikeout_over_score(p):
    """Signals favoring the strikeout OVER: pitching better than usual lately, and/or a lineup full of bad matchups for the batters facing him."""
    score = 0.0
    reasons = []
    if p.get("form_trend") == "dominant":
        score += 1.5
        reasons.append("pitching well above his season norm over his last few starts")
    elif p.get("form_trend") == "rough":
        # A pitcher who's actively getting rocked lately is typically also
        # getting pulled early -- fewer innings means a real cap on his
        # strikeout upside no matter how favorable tonight's matchup looks
        # on paper, so this drags the score down rather than just not
        # helping it.
        score -= PITCHER_ROUGH_FORM_PENALTY
    tough = (p.get("opponent_matchup") or {}).get("tough_matchups") or []
    if len(tough) >= PITCHER_TOUGH_MATCHUP_COUNT:
        score += 2.0
        reasons.append(f"{len(tough)} hitters in tonight's lineup are in a tough matchup against him")
    elif tough:
        score += 0.75
        reasons.append(f"{len(tough)} hitter(s) in tonight's lineup are in a tough matchup against him")
    k_rate = p.get("opponent_recent_k_rate")
    if k_rate and k_rate["elevated"]:
        # A team that's been striking out well above its own norm the last
        # couple games (e.g. run through by a tough starter the game
        # before) plausibly carries some of that into the next game of the
        # same series against another good arm -- real, if softer, signal
        # than the per-batter platoon read above, and independent of it.
        score += 1.0
        reasons.append(
            f"opposing lineup has struck out at an elevated rate over their last {k_rate['games']} games "
            f"({k_rate['recent_k_rate']:.0%} vs their own {k_rate['season_k_rate']:.0%} season rate)"
        )
    return score, reasons


def pitcher_runs_under_score(p):
    """Signals favoring the runs/hits-allowed UNDER: same 'pitching well' signals, framed toward a clean outing."""
    score = 0.0
    reasons = []
    if p.get("form_trend") == "dominant":
        score += 1.5
        reasons.append("allowing fewer runs than usual over his last few starts")
    elif p.get("form_trend") == "rough":
        # The direct opposite of the "under" thesis -- he's been allowing
        # MORE runs than usual, not fewer, so this should count against a
        # clean-outing bet, not just sit neutral.
        score -= PITCHER_ROUGH_FORM_PENALTY
    tough = (p.get("opponent_matchup") or {}).get("tough_matchups") or []
    if len(tough) >= PITCHER_TOUGH_MATCHUP_COUNT:
        score += 1.5
        reasons.append(f"{len(tough)} hitters in tonight's lineup are in a tough matchup against him")
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
        {
            "label": most_over_label, "line": most_over_line["line"], "pct": most_over_line["pct"],
            "n": most_over_line["n"], "season_pct": most_over_line["pct"] - most_over_delta, "delta": most_over_delta,
        }
        if most_over_delta > 0
        else None
    )
    most_under = (
        {
            "label": most_under_label, "line": most_under_line["line"], "pct": most_under_line["pct"],
            "n": most_under_line["n"], "season_pct": most_under_line["pct"] - most_under_delta, "delta": most_under_delta,
        }
        if most_under_delta < 0
        else None
    )
    return most_over, most_under


def headline_prop(best_over, best_under):
    """
    A single "best prop" to headline in a player's row -- whichever of the
    over/under angles deviates furthest from this player's own season norm.
    Returns (category_dict, direction) where direction is 'over'/'under', or
    (None, None) if neither angle cleared the min-sample bar.
    """
    if best_over and best_under:
        return (best_over, "over") if best_over["delta"] >= abs(best_under["delta"]) else (best_under, "under")
    if best_over:
        return best_over, "over"
    if best_under:
        return best_under, "under"
    return None, None


def best_matchup_lean(categories):
    """
    The single category this player's row should headline once the game has
    actually started, next to the live/final box score: which prop did the
    model call over/under for THIS specific matchup, picking whichever
    category's today-vs-line gap is largest. This is a different signal
    from "Best prop" above -- that one compares a recent hot/cold trend
    against this player's own season rate; this one is the matchup-adjusted
    projection (today_projection, which already factors in today's specific
    opposing pitcher/handedness) against the prop line itself. The two can
    and do disagree, and neither is a stand-in for the other -- without
    this, a live/final box score had no visible reminder of which side of a
    line the model actually leaned pre-game, since that only ever lived in
    the click-to-expand category detail.
    """
    candidates = [c for c in categories if c.get("lean")]
    if not candidates:
        return None
    best = max(candidates, key=lambda c: abs(c["today_projection"] - c["primary_line"]))
    return {"label": best["label"], "line": best["primary_line"], "direction": best["lean"], "projection": best["today_projection"]}


def fallback_best_prop(recent_categories, season_categories):
    """
    prop_category_delta() requires 8+ recent games (4+ for pitchers) AND a
    real deviation from the player's own season norm in the matching
    direction -- a real bar to clear, so part-time players and recent
    call-ups with a short recent-game sample routinely have nothing that
    qualifies, leaving their row with no "Best prop" line at all. Used only
    when headline_prop() came back empty: drops both the games-played floor
    and the deviation requirement, and just shows whichever category has
    the most recent-game data to look at, still compared against the
    player's season rate where available. Not a claimed "edge" -- just
    ensures every player with at least one logged game shows a concrete
    number instead of a blank space.
    """
    if not recent_categories:
        return None, None
    season_by_label = {c["label"]: c for c in (season_categories or [])}
    candidates = []
    for rc in recent_categories:
        if not rc["hit_rates"]:
            continue
        r_line = rc["hit_rates"][0]
        sc = season_by_label.get(rc["label"])
        s_line = next((x for x in (sc["hit_rates"] if sc else []) if x["line"] == r_line["line"]), None)
        season_pct = s_line["pct"] if s_line else r_line["pct"]
        candidates.append((r_line["n"], rc["label"], r_line, season_pct))
    if not candidates:
        return None, None
    candidates.sort(key=lambda c: c[0], reverse=True)  # most recent-game data first -- a 10-game read beats a 3-game one
    n, label, r_line, season_pct = candidates[0]
    direction = "over" if r_line["pct"] >= season_pct else "under"
    prop = {"label": label, "line": r_line["line"], "pct": r_line["pct"], "n": n, "season_pct": season_pct}
    return prop, direction


def pitcher_best_category(p, label):
    for cat in p.get("prop_categories") or []:
        if cat["label"] == label and cat["hit_rates"]:
            hr = cat["hit_rates"][0]
            return {"label": cat["label"], "line": hr["line"], "pct": hr["pct"], "n": hr["n"]}
    return None


def _pct_str(rate):
    return f".{round(rate * 1000):03d}"


def fallback_pick_angle(entity, role, direction):
    """
    A Top Over/Under pick can qualify purely on a signal other than a
    specific prop category (matchup edge, hot/cold trend, hit streak) --
    prop_category_delta() requires 8+ recent games (4+ for pitchers) AND a
    real deviation from the player's own season norm in the right
    direction, so it's entirely possible for best_over/best_under/
    pitcher_best_category to come back None even though the pick is
    legitimate. Rather than leave that card with a reasons bullet but no
    concrete number at all, this surfaces whichever number actually backs
    whichever signal got the player on the list -- checked in the same
    priority order the scoring functions use (matchup first, then trend).
    Only ever called as a fallback when the real best_category is None.
    """
    if role == "batter":
        m = entity.get("matchup") or {}
        wants = "favorable" if direction == "over" else "unfavorable"
        if m.get(wants) and m.get("pitcher_avg_against") is not None:
            hand = "same-handed" if m.get("platoon") == "same-hand" else "opposite-handed"
            return f"Opposing pitcher allows a {_pct_str(m['pitcher_avg_against'])} average to {hand} hitters this season"
        l15 = entity.get("l15") or {}
        if l15.get("avg") is not None and l15.get("games"):
            return f"Hitting {_pct_str(l15['avg'])} over his last {l15['games']} games"
        return None

    l5 = entity.get("l5") or {}
    if not l5.get("games"):
        return None
    if direction == "over" and l5.get("strike_outs") is not None:
        return f"{round(l5['strike_outs'] / l5['games'], 1)} strikeouts per start over his last {l5['games']} starts"
    if direction == "under" and l5.get("era") is not None:
        return f"{l5['era']} ERA over his last {l5['games']} starts"
    return None


def build_top_picks(report_games, batter_limit=15, pitcher_limit=8):
    """
    Cross-game leaderboards: the best OVER and UNDER candidates across the
    *entire* day/date range, not buried inside each game's card. Excludes
    injured players from both (can't bet on someone who might not play at
    all, in either direction). Unconfirmed-lineup batters are still
    included -- lineups usually don't post until 1-3 hours before game
    time, so requiring CONFIRMED here would leave both lists empty most of
    the day -- but they're scored slightly lower and clearly labeled,
    since "projected" is a real guess.

    Batters and pitchers are scored on two different, NOT directly
    comparable point scales (a batter's score can run up to ~5.0 stacking
    hot-streak + matchup + hit-streak bonuses; a pitcher's tops out around
    3.5-4.0) -- merging both into one score-sorted list would silently put
    every pitcher below every batter regardless of actual confidence, which
    looks like "pitchers are always the worst picks" when it's really just
    a scale mismatch. So each role is ranked separately and returned as its
    own sub-list; the caller renders them as two clearly-labeled rankings
    (batters #1-N, pitchers #1-N) instead of one misleadingly-precise #1-12.
    "Over" for a pitcher means the strikeout prop; "under" means runs/hits
    allowed -- i.e. both lists stay true to their heading ("bet the over" /
    "bet the under"), not "good pitcher / bad pitcher".
    """
    batter_overs, batter_unders = [], []
    pitcher_overs, pitcher_unders = [], []
    for g in report_games:
        for side_key in ("home", "away"):
            side = g[side_key]
            opp_side = g["away"] if side_key == "home" else g["home"]
            for b in side["batters"]:
                if b["injury"]:
                    continue
                confirmed_penalty = 0 if side["lineup_confirmed"] else 0.5
                base = {
                    "role": "batter",
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
                    best_over = b.get("best_over")
                    fallback = None if best_over else fallback_pick_angle(b, "batter", "over")
                    batter_overs.append({**base, "score": over_score, "reasons": over_reasons, "best_category": best_over, "fallback_angle": fallback})

                under_score, under_reasons = batter_under_score(b)
                under_score -= confirmed_penalty
                if under_reasons and under_score > 0:
                    best_under = b.get("best_under")
                    fallback = None if best_under else fallback_pick_angle(b, "batter", "under")
                    batter_unders.append({**base, "score": under_score, "reasons": under_reasons, "best_category": best_under, "fallback_angle": fallback})

            p = side["probable_pitcher"]
            if p and not p["injury"]:
                base = {
                    "role": "pitcher",
                    "player_id": p["player_id"],
                    "name": p["name"],
                    "team": side["team_name"],
                    "opponent": opp_side["team_name"],
                    "date": g["date"],
                    "lineup_confirmed": True,  # probable-pitcher assignments come from the schedule, not the lineups table
                }
                over_score, over_reasons = pitcher_strikeout_over_score(p)
                if over_reasons and over_score > 0:
                    best_over = pitcher_best_category(p, "Strikeouts")
                    fallback = None if best_over else fallback_pick_angle(p, "pitcher", "over")
                    pitcher_overs.append({**base, "score": over_score, "reasons": over_reasons, "best_category": best_over, "fallback_angle": fallback})

                under_score, under_reasons = pitcher_runs_under_score(p)
                if under_reasons and under_score > 0:
                    best_under = pitcher_best_category(p, "Runs Allowed")
                    fallback = None if best_under else fallback_pick_angle(p, "pitcher", "under")
                    pitcher_unders.append({**base, "score": under_score, "reasons": under_reasons, "best_category": best_under, "fallback_angle": fallback})

    batter_overs.sort(key=lambda c: c["score"], reverse=True)
    batter_unders.sort(key=lambda c: c["score"], reverse=True)
    pitcher_overs.sort(key=lambda c: c["score"], reverse=True)
    pitcher_unders.sort(key=lambda c: c["score"], reverse=True)

    overs = {"batters": batter_overs[:batter_limit], "pitchers": pitcher_overs[:pitcher_limit]}
    unders = {"batters": batter_unders[:batter_limit], "pitchers": pitcher_unders[:pitcher_limit]}
    return overs, unders


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
        home_side = build_team_side(conn, game, "home")
        away_side = build_team_side(conn, game, "away")

        # A pitcher's matchup summary needs the *opposing* lineup, which
        # doesn't exist yet while build_team_side() is building his own
        # side -- so it's filled in here, once both sides of the game exist.
        if home_side["probable_pitcher"]:
            home_side["probable_pitcher"]["opponent_matchup"] = pitcher_matchup_summary(away_side["batters"])
        if away_side["probable_pitcher"]:
            away_side["probable_pitcher"]["opponent_matchup"] = pitcher_matchup_summary(home_side["batters"])

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
                "live_score": live_score(conn, game["game_pk"], game["home_team_id"], game["away_team_id"])
                if game["home_score"] is None
                else None,
                "projection": latest_projection(conn, game["game_pk"]),
                "home": home_side,
                "away": away_side,
            }
        )

    # Today only, not the full days_ahead window -- a player in a multi-day
    # series against the same opponent would otherwise show up once per
    # game (e.g. Yordan Alvarez picked both for tonight AND tomorrow's game
    # against the same team), which reads as a duplicate even though each
    # row is technically a different game.
    todays_games = [g for g in report_games if g["date"] == today]
    top_overs, top_unders = build_top_picks(todays_games)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "range": [today, end],
        "games": report_games,
        "top_overs": top_overs,
        "top_unders": top_unders,
        "batter_prop_labels": [label for _, label in BATTER_PROP_CATEGORIES],
        "pitcher_prop_labels": [label for _, label in PITCHER_PROP_CATEGORIES],
    }


def _render_pick_list(lines, subheading, picks):
    if not picks:
        return
    lines.append(f"### {subheading}")
    for pick in picks:
        cat_txt = ""
        if pick["best_category"]:
            c = pick["best_category"]
            cat_txt = f" -- try {c['label']}: {c['pct']}% over {c['line']} recently (vs. {c['n']}-game sample)"
        lines.append(f"- **{pick['name']}** ({pick['team']} vs {pick['opponent']}): {', '.join(pick['reasons'])}{cat_txt}")
    lines.append("")


def _render_picks_section(lines, heading, picks):
    """
    `picks` is {"batters": [...], "pitchers": [...]} -- rendered as two
    separately-ranked lists rather than one merged list, since batter and
    pitcher scores aren't on a comparable scale (see build_top_picks).
    """
    if not picks or not (picks.get("batters") or picks.get("pitchers")):
        return
    lines.append(f"## {heading}")
    _render_pick_list(lines, "Batters", picks.get("batters"))
    _render_pick_list(lines, "Pitchers", picks.get("pitchers"))


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
            lines.append(f"Projected score: {g['away']['team_name']} {p['away_exp_runs']} - {g['home']['team_name']} {p['home_exp_runs']}")
            ml_team = g["home"]["team_name"] if p.get("moneyline_pick") == "home" else g["away"]["team_name"]
            win_prob = p["home_win_prob"] if p.get("moneyline_pick") == "home" else 1 - p["home_win_prob"]
            total_pick = p.get("total_pick") or ("over" if p["over_prob"] >= 0.5 else "under")
            total_prob = p.get("total_pick_prob")
            if total_prob is None:
                total_prob = max(p["over_prob"], 1 - p["over_prob"])
            summary = f"Model likes: **{ml_team}** to win ({win_prob:.0%})"
            if p.get("spread_pick") is not None:
                spread_team = g["home"]["team_name"] if p["spread_pick"] == "home" else g["away"]["team_name"]
                spread_favorite = p.get("spread_favorite") or ("home" if p["home_win_prob"] >= 0.5 else "away")
                spread_side = f"-{p['spread_line']}" if p["spread_pick"] == spread_favorite else f"+{p['spread_line']}"
                summary += f" | Run line: **{spread_team}** {spread_side} ({p['spread_pick_prob']:.0%} to cover)"
            summary += f" | Total {p['total_line']}: lean **{total_pick.upper()}** ({total_prob:.0%})"
            lines.append(summary)
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
                if b["matchup"].get("favorable"):
                    matchup_txt = f" [MATCHUP EDGE: pitcher hits {b['matchup']['pitcher_avg_against']} avg-against vs this hand]"
                elif b["matchup"].get("unfavorable"):
                    matchup_txt = f" [TOUGH MATCHUP: pitcher holds this hand to {b['matchup']['pitcher_avg_against']} avg-against]"
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


MAX_COMPLETENESS_REGRESSION = 0.15  # refuse to publish if data completeness drops by more than this


def _data_completeness(report):
    """
    Fraction of batters in this report that actually have recent-game data
    -- a cheap proxy for "is the underlying database actually populated,
    or is this running against a still-recovering/thin cache." Used to
    guard against ever publishing a real regression (see run() below).
    """
    total = 0
    with_data = 0
    for g in report.get("games", []):
        for side in (g["home"], g["away"]):
            for b in side["batters"]:
                total += 1
                if b.get("l7") is not None:
                    with_data += 1
    return with_data / total if total else 0.0


def run(days_ahead=2, write_archive=True):
    init_db()
    conn = get_conn()
    report = build_report(conn, days_ahead=days_ahead)
    conn.close()

    os.makedirs(OUT_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    json_path = os.path.join(OUT_DIR, "latest.json")

    # This whole pipeline runs against a database that's occasionally mid-
    # recovery (a cache restore that lags the real backfill, a partial sync
    # after some earlier hiccup) -- if it ever is, a normal run here would
    # silently publish sparse data over whatever good, complete report is
    # already live. Refuse instead: only overwrite if the new report isn't
    # a big step down from what's already there. A quieter/normal dip
    # (a couple of call-ups without game logs yet, say) stays well under
    # the threshold and publishes as usual.
    if os.path.exists(json_path):
        try:
            with open(json_path) as f:
                previous_report = json.load(f)
            previous_completeness = _data_completeness(previous_report)
            new_completeness = _data_completeness(report)
            if previous_completeness - new_completeness > MAX_COMPLETENESS_REGRESSION:
                print(
                    f"Refusing to publish: new report's batter data completeness "
                    f"({new_completeness:.0%}) is a big regression from the current live one "
                    f"({previous_completeness:.0%}). This usually means the database this ran "
                    f"against is still mid-recovery -- leaving the existing, better output in place."
                )
                return
        except (json.JSONDecodeError, KeyError, OSError):
            pass  # no usable previous report to compare against -- proceed normally

    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    md_path = os.path.join(OUT_DIR, "latest.md")
    with open(md_path, "w") as f:
        f.write(render_markdown(report))

    # The dated archive is a byte-for-byte duplicate of latest.json, so it's
    # skippable on the frequent quick-refresh cycle (see quick-refresh.yml)
    # where it'd just get overwritten again in minutes anyway -- every skip
    # halves that run's commit size for zero loss of information. The
    # hourly job still writes it -- but only the FIRST time that day (never
    # overwritten again after), so it freezes the earliest same-day
    # snapshot rather than whatever the last hourly run before midnight
    # happened to look like. That matters for grade_picks.py: a LATE-day
    # snapshot could already have some of that day's own game results
    # folded into a batter's rolling stats by the time it's written, which
    # would make "here's what was predicted" quietly include a bit of
    # "here's what already happened" -- a real look-ahead leak for grading
    # purposes, even though it's a non-issue for the live dashboard itself.
    if write_archive:
        archive_path = os.path.join(OUT_DIR, f"props_{today}.json")
        if not os.path.exists(archive_path):
            with open(archive_path, "w") as f:
                json.dump(report, f, indent=2, default=str)

    html_path = os.path.join(OUT_DIR, "index.html")
    with open(html_path, "w") as f:
        f.write(render_html(report))

    # Regenerated every time build_props runs (hourly or quick-refresh)
    # rather than only after grade_picks.py runs, so the two pages can
    # never drift out of sync with each other -- this just re-reads
    # whatever's in track_record.json, no DB access, effectively free.
    render_track_record.run()

    print(f"Wrote report for {len(report['games'])} games to {json_path}, {md_path}, {html_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days-ahead", type=int, default=2)
    p.add_argument(
        "--skip-archive", action="store_true",
        help="Skip writing the dated props_YYYY-MM-DD.json archive (for frequent runs where it'd just be overwritten again in minutes)",
    )
    args = p.parse_args()
    run(days_ahead=args.days_ahead, write_archive=not args.skip_archive)
