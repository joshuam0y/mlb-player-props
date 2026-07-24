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
overall run-prevention rate as an explicit stand-in for the bullpen quality
this project has no data on.

Run distribution: Negative Binomial (via Gamma-Poisson mixture), not plain
Poisson -- real MLB team runs/game are overdispersed (variance > mean), and
the overdispersion parameter is fit from this season's actual results
rather than assumed, so it's at least internally honest even though the
model overall is coarse.
"""

import math
import random

from db import CAREER_SEASON

STARTER_WEIGHT = 0.6  # fraction of "defense" attributed to the probable starter vs. team-wide rate
PARK_MULTIPLIER = {"hitter": 1.05, "neutral": 1.0, "pitcher": 0.95}


def league_run_distribution(conn):
    scores = []
    for r in conn.execute("SELECT home_score, away_score FROM games WHERE home_score IS NOT NULL"):
        scores.append(r["home_score"])
        scores.append(r["away_score"])
    if len(scores) < 20:
        return None  # not enough completed games yet to fit a distribution
    mean = sum(scores) / len(scores)
    var = sum((s - mean) ** 2 for s in scores) / len(scores)
    dispersion_r = mean * mean / (var - mean) if var > mean else 50.0
    return {"league_avg": mean, "variance": var, "dispersion_r": dispersion_r}


def team_season_run_rates(conn, team_id):
    home_rows = conn.execute(
        "SELECT home_score, away_score FROM games WHERE home_team_id = ? AND home_score IS NOT NULL", (team_id,)
    ).fetchall()
    away_rows = conn.execute(
        "SELECT home_score, away_score FROM games WHERE away_team_id = ? AND home_score IS NOT NULL", (team_id,)
    ).fetchall()
    games = len(home_rows) + len(away_rows)
    if games == 0:
        return None
    scored = sum(r["home_score"] for r in home_rows) + sum(r["away_score"] for r in away_rows)
    allowed = sum(r["away_score"] for r in home_rows) + sum(r["home_score"] for r in away_rows)
    return {"games": games, "runs_scored_avg": scored / games, "runs_allowed_avg": allowed / games}


def starter_run_rate(conn, pitcher_id):
    """Season ERA-as-runs-per-9 for the probable starter, used as a per-game run-rate proxy."""
    if not pitcher_id:
        return None
    row = conn.execute(
        "SELECT SUM(outs) as outs, SUM(earned_runs) as er FROM pitching_game_logs WHERE player_id = ? AND season != ?",
        (pitcher_id, CAREER_SEASON),
    ).fetchone()
    if not row or not row["outs"]:
        return None
    innings = row["outs"] / 3
    if innings < 10:  # too small a sample to trust over the team rate
        return None
    return row["er"] * 9 / innings


def project_matchup(conn, home_team_id, away_team_id, home_pitcher_id, away_pitcher_id, park_tier):
    league = league_run_distribution(conn)
    if league is None:
        return None
    avg = league["league_avg"]

    home_rates = team_season_run_rates(conn, home_team_id)
    away_rates = team_season_run_rates(conn, away_team_id)
    if not home_rates or not away_rates:
        return None

    home_off_idx = home_rates["runs_scored_avg"] / avg
    away_off_idx = away_rates["runs_scored_avg"] / avg
    home_def_idx = home_rates["runs_allowed_avg"] / avg
    away_def_idx = away_rates["runs_allowed_avg"] / avg

    away_starter = starter_run_rate(conn, away_pitcher_id)
    home_starter = starter_run_rate(conn, home_pitcher_id)
    if away_starter is not None:
        away_def_idx = STARTER_WEIGHT * (away_starter / avg) + (1 - STARTER_WEIGHT) * away_def_idx
    if home_starter is not None:
        home_def_idx = STARTER_WEIGHT * (home_starter / avg) + (1 - STARTER_WEIGHT) * home_def_idx

    park_mult = PARK_MULTIPLIER.get(park_tier, 1.0)

    home_exp_runs = avg * home_off_idx * away_def_idx * park_mult
    away_exp_runs = avg * away_off_idx * home_def_idx * park_mult

    return {
        "home_exp_runs": round(home_exp_runs, 2),
        "away_exp_runs": round(away_exp_runs, 2),
        "dispersion_r": league["dispersion_r"],
        "league_avg": avg,
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
