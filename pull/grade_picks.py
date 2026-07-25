"""
grade_picks.py

Grades a day's picks and signal flags against what actually happened,
using that day's own archived report (output/props_{date}.json) as the
record of what was actually shown -- not a reconstruction after the fact.
Covers:

  * Top Overs/Top Unders -- the curated picks, graded hit/miss against
    each pick's own best_category line.
  * Batter HOT/COLD trend -- every flagged batter in the report (not just
    Top Picks), bucketed by trend, against their actual line that day.
  * Batter matchup edge -- same idea, bucketed by favorable/unfavorable/
    neutral. This is the one signal backtest_props.py deliberately can't
    backtest historically (platoon splits are a current-snapshot-only
    table, no dated history to reconstruct from) -- grading it same-day,
    right after the fact, sidesteps that: there's no leakage risk in
    checking today's split-based flag against today's own result.
  * Pitcher form trend (dominant/rough) -- mirrors the batter trend check.

Results are appended to output/track_record.json, keyed by date, so
grading is a running, permanent record rather than a one-off report --
re-grading an already-graded date (e.g. after an extra-innings game
finishes late) just recomputes and overwrites that date's entry.

Deliberately reads whatever shape that day's archive happens to be in --
older archives (before build_top_picks() was fixed to filter to a single
date) had top_overs/top_unders as a flat, possibly multi-date list;
current ones are a {"batters": [...], "pitchers": [...]} dict already
scoped to that one day. _picks_for_date() normalizes either.
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from db import get_conn, init_db

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
TRACK_RECORD_PATH = os.path.join(OUT_DIR, "track_record.json")

BATTER_COL = {
    "Hits": "hits", "Total Bases": "total_bases", "Home Runs": "home_runs",
    "RBIs": "rbi", "Runs Scored": "runs", "Walks": "base_on_balls",
}
PITCHER_COL = {
    "Strikeouts": "strike_outs", "Runs Allowed": "earned_runs",
    "Hits Allowed": "hits", "Walks Allowed": "base_on_balls",
}


def _load_day_report(date):
    path = os.path.join(OUT_DIR, f"props_{date}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _picks_for_date(top_picks_field, target_date):
    """Normalizes either the old (flat list, possibly multi-date) or current ({"batters":[], "pitchers":[]}, single-date) shape."""
    if top_picks_field is None:
        return []
    if isinstance(top_picks_field, dict):
        return (top_picks_field.get("batters") or []) + (top_picks_field.get("pitchers") or [])
    return [p for p in top_picks_field if p.get("date") == target_date]


def _grade_pick(conn, pick, direction):
    role = pick.get("role", "batter")
    cat = pick.get("best_category")
    if not cat:
        return "no_data", None
    col = (BATTER_COL if role == "batter" else PITCHER_COL).get(cat["label"])
    if not col:
        return "no_data", None
    table = "batting_game_logs" if role == "batter" else "pitching_game_logs"
    row = conn.execute(
        f"SELECT {col} as val FROM {table} WHERE player_id = ? AND date = ?",
        (pick["player_id"], pick["date"]),
    ).fetchone()
    if not row or row["val"] is None:
        return "no_data", None
    val = row["val"]
    cleared = val > cat["line"]
    hit = cleared if direction == "over" else not cleared
    return ("hit" if hit else "miss"), val


def _grade_picks_bucket(conn, picks, direction):
    hits = misses = no_data = 0
    details = []
    for p in picks:
        outcome, val = _grade_pick(conn, p, direction)
        if outcome == "no_data":
            no_data += 1
        elif outcome == "hit":
            hits += 1
        else:
            misses += 1
        cat = p.get("best_category") or {}
        details.append({
            "name": p["name"], "role": p.get("role", "batter"),
            "category": cat.get("label"), "line": cat.get("line"),
            "actual": val, "outcome": outcome,
        })
    graded = hits + misses
    return {
        "n": len(picks), "hits": hits, "misses": misses, "no_data": no_data,
        "hit_rate": round(hits / graded, 3) if graded else None,
        "picks": details,
    }


def _batter_bucket_stats(rows):
    n = len(rows)
    if n == 0:
        return {"games": 0, "avg": None, "tb_per_game": None}
    hits = sum(r["hits"] or 0 for r in rows)
    at_bats = sum(r["at_bats"] or 0 for r in rows)
    total_bases = sum(r["total_bases"] or 0 for r in rows)
    return {
        "games": n,
        "avg": round(hits / at_bats, 3) if at_bats else None,
        "tb_per_game": round(total_bases / n, 2),
    }


def _pitcher_bucket_stats(rows):
    n = len(rows)
    if n == 0:
        return {"games": 0, "era": None, "k_per_game": None}
    outs = sum(r["outs"] or 0 for r in rows)
    earned_runs = sum(r["earned_runs"] or 0 for r in rows)
    strike_outs = sum(r["strike_outs"] or 0 for r in rows)
    innings = outs / 3 if outs else 0
    return {
        "games": n,
        "era": round(earned_runs * 9 / innings, 2) if innings else None,
        "k_per_game": round(strike_outs / n, 2),
    }


def _grade_batter_signals(conn, report, date):
    trend_buckets = {"hot": [], "cold": [], "neutral": []}
    matchup_buckets = {"favorable": [], "unfavorable": [], "neutral": []}
    for g in report["games"]:
        if g["date"] != date:
            continue
        for side in (g["home"], g["away"]):
            for b in side["batters"]:
                row = conn.execute(
                    "SELECT hits, at_bats, total_bases FROM batting_game_logs WHERE player_id = ? AND date = ?",
                    (b["player_id"], date),
                ).fetchone()
                if not row or row["at_bats"] is None:
                    continue  # DNP that day -- excludes both a hot/cold AND matchup read, same as a real prop would grade "no action"
                trend_key = b.get("trend") or "neutral"
                trend_buckets.setdefault(trend_key, []).append(row)
                m = b.get("matchup") or {}
                matchup_key = "favorable" if m.get("favorable") else ("unfavorable" if m.get("unfavorable") else "neutral")
                matchup_buckets[matchup_key].append(row)
    return (
        {k: _batter_bucket_stats(v) for k, v in trend_buckets.items()},
        {k: _batter_bucket_stats(v) for k, v in matchup_buckets.items()},
    )


def _grade_pitcher_signals(conn, report, date):
    form_buckets = {"dominant": [], "rough": [], "neutral": []}
    for g in report["games"]:
        if g["date"] != date:
            continue
        for side in (g["home"], g["away"]):
            p = side["probable_pitcher"]
            if not p:
                continue
            row = conn.execute(
                "SELECT outs, earned_runs, strike_outs FROM pitching_game_logs WHERE player_id = ? AND date = ?",
                (p["player_id"], date),
            ).fetchone()
            if not row or row["outs"] is None:
                continue  # scratched/didn't start that day
            key = p.get("form_trend") or "neutral"
            form_buckets.setdefault(key, []).append(row)
    return {k: _pitcher_bucket_stats(v) for k, v in form_buckets.items()}


def grade_day(conn, date):
    report = _load_day_report(date)
    if report is None:
        return None
    top_overs = _picks_for_date(report.get("top_overs"), date)
    top_unders = _picks_for_date(report.get("top_unders"), date)
    batter_trend, batter_matchup = _grade_batter_signals(conn, report, date)
    pitcher_form = _grade_pitcher_signals(conn, report, date)
    return {
        "date": date,
        "graded_at": datetime.now(timezone.utc).isoformat(),
        "top_overs": _grade_picks_bucket(conn, top_overs, "over"),
        "top_unders": _grade_picks_bucket(conn, top_unders, "under"),
        "batter_trend": batter_trend,
        "batter_matchup": batter_matchup,
        "pitcher_form": pitcher_form,
    }


def _sum_bucket_stats(days, path, kind):
    """
    kind: 'batter' or 'pitcher'. Each day's bucket already stores a rate
    (avg/tb-per-game or era/k-per-game), not raw counts, so the cumulative
    figure is a game-count-weighted average of those daily rates rather
    than a fresh recompute from underlying at-bats -- exact for this
    purpose since each daily rate is itself already per-game.
    """
    weighted_avg_num = 0.0
    weighted_tb_num = 0.0
    games = 0
    for day in days:
        bucket = day
        for key in path:
            bucket = bucket.get(key, {})
        g = bucket.get("games") or 0
        if not g:
            continue
        games += g
        if kind == "batter":
            if bucket.get("avg") is not None:
                weighted_avg_num += bucket["avg"] * g
            if bucket.get("tb_per_game") is not None:
                weighted_tb_num += bucket["tb_per_game"] * g
        else:
            if bucket.get("k_per_game") is not None:
                weighted_avg_num += bucket["k_per_game"] * g
            if bucket.get("era") is not None:
                weighted_tb_num += bucket["era"] * g
    if games == 0:
        return {"games": 0}
    if kind == "batter":
        return {"games": games, "avg": round(weighted_avg_num / games, 3), "tb_per_game": round(weighted_tb_num / games, 2)}
    return {"games": games, "k_per_game": round(weighted_avg_num / games, 2), "era": round(weighted_tb_num / games, 2)}


def _cumulative(days_dict):
    days = list(days_dict.values())
    if not days:
        return {}
    picks_totals = {}
    for section in ("top_overs", "top_unders"):
        hits = sum(d[section]["hits"] for d in days)
        misses = sum(d[section]["misses"] for d in days)
        no_data = sum(d[section]["no_data"] for d in days)
        graded = hits + misses
        picks_totals[section] = {
            "days": len(days), "n": sum(d[section]["n"] for d in days),
            "hits": hits, "misses": misses, "no_data": no_data,
            "hit_rate": round(hits / graded, 3) if graded else None,
        }
    result = dict(picks_totals)
    for key in ("hot", "cold", "neutral"):
        result.setdefault("batter_trend", {})[key] = _sum_bucket_stats(days, ["batter_trend", key], "batter")
    for key in ("favorable", "unfavorable", "neutral"):
        result.setdefault("batter_matchup", {})[key] = _sum_bucket_stats(days, ["batter_matchup", key], "batter")
    for key in ("dominant", "rough", "neutral"):
        result.setdefault("pitcher_form", {})[key] = _sum_bucket_stats(days, ["pitcher_form", key], "pitcher")
    result["days_tracked"] = len(days)
    return result


def update_track_record(day_result):
    if os.path.exists(TRACK_RECORD_PATH):
        with open(TRACK_RECORD_PATH) as f:
            record = json.load(f)
    else:
        record = {"days": {}}
    record["days"][day_result["date"]] = day_result
    record["cumulative"] = _cumulative(record["days"])
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(TRACK_RECORD_PATH, "w") as f:
        json.dump(record, f, indent=2, default=str)
    return record


def run(dates):
    init_db()
    conn = get_conn()
    for date in dates:
        result = grade_day(conn, date)
        if result is None:
            print(f"{date}: no archived report found (output/props_{date}.json missing) -- skipped.")
            continue
        update_track_record(result)
        to, tu = result["top_overs"], result["top_unders"]
        over_txt = f"{to['hits']}/{to['hits']+to['misses']} ({to['hit_rate']:.0%})" if to["hit_rate"] is not None else "no graded picks"
        under_txt = f"{tu['hits']}/{tu['hits']+tu['misses']} ({tu['hit_rate']:.0%})" if tu["hit_rate"] is not None else "no graded picks"
        print(f"{date}: graded. Top Overs {over_txt} | Top Unders {under_txt}")
    conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD to grade. Defaults to yesterday (UTC).")
    p.add_argument("--backfill-days", type=int, default=0, help="Also grade this many days before --date/yesterday.")
    args = p.parse_args()

    base = datetime.strptime(args.date, "%Y-%m-%d") if args.date else (datetime.now(timezone.utc) - timedelta(days=1))
    dates = [(base - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(args.backfill_days + 1)]
    run(dates)
