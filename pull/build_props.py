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
from db import CAREER_SEASON, MLB_TZ, get_conn, init_db, mlb_today
from game_model import team_bullpen_fatigue
import render_track_record
from render_dashboard import render_html
from sync_teams_and_roster import upsert_player_bio

CURRENT_SEASON = datetime.now(timezone.utc).year
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")

# Hour (US Eastern) at which the day's Top Overs/Unders archive is allowed
# to freeze -- see run()'s own comment for the full reasoning. 4pm ET/1pm
# PT is late enough that most evening games' lineups (typically posted
# 1-3 hours before a ~7pm ET start) have already posted, early enough
# that it's still hours before most games actually start. Deliberately
# checked in Eastern time, not UTC -- see mlb_today()'s own docstring for
# why raw UTC is the wrong clock for "how far into the baseball day is it".
TOP_PICKS_FREEZE_HOUR_ET = 16

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
    """
    as_of_date, if given, restricts to games strictly before it. Originally
    added for point-in-time backtesting, but just as necessary in the live
    build: sync_stats.py pulls MLB's gameLog endpoint, which includes
    TODAY's own game with a live, still-changing line as soon as any of it
    has been played -- without this cutoff, a player's "recent form" (and
    everything downstream of it: the projected line, the lean, whether he
    even shows up in Top Overs/Unders) would start including PART OF the
    very game being predicted the moment the game goes live, and would keep
    including more of it every rebuild until it's silently just describing
    what already happened instead of what was predicted beforehand.
    """
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


def hit_streak(conn, player_id, lookback=40, as_of_date=None):
    """Current consecutive-games-with-a-hit streak -- a prop market in its own right. as_of_date restricts to games strictly before it, same reasoning as batting_rolling()."""
    date_frag = " AND date < ?" if as_of_date else ""
    params = (player_id, as_of_date, lookback) if as_of_date else (player_id, lookback)
    rows = conn.execute(
        f"SELECT hits FROM batting_game_logs WHERE player_id = ?{date_frag} ORDER BY date DESC LIMIT ?",
        params,
    ).fetchall()
    streak = 0
    for r in rows:
        if (r["hits"] or 0) > 0:
            streak += 1
        else:
            break
    return streak


