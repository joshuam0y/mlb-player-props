"""
simulate_games.py

Runs the game_model simulation for upcoming scheduled games and snapshots
the result into `game_projections`, timestamped -- so a later backtest can
tell what this model would have said *before* the game, not reconstruct it
with hindsight. See game_model.py for the method and its honest limits.
"""

import argparse
from datetime import datetime, timedelta, timezone

from build_props import park_factor_tier
from db import get_conn, init_db
from game_model import project_matchup, simulate

MODEL_VERSION = "v1-log5-negbinom"


def run(days_ahead=2, n_trials=100000):
    init_db()
    conn = get_conn()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    end = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    games = conn.execute(
        "SELECT * FROM games WHERE official_date BETWEEN ? AND ? AND home_score IS NULL ORDER BY official_date",
        (today, end),
    ).fetchall()

    generated_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for game in games:
        # If the real lineup has posted, use those 9 specific hitters for the
        # lineup-strength nudge; otherwise pass nothing and the model runs on
        # team-level rates alone (see game_model.lineup_strength_adjustment).
        home_batter_ids = [
            r["player_id"] for r in conn.execute(
                "SELECT player_id FROM lineups WHERE game_pk = ? AND team_id = ?",
                (game["game_pk"], game["home_team_id"]),
            )
        ]
        away_batter_ids = [
            r["player_id"] for r in conn.execute(
                "SELECT player_id FROM lineups WHERE game_pk = ? AND team_id = ?",
                (game["game_pk"], game["away_team_id"]),
            )
        ]
        projection = project_matchup(
            conn,
            game["home_team_id"],
            game["away_team_id"],
            game["home_probable_pitcher_id"],
            game["away_probable_pitcher_id"],
            park_factor_tier(game["venue_name"]),
            home_batter_ids=home_batter_ids,
            away_batter_ids=away_batter_ids,
        )
        if projection is None:
            continue
        sim = simulate(projection, n_trials=n_trials)
        rows.append(
            (
                game["game_pk"], generated_at, MODEL_VERSION,
                projection["home_exp_runs"], projection["away_exp_runs"],
                sim["home_win_prob"], sim["spread_line"], sim["spread_cover_prob"],
                sim["total_line"], sim["over_prob"],
                sim["moneyline_pick"], sim["spread_favorite"], sim["spread_pick"], sim["spread_pick_prob"],
                sim["total_pick"], sim["total_pick_prob"],
            )
        )

    if rows:
        conn.executemany(
            """
            INSERT INTO game_projections
                (game_pk, generated_at, model_version, home_exp_runs, away_exp_runs,
                 home_win_prob, spread_line, spread_cover_prob, total_line, over_prob,
                 moneyline_pick, spread_favorite, spread_pick, spread_pick_prob, total_pick, total_pick_prob)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

    conn.close()
    print(f"Simulated {len(rows)}/{len(games)} upcoming games (skipped ones with no run-rate data yet).")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days-ahead", type=int, default=2)
    p.add_argument("--n-trials", type=int, default=100000)
    args = p.parse_args()
    run(days_ahead=args.days_ahead, n_trials=args.n_trials)
