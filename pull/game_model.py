"""
game_model.py

Team-level run-scoring model for game simulation. Deliberately simple and
honest about what this data can support: no bullpen usage, no Statcast, no
play-by-play. Method is the standard sabermetric log5-style blend:

    team_exp_runs = league_avg * (team_offense_idx) * (opp_defense_idx)

where offense/defense indices come from each team's own season-to-date
runs scored/allowed per game (from `games.home_score`/`away_score`, backfilled
by sync_results.py) -- NOT from reconstructing lineups player-by-player,
which would compound uncertainty (playing time, confirmed-lineup gaps) for
no real gain over the team's own actual scoring record.

The probable starter is folded in as a partial adjustment to the opposing
defense index (starters pitch ~55-60% of a game), blended with the team's
actual bullpen quality (relief-only appearances, `games_started = 0`) --
not the team's blended overall rate, since that conflates starters and
relievers. Bullpen quality is further nudged by a recent-workload fatigue
signal (relievers who've thrown heavily the last 2 days are less sharp/
available) -- still no per-pitcher role or rest-day data, just an honest
team-level workload proxy from the same game logs already being synced.

Run distribution: Negative Binomial (via Gamma-Poisson mixture), not plain
Poisson -- real MLB team runs/game are overdispersed (variance > mean), and
the overdispersion parameter is fit from this season's actual results
rather than assumed, so it's at least internally honest even though the
model overall is coarse.

Every rate function takes an `as_of_date` (YYYY-MM-DD). Pass None for the
live/forward case (today's real games, uses all data available now); pass
a past date for backtest.py to reconstruct what was knowable *before* that
date, so a backtest never leaks the game's own outcome (or later games)
into its own projection.
"""

import math
import random
from datetime import date, datetime, timedelta, timezone

CURRENT_SEASON = datetime.now(timezone.utc).year

STARTER_WEIGHT = 0.6  # fraction of "defense" attributed to the probable starter vs. team-wide rate
PARK_MULTIPLIER = {"hitter": 1.05, "neutral": 1.0, "pitcher": 0.95}
FATIGUE_ADJUSTMENT_CAP = 0.15  # max +/-15% swing to bullpen rate from recent workload


def _date_filter(column, as_of_date):
    """Returns (sql_fragment, params) -- empty fragment/params when as_of_date is None (live mode)."""
    if as_of_date is None:
        return "", ()
    return f" AND {column} < ?", (as_of_date,)


def league_run_distribution(conn, as_of_date=None):
    frag, params = _date_filter("official_date", as_of_date)
    scores = []
    for r in conn.execute(f"SELECT home_score, away_score FROM games WHERE home_score IS NOT NULL{frag}", params):
        scores.append(r["home_score"])
        scores.append(r["away_score"])
    if len(scores) < 20:
        return None  # not enough completed games yet to fit a distribution
    mean = sum(scores) / len(scores)
    var = sum((s - mean) ** 2 for s in scores) / len(scores)
    dispersion_r = mean * mean / (var - mean) if var > mean else 50.0
    return {"league_avg": mean, "variance": var, "dispersion_r": dispersion_r}


def _run_rate(pairs):
    """pairs: list of (row, scored_key, allowed_key)."""
    n = len(pairs)
    if n == 0:
        return None
    scored = sum(r[sk] for r, sk, ak in pairs)
    allowed = sum(r[ak] for r, sk, ak in pairs)
    return {"games": n, "runs_scored_avg": scored / n, "runs_allowed_avg": allowed / n}


def team_season_run_rates(conn, team_id, as_of_date=None, context=None, min_context_games=10):
    """
    context=None: blended home+away rate (used as fallback).
    context="home"/"away": rate from that team's games in that context only --
    real MLB home-field advantage (~54% win rate league-wide, not modeled
    anywhere else) lives entirely in this split; blending it away was the
    single biggest gap in the first version of this model. Falls back to
    the blended rate if there aren't yet `min_context_games` in that context
    (early season / a new team).
    """
    frag, params = _date_filter("official_date", as_of_date)
    home_rows = conn.execute(
        f"SELECT home_score, away_score FROM games WHERE home_team_id = ? AND home_score IS NOT NULL{frag}",
        (team_id, *params),
    ).fetchall()
    away_rows = conn.execute(
        f"SELECT home_score, away_score FROM games WHERE away_team_id = ? AND home_score IS NOT NULL{frag}",
        (team_id, *params),
    ).fetchall()

    blended = _run_rate(
        [(r, "home_score", "away_score") for r in home_rows] + [(r, "away_score", "home_score") for r in away_rows]
    )
    if blended is None or context is None:
        return blended

    if context == "home":
        specific = _run_rate([(r, "home_score", "away_score") for r in home_rows])
    else:
        specific = _run_rate([(r, "away_score", "home_score") for r in away_rows])

    if specific is None or specific["games"] < min_context_games:
        return blended
    return specific


