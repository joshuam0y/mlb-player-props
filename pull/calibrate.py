"""
calibrate.py

Fits a Platt-scaling recalibration for game_model's win-probability output:
calibrated_prob = sigmoid(A * logit(raw_prob) + B), fit via gradient descent
on a TRAIN split of point-in-time backtested (raw_prob, actual_outcome)
pairs, then evaluated -- honestly, on a held-out TEST split it never saw
during fitting -- against the raw (uncalibrated) probability and the naive
home-field-rate baseline. This is the calibration layer flagged early on as
the real next step for the game-sim model (a fitted correction, not a
hand-picked constant).

Chronological train/test split (not random): this is a time series, and a
"calibration" fit on a random shuffle that includes future games would leak
information a real forward-looking calibration could never have had.

If calibration doesn't measurably beat the raw probability on the held-out
test set, that's reported plainly, not hidden -- and nothing is written to
output/calibration.json, so simulate_games.py keeps using raw probabilities.
"""

import argparse
import json
import math
import os

from build_props import park_factor_tier
from db import get_conn, init_db
from game_model import project_matchup, simulate

HOME_FIELD_BASELINE = 0.54  # long-run MLB home-win rate, used only as a naive comparison point
CALIBRATION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "calibration.json")


def brier_score(pairs):
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else None


def logit(p, eps=1e-6):
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def sigmoid(x):
    if x >= 0:
        z = math.exp(-x)
        return 1 / (1 + z)
    z = math.exp(x)
    return z / (1 + z)


def fit_platt(pairs, lr=0.05, epochs=3000):
    """pairs: [(raw_prob, actual 0/1)]. Fits calibrated = sigmoid(A*logit(raw)+B) via batch gradient descent on log loss."""
    xs = [logit(p) for p, _ in pairs]
    ys = [o for _, o in pairs]
    n = len(pairs)
    a, b = 1.0, 0.0
    for _ in range(epochs):
        grad_a = grad_b = 0.0
        for x, y in zip(xs, ys):
            err = sigmoid(a * x + b) - y
            grad_a += err * x
            grad_b += err
        a -= lr * grad_a / n
        b -= lr * grad_b / n
    return a, b


def apply_platt(raw_prob, a, b):
    return sigmoid(a * logit(raw_prob) + b)


def collect_pairs(conn, games, n_trials):
    pairs = []
    for game in games:
        projection = project_matchup(
            conn, game["home_team_id"], game["away_team_id"],
            game["home_probable_pitcher_id"], game["away_probable_pitcher_id"],
            park_factor_tier(game["venue_name"]), as_of_date=game["official_date"],
        )
        if projection is None:
            continue
        actual_home_win = 1.0 if game["home_score"] > game["away_score"] else 0.0
        sim = simulate(projection, n_trials=n_trials, seed=game["game_pk"])
        pairs.append((sim["home_win_prob"], actual_home_win))
    return pairs


def run(train_frac=0.8, n_trials=3000):
    init_db()
    conn = get_conn()
    games = conn.execute(
        "SELECT * FROM games WHERE home_score IS NOT NULL ORDER BY official_date"
    ).fetchall()

    all_pairs = collect_pairs(conn, games, n_trials)
    conn.close()

    n = len(all_pairs)
    if n < 100:
        print(f"Only {n} games with a usable projection -- too few for a stable calibration fit. Not proceeding.")
        return
    split = int(n * train_frac)
    train, test = all_pairs[:split], all_pairs[split:]
    print(f"{n} total games with a projection -- {len(train)} train / {len(test)} test (chronological split)")

    a, b = fit_platt(train)
    print(f"Fitted Platt scaling on train set: A={a:.3f}, B={b:.3f}")

    raw_test_brier = brier_score(test)
    calibrated_test = [(apply_platt(p, a, b), o) for p, o in test]
    calibrated_test_brier = brier_score(calibrated_test)
    baseline_test_brier = brier_score([(HOME_FIELD_BASELINE, o) for _, o in test])

    print(f"Held-out test set ({len(test)} games):")
    print(f"  Raw model Brier score:        {raw_test_brier:.4f}")
    print(f"  Calibrated model Brier score: {calibrated_test_brier:.4f}")
    print(f"  Home-field baseline Brier:    {baseline_test_brier:.4f}")

    if calibrated_test_brier < raw_test_brier:
        improvement = raw_test_brier - calibrated_test_brier
        with open(CALIBRATION_PATH, "w") as f:
            json.dump(
                {"a": a, "b": b, "fit_on_games": len(train), "test_games": len(test), "test_brier_improvement": improvement},
                f, indent=2,
            )
        print(f"Calibration improves the held-out Brier score by {improvement:.4f} -- saved to {CALIBRATION_PATH}")
    else:
        print("Calibration does NOT improve the held-out Brier score -- not applying it. Raw probabilities stay as-is.")
        if os.path.exists(CALIBRATION_PATH):
            os.remove(CALIBRATION_PATH)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--train-frac", type=float, default=0.8)
    p.add_argument("--n-trials", type=int, default=3000)
    args = p.parse_args()
    run(train_frac=args.train_frac, n_trials=args.n_trials)
