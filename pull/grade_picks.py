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
  * Projection accuracy -- for every prop category on every player shown
    that day, how close was the actual NUMBER projected (today_projection,
    e.g. "8 projected strikeouts") to what actually happened (e.g. "12
    actual"). This is a different, more granular cut than hit/miss: it
    grades the projection's magnitude, not just whether a specific line
    was cleared.
  * Game-level picks -- moneyline, run line, and total (over/under),
    graded hit/miss the same way as Top Overs/Unders, plus projected vs.
    actual final score (same magnitude-accuracy idea as the per-stat
    projections above).
  * Matchup-lean headline ("Predicted: X OVER/UNDER Y" next to each box
    score) and the per-team Best-prop star -- both graded the same
    pick_result() logic as Top Overs/Unders. Only gradable for dates whose
    archive actually has these fields: they were added well after this
    project started, so older archives simply have nothing to grade (an
    empty bucket, not an error). TODAY is a special case (see
    _matchup_lean_source()) -- since build_report()'s own "today" is
    anchored to right now, a same-day archive written before these fields
    existed can be backfilled by recomputing a fresh report; both fields
    are already as_of_date-filtered to exclude today's own game, so
    recomputing them later in the day gives the exact same answer
    computing them that morning would have. That trick doesn't extend to
    older backfilled dates -- there's no safe way to reconstruct a PAST
    day's version of these after the fact.

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

from build_props import (
    _player_context,
    _refresh_frozen_pick,
    _stat_sql_expr,
    batter_game_result,
    build_report,
    pick_result,
    pitcher_game_result,
)
from db import get_conn, init_db, mlb_today

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
TRACK_RECORD_PATH = os.path.join(OUT_DIR, "track_record.json")

# Values are the same keys pick_result()'s own _BATTER_CATEGORY_FIELD uses
# (batter_game_result()'s dict, read directly at grade_picks.py:487) -- the
# two raw-SQL query sites below (_grade_pick, _collect_player_projection_errors)
# convert a key to the real SQL expression via _stat_sql_expr() (imported
# from build_props.py) rather than storing the SQL text here directly, so
# there's exactly one place ("h_r_rbi" -> "(hits + runs + rbi)") that has
# to stay right.
BATTER_COL = {
    "Hits": "hits", "Total Bases": "total_bases", "Home Runs": "home_runs",
    "RBIs": "rbi", "Runs Scored": "runs", "Walks": "base_on_balls",
    "Hits + Runs + RBIs": "h_r_rbi",
}
PITCHER_COL = {
    "Strikeouts": "strike_outs", "Outs Recorded": "outs", "Runs Allowed": "earned_runs",
    "Hits Allowed": "hits", "Walks Allowed": "base_on_balls",
}


def _load_day_report(date):
    path = os.path.join(OUT_DIR, f"props_{date}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _picks_for_date(top_picks_field, target_date):
    """
    Normalizes either the old (flat list, possibly multi-date) or current
    ({"batters": [...], "pitchers": [...]}) shape -- and always filters to
    target_date regardless of which shape it is. Trusting the dict shape to
    already be single-date was itself a bug: build_top_picks() only started
    scoping to a single date partway through this project, so a dict-shaped
    top_overs/top_unders frozen before that fix landed can still span
    multiple dates. Confirmed on a real archive: a player who'd cleared his
    RBI line that day still showed as "no data" because his ACTUAL game
    (today) sat right next to a phantom entry for a different date's game
    (tomorrow's, not yet played) that was never supposed to be graded here
    at all -- both got graded, so he appeared twice, once correctly and
    once as a false miss.
    """
    if top_picks_field is None:
        return []
    picks = (top_picks_field.get("batters") or []) + (top_picks_field.get("pitchers") or []) if isinstance(top_picks_field, dict) else top_picks_field
    return [p for p in picks if p.get("date") == target_date]


def _grade_pick(conn, pick, direction):
    """
    "No data" should mean "didn't play," not "played, but this one column
    came back empty." The per-game sync pulls a whole stat line from a
    single API object in one shot, so it's possible for at_bats (batters)
    or outs (pitchers) to be a real number while some OTHER column in that
    same row -- the one this particular pick happens to be graded on -- is
    None (the API omitted that key for this game). Checking the played
    indicator separately from the graded column means a real appearance
    with, say, a missing home_runs value grades as a real 0 (a miss on an
    OVER, a hit on an UNDER) instead of being wrongly written off as DNP.
    """
    role = pick.get("role", "batter")
    cat = pick.get("best_category")
    if not cat:
        return "no_data", None
    col = (BATTER_COL if role == "batter" else PITCHER_COL).get(cat["label"])
    if not col:
        return "no_data", None
    table = "batting_game_logs" if role == "batter" else "pitching_game_logs"
    played_col = "at_bats" if role == "batter" else "outs"
    row = conn.execute(
        f"SELECT {_stat_sql_expr(col)} as val, {played_col} as played FROM {table} WHERE player_id = ? AND date = ?",
        (pick["player_id"], pick["date"]),
    ).fetchone()
    if not row or row["played"] is None:
        return "no_data", None
    val = row["val"] if row["val"] is not None else 0
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
            "name": p["name"], "role": p.get("role", "batter"), "position": p.get("position"),
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
        return {"games": 0, "avg": None, "tb_per_game": None, "examples": []}
    hits = sum(r["hits"] or 0 for r in rows)
    at_bats = sum(r["at_bats"] or 0 for r in rows)
    total_bases = sum(r["total_bases"] or 0 for r in rows)
    return {
        "games": n,
        "avg": round(hits / at_bats, 3) if at_bats else None,
        "tb_per_game": round(total_bases / n, 2),
        # Biggest performances first (most hits that game) -- a HOT/COLD or
        # matchup bucket is a categorization, not a graded pick with its
        # own line, so there's no hit/miss to show here, just who actually
        # played and what they did.
        "examples": [
            {"name": r.get("name"), "player_id": r.get("player_id"), "line": f"{r['hits'] or 0}-for-{r['at_bats'] or 0}, {r['total_bases'] or 0} TB"}
            for r in sorted(rows, key=lambda r: -(r["hits"] or 0))[:10]
        ],
    }


def _pitcher_bucket_stats(rows):
    n = len(rows)
    if n == 0:
        return {"games": 0, "era": None, "k_per_game": None, "examples": []}
    outs = sum(r["outs"] or 0 for r in rows)
    earned_runs = sum(r["earned_runs"] or 0 for r in rows)
    strike_outs = sum(r["strike_outs"] or 0 for r in rows)
    innings = outs / 3 if outs else 0
    return {
        "games": n,
        "era": round(earned_runs * 9 / innings, 2) if innings else None,
        "k_per_game": round(strike_outs / n, 2),
        "examples": [
            {
                "name": r.get("name"), "player_id": r.get("player_id"),
                "line": f"{round((r['outs'] or 0) / 3, 1)} IP, {r['strike_outs'] or 0} K, {r['earned_runs'] or 0} ER",
            }
            for r in sorted(rows, key=lambda r: -(r["strike_outs"] or 0))[:10]
        ],
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
                entry = dict(row)
                entry["name"] = b["name"]
                entry["player_id"] = b["player_id"]
                trend_key = b.get("trend") or "neutral"
                trend_buckets.setdefault(trend_key, []).append(entry)
                m = b.get("matchup") or {}
                matchup_key = "favorable" if m.get("favorable") else ("unfavorable" if m.get("unfavorable") else "neutral")
                matchup_buckets[matchup_key].append(entry)
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
            entry = dict(row)
            entry["name"] = p["name"]
            entry["player_id"] = p["player_id"]
            key = p.get("form_trend") or "neutral"
            form_buckets.setdefault(key, []).append(entry)
    return {k: _pitcher_bucket_stats(v) for k, v in form_buckets.items()}


def _collect_player_projection_errors(conn, player, role, date, by_category, examples):
    col_map = BATTER_COL if role == "batter" else PITCHER_COL
    table = "batting_game_logs" if role == "batter" else "pitching_game_logs"
    played_col = "at_bats" if role == "batter" else "outs"
    player_id = player.get("player_id")
    if player_id is None:
        return
    for cat in player.get("prop_categories") or []:
        col = col_map.get(cat["label"])
        projected = cat.get("today_projection")
        if not col or projected is None:
            continue
        row = conn.execute(
            f"SELECT {_stat_sql_expr(col)} as val, {played_col} as played FROM {table} WHERE player_id = ? AND date = ?",
            (player_id, date),
        ).fetchone()
        if not row or row["played"] is None:
            continue  # DNP that day -- no actual to compare the projection against
        actual = row["val"] if row["val"] is not None else 0  # played, just a real 0 in this specific column
        error = actual - projected
        by_category.setdefault(cat["label"], []).append((projected, actual))
        examples.append({
            "name": player["name"], "role": role, "position": player.get("position"), "category": cat["label"],
            "projected": projected, "actual": actual, "error": round(error, 2),
        })


def _grade_projections(conn, report, date):
    """
    Per-category projection accuracy: for every player shown that day, how
    close was their projected number (e.g. "8.0 projected strikeouts") to
    what they actually did (e.g. "12 strikeouts")? Bucketed by stat label
    across every batter and probable pitcher in the day's games -- not
    just Top Picks -- so this is the broadest possible read on "how good
    are the numbers themselves," independent of any over/under line.
    """
    by_category = {}
    examples = []
    for g in report["games"]:
        if g["date"] != date:
            continue
        for side in (g["home"], g["away"]):
            for b in side["batters"]:
                _collect_player_projection_errors(conn, b, "batter", date, by_category, examples)
            p = side["probable_pitcher"]
            if p:
                _collect_player_projection_errors(conn, p, "pitcher", date, by_category, examples)
    category_stats = {}
    for label, pairs in by_category.items():
        n = len(pairs)
        errors = [actual - projected for projected, actual in pairs]
        category_stats[label] = {
            "n": n,
            "avg_projected": round(sum(p for p, a in pairs) / n, 2),
            "avg_actual": round(sum(a for p, a in pairs) / n, 2),
            "mae": round(sum(abs(e) for e in errors) / n, 2),
            "bias": round(sum(errors) / n, 2),  # positive = actual ran hotter than projected, on average
        }
    examples.sort(key=lambda e: -abs(e["error"]))
    return category_stats, examples[:10]


def _grade_games(conn, report, date):
    """
    Grades each game's projection against the actual final score:
    moneyline (correct winner?), run line (did the picked side cover the
    favorite/underdog-aware spread -- see game_model.simulate()'s own
    definition, mirrored exactly here), and total (over/under pick vs.
    actual combined runs). Also tracks how close the projected score
    itself was to the actual score, the same magnitude-accuracy idea as
    _grade_projections() above but for the game as a whole.
    """
    ml_hits = ml_misses = 0
    spread_hits = spread_misses = 0
    total_hits = total_misses = 0
    score_errors = []
    score_examples = []
    ml_examples = []
    run_line_examples = []
    total_examples = []
    for g in report["games"]:
        if g["date"] != date:
            continue
        proj = g.get("projection")
        if not proj:
            continue
        row = conn.execute(
            "SELECT home_score, away_score FROM games WHERE game_pk = ?", (g["game_pk"],)
        ).fetchone()
        if not row or row["home_score"] is None or row["away_score"] is None:
            continue  # not final (postponed/suspended) -- can't grade yet
        home_score, away_score = row["home_score"], row["away_score"]
        margin = home_score - away_score  # MLB games never end tied
        matchup = f"{g['away']['team_name']} @ {g['home']['team_name']}"

        ml_pick = proj.get("moneyline_pick") or ("home" if proj["home_win_prob"] >= 0.5 else "away")
        ml_hit = (ml_pick == "home") == (margin > 0)
        if ml_hit:
            ml_hits += 1
        else:
            ml_misses += 1
        ml_team = g["home"]["team_name"] if ml_pick == "home" else g["away"]["team_name"]
        ml_examples.append({
            "matchup": matchup, "pick": f"{ml_team} to win", "actual": f"{away_score}-{home_score}",
            "outcome": "hit" if ml_hit else "miss",
        })

        spread_pick = proj.get("spread_pick")
        if spread_pick:
            favorite = proj.get("spread_favorite") or ("home" if proj["home_win_prob"] >= 0.5 else "away")
            spread_line = proj.get("spread_line", 1.5)
            favorite_margin = margin if favorite == "home" else -margin
            favorite_covers = favorite_margin > spread_line
            covered = favorite_covers if spread_pick == favorite else not favorite_covers
            if covered:
                spread_hits += 1
            else:
                spread_misses += 1
            spread_team = g["home"]["team_name"] if spread_pick == "home" else g["away"]["team_name"]
            spread_side = f"-{spread_line}" if spread_pick == favorite else f"+{spread_line}"
            run_line_examples.append({
                "matchup": matchup, "pick": f"{spread_team} {spread_side}", "actual": f"margin {margin:+d}",
                "outcome": "hit" if covered else "miss",
            })

        total_line = proj.get("total_line")
        if total_line is not None:
            actual_total = home_score + away_score
            total_pick = proj.get("total_pick") or ("over" if proj.get("over_prob", 0.5) >= 0.5 else "under")
            actual_over = actual_total > total_line
            total_hit = actual_over == (total_pick == "over")
            if total_hit:
                total_hits += 1
            else:
                total_misses += 1
            total_examples.append({
                "matchup": matchup, "pick": f"{total_pick.upper()} {total_line}", "actual": f"{actual_total} runs",
                "outcome": "hit" if total_hit else "miss",
            })

        if proj.get("home_exp_runs") is not None and proj.get("away_exp_runs") is not None:
            actual_total = home_score + away_score
            projected_total = proj["home_exp_runs"] + proj["away_exp_runs"]
            error = actual_total - projected_total
            score_errors.append(error)
            score_examples.append({
                "matchup": f"{g['away']['team_name']} @ {g['home']['team_name']}",
                "projected_away": proj["away_exp_runs"], "projected_home": proj["home_exp_runs"],
                "actual_away": away_score, "actual_home": home_score,
                "error": round(error, 2),
            })

    def _bucket(hits, misses, examples):
        graded = hits + misses
        return {
            "n": graded, "hits": hits, "misses": misses,
            "hit_rate": round(hits / graded, 3) if graded else None,
            "examples": examples[:10],
        }

    score_examples.sort(key=lambda e: -abs(e["error"]))
    n = len(score_errors)
    score_accuracy = {
        "n": n,
        "mae": round(sum(abs(e) for e in score_errors) / n, 2) if n else None,
        "bias": round(sum(score_errors) / n, 2) if n else None,
    }
    return {
        "moneyline": _bucket(ml_hits, ml_misses, ml_examples),
        "run_line": _bucket(spread_hits, spread_misses, run_line_examples),
        "total": _bucket(total_hits, total_misses, total_examples),
        "score_accuracy": score_accuracy,
        "score_examples": score_examples[:10],
    }


def _grade_matchup_leans(conn, report, date):
    """
    Whether the "Predicted: X OVER/UNDER Y" headline (best_matchup_lean()
    in build_props.py) actually hit, across every batter and probable
    pitcher shown that day. Games not yet truly Final are skipped
    entirely (not counted as no_data) -- hourly.yml re-grades the last
    few days on every run, so a game that finishes late just gets graded
    on a later pass instead of being locked in early as a premature miss.
    """
    hits = misses = no_data = 0
    examples = []
    for g in report["games"]:
        if g["date"] != date:
            continue
        row = conn.execute("SELECT home_score, away_score FROM games WHERE game_pk = ?", (g["game_pk"],)).fetchone()
        if not row or row["home_score"] is None or row["away_score"] is None:
            continue  # not final yet -- graded on a later pass instead
        for side_key in ("home", "away"):
            side = g[side_key]
            entities = [("batter", b) for b in side["batters"]]
            p = side.get("probable_pitcher")
            if p:
                entities.append(("pitcher", p))
            for role, entity in entities:
                lean = entity.get("matchup_lean")
                if not lean:
                    continue
                result_fn = batter_game_result if role == "batter" else pitcher_game_result
                game_result = result_fn(conn, entity["player_id"], g["game_pk"])
                if not game_result:
                    no_data += 1
                    continue
                outcome = pick_result(role, lean, game_result, lean["direction"], True)
                if outcome is None:
                    no_data += 1
                    continue
                if outcome == "hit":
                    hits += 1
                else:
                    misses += 1
                col = (BATTER_COL if role == "batter" else PITCHER_COL).get(lean["label"])
                examples.append({
                    "name": entity["name"], "role": role, "position": entity.get("position"),
                    "category": f'{lean["label"]} {lean["direction"].upper()}',
                    "line": lean["line"], "actual": game_result.get(col), "outcome": outcome,
                })
    graded = hits + misses
    return {
        "n": hits + misses + no_data, "hits": hits, "misses": misses, "no_data": no_data,
        "hit_rate": round(hits / graded, 3) if graded else None, "examples": examples[:10],
    }


def _grade_best_prop_stars(conn, report, date):
    """Whether the team's starred Best-prop pick (best_prop_star() in build_props.py -- batter or the probable pitcher, whichever had the strongest signal) actually hit. Games not yet Final skipped, same reasoning as _grade_matchup_leans()."""
    hits = misses = no_data = 0
    examples = []
    for g in report["games"]:
        if g["date"] != date:
            continue
        row = conn.execute("SELECT home_score, away_score FROM games WHERE game_pk = ?", (g["game_pk"],)).fetchone()
        if not row or row["home_score"] is None or row["away_score"] is None:
            continue
        for side_key in ("home", "away"):
            side = g[side_key]
            star_id = side.get("star_player_id")
            if not star_id:
                continue
            entity, role = None, None
            for b in side["batters"]:
                if b["player_id"] == star_id:
                    entity, role = b, "batter"
                    break
            p = side.get("probable_pitcher")
            if entity is None and p and p["player_id"] == star_id:
                entity, role = p, "pitcher"
            if entity is None:
                continue
            best = entity.get("best_prop")
            direction = entity.get("best_prop_direction")
            if not best or not direction:
                continue
            result_fn = batter_game_result if role == "batter" else pitcher_game_result
            game_result = result_fn(conn, star_id, g["game_pk"])
            if not game_result:
                no_data += 1
                continue
            outcome = pick_result(role, best, game_result, direction, True)
            if outcome is None:
                no_data += 1
                continue
            if outcome == "hit":
                hits += 1
            else:
                misses += 1
            col = (BATTER_COL if role == "batter" else PITCHER_COL).get(best["label"])
            examples.append({
                "name": entity["name"], "role": role, "position": entity.get("position"),
                "category": f'{best["label"]} {direction.upper()}',
                "line": best["line"], "actual": game_result.get(col), "outcome": outcome,
            })
    graded = hits + misses
    return {
        "n": hits + misses + no_data, "hits": hits, "misses": misses, "no_data": no_data,
        "hit_rate": round(hits / graded, 3) if graded else None, "examples": examples[:10],
    }


def _has_matchup_lean_data(report):
    for g in report.get("games") or []:
        for side_key in ("home", "away"):
            side = g.get(side_key) or {}
            for b in side.get("batters") or []:
                if "matchup_lean" in b:
                    return True
    return False


def _matchup_lean_source(conn, report, date):
    """
    matchup_lean/star_player_id didn't exist in archives written before
    those features shipped. If this archive already has them (any future
    day, once a fresh archive naturally includes them), use it directly --
    otherwise, only for TODAY specifically, recompute a fresh report to
    source them instead. See this module's own docstring for why that's
    safe for today but not for a backfilled past date.
    """
    if _has_matchup_lean_data(report):
        return report
    if date != mlb_today():
        return report  # nothing to grade; _grade_* will just find no matchup_lean/star_player_id fields and skip everyone
    return build_report(conn, days_ahead=0)


def grade_day(conn, date):
    report = _load_day_report(date)
    if report is None:
        return None
    top_overs = _picks_for_date(report.get("top_overs"), date)
    top_unders = _picks_for_date(report.get("top_unders"), date)
    # Backfills best_category on any pick frozen before resolve_best_category()
    # existed (a text-only "fallback_angle", or nothing at all) using
    # prop_categories already sitting in this same archived report -- the
    # live dashboard already self-heals this way (see _refresh_frozen_pick's
    # own docstring in build_props.py); grading read the raw archive
    # directly and skipped that step, so a day frozen before the fix shipped
    # stayed stuck showing "no data" for those picks forever.
    player_context = _player_context(report.get("games") or [])
    for pick in top_overs:
        _refresh_frozen_pick(pick, "over", player_context)
    # exclude_label: never let a refreshed under-pick land on the exact same
    # category+line this same player's over-pick already has (or vice versa)
    # -- same reasoning as build_top_picks()'s own exclude_label in build_props.py.
    over_category_by_player = {p["player_id"]: p["best_category"]["label"] for p in top_overs if p.get("best_category")}
    for pick in top_unders:
        _refresh_frozen_pick(pick, "under", player_context, exclude_label=over_category_by_player.get(pick["player_id"]))
    batter_trend, batter_matchup = _grade_batter_signals(conn, report, date)
    pitcher_form = _grade_pitcher_signals(conn, report, date)
    projection_accuracy, projection_examples = _grade_projections(conn, report, date)
    lean_source = _matchup_lean_source(conn, report, date)
    return {
        "date": date,
        "graded_at": datetime.now(timezone.utc).isoformat(),
        "top_overs": _grade_picks_bucket(conn, top_overs, "over"),
        "top_unders": _grade_picks_bucket(conn, top_unders, "under"),
        "batter_trend": batter_trend,
        "batter_matchup": batter_matchup,
        "pitcher_form": pitcher_form,
        "projection_accuracy": projection_accuracy,
        "projection_examples": projection_examples,
        "games": _grade_games(conn, report, date),
        "matchup_leans": _grade_matchup_leans(conn, lean_source, date),
        "best_prop_stars": _grade_best_prop_stars(conn, lean_source, date),
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


def _sum_hit_miss(days, path):
    """Same idea as top_overs/top_unders' own rollup, generalized for any hit/miss bucket (used for the game-level picks, which have no 'no_data' concept -- a game either finished or wasn't graded at all)."""
    hits = misses = 0
    for day in days:
        bucket = day
        for key in path:
            bucket = bucket.get(key) or {}
        hits += bucket.get("hits") or 0
        misses += bucket.get("misses") or 0
    graded = hits + misses
    return {"n": graded, "hits": hits, "misses": misses, "hit_rate": round(hits / graded, 3) if graded else None}


def _sum_accuracy_stats(days, path):
    """
    Game-count-weighted average of each of mae/bias/avg_projected/avg_actual,
    mirroring _sum_bucket_stats()'s own reasoning: each day's figure is
    already a per-item average, so weighting by that day's n and
    re-averaging is exact. avg_projected/avg_actual are only present on
    the per-stat-category buckets (not the game-level score_accuracy
    bucket) -- fields absent from a given bucket are just skipped.
    """
    fields = ("mae", "bias", "avg_projected", "avg_actual")
    sums = {f: 0.0 for f in fields}
    n = 0
    for day in days:
        bucket = day
        for key in path:
            bucket = bucket.get(key) or {}
        dn = bucket.get("n") or 0
        if not dn:
            continue
        n += dn
        for f in fields:
            if bucket.get(f) is not None:
                sums[f] += bucket[f] * dn
    if n == 0:
        return {"n": 0, **{f: None for f in fields}}
    return {"n": n, **{f: round(sums[f] / n, 2) for f in fields}}


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

    result["games"] = {
        "moneyline": _sum_hit_miss(days, ["games", "moneyline"]),
        "run_line": _sum_hit_miss(days, ["games", "run_line"]),
        "total": _sum_hit_miss(days, ["games", "total"]),
        "score_accuracy": _sum_accuracy_stats(days, ["games", "score_accuracy"]),
    }

    # Older graded days simply have no "matchup_leans"/"best_prop_stars" key
    # at all (graded before those signals existed) -- .get() defaults them
    # to zero contribution rather than erroring.
    for section in ("matchup_leans", "best_prop_stars"):
        hits = sum((d.get(section) or {}).get("hits", 0) for d in days)
        misses = sum((d.get(section) or {}).get("misses", 0) for d in days)
        no_data = sum((d.get(section) or {}).get("no_data", 0) for d in days)
        graded = hits + misses
        result[section] = {
            "days": len(days), "n": sum((d.get(section) or {}).get("n", 0) for d in days),
            "hits": hits, "misses": misses, "no_data": no_data,
            "hit_rate": round(hits / graded, 3) if graded else None,
        }

    all_categories = set()
    for d in days:
        all_categories.update((d.get("projection_accuracy") or {}).keys())
    result["projection_accuracy"] = {
        label: _sum_accuracy_stats(days, ["projection_accuracy", label]) for label in all_categories
    }

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
        games = result["games"]

        def _txt(b):
            return f"{b['hits']}/{b['n']} ({b['hit_rate']:.0%})" if b["hit_rate"] is not None else "no games"

        mae = games["score_accuracy"]["mae"]
        mae_txt = f"{mae} runs/game avg" if mae is not None else "no graded scores"
        print(f"{date}: graded. Top Overs {over_txt} | Top Unders {under_txt}")
        print(
            f"  Games -- moneyline {_txt(games['moneyline'])} | run line {_txt(games['run_line'])} | "
            f"total {_txt(games['total'])} | score off by {mae_txt}"
        )
    conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD to grade. Defaults to yesterday (US Eastern).")
    p.add_argument("--backfill-days", type=int, default=0, help="Also grade this many days before --date/yesterday.")
    args = p.parse_args()

    # US Eastern, not raw UTC -- UTC rolls over to "tomorrow" 4-5 hours
    # before the US baseball day is actually done, which used to make this
    # prematurely grade TODAY (still in progress, most games unplayed) as
    # if it were a finished day the moment UTC's calendar flipped. See
    # mlb_today()'s own docstring in db.py for the full story -- every
    # other "today" cutoff in the pipeline was already fixed this way.
    base = datetime.strptime(args.date, "%Y-%m-%d") if args.date else (datetime.strptime(mlb_today(), "%Y-%m-%d") - timedelta(days=1))
    dates = [(base - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(args.backfill_days + 1)]
    run(dates)