def starter_run_rate(conn, pitcher_id, as_of_date=None):
    """
    This-season ERA-as-runs-per-9 for the probable starter, used as a
    per-game run-rate proxy. Filters season = CURRENT_SEASON explicitly --
    game logs never use the CAREER_SEASON(0) sentinel (that's only a
    splits-table convention), so a prior `season != CAREER_SEASON` filter
    here was a no-op that silently blended in the pitcher's entire career
    back to their debut instead of just this year.
    """
    if not pitcher_id:
        return None
    frag, params = _date_filter("date", as_of_date)
    row = conn.execute(
        f"SELECT SUM(outs) as outs, SUM(earned_runs) as er FROM pitching_game_logs "
        f"WHERE player_id = ? AND season = ?{frag}",
        (pitcher_id, CURRENT_SEASON, *params),
    ).fetchone()
    if not row or not row["outs"]:
        return None
    innings = row["outs"] / 3
    if innings < 10:  # too small a sample to trust over the team rate
        return None
    return row["er"] * 9 / innings


def team_bullpen_rate(conn, team_id, as_of_date=None):
    """
    Season ERA-as-runs-per-9 across this team's *relief* appearances only
    (games_started = 0) -- an actual bullpen-quality signal from data this
    project already collects, rather than folding relievers into the team's
    blended runs-allowed average the way the starter-only version did.
    """
    frag, params = _date_filter("date", as_of_date)
    row = conn.execute(
        f"SELECT SUM(outs) as outs, SUM(earned_runs) as er FROM pitching_game_logs "
        f"WHERE team_id = ? AND games_started = 0 AND season = ?{frag}",
        (team_id, CURRENT_SEASON, *params),
    ).fetchone()
    if not row or not row["outs"]:
        return None
    innings = row["outs"] / 3
    if innings < 20:
        return None
    return row["er"] * 9 / innings


def team_bullpen_fatigue(conn, team_id, as_of_date=None, recent_days=2):
    """
    Relief innings thrown in the `recent_days` before as_of_date (or before
    now, in live mode) vs. this team's own season-average relief innings/day
    up to that same point. >1 means the pen was worked harder than its own
    normal lately (fatigued/less available); <1 means comparatively fresh.
    """
    reference = datetime.strptime(as_of_date, "%Y-%m-%d") if as_of_date else datetime.now(timezone.utc)
    recent_cutoff = (reference - timedelta(days=recent_days)).strftime("%Y-%m-%d")
    upper_frag, upper_params = _date_filter("date", as_of_date)

    recent_row = conn.execute(
        f"SELECT SUM(outs) as outs FROM pitching_game_logs "
        f"WHERE team_id = ? AND games_started = 0 AND date >= ? AND season = ?{upper_frag}",
        (team_id, recent_cutoff, CURRENT_SEASON, *upper_params),
    ).fetchone()
    recent_innings = (recent_row["outs"] or 0) / 3

    season_row = conn.execute(
        f"SELECT SUM(outs) as outs, MIN(date) as first_date, MAX(date) as last_date FROM pitching_game_logs "
        f"WHERE team_id = ? AND games_started = 0 AND season = ?{upper_frag}",
        (team_id, CURRENT_SEASON, *upper_params),
    ).fetchone()
    if not season_row or not season_row["outs"] or not season_row["first_date"]:
        return None

    days_elapsed = max((date.fromisoformat(season_row["last_date"]) - date.fromisoformat(season_row["first_date"])).days, 1)
    season_daily_avg = (season_row["outs"] / 3) / days_elapsed
    if season_daily_avg <= 0:
        return None

    return {
        "recent_innings": round(recent_innings, 1),
        "season_daily_avg": round(season_daily_avg, 2),
        "fatigue_ratio": round(recent_innings / (season_daily_avg * recent_days), 2),
    }


def fatigue_adjusted_rate(bullpen_rate, fatigue):
    if bullpen_rate is None or not fatigue:
        return bullpen_rate
    delta = fatigue["fatigue_ratio"] - 1.0
    adjustment = max(-FATIGUE_ADJUSTMENT_CAP, min(FATIGUE_ADJUSTMENT_CAP, delta * 0.3))
    return bullpen_rate * (1 + adjustment)


