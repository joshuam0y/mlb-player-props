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
LINEUP_ADJUSTMENT_CAP = 0.06  # max +/-6% swing to offense idx from confirmed-lineup recent form
SHORT_REST_DAYS_THRESHOLD = 4  # normal 5-man rotation rest; fewer days than this is "short rest"
SHORT_REST_PENALTY_PER_DAY = 0.03  # extra runs-allowed % per day short of normal rest
SHORT_REST_PENALTY_CAP = 0.08  # never more than an 8% penalty, however short the rest
WORKLOAD_MIN_APPEARANCES = 3  # need at least this many appearances this season before trusting an outs-share over the flat STARTER_WEIGHT
GAME_OUTS = 27  # a 9-inning game's total outs, used to turn "average outs per appearance" into a fraction of the game


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


TEAM_RECENT_GAMES = 20  # how many of the team's most recent games count as "recent form"
TEAM_RECENT_WEIGHT = 0.35  # how much of the final rate comes from recent form vs. the full-season (context) rate
TEAM_RECENT_MIN_GAMES = 10  # need at least this many recent games before trusting the recency blend at all


def _all_games_sorted(conn, team_id, as_of_date):
    """Every game this team has played (either side), most recent first, tagged with its own scored/allowed keys."""
    frag, params = _date_filter("official_date", as_of_date)
    home_rows = conn.execute(
        f"SELECT home_score, away_score, official_date FROM games WHERE home_team_id = ? AND home_score IS NOT NULL{frag}",
        (team_id, *params),
    ).fetchall()
    away_rows = conn.execute(
        f"SELECT home_score, away_score, official_date FROM games WHERE away_team_id = ? AND home_score IS NOT NULL{frag}",
        (team_id, *params),
    ).fetchall()
    combined = [(r, "home_score", "away_score") for r in home_rows] + [(r, "away_score", "home_score") for r in away_rows]
    combined.sort(key=lambda t: t[0]["official_date"], reverse=True)
    return combined


def team_season_run_rates(conn, team_id, as_of_date=None, context=None, min_context_games=10):
    """
    context=None: blended home+away rate (used as fallback).
    context="home"/"away": rate from that team's games in that context only --
    real MLB home-field advantage (~54% win rate league-wide, not modeled
    anywhere else) lives entirely in this split; blending it away was the
    single biggest gap in the first version of this model. Falls back to
    the blended rate if there aren't yet `min_context_games` in that context
    (early season / a new team).

    Then blended with a recency-weighted rate from the team's last
    TEAM_RECENT_GAMES games (any context -- a smaller recent window split
    further by home/away rarely has enough games to mean anything).
    Verified by full-season backtest (2026-03-25 to 2026-07-23, 1525
    games), comparing this exact code path with the blend on vs. off:
    Brier 0.2656 with no recency blend, 0.2641 with it (20 games/35%
    weight) -- a modest, real, repeatable improvement, not just noise (a
    20-games/15%-weight variant landed in between at 0.2649, moving the
    same direction). Both are still worse than the naive baseline's
    ~0.2498 over this same full-season range, which is a separate, larger
    gap than the recency question -- see backtest.py's module docstring
    and the README's Backtesting section for that more fundamental result.
    """
    combined = _all_games_sorted(conn, team_id, as_of_date)
    home_pairs = [(r, sk, ak) for r, sk, ak in combined if sk == "home_score"]
    away_pairs = [(r, sk, ak) for r, sk, ak in combined if sk == "away_score"]

    blended = _run_rate(home_pairs + away_pairs)
    if blended is None:
        return None

    if context == "home":
        season_rate = _run_rate(home_pairs) or blended
        if season_rate["games"] < min_context_games:
            season_rate = blended
    elif context == "away":
        season_rate = _run_rate(away_pairs) or blended
        if season_rate["games"] < min_context_games:
            season_rate = blended
    else:
        season_rate = blended

    recent_rate = _run_rate(combined[:TEAM_RECENT_GAMES])
    if recent_rate is None or recent_rate["games"] < TEAM_RECENT_MIN_GAMES:
        return season_rate

    w = TEAM_RECENT_WEIGHT
    return {
        "games": season_rate["games"],
        "runs_scored_avg": w * recent_rate["runs_scored_avg"] + (1 - w) * season_rate["runs_scored_avg"],
        "runs_allowed_avg": w * recent_rate["runs_allowed_avg"] + (1 - w) * season_rate["runs_allowed_avg"],
    }