def team_streak_and_form(conn, team_id, lookback=10, as_of_date=None):
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

    home_score IS NOT NULL already excludes any game still in progress (see
    live_score()'s own docstring), but NOT today's own game once it's
    finished -- as_of_date (this game's own date) closes that gap, same
    reasoning as batting_rolling().
    """
    date_frag = " AND official_date < ?" if as_of_date else ""
    home_params = (team_id, as_of_date, lookback) if as_of_date else (team_id, lookback)
    away_params = home_params
    home_rows = conn.execute(
        f"SELECT official_date, home_score as scored, away_score as allowed FROM games "
        f"WHERE home_team_id = ? AND home_score IS NOT NULL{date_frag} ORDER BY official_date DESC LIMIT ?",
        home_params,
    ).fetchall()
    away_rows = conn.execute(
        f"SELECT official_date, away_score as scored, home_score as allowed FROM games "
        f"WHERE away_team_id = ? AND home_score IS NOT NULL{date_frag} ORDER BY official_date DESC LIMIT ?",
        away_params,
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


def team_recent_k_rate(conn, team_id, recent_games=2, as_of_date=None):
    """
    This team's actual strikeout rate (batting side) across its last
    `recent_games` games, vs. its own season rate -- a team that just got
    struck out at an elevated clip (e.g. run through by a tough starter the
    game before) plausibly carries some of that into the very next game of
    the same series against another good arm, which a per-batter platoon
    read alone wouldn't capture. Returns None if there's not enough of
    either window yet.

    as_of_date is essential here, not just good hygiene: this is called
    with the OPPOSING team's id to boost THIS game's own pitcher's
    strikeout score. Without a cutoff, "recent games" would include the
    opposing team's own at-bats in THIS SAME game (once any of it's been
    played and synced) -- using today's game's own result to boost today's
    own pick for that same game, then grading it later as if it had been
    predicted beforehand.
    """
    date_frag = " AND date < ?" if as_of_date else ""
    recent_params = (team_id, as_of_date, recent_games) if as_of_date else (team_id, recent_games)
    recent_pks = conn.execute(
        f"SELECT DISTINCT game_pk, MAX(date) as d FROM batting_game_logs WHERE team_id = ?{date_frag} "
        f"GROUP BY game_pk ORDER BY d DESC LIMIT ?",
        recent_params,
    ).fetchall()
    if len(recent_pks) < recent_games:
        return None
    pks = [r["game_pk"] for r in recent_pks]
    recent = conn.execute(
        f"SELECT SUM(strike_outs) as k, SUM(at_bats) as ab FROM batting_game_logs "
        f"WHERE team_id = ? AND game_pk IN ({','.join('?' for _ in pks)})",
        (team_id, *pks),
    ).fetchone()
    season_date_frag = " AND date < ?" if as_of_date else ""
    season_params = (team_id, CURRENT_SEASON, as_of_date) if as_of_date else (team_id, CURRENT_SEASON)
    season = conn.execute(
        f"SELECT SUM(strike_outs) as k, SUM(at_bats) as ab FROM batting_game_logs WHERE team_id = ? AND season = ?{season_date_frag}",
        season_params,
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
    ("outs", "Outs Recorded"),
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


def season_stat_averages(conn, table, player_id, stats, season=CURRENT_SEASON, as_of_date=None):
    """
    Per-game average for each stat, strictly within `season`. Deliberately
    separate from batting_rolling()/pitching_rolling()'s "season" rollup
    (which is really just "last up to 162/162 games regardless of year" --
    fine for that function's own purposes, since backtest.py relies on that
    exact behavior, but wrong for a projected line: an established
    veteran's last 162 games can span several years, and calling that "this
    season" would be a false claim in the UI text).

    as_of_date, if given, restricts to games strictly before it -- see
    batting_rolling()'s own docstring for why.
    """
    cols = ", ".join(f"SUM({stat}) as {stat}" for stat, _ in stats)
    date_frag = " AND date < ?" if as_of_date else ""
    params = (player_id, season, as_of_date) if as_of_date else (player_id, season)
    row = conn.execute(
        f"SELECT {cols}, COUNT(*) as games FROM {table} WHERE player_id = ? AND season = ?{date_frag}",
        params,
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
        line = round_to_half(avg)
        # Total Bases specifically has a real floor of 1.5, not 0.5: any
        # single (the most common hit) already clears 0.5 total bases, so
        # "Over 0.5 Total Bases" and "Over 0.5 Hits" are the exact same
        # event (both true iff the player gets >=1 hit) -- a 0.5 TB line
        # carries zero information the Hits category doesn't already show,
        # which is why real sportsbooks never post one below 1.5.
        if stat == "total_bases":
            line = max(line, 1.5)
        out[stat] = (avg, line)
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


def batter_prop_categories(conn, player_id, baselines, factor_fn=None, n=10, include_values=True, as_of_date=None):
    cols = [c for c, _ in BATTER_PROP_CATEGORIES]
    date_frag = " AND date < ?" if as_of_date else ""
    params = (player_id, as_of_date, n) if as_of_date else (player_id, n)
    rows = conn.execute(
        f"SELECT {', '.join(cols)}, date FROM batting_game_logs WHERE player_id = ?{date_frag} ORDER BY date DESC LIMIT ?",
        params,
    ).fetchall()
    return _prop_categories(rows, BATTER_PROP_CATEGORIES, baselines, factor_fn=factor_fn, include_values=include_values)


def pitcher_prop_categories(conn, player_id, baselines, factor_fn=None, n=5, include_values=True, as_of_date=None):
    cols = [c for c, _ in PITCHER_PROP_CATEGORIES]
    date_frag = " AND date < ?" if as_of_date else ""
    params = (player_id, as_of_date, n) if as_of_date else (player_id, n)
    rows = conn.execute(
        f"SELECT {', '.join(cols)}, date FROM pitching_game_logs WHERE player_id = ?{date_frag} ORDER BY date DESC LIMIT ?",
        params,
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


PITCHER_POSITIVE_CATEGORIES = {"Strikeouts", "Outs Recorded"}  # higher is "pitching well" for these; lower for everything else


def pitcher_category_factor(label, form_trend):
    """
    Unlike batters, 'pitching well' doesn't push every category the same
    direction: a dominant stretch means MORE strikeouts and MORE outs
    recorded (going deeper into games instead of getting an early hook)
    but FEWER runs/hits/walks allowed, so the sign flips depending on the
    category.
    """
    positive = label in PITCHER_POSITIVE_CATEGORIES
    if form_trend == "dominant":
        return 1 + PITCHER_FORM_PROJECTION_FACTOR if positive else 1 - PITCHER_FORM_PROJECTION_FACTOR
    if form_trend == "rough":
        return 1 - PITCHER_FORM_PROJECTION_FACTOR if positive else 1 + PITCHER_FORM_PROJECTION_FACTOR
    return 1.0


def pitching_rolling(conn, player_id, n, as_of_date=None):
    """as_of_date, if given, restricts to games strictly before it -- see batting_rolling()'s own docstring for why this matters."""
    date_frag = " AND date < ?" if as_of_date else ""
    params = (player_id, as_of_date, n) if as_of_date else (player_id, n)
    rows = conn.execute(
        f"SELECT * FROM pitching_game_logs WHERE player_id = ?{date_frag} ORDER BY date DESC LIMIT ?",
        params,
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
        "SELECT innings_pitched, outs, hits, earned_runs, runs, base_on_balls, strike_outs, home_runs "
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


def build_batter_entry(conn, player_id, opp_hand, opp_pitcher_id, is_home_game, team_id, batting_order=None, game_pk=None, as_of_date=None, status=None):
    """
    as_of_date is this game's own date -- passed down into every rolling/
    recent-form/streak lookup below so none of them can ever include THIS
    game's own result (in progress or already final) in what's supposed to
    be a pre-game read. See batting_rolling()'s own docstring for why that
    matters. game_result itself is deliberately NOT filtered by it -- that
    one's supposed to be this specific game's actual result. status is
    this game's current status, used only to gate matchup_lean's hit/miss
    verdict to real, final results (see pick_result()'s own docstring).
    """
    player = conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
    if not player:
        player = _ensure_player(conn, player_id, team_id)
        if not player:
            return None
    l7 = batting_rolling(conn, player_id, 7, as_of_date=as_of_date)
    l15 = batting_rolling(conn, player_id, 15, as_of_date=as_of_date)
    season = batting_rolling(conn, player_id, 162, as_of_date=as_of_date)
    trend = form_trend(l7, season)
    matchup = matchup_edge(conn, player["bat_side"], opp_hand, opp_pitcher_id)

    season_avgs = season_stat_averages(conn, "batting_game_logs", player_id, BATTER_PROP_CATEGORIES, as_of_date=as_of_date)
    baselines = category_baselines(l15, season_avgs, BATTER_PROP_CATEGORIES)
    factor_fn = lambda label: batter_matchup_factor(matchup)  # noqa: E731 -- same factor for every batting category
    recent_categories = batter_prop_categories(conn, player_id, baselines, factor_fn=factor_fn, as_of_date=as_of_date)
    season_categories = batter_prop_categories(conn, player_id, baselines, factor_fn=factor_fn, n=200, include_values=False, as_of_date=as_of_date)
    best_over, best_under = prop_category_delta(recent_categories, season_categories, require_lean_agreement=True)
    best_prop, best_prop_direction = headline_prop(best_over, best_under)
    if best_prop is None:
        best_prop, best_prop_direction = fallback_best_prop(recent_categories, season_categories)

    game_result = batter_game_result(conn, player_id, game_pk) if game_pk else None
    matchup_lean = _lean_with_result(best_matchup_lean(recent_categories), game_result, "batter", status in FINAL_STATUSES)

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
        "hit_streak": hit_streak(conn, player_id, as_of_date=as_of_date),
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
        "matchup_lean": matchup_lean,
        "game_result": game_result,
    }


def build_pitcher_entry(conn, player_id, team_id, is_home_game=None, game_pk=None, as_of_date=None, status=None):
    """as_of_date, status -- see build_batter_entry()'s own docstring; same reasoning, pitcher side."""
    if not player_id:
        return None
    player = conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
    if not player:
        player = _ensure_player(conn, player_id, team_id)
        if not player:
            return None
    l5 = pitching_rolling(conn, player_id, 5, as_of_date=as_of_date)
    season = pitching_rolling(conn, player_id, 162, as_of_date=as_of_date)
    form_trend_value = pitcher_form_trend(l5, season)

    season_avgs = season_stat_averages(conn, "pitching_game_logs", player_id, PITCHER_PROP_CATEGORIES, as_of_date=as_of_date)
    baselines = category_baselines(l5, season_avgs, PITCHER_PROP_CATEGORIES)
    factor_fn = lambda label: pitcher_category_factor(label, form_trend_value)  # noqa: E731
    recent_categories = pitcher_prop_categories(conn, player_id, baselines, factor_fn=factor_fn, as_of_date=as_of_date)
    season_categories = pitcher_prop_categories(conn, player_id, baselines, factor_fn=factor_fn, n=200, include_values=False, as_of_date=as_of_date)
    best_over, best_under = prop_category_delta(recent_categories, season_categories, min_games=4)
    best_prop, best_prop_direction = headline_prop(best_over, best_under)
    if best_prop is None:
        best_prop, best_prop_direction = fallback_best_prop(recent_categories, season_categories)

    game_result = pitcher_game_result(conn, player_id, game_pk) if game_pk else None
    matchup_lean = _lean_with_result(best_matchup_lean(recent_categories), game_result, "pitcher", status in FINAL_STATUSES)

    return {
        "player_id": player_id,
        "name": player["full_name"],
        "pitch_hand": player["pitch_hand"],
        "injury": injury_status(conn, player_id),
        # MLB's schedule API has no real "confirmed starter" flag for a
        # probable pitcher the way a posted batting lineup does -- but a
        # game's own status transitioning past Scheduled/Preview (usually
        # ~1-2 hours before first pitch, same rough window as batting
        # lineups posting) is a real, if approximate, signal that this
        # start is locked in: bullpens are already getting loose by then,
        # far too late for a rotation reshuffle. A probable pitcher still
        # listed for a game days out (status still Scheduled) is
        # genuinely less certain -- confirmed real case: the Guardians/Reds
        # doubleheader created by a 7/27 postponement reshuffled who
        # started which game.
        "lineup_confirmed": status not in (None, "Scheduled", "Preview"),
        "l3": pitching_rolling(conn, player_id, 3, as_of_date=as_of_date),
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
        "matchup_lean": matchup_lean,
        # filled in by build_report() once the opposing lineup for this game
        # is known -- a pitcher's own entry has no visibility into the other
        # team's batters at the point build_team_side() constructs it.
        "opponent_matchup": None,
        "game_result": game_result,
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

    # This game's own date -- passed into every entry below so none of
    # their "recent form" ever includes THIS game's own result. See
    # build_batter_entry()'s own docstring for why.
    as_of_date = game["official_date"]

    if confirmed:
        lineup_confirmed = True
        batters = [
            build_batter_entry(
                conn, r["player_id"], opp_hand, opp_pitcher_id, is_home_game, team_id, r["batting_order"], game["game_pk"],
                as_of_date=as_of_date, status=game["status"],
            )
            for r in confirmed
        ]
    else:
        lineup_confirmed = False
        batters = [
            build_batter_entry(
                conn, pid, opp_hand, opp_pitcher_id, is_home_game, team_id, game_pk=game["game_pk"], as_of_date=as_of_date, status=game["status"]
            )
            for pid in likely_starters(conn, team_id)
        ]

    opp_team_id = game[f"{opp_side}_team_id"]
    pitcher = build_pitcher_entry(
        conn, game[f"{side}_probable_pitcher_id"], team_id, is_home_game, game["game_pk"], as_of_date=as_of_date, status=game["status"]
    )
    if pitcher:
        # Unlike opponent_matchup (needs the opposing BATTERS list, so it's
        # filled in later by build_report() once both sides exist), this
        # only needs the opposing team_id, already in scope here.
        pitcher["opponent_recent_k_rate"] = team_recent_k_rate(conn, opp_team_id, as_of_date=as_of_date)
    confirmed_batters = [b for b in batters if b]
    return {
        "team_id": team_id,
        "team_name": team["name"] if team else None,
        "lineup_confirmed": lineup_confirmed,
        "probable_pitcher": pitcher,
        # bullpen fatigue is about who these batters face in relief innings,
        # so it's the *opponent's* pen -- unrelated to (and doesn't touch)
        # the platoon/vs-hand matchup logic on the starter above.
        "opponent_bullpen_fatigue": team_bullpen_fatigue(conn, opp_team_id, as_of_date=as_of_date),
        "form": team_streak_and_form(conn, team_id, as_of_date=as_of_date),
        "batters": confirmed_batters,
        "injuries": team_injury_report(conn, team_id),
        "star_player_id": best_prop_star(confirmed_batters, pitcher),
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


def prop_category_delta(recent_categories, season_categories, min_games=8, require_lean_agreement=False):
    """
    The category where recent performance deviates most from this player's
    OWN season norm, in each direction -- not the category with the
    highest raw hit-rate. Comparing raw rates across categories always
    picks "1+ hits" (the easiest bar to clear for almost any hitter), which
    isn't a meaningful "best angle," just an artifact of it being the
    lowest threshold.

    require_lean_agreement (batters only -- see build_batter_entry()) adds
    a second, independent bar: a raw recent-vs-season deviation alone is
    exactly the "hot streak" signal backtest_props.py already found has
    near-zero single-game predictive power on its own -- confirmed live,
    not just in that backtest: the Top Overs leaderboard, built from this
    exact signal, hit only ~34% across its first 4 tracked days, worse
    than a coin flip. A category only qualifies here if its own `lean`
    (today's matchup-adjusted projection vs. line, which DOES fold in the
    real, backtest-validated matchup-edge factor -- see
    batter_matchup_factor()) agrees with the deviation's direction. A
    category that merely ran hot recently with no supporting matchup edge
    tonight no longer qualifies as a confident "best" angle -- callers
    fall back to resolve_best_category()'s own lean/popularity chain
    instead, which is the intended effect: an uncorroborated hot streak
    shouldn't headline with false confidence. Pitchers don't get this
    (their own `lean` is driven by recent form_trend, not an opposing-
    lineup matchup edge -- the same kind of streak signal being
    questioned here, not an independent check on it).
    Returns (most_over, most_under), either possibly None.
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
        deltas.append((r_line["pct"] - s_line["pct"], rc["label"], r_line, rc.get("lean")))
    if not deltas:
        return None, None

    over_pool = [d for d in deltas if d[0] > 0 and (not require_lean_agreement or d[3] == "over")]
    under_pool = [d for d in deltas if d[0] < 0 and (not require_lean_agreement or d[3] == "under")]

    most_over = None
    if over_pool:
        delta, label, line, _ = max(over_pool, key=lambda d: d[0])
        most_over = {"label": label, "line": line["line"], "pct": line["pct"], "n": line["n"], "season_pct": line["pct"] - delta, "delta": delta}
    most_under = None
    if under_pool:
        delta, label, line, _ = min(under_pool, key=lambda d: d[0])
        most_under = {"label": label, "line": line["line"], "pct": line["pct"], "n": line["n"], "season_pct": line["pct"] - delta, "delta": delta}
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


_BATTER_CATEGORY_FIELD = {label: field for field, label in BATTER_PROP_CATEGORIES}
_PITCHER_CATEGORY_FIELD = {label: field for field, label in PITCHER_PROP_CATEGORIES}

FINAL_STATUSES = {"Final", "Game Over", "Completed Early"}


def pick_result(role, category, game_result, direction, is_final):
    """
    Whether a Top Overs/Unders pick has actually hit, missed, or has no
    verdict yet -- read straight from this player's own game_result (the
    same box-score data the dashboard already shows for them), not a fresh
    query, so it always matches whatever's currently on screen.

    Every one of these stats only ever goes UP over the course of a game
    (a hit total can't decrease), which makes "cleared its line" a
    one-way, permanent fact the moment it happens -- an OVER that's
    already cleared can never un-clear, and an UNDER that's already been
    exceeded can never un-exceed. So that state is safe to report
    immediately, live, mid-game. The opposite state ("hasn't cleared yet")
    is NOT permanent -- it could still flip before the last out -- so
    that one is deliberately withheld (returns None) until is_final,
    rather than reporting a still-reversible snapshot as the final word.
    grade_picks.py's own end-of-day grading is the permanent, authoritative
    record either way; this is just a same-glance version for the
    leaderboard itself.
    """
    if not category or not game_result:
        return None
    field = (_BATTER_CATEGORY_FIELD if role == "batter" else _PITCHER_CATEGORY_FIELD).get(category["label"])
    if not field or game_result.get(field) is None:
        return None
    cleared = game_result[field] > category["line"]
    if not cleared and not is_final:
        return None
    return "hit" if (cleared if direction == "over" else not cleared) else "miss"


def _lean_with_result(lean, game_result, role, is_final):
    """Attaches the hit/miss verdict directly onto the matchup_lean dict, using the same grading logic as Top Overs/Unders -- so the dashboard's "Predicted: X" line can say whether it actually hit, not just restate the pre-game call. Same is_final gating as pick_result()."""
    if lean and game_result:
        lean["result"] = pick_result(role, lean, game_result, lean["direction"], is_final)
    return lean


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

    Home Runs UNDER is excluded -- a home run is rare enough per game that
    "under" a low line (usually 0.5) is true almost every time regardless
    of matchup, making it a near-guaranteed, low-value headline rather
    than a meaningful call. Home Runs OVER stays eligible: precisely
    because it's rare, a real OVER lean is the interesting, worth-
    surfacing case.
    """
    candidates = [c for c in categories if c.get("lean") and not (c["label"] == "Home Runs" and c["lean"] == "under")]
    if not candidates:
        return None
    best = max(candidates, key=lambda c: abs(c["today_projection"] - c["primary_line"]))
    return {"label": best["label"], "line": best["primary_line"], "direction": best["lean"], "projection": best["today_projection"]}


def best_prop_star(batters, pitcher):
    """
    One player per team side to headline with a star -- whichever batter
    OR the probable pitcher has the single strongest "Best prop" signal on
    the team, batters and pitcher compared directly against each other.
    Unlike the batter_over_score/pitcher_strikeout_over_score point
    systems (explicitly NOT comparable across roles -- see
    build_top_picks()'s own docstring), best_prop's own "delta" (recent
    hit-rate% minus season hit-rate%) is a plain percentage-point
    deviation either way, so it's the one signal that's already
    apples-to-apples between a batter and a pitcher.

    Deliberately pre-game only: only considers entries with a real
    "delta" key, meaning they cleared prop_category_delta()'s actual
    8+-recent-games-and-real-deviation bar (headline_prop's path) --
    fallback_best_prop()'s lenient version has no "delta" at all, so a
    thin-sample "just show something" pick can never win the star. And
    since best_prop/delta are built from recent_categories/
    season_categories, which are already as_of_date-filtered to exclude
    this game's own result (see build_batter_entry()'s docstring), the
    star can never be swayed by how the game actually turns out --
    only ever a pre-game read.
    """
    candidates = [(b["player_id"], "batter", b["best_prop"]) for b in batters if b.get("best_prop") and "delta" in b["best_prop"]]
    if pitcher and pitcher.get("best_prop") and "delta" in pitcher["best_prop"]:
        candidates.append((pitcher["player_id"], "pitcher", pitcher["best_prop"]))
    if not candidates:
        return None
    player_id, role, prop = max(candidates, key=lambda c: abs(c[2]["delta"]))
    return player_id


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


# Most commonly bet MLB player-prop categories, in priority order -- the
# guaranteed last-resort fallback below picks the first of these a player
# has any recent-game data for at all. Home Runs deliberately excluded --
# a much more volatile/streaky event per game than Hits/Total Bases/RBIs,
# a worse choice specifically as a generic "at least show something real"
# fallback (it can still surface on its own merits via the stricter,
# deviation-based best_over/best_under path above this one).
POPULAR_BATTER_CATEGORIES = ["Hits", "Total Bases", "RBIs"]
POPULAR_PITCHER_CATEGORIES = ["Strikeouts", "Outs Recorded", "Hits Allowed"]


def _lenient_category_for_direction(categories, direction, exclude_label=None):
    """
    Like fallback_best_prop(), but constrained to the direction this pick
    actually needs: a Top Over/Under pick can qualify on a signal other
    than a specific prop category (matchup edge, hot streak), and
    prop_category_delta()'s 8+-recent-games-and-real-deviation bar can
    legitimately come back empty even for a real pick -- but a plain text
    remark with no concrete number isn't a real prop. This picks whichever
    of the player's own categories (already matchup-adjusted -- see
    today_projection/lean in _prop_categories()) actually leans the
    requested direction, with the most recent-game data behind it. Home
    Runs excluded here too (see POPULAR_BATTER_CATEGORIES) -- too
    volatile/streaky a category to lean on as a generic filler, even
    when it happens to lean the right way; it can still surface on its
    own merits via the stricter best_over/best_under path above this one.
    exclude_label skips whatever category the SAME player's opposite-
    direction pick already resolved to -- see resolve_best_category().
    """
    candidates = [
        c for c in (categories or [])
        if c.get("lean") == direction and c.get("hit_rates") and c["label"] != "Home Runs" and c["label"] != exclude_label
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda c: abs(c["today_projection"] - c["primary_line"]))
    hr = best["hit_rates"][0]
    return {"label": best["label"], "line": best["primary_line"], "pct": hr["pct"], "n": hr["n"]}


def _popular_prop_category(categories, role, exclude_label=None):
    """
    Guaranteed last resort so a Top Over/Under pick NEVER shows as just a
    name with a text remark and no real number: the first category, in
    order of how commonly these are actually bet, this player has any
    recent-game data for. Not scored, doesn't affect ranking or who
    qualifies -- purely ensures every pick has a concrete prop+line to
    display and grade. exclude_label skips whatever category the SAME
    player's opposite-direction pick already resolved to -- unlike the
    lenient path above, this one ignores lean/direction entirely (it's the
    last resort), so without this it would happily hand back the exact
    same category+line for both a player's over AND under pick whenever
    neither direction had a real signal -- confirmed on a real case (a
    pitcher with only 4 recent starts, no deviation either way, showing
    identical "Strikeouts" on both his Top Overs and Top Unders entries).
    """
    priority = POPULAR_BATTER_CATEGORIES if role == "batter" else POPULAR_PITCHER_CATEGORIES
    by_label = {c["label"]: c for c in (categories or []) if c.get("hit_rates")}
    for label in priority:
        if label == exclude_label:
            continue
        cat = by_label.get(label)
        if cat:
            hr = cat["hit_rates"][0]
            return {"label": label, "line": cat["primary_line"], "pct": hr["pct"], "n": hr["n"]}
    return None


def resolve_best_category(categories, role, direction, exclude_label=None):
    """
    Real prop+line for a Top Over/Under pick, even when nothing clears
    prop_category_delta()'s strict bar -- see the two helpers above.
    exclude_label, when given, is the category label the SAME player's
    opposite-direction pick already settled on -- passed by build_top_picks()
    so a player never ends up with the identical category+line headlining
    both his over and his under pick.
    """
    return (
        _lenient_category_for_direction(categories, direction, exclude_label)
        or _popular_prop_category(categories, role, exclude_label)
    )


def _pick_category(strict, categories, role, direction, exclude_label):
    """
    strict is prop_category_delta()'s best_over/best_under (already
    guaranteed by that function to never equal each other WITHIN one
    direction pairing) -- but it's a DIFFERENT metric from the lean-based
    fallback (recent-vs-season hit-rate delta here, vs today's
    matchup-adjusted projection vs. the line there), so the two can still
    independently agree on the same category even though they measure
    different things. Confirmed on a real pitcher: best_under legitimately
    landed on "Strikeouts" via the delta metric, while the over pick (no
    strict signal) fell back to "Strikeouts" too via the lean metric --
    exclude_label alone doesn't catch this, since the truthy strict value
    short-circuits past the excluding fallback entirely. This discards
    strict too if it matches what the other direction already has, so the
    fallback gets a real chance to find something different.
    """
    if strict and strict["label"] == exclude_label:
        strict = None
    return strict or resolve_best_category(categories, role, direction, exclude_label=exclude_label)


LINEUP_WINDOW_HOURS = 3  # matches the "typically 1-3 hours before first pitch" window referenced elsewhere


def _hours_until_first_pitch(game_time_utc):
    if not game_time_utc:
        return None
    try:
        start = datetime.fromisoformat(game_time_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (start - datetime.now(timezone.utc)).total_seconds() / 3600


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
    A pitcher's OVER/UNDER category isn't fixed to strikeouts/runs-allowed --
    like batters, it's whichever of his 4 categories (Strikeouts, Runs
    Allowed, Hits Allowed, Walks Allowed) deviates most from his own season
    norm in that direction (best_over/best_under, from prop_category_delta()
    in build_pitcher_entry()), falling back to resolve_best_category() the
    same way batters do. The pitching-well/pitching-poorly SELECTION signal
    (pitcher_strikeout_over_score()/pitcher_runs_under_score() below) is
    separate from and doesn't need to match the specific category shown --
    a pitcher can make the Over list on "pitching well" grounds and still
    headline, say, a Walks Allowed over if that's his strongest recent
    trend.
    """
    batter_overs, batter_unders = [], []
    pitcher_overs, pitcher_unders = [], []
    for g in report_games:
        is_final = g["status"] in FINAL_STATUSES
        game_started = g["status"] not in (None, "Scheduled", "Pre-Game", "Preview", "Warmup")
        # Being unconfirmed is only a real signal once we're actually
        # inside the window a lineup/pitcher assignment would normally
        # post in -- outside it, EVERY game is unconfirmed regardless of
        # pick quality, purely because it's too early in the day to know
        # yet. Without this gate, the Top Overs/Unders freeze (one fixed
        # daily clock time -- see TOP_PICKS_FREEZE_HOUR_ET) structurally
        # favored whichever games happened to start earliest that day
        # (their window had already passed by freeze time) over anything
        # starting significantly later -- confirmed real case: every West
        # Coast night game was still unconfirmed at the freeze moment,
        # every single time, regardless of the underlying pick. hours is
        # None (missing/malformed game time) falls back to the old
        # always-penalize behavior rather than silently waiving it.
        hours_until_first_pitch = _hours_until_first_pitch(g.get("game_time_utc"))
        lineup_window_open = hours_until_first_pitch is None or hours_until_first_pitch <= LINEUP_WINDOW_HOURS
        for side_key in ("home", "away"):
            side = g[side_key]
            opp_side = g["away"] if side_key == "home" else g["home"]
            for b in side["batters"]:
                if b["injury"]:
                    continue
                confirmed_penalty = 0 if (side["lineup_confirmed"] or not lineup_window_open) else 0.5
                base = {
                    "role": "batter",
                    "player_id": b["player_id"],
                    "name": b["name"],
                    "team": side["team_name"],
                    "opponent": opp_side["team_name"],
                    "date": g["date"],
                    "game_pk": g["game_pk"],
                    "lineup_confirmed": side["lineup_confirmed"],
                    # Recorded so a later, real starting-pitcher swap can be
                    # detected against this and the pick's matchup-dependent
                    # fields recomputed -- see _refresh_frozen_pick().
                    "opp_pitcher_id": (opp_side["probable_pitcher"] or {}).get("player_id"),
                }

                best_over = None
                over_score, over_reasons = batter_over_score(b)
                over_score -= confirmed_penalty
                if over_reasons and over_score > 0:
                    best_over = b.get("best_over") or resolve_best_category(b.get("prop_categories"), "batter", "over")
                    result = pick_result("batter", best_over, b.get("game_result"), "over", is_final)
                    dnp = game_started and b.get("game_result") is None
                    batter_overs.append({**base, "score": over_score, "reasons": over_reasons, "best_category": best_over, "result": result, "dnp": dnp})

                under_score, under_reasons = batter_under_score(b)
                under_score -= confirmed_penalty
                if under_reasons and under_score > 0:
                    # exclude_label: never repeat this same player's own OVER
                    # category on his UNDER pick (or vice versa) -- confirmed
                    # this happened for real (a pitcher with no clear trend
                    # either way landing on the identical fallback category
                    # for both), which reads as the model contradicting
                    # itself on the same prop.
                    exclude = best_over["label"] if best_over else None
                    best_under = _pick_category(b.get("best_under"), b.get("prop_categories"), "batter", "under", exclude)
                    result = pick_result("batter", best_under, b.get("game_result"), "under", is_final)
                    dnp = game_started and b.get("game_result") is None
                    batter_unders.append({**base, "score": under_score, "reasons": under_reasons, "best_category": best_under, "result": result, "dnp": dnp})

            p = side["probable_pitcher"]
            if p and not p["injury"]:
                # Same confirmed_penalty treatment as batters above -- a
                # probable pitcher still days out (lineup_confirmed False,
                # see build_pitcher_entry()) is a real guess, not a lock,
                # so he shouldn't rank into Top Overs/Unders on equal
                # footing with a start that's essentially already locked
                # in. Same lineup_window_open gate too -- pitcher
                # confirmation is itself just this same game's own status
                # crossing into Pre-Game, which is exactly as vulnerable
                # to the early-vs-late-game freeze-time bias as a batting
                # lineup is.
                pitcher_confirmed_penalty = 0 if (p.get("lineup_confirmed", True) or not lineup_window_open) else 0.5
                base = {
                    "role": "pitcher",
                    "player_id": p["player_id"],
                    "name": p["name"],
                    "team": side["team_name"],
                    "opponent": opp_side["team_name"],
                    "date": g["date"],
                    "game_pk": g["game_pk"],
                    "lineup_confirmed": p.get("lineup_confirmed", True),
                }
                best_over = None
                over_score, over_reasons = pitcher_strikeout_over_score(p)
                over_score -= pitcher_confirmed_penalty
                if over_reasons and over_score > 0:
                    best_over = p.get("best_over") or resolve_best_category(p.get("prop_categories"), "pitcher", "over")
                    result = pick_result("pitcher", best_over, p.get("game_result"), "over", is_final)
                    dnp = game_started and p.get("game_result") is None
                    pitcher_overs.append({**base, "score": over_score, "reasons": over_reasons, "best_category": best_over, "result": result, "dnp": dnp})

                under_score, under_reasons = pitcher_runs_under_score(p)
                under_score -= pitcher_confirmed_penalty
                if under_reasons and under_score > 0:
                    exclude = best_over["label"] if best_over else None
                    best_under = _pick_category(p.get("best_under"), p.get("prop_categories"), "pitcher", "under", exclude)
                    result = pick_result("pitcher", best_under, p.get("game_result"), "under", is_final)
                    dnp = game_started and p.get("game_result") is None
                    pitcher_unders.append({**base, "score": under_score, "reasons": under_reasons, "best_category": best_under, "result": result, "dnp": dnp})

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


def _load_frozen_top_picks(today):
    """
    output/props_{date}.json is already written exactly ONCE per day, the
    very first time build_props.py runs that day (see run()'s own
    docstring) -- specifically so grade_picks.py has a frozen, before-any-
    results snapshot to grade against later. Reusing that same archive
    here means the Top Overs/Unders section on the LIVE dashboard gets the
    same guarantee: which players are on it, their rank, and their
    predicted category/line are locked in from the morning and never
    reshuffle over the course of the day -- confirmed lineups posting,
    injury updates, or (pre-fix) a player's own in-game stats could
    otherwise change the score enough to add, drop, or reorder picks after
    their games had already started. Returns None if nothing's archived
    yet today (the current, first-of-the-day build), so the caller falls
    back to computing fresh.
    """
    path = os.path.join(OUT_DIR, f"props_{today}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            frozen = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    top_overs, top_unders = frozen.get("top_overs"), frozen.get("top_unders")
    if not top_overs and not top_unders:
        return None
    return top_overs, top_unders


def _game_pk_for_date(conn, role, player_id, date):
    """Fallback for archives frozen before game_pk was added to each pick -- looked up from the player's own game log row for that date instead. Only finds anything once the player's actually recorded a plate appearance/inning, so it's useless before then -- see _game_pk_for_team_and_date() for the pre-any-stats case."""
    table = "batting_game_logs" if role == "batter" else "pitching_game_logs"
    row = conn.execute(f"SELECT game_pk FROM {table} WHERE player_id = ? AND date = ?", (player_id, date)).fetchone()
    return row["game_pk"] if row else None


def _game_pk_for_team_and_date(conn, player_id, date):
    """
    Second fallback: resolves game_pk via this player's CURRENT team +
    the pick's date, via the games table directly -- works even before
    the player has recorded any stat at all today (unlike
    _game_pk_for_date, which needs an existing game log row). Without
    this, a player who simply hadn't batted/pitched yet (game not
    started, or early innings before his first PA) could never have his
    lineup-confirmed status or result checked at all, no matter how long
    the day went on -- exactly the gap that left a genuinely-confirmed
    starter still showing PROJECTED.
    """
    player = conn.execute("SELECT current_team_id FROM players WHERE player_id = ?", (player_id,)).fetchone()
    if not player or not player["current_team_id"]:
        return None
    row = conn.execute(
        "SELECT game_pk FROM games WHERE official_date = ? AND ? IN (home_team_id, away_team_id)",
        (date, player["current_team_id"]),
    ).fetchone()
    return row["game_pk"] if row else None


def _game_status(conn, game_pk):
    row = conn.execute("SELECT status FROM games WHERE game_pk = ?", (game_pk,)).fetchone()
    return row["status"] if row else None


def _lineup_confirmed_for_player(conn, game_pk, player_id):
    """
    Whether THIS SPECIFIC player has a confirmed-lineup row for this exact
    game -- checked directly against the lineups table, not whether he's
    still present in a recomputed batters list. A player picked this
    morning off a "likely starter" guess who then isn't in the real
    lineup (rest day, late scratch) would otherwise never get looked up
    at all, leaving a stale PROJECTED tag with no way to tell "the real
    lineup posted, he's just not in it" apart from "nothing's posted yet".
    """
    row = conn.execute("SELECT 1 FROM lineups WHERE game_pk = ? AND player_id = ?", (game_pk, player_id)).fetchone()
    return row is not None


def _regrade_picks(conn, picks_field, direction):
    """
    Refreshes the hit/miss verdict and the lineup-confirmed status on an
    already-frozen pick list, against each pick's own frozen best_category/
    line -- never touches who made the list, their rank, or that line
    itself. The verdict stays None (pick_result()'s own gate) until the
    game the pick belongs to is actually Final.
    """
    if not picks_field:
        return
    for role_key in ("batters", "pitchers"):
        for pick in picks_field.get(role_key) or []:
            role = pick.get("role", "batter")
            game_pk = (
                pick.get("game_pk")
                or _game_pk_for_date(conn, role, pick["player_id"], pick["date"])
                or _game_pk_for_team_and_date(conn, pick["player_id"], pick["date"])
            )
            if not game_pk:
                continue
            status = _game_status(conn, game_pk)
            if role == "batter":
                pick["lineup_confirmed"] = _lineup_confirmed_for_player(conn, game_pk, pick["player_id"])
            else:
                # No lineups-table equivalent for a probable pitcher -- see
                # build_pitcher_entry()'s own comment on this same status-
                # based proxy.
                pick["lineup_confirmed"] = status not in (None, "Scheduled", "Preview")
            game_result = (batter_game_result if role == "batter" else pitcher_game_result)(conn, pick["player_id"], game_pk)
            pick["result"] = pick_result(role, pick.get("best_category"), game_result, direction, status in FINAL_STATUSES)
            # DNP: the game's underway or over and this player still has no
            # stat line at all -- a real thing to say (he wasn't in it, a
            # rest day, a late scratch), distinct from "no result yet"
            # (game hasn't reached that point) or a genuine graded miss.
            pick["dnp"] = game_result is None and status not in (None, "Scheduled", "Pre-Game", "Preview", "Warmup")


def _player_context(todays_games):
    """
    Current data for every player showing up today, keyed by (role,
    player_id) -- used to refresh an already-frozen pick without needing to
    recompute the whole entry. For batters this also carries which
    opposing starting pitcher the CURRENT build used, plus the fields
    batter_over_score()/batter_under_score() need -- compared against the
    pitcher a frozen pick was originally built against in
    _refresh_frozen_pick(), so a real late pitcher swap (scratch, bullpen
    game, etc.) can be detected and the pick's matchup-dependent fields
    recomputed, rather than silently left stale against a pitcher who's no
    longer even starting.
    """
    context = {}
    for g in todays_games:
        for side_key in ("home", "away"):
            side = g[side_key]
            opp_side = g["away"] if side_key == "home" else g["home"]
            opp_pitcher_id = (opp_side["probable_pitcher"] or {}).get("player_id")
            for b in side["batters"]:
                context[("batter", b["player_id"])] = {
                    "prop_categories": b.get("prop_categories"),
                    "best_over": b.get("best_over"),
                    "best_under": b.get("best_under"),
                    "trend": b.get("trend"),
                    "trend_caveat": b.get("trend_caveat"),
                    "matchup": b.get("matchup"),
                    "hit_streak": b.get("hit_streak"),
                    "opp_pitcher_id": opp_pitcher_id,
                    "lineup_confirmed": side["lineup_confirmed"],
                }
            p = side["probable_pitcher"]
            if p:
                context[("pitcher", p["player_id"])] = {
                    "prop_categories": p.get("prop_categories"),
                    "best_over": p.get("best_over"),
                    "best_under": p.get("best_under"),
                }
    return context


def _refresh_frozen_pick(pick, direction, player_context, exclude_label=None):
    """
    Refreshes an already-frozen pick's matchup-dependent fields using
    CURRENT (but still as-of-date-safe) data, without touching who's on
    the list or their rank -- two cases:

    1. best_category missing entirely: backfills it (a pick frozen before
       resolve_best_category() existed -- previously a text-remark-only
       "fallback_angle", or nothing at all).
    2. (batters only) the opposing starting pitcher has genuinely changed
       since this pick was frozen: recomputes score/reasons/best_category
       against the new pitcher. Still pre-game information, not a result,
       so this doesn't compromise grading integrity the way reacting to
       the game's own outcome would -- it just keeps the pick honest about
       who's actually starting. Picks frozen before this field existed
       have no opp_pitcher_id to compare against, so they're left alone
       until the next day's fresh freeze.

    exclude_label is this same player's OTHER pick's already-resolved
    category (over vs under) -- the caller looks it up across the two
    lists before calling this, same reasoning as build_top_picks()'s own
    exclude_label: without it, a fallback resolve here could hand back the
    identical category+line the player's other-direction pick already
    has, which reads as the model contradicting itself on the same prop.

    lineup_confirmed is handled separately in _regrade_picks(), via a
    direct per-player lineups-table lookup rather than this reconstructed
    batters list -- a player who isn't actually starting today wouldn't be
    in it at all, which left him stuck without a real check either way.
    """
    role = pick.get("role", "batter")
    ctx = player_context.get((role, pick["player_id"]))
    if not ctx:
        return
    pitcher_changed = (
        role == "batter"
        and pick.get("opp_pitcher_id") is not None
        and ctx.get("opp_pitcher_id") is not None
        and pick["opp_pitcher_id"] != ctx["opp_pitcher_id"]
    )
    if not pick.get("best_category") and not pitcher_changed:
        resolved = resolve_best_category(ctx.get("prop_categories"), role, direction, exclude_label=exclude_label)
        if resolved:
            pick["best_category"] = resolved
        return
    if not pitcher_changed:
        return
    score_fn = batter_over_score if direction == "over" else batter_under_score
    confirmed_penalty = 0 if ctx.get("lineup_confirmed") else 0.5
    score, reasons = score_fn(ctx)
    pick["score"] = score - confirmed_penalty
    pick["reasons"] = reasons
    pick["opp_pitcher_id"] = ctx["opp_pitcher_id"]
    strict = ctx.get("best_over") if direction == "over" else ctx.get("best_under")
    resolved = _pick_category(strict, ctx.get("prop_categories"), role, direction, exclude_label)
    if resolved:
        pick["best_category"] = resolved


def build_report(conn, days_ahead=2):
    today = mlb_today()
    end = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
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
    frozen = _load_frozen_top_picks(today)
    if frozen:
        top_overs, top_unders = frozen
        player_context = _player_context(todays_games)
        over_picks = (top_overs.get("batters") or []) + (top_overs.get("pitchers") or [])
        under_picks = (top_unders.get("batters") or []) + (top_unders.get("pitchers") or [])
        for pick in over_picks:
            _refresh_frozen_pick(pick, "over", player_context)
        # exclude_label: never let a refreshed under-pick land on the exact
        # same category+line this same player's over-pick already has (or
        # vice versa) -- same reasoning as build_top_picks()'s own
        # exclude_label.
        over_category_by_player = {p["player_id"]: p["best_category"]["label"] for p in over_picks if p.get("best_category")}
        for pick in under_picks:
            _refresh_frozen_pick(pick, "under", player_context, exclude_label=over_category_by_player.get(pick["player_id"]))
        _regrade_picks(conn, top_overs, "over")
        _regrade_picks(conn, top_unders, "under")
    else:
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
    today = mlb_today()

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
    # hourly job still writes it -- but only the FIRST time that day AT OR
    # AFTER TOP_PICKS_FREEZE_HOUR_ET (never overwritten again after that),
    # not simply the first run of the day. Freezing at the very first
    # hourly run (as early as 00:xx UTC) meant every Top Overs/Unders pick
    # started life as PROJECTED regardless of role, since real lineups
    # don't typically post until 1-3 hours before first pitch -- hours
    # later. Waiting until most evening games' lineups have had a chance
    # to post (without waiting so long that the archive misses being a
    # genuine pre-game snapshot for most of the day's games, which mostly
    # start 23:00-02:00 UTC) means the frozen picks reflect real, known
    # lineup status from the start instead of a stale guess that (before
    # this fix) never updated again for the rest of the day. Before this
    # hour, top_overs/top_unders in build_report() are computed fresh on
    # every build, same as if no archive existed yet at all. That matters
    # for grade_picks.py too: a snapshot written too late in the day could
    # already have some of that day's own game results folded into a
    # batter's rolling stats, which would make "here's what was predicted"
    # quietly include a bit of "here's what already happened" -- a real
    # look-ahead leak for grading purposes (this window keeps it well
    # before any of today's games actually start).
    if write_archive:
        archive_path = os.path.join(OUT_DIR, f"props_{today}.json")
        if not os.path.exists(archive_path) and datetime.now(MLB_TZ).hour >= TOP_PICKS_FREEZE_HOUR_ET:
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

    # Local import: sync_bets_firestore.py imports FROM this module (it
    # reuses pick_result()/batter_game_result()/etc. directly so a bet leg
    # can never disagree with what the live dashboard says about the same
    # player+category+line) -- importing it at module level here would be
    # circular. Regrading on every run (not just when a new bet is logged
    # via the site's own sign-in form) is what lets a bet settle
    # automatically as its game finishes, the same reasoning as
    # grade_picks.py re-grading recent days on every hourly run. A no-op
    # if FIREBASE_SERVICE_ACCOUNT isn't configured for this environment --
    # the bet tracker is optional, not a hard pipeline dependency.
    # my-bets.html itself is NOT regenerated here -- everything on that
    # page (sign-in, the bet list, P/L) is driven live by Firestore, so
    # render_my_bets.run() only needs to be called when its own code
    # changes, not every cycle.
    import sync_bets_firestore

    bet_conn = get_conn()
    sync_bets_firestore.regrade_all_pending(bet_conn)
    bet_conn.close()

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