def combined_defense_index(starter_rate, bullpen_rate, team_def_idx_fallback, avg):
    if starter_rate is not None and bullpen_rate is not None:
        return STARTER_WEIGHT * (starter_rate / avg) + (1 - STARTER_WEIGHT) * (bullpen_rate / avg)
    if starter_rate is not None:
        return STARTER_WEIGHT * (starter_rate / avg) + (1 - STARTER_WEIGHT) * team_def_idx_fallback
    return team_def_idx_fallback


def project_matchup(conn, home_team_id, away_team_id, home_pitcher_id, away_pitcher_id, park_tier, as_of_date=None):
    league = league_run_distribution(conn, as_of_date)
    if league is None:
        return None
    avg = league["league_avg"]

    home_rates = team_season_run_rates(conn, home_team_id, as_of_date, context="home")
    away_rates = team_season_run_rates(conn, away_team_id, as_of_date, context="away")
    if not home_rates or not away_rates:
        return None

    home_off_idx = home_rates["runs_scored_avg"] / avg
    away_off_idx = away_rates["runs_scored_avg"] / avg
    home_def_idx_fallback = home_rates["runs_allowed_avg"] / avg
    away_def_idx_fallback = away_rates["runs_allowed_avg"] / avg

    away_starter = starter_run_rate(conn, away_pitcher_id, as_of_date)
    home_starter = starter_run_rate(conn, home_pitcher_id, as_of_date)
    away_bullpen_fatigue = team_bullpen_fatigue(conn, away_team_id, as_of_date)
    home_bullpen_fatigue = team_bullpen_fatigue(conn, home_team_id, as_of_date)
    away_bullpen = fatigue_adjusted_rate(team_bullpen_rate(conn, away_team_id, as_of_date), away_bullpen_fatigue)
    home_bullpen = fatigue_adjusted_rate(team_bullpen_rate(conn, home_team_id, as_of_date), home_bullpen_fatigue)

    away_def_idx = combined_defense_index(away_starter, away_bullpen, away_def_idx_fallback, avg)
    home_def_idx = combined_defense_index(home_starter, home_bullpen, home_def_idx_fallback, avg)

    park_mult = PARK_MULTIPLIER.get(park_tier, 1.0)

    home_exp_runs = avg * home_off_idx * away_def_idx * park_mult
    away_exp_runs = avg * away_off_idx * home_def_idx * park_mult

    return {
        "home_exp_runs": round(home_exp_runs, 2),
        "away_exp_runs": round(away_exp_runs, 2),
        "dispersion_r": league["dispersion_r"],
        "league_avg": avg,
        "home_bullpen_fatigue": home_bullpen_fatigue,
        "away_bullpen_fatigue": away_bullpen_fatigue,
    }


def _sample_poisson(lam, rng):
    """Knuth's algorithm -- fine for the small lambdas (single-digit runs) this model produces."""
    if lam <= 0:
        return 0
    L = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


def _sample_neg_binomial(mean, dispersion_r, rng):
    """Gamma-Poisson mixture: NB2 parameterization, Var = mean + mean^2/dispersion_r."""
    if mean <= 0:
        return 0
    gamma_sample = rng.gammavariate(dispersion_r, mean / dispersion_r)
    return _sample_poisson(gamma_sample, rng)


def simulate(projection, n_trials=20000, spread_line=1.5, total_line=None, seed=None):
    rng = random.Random(seed)
    home_exp, away_exp, r = projection["home_exp_runs"], projection["away_exp_runs"], projection["dispersion_r"]
    total_line = total_line if total_line is not None else round(home_exp + away_exp)

    home_wins = 0.0
    cover = 0
    over = 0
    for _ in range(n_trials):
        h = _sample_neg_binomial(home_exp, r, rng)
        a = _sample_neg_binomial(away_exp, r, rng)
        if h > a:
            home_wins += 1
        elif h == a:
            home_wins += 0.5  # MLB games don't end in ties; extra-inning edge treated as a coin flip
        if (h - a) > spread_line:
            cover += 1
        if (h + a) > total_line:
            over += 1

    return {
        "home_win_prob": round(home_wins / n_trials, 3),
        "spread_line": spread_line,
        "spread_cover_prob": round(cover / n_trials, 3),
        "total_line": total_line,
        "over_prob": round(over / n_trials, 3),
    }