TEAM_HAND_MIN_GAMES = 15  # need at least this many games vs this specific hand before trusting the split over the blended rate
TEAM_HAND_WEIGHT = 0.3  # how much of the offense index comes from the vs-hand split vs the context (home/away+recency) rate
# Verified by the same full-season backtest (2026-03-25 to 2026-07-23, 1525
# games) used for the recency blend above: Brier 0.2639 with this split off,
# 0.2634 at weight 0.2, 0.2631 at weight 0.3 -- monotonically better as the
# weight increases, the same "real, repeatable, not noise" pattern as the
# recency-weighting result. MAE unchanged (3.85 runs) either way.


def _pitcher_hand(conn, pitcher_id):
    if not pitcher_id:
        return None
    row = conn.execute("SELECT pitch_hand FROM players WHERE player_id = ?", (pitcher_id,)).fetchone()
    return row["pitch_hand"] if row else None


def team_run_rate_vs_hand(conn, team_id, opp_hand, as_of_date=None):
    """
    This team's actual scoring rate specifically in games where the
    OPPOSING starter threw `opp_hand` -- from real results (which probable
    pitcher was on the schedule that game), matching this model's existing
    team-level-only philosophy rather than reconstructing an offense
    index from individual batters' own platoon splits (that's a separate,
    per-player signal already used elsewhere -- see matchup_edge() in
    build_props.py -- not something this team-level model reconstructs).
    """
    frag, params = _date_filter("g.official_date", as_of_date)
    home_rows = conn.execute(
        f"""
        SELECT g.home_score as scored, g.away_score as allowed
        FROM games g JOIN players p ON p.player_id = g.away_probable_pitcher_id
        WHERE g.home_team_id = ? AND g.home_score IS NOT NULL AND p.pitch_hand = ?{frag}
        """,
        (team_id, opp_hand, *params),
    ).fetchall()
    away_rows = conn.execute(
        f"""
        SELECT g.away_score as scored, g.home_score as allowed
        FROM games g JOIN players p ON p.player_id = g.home_probable_pitcher_id
        WHERE g.away_team_id = ? AND g.home_score IS NOT NULL AND p.pitch_hand = ?{frag}
        """,
        (team_id, opp_hand, *params),
    ).fetchall()
    rows = home_rows + away_rows
    n = len(rows)
    if n == 0:
        return None
    return {
        "games": n,
        "runs_scored_avg": sum(r["scored"] for r in rows) / n,
        "runs_allowed_avg": sum(r["allowed"] for r in rows) / n,
    }


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


