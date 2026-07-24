"""
backtest.py

Point-in-time backtest of game_model.py: for every completed game this
season, reconstructs what the model would have projected using *only* data
dated strictly before that game (as_of_date=official_date), then scores
the projection against what actually happened.

This is the real test of whether the model has any signal -- not whether
it "looks reasonable" on today's slate. Two metrics:
  * Brier score on win probability (0 = perfect, 0.25 = coin-flip-level,
    lower is better; a well-calibrated coin flip on every game scores 0.25)
  * MAE on total runs (home_exp_runs + away_exp_runs vs actual total)

Also reports a "guess the league-wide home-win rate every time" baseline
alongside the model, since a Brier score in isolation doesn't tell you
whether the model beats doing nothing.

Games early in the season are naturally skipped (project_matchup returns
None until each team has enough of its own game history and the league
distribution has enough completed games -- no separate cutoff is hardcoded
here, since that threshold is already enforced inside game_model.py).
"""

import argparse

from build_props import park_factor_tier
from db import get_conn, init_db
from game_model import project_matchup, simulate

HOME_FIELD_BASELINE = 0.54  # long-run MLB home-win rate, used only as a naive comparison point


def brier_score(pairs):
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else None


def run(start=None, end=None, n_trials=3000):
    init_db()
    conn = get_conn()

    query = "SELECT * FROM games WHERE home_score IS NOT NULL"
    params = []
    if start:
        query += " AND official_date >= ?"
        params.append(start)
    if end:
        query += " AND official_date <= ?"
        params.append(end)
    query += " ORDER BY official_date"
    games = conn.execute(query, params).fetchall()

    model_pairs, home_field_pairs = [], []
    total_errors = []
    skipped = 0

    for game in games:
        projection = project_matchup(
            conn,
            game["home_team_id"], game["away_team_id"],
            game["home_probable_pitcher_id"], game["away_probable_pitcher_id"],
            park_factor_tier(game["venue_name"]),
            as_of_date=game["official_date"],
        )
        if projection is None:
            skipped += 1
            continue

        actual_home_win = 1.0 if game["home_score"] > game["away_score"] else 0.0
        actual_total = game["home_score"] + game["away_score"]

        sim = simulate(projection, n_trials=n_trials, seed=game["game_pk"])
        model_pairs.append((sim["home_win_prob"], actual_home_win))
        home_field_pairs.append((HOME_FIELD_BASELINE, actual_home_win))
        total_errors.append(abs((projection["home_exp_runs"] + projection["away_exp_runs"]) - actual_total))

    conn.close()

    n = len(model_pairs)
    print(f"Backtested {n} games ({skipped} skipped -- not enough prior data yet).")
    if n == 0:
        return

    print(f"Model Brier score (win prob):        {brier_score(model_pairs):.4f}")
    print(f"Home-field-only baseline Brier score: {brier_score(home_field_pairs):.4f}  (always guess {HOME_FIELD_BASELINE:.0%} home win)")
    print(f"Model total-runs MAE:                 {sum(total_errors)/n:.2f} runs")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", help="YYYY-MM-DD")
    p.add_argument("--end", help="YYYY-MM-DD")
    p.add_argument("--n-trials", type=int, default=3000)
    args = p.parse_args()
    run(start=args.start, end=args.end, n_trials=args.n_trials)