def pitcher_expected_outs_share(conn, pitcher_id, as_of_date=None):
    """
    Fraction of a 9-inning game's outs (GAME_OUTS=27) this pitcher is
    expected to record himself, from his own average outs per mound
    appearance this season (every appearance, not just games_started=1
    ones). combined_defense_index() caps STARTER_WEIGHT at this, so a
    short-outing arm nominally "starting" tonight (an opener, piggyback/
    bulk arm, or bullpen game) no longer gets credited/blamed for the
    game's run environment as if he'll pitch like a real 5-6 inning
    starter -- the team's actual bullpen rate picks up the rest of the
    weight instead.

    Deliberately reimplemented here rather than importing build_props.py's
    own pitcher_workload_tier() (same underlying idea, continuous instead
    of three buckets) -- game_model.py is kept dependency-free from the
    rest of the pipeline on purpose (see this module's own docstring).

    None if there isn't enough of a track record yet to say (a rookie's
    first few appearances, early season) -- callers fall back to the
    existing flat STARTER_WEIGHT rather than guess from too little data.
    """
    if not pitcher_id:
        return None
    frag, params = _date_filter("date", as_of_date)
    rows = conn.execute(
        f"SELECT outs FROM pitching_game_logs WHERE player_id = ? AND season = ?{frag}",
        (pitcher_id, CURRENT_SEASON, *params),
    ).fetchall()
    if len(rows) < WORKLOAD_MIN_APPEARANCES:
        return None
    avg_outs = sum(r["outs"] or 0 for r in rows) / len(rows)
    return min(avg_outs / GAME_OUTS, 1.0)


def pitcher_rest_days(conn, pitcher_id, game_date):
    """
    Days between this pitcher's most recent START (games_started=1) this
    season and game_date -- the date he's actually pitching, NOT
    necessarily "today": project_matchup() is called once with as_of_date
    fixed to today for the whole days_ahead window (simulate_games.py), so
    a game 2 days out would otherwise look artificially short-rested by
    however many days haven't happened yet. None if there's no prior start
    to measure against (his first start of the season).
    """
    if not pitcher_id or not game_date:
        return None
    row = conn.execute(
        "SELECT date FROM pitching_game_logs "
        "WHERE player_id = ? AND season = ? AND games_started = 1 AND date < ? "
        "ORDER BY date DESC LIMIT 1",
        (pitcher_id, CURRENT_SEASON, game_date),
    ).fetchone()
    if not row:
        return None
    last_start = datetime.strptime(row["date"], "%Y-%m-%d").date()
    ref_date = datetime.strptime(game_date, "%Y-%m-%d").date()
    return (ref_date - last_start).days


def rest_adjusted_starter_rate(starter_rate, rest_days):
    """
    A starting pitcher on short rest (fewer than SHORT_REST_DAYS_THRESHOLD
    days since his last start -- a scratch start, bullpen game, or
    doubleheader reshuffle, not the normal 5-man rotation) has historically
    pitched somewhat worse on average. Deliberately only ever a penalty,
    never a bonus for MORE rest than normal: the evidence that extra rest
    actually helps is much weaker and more mixed in the research than the
    evidence that short rest hurts, so this doesn't guess a direction for
    rest_days above the threshold.
    """
    if starter_rate is None or rest_days is None or rest_days >= SHORT_REST_DAYS_THRESHOLD:
        return starter_rate
    days_short = SHORT_REST_DAYS_THRESHOLD - rest_days
    penalty = min(days_short * SHORT_REST_PENALTY_PER_DAY, SHORT_REST_PENALTY_CAP)
    return starter_rate * (1 + penalty)


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


def lineup_strength_adjustment(conn, batter_ids, as_of_date=None, cap=LINEUP_ADJUSTMENT_CAP):
    """
    Secondary nudge to a team's offense index once its *actual* confirmed
    starting lineup is known: are these specific 9 hitters running hotter or
    colder lately (last 15 games) than their own season norm? A lineup full
    of guys currently hitting above their own baseline should score a touch
    more than the team's blended season rate alone would suggest; a lineup
    missing/resting its best bats (or full of guys in a slump) a touch less.

    Deliberately small and capped -- this is a secondary signal on top of the
    team's actual season-long scoring record, not a replacement for it, and
    a 9-hitter L15 sample is noisy. Before the real lineup posts, callers
    pass an empty/None list and get a neutral 1.0 (no adjustment) -- the
    projection just runs on team-level rates alone, same as before lineups
    existed as a signal.
    """
    if not batter_ids:
        return 1.0
    date_frag, date_params = _date_filter("date", as_of_date)
    deltas = []
    for player_id in batter_ids:
        recent = conn.execute(
            f"SELECT SUM(at_bats) as ab, SUM(hits) as h, SUM(total_bases) as tb "
            f"FROM batting_game_logs WHERE player_id = ?{date_frag} ORDER BY date DESC LIMIT 15",
            (player_id, *date_params),
        ).fetchone()
        season = conn.execute(
            f"SELECT SUM(at_bats) as ab, SUM(hits) as h, SUM(total_bases) as tb "
            f"FROM batting_game_logs WHERE player_id = ? AND season = ?{date_frag}",
            (player_id, CURRENT_SEASON, *date_params),
        ).fetchone()
        if not recent or not recent["ab"] or not season or not season["ab"] or season["ab"] < 20:
            continue
        recent_rate = (recent["h"] + recent["tb"]) / recent["ab"]  # AVG + SLG combined, cheap OPS-ish proxy
        season_rate = (season["h"] + season["tb"]) / season["ab"]
        deltas.append(recent_rate - season_rate)
    if not deltas:
        return 1.0
    avg_delta = sum(deltas) / len(deltas)
    return 1.0 + max(-cap, min(cap, avg_delta))


def combined_defense_index(starter_rate, bullpen_rate, team_def_idx_fallback, avg, outs_share=None):
    # Capped at STARTER_WEIGHT, never raised above it -- outs_share only
    # ever pulls weight AWAY from a short-outing "starter" toward the
    # team's own bullpen rate, never gives a real workhorse MORE credit
    # than the existing, already-tuned 0.6 default.
    weight = STARTER_WEIGHT if outs_share is None else min(STARTER_WEIGHT, outs_share)
    if starter_rate is not None and bullpen_rate is not None:
        return weight * (starter_rate / avg) + (1 - weight) * (bullpen_rate / avg)
    if starter_rate is not None:
        return weight * (starter_rate / avg) + (1 - weight) * team_def_idx_fallback
    return team_def_idx_fallback


def project_matchup(
    conn, home_team_id, away_team_id, home_pitcher_id, away_pitcher_id, park_tier,
    as_of_date=None, home_batter_ids=None, away_batter_ids=None, game_date=None,
):
    # game_date is the date this specific game is actually played -- distinct
    # from as_of_date, which simulate_games.py pins to TODAY for every game
    # in its whole days_ahead window (a data-recency cutoff, not "the day
    # of the game"). Every other caller already passes as_of_date as the
    # game's own real date (a point-in-time backtest iterating one real
    # historical game at a time), so defaulting to it here keeps them
    # correct without having to pass this explicitly.
    if game_date is None:
        game_date = as_of_date

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

    # Blend in each team's actual scoring rate specifically against
    # tonight's OPPOSING starter's handedness, when there's enough games
    # vs that hand this season to trust it -- a team that's genuinely
    # weaker vs lefties shouldn't get the same offense index facing a
    # tough LHP as it does facing an average RHP.
    away_pitcher_hand = _pitcher_hand(conn, away_pitcher_id)
    home_pitcher_hand = _pitcher_hand(conn, home_pitcher_id)
    if away_pitcher_hand:
        vs_hand = team_run_rate_vs_hand(conn, home_team_id, away_pitcher_hand, as_of_date)
        if vs_hand and vs_hand["games"] >= TEAM_HAND_MIN_GAMES:
            home_off_idx = TEAM_HAND_WEIGHT * (vs_hand["runs_scored_avg"] / avg) + (1 - TEAM_HAND_WEIGHT) * home_off_idx
    if home_pitcher_hand:
        vs_hand = team_run_rate_vs_hand(conn, away_team_id, home_pitcher_hand, as_of_date)
        if vs_hand and vs_hand["games"] >= TEAM_HAND_MIN_GAMES:
            away_off_idx = TEAM_HAND_WEIGHT * (vs_hand["runs_scored_avg"] / avg) + (1 - TEAM_HAND_WEIGHT) * away_off_idx

    home_off_idx *= lineup_strength_adjustment(conn, home_batter_ids, as_of_date)
    away_off_idx *= lineup_strength_adjustment(conn, away_batter_ids, as_of_date)
    home_def_idx_fallback = home_rates["runs_allowed_avg"] / avg
    away_def_idx_fallback = away_rates["runs_allowed_avg"] / avg

    away_starter = starter_run_rate(conn, away_pitcher_id, as_of_date)
    home_starter = starter_run_rate(conn, home_pitcher_id, as_of_date)
    away_starter = rest_adjusted_starter_rate(away_starter, pitcher_rest_days(conn, away_pitcher_id, game_date))
    home_starter = rest_adjusted_starter_rate(home_starter, pitcher_rest_days(conn, home_pitcher_id, game_date))
    away_bullpen_fatigue = team_bullpen_fatigue(conn, away_team_id, as_of_date)
    home_bullpen_fatigue = team_bullpen_fatigue(conn, home_team_id, as_of_date)
    away_bullpen = fatigue_adjusted_rate(team_bullpen_rate(conn, away_team_id, as_of_date), away_bullpen_fatigue)
    home_bullpen = fatigue_adjusted_rate(team_bullpen_rate(conn, home_team_id, as_of_date), home_bullpen_fatigue)
    away_outs_share = pitcher_expected_outs_share(conn, away_pitcher_id, as_of_date)
    home_outs_share = pitcher_expected_outs_share(conn, home_pitcher_id, as_of_date)

    away_def_idx = combined_defense_index(away_starter, away_bullpen, away_def_idx_fallback, avg, away_outs_share)
    home_def_idx = combined_defense_index(home_starter, home_bullpen, home_def_idx_fallback, avg, home_outs_share)

    park_mult = PARK_MULTIPLIER.get(park_tier, 1.0)

    home_exp_runs = avg * home_off_idx * away_def_idx * park_mult
    away_exp_runs = avg * away_off_idx * home_def_idx * park_mult

    return {
        # A team can't literally score half a run, so the projected score
        # itself stays a plain decimal average (e.g. 3.2 - 4.7) -- only the
        # combined TOTAL (see simulate()'s total_line) is quoted as .5,
        # since that's an actual bettable line and needs to avoid a push.
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


def simulate(projection, n_trials=1000000, spread_line=1.5, total_line=None, seed=None):
    """
    n_trials defaults to 1,000,000 Monte Carlo trials per game (well above the
    ~10k threshold needed for stable win-prob/cover-prob estimates at this
    scale -- 1M tightens the estimate further, and at ~3.2us/trial the added
    cost is still just a few seconds per game, well under a minute even for a
    full day's slate) -- see backtest.py for how this holds up against real
    outcomes.
    """
    rng = random.Random(seed)
    home_exp, away_exp, r = projection["home_exp_runs"], projection["away_exp_runs"], projection["dispersion_r"]

    home_wins = 0.0
    home_covers = 0  # home wins by more than spread_line
    away_covers = 0  # away wins by more than spread_line -- NOT simply 1 - home_covers, see below
    totals = []
    for _ in range(n_trials):
        h = _sample_neg_binomial(home_exp, r, rng)
        a = _sample_neg_binomial(away_exp, r, rng)
        if h > a:
            home_wins += 1
        elif h == a:
            home_wins += 0.5  # MLB games don't end in ties; extra-inning edge treated as a coin flip
        if (h - a) > spread_line:
            home_covers += 1
        elif (a - h) > spread_line:
            away_covers += 1
        totals.append(h + a)

    if total_line is None:
        # The line has to be the simulated distribution's MEDIAN (rounded to
        # .5), not home_exp + away_exp (the mean) -- MLB run totals are
        # right-skewed (a long tail of high-scoring games pulls the mean
        # above the median), so a mean-based line is *always* modestly
        # harder to clear than a coin flip. Anchoring on the mean instead of
        # the median was silently forcing "lean UNDER" on nearly every
        # single game regardless of the two teams involved, which isn't a
        # real signal -- just an artifact of comparing actual outcomes
        # against a line that was never a fair 50/50 split to begin with.
        #
        # median_total is always a whole run count, so it sits exactly
        # halfway between two valid .5 lines (median-0.5 and median+0.5) --
        # naively always rounding the SAME direction (e.g. always up)
        # reintroduces the exact "systematically favors one side" problem
        # this median-based approach exists to avoid, just shifted onto
        # whichever tie mass sits at the median (usually the single most
        # common value in a unimodal distribution). Picking whichever of
        # the two candidates lands closer to an actual 50/50 split -- using
        # the real simulated counts, not an assumption -- is what a
        # sportsbook actually optimizes for when it sets a total.
        totals_sorted = sorted(totals)
        median_total = totals_sorted[len(totals_sorted) // 2]
        count_above_median = sum(1 for t in totals if t > median_total)
        count_at_median = sum(1 for t in totals if t == median_total)
        over_prob_lower_line = (count_above_median + count_at_median) / n_trials  # line = median - 0.5
        over_prob_upper_line = count_above_median / n_trials  # line = median + 0.5
        if abs(over_prob_lower_line - 0.5) <= abs(over_prob_upper_line - 0.5):
            total_line = median_total - 0.5
        else:
            total_line = median_total + 0.5
    over = sum(1 for t in totals if t > total_line)

    home_win_prob = round(home_wins / n_trials, 3)
    home_cover_prob = round(home_covers / n_trials, 3)  # P(home wins by 2+, i.e. covers -1.5)
    away_cover_prob = round(away_covers / n_trials, 3)  # P(away wins by 2+, i.e. covers -1.5)
    over_prob = round(over / n_trials, 3)

    # Run line: -1.5 goes to whichever side is the actual moneyline favorite
    # (real sportsbook convention) -- NOT fixed to home the way this used to
    # work, which mislabeled an away favorite as "+1.5" (backwards) any time
    # the road team was actually favored. The favorite covers -1.5 by
    # winning by 2+; the underdog covers +1.5 by anything else, including a
    # close 1-run loss -- that's the exact complement of "does the favorite
    # win by 2+", NOT the same question as "does the underdog win by 2+"
    # (home_cover_prob and away_cover_prob are genuinely independent, not
    # complements of each other -- there's a real gap in between covering
    # 1-run games either way).
    if home_win_prob >= 0.5:
        favorite, favorite_cover_prob = "home", home_cover_prob
        underdog_cover_prob = 1 - home_cover_prob
    else:
        favorite, favorite_cover_prob = "away", away_cover_prob
        underdog_cover_prob = 1 - away_cover_prob
    underdog = "away" if favorite == "home" else "home"
    spread_pick = favorite if favorite_cover_prob >= 0.5 else underdog
    spread_pick_prob = favorite_cover_prob if spread_pick == favorite else underdog_cover_prob

    return {
        "home_win_prob": home_win_prob,
        "spread_line": spread_line,
        "spread_cover_prob": home_cover_prob,  # P(home covers -1.5) -- kept under its original name for backward compat
        "total_line": total_line,
        "over_prob": over_prob,
        # Plain-language "what to pick" summary so callers don't have to
        "moneyline_pick": "home" if home_win_prob >= 0.5 else "away",
        "spread_favorite": favorite,  # which side (home/away) is actually assigned -1.5
        "spread_pick": spread_pick,   # which side (home/away) to bet
        "spread_pick_prob": round(spread_pick_prob, 3),
        "total_pick": "over" if over_prob >= 0.5 else "under",
        "total_pick_prob": round(over_prob if over_prob >= 0.5 else 1 - over_prob, 3),
    }
