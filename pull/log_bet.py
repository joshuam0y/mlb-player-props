"""
log_bet.py

Personal bet tracker: lets you record a real wager you placed (FanDuel or
otherwise) -- who, what prop, what line, what direction, the odds, the
stake, and the profit if it wins -- and automatically grades it against
real results as games finish, the same way every other pick on this site
gets graded (reuses pick_result()/batter_game_result()/pitcher_game_result()
from build_props.py directly, so a leg here can never disagree with what
the live dashboard itself would say about the same player+category+line).

Storage is a single git-committed JSON file (output/user_bets.json), NOT a
row in mlb_props.db -- that database is a disposable Actions-cache-only
scratch space (see db.py's own docstring), wrong for the one thing on this
whole site that has to survive forever no matter what. A bet is the
permanent record; the database is only ever read here, never written, to
resolve player names and look up actual results.

A "bet" is one or more "legs" (a straight bet has one leg; a parlay has
several) -- the bet only settles "won" once every leg has settled "hit",
and settles "lost" the moment any single leg misses, mirroring how a real
parlay slip actually works.

Run standalone to log a new bet:
    python pull/log_bet.py --date 2026-07-27 --stake 5 --to-win 5.75 \\
        --odds +115 --legs "Cole Young | Total Bases | 1.5 | over"

For a parlay, one leg per line in --legs:
    --legs "Kirby | Outs Recorded | 15.5 | over
Nick Lopez | Hits | 0.5 | over"

Every run also regrades every still-pending leg across ALL previously
logged bets (idempotent, same reasoning as grade_picks.py re-grading
recent days on every hourly run) -- so a bet logged before its game
finished gets its result filled in automatically on a later run, without
needing to re-invoke this with the same bet again.
"""

import argparse
import json
import os
from datetime import datetime, timezone

from build_props import (
    BATTER_PROP_CATEGORIES,
    FINAL_STATUSES,
    PITCHER_PROP_CATEGORIES,
    _BATTER_CATEGORY_FIELD,
    _game_pk_for_date,
    _game_pk_for_team_and_date,
    _game_status,
    _PITCHER_CATEGORY_FIELD,
    batter_game_result,
    build_report,
    pick_result,
    pitcher_game_result,
)
from db import get_conn, init_db, mlb_today

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
BETS_PATH = os.path.join(OUT_DIR, "user_bets.json")

_CATEGORY_ROLE = {label: "batter" for _, label in BATTER_PROP_CATEGORIES}
_CATEGORY_ROLE.update({label: "pitcher" for _, label in PITCHER_PROP_CATEGORIES})
VALID_CATEGORIES = sorted(_CATEGORY_ROLE)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _load_bets():
    if not os.path.exists(BETS_PATH):
        return {"bets": [], "updated_at": None}
    with open(BETS_PATH) as f:
        return json.load(f)


def _save_bets(record):
    os.makedirs(OUT_DIR, exist_ok=True)
    record["updated_at"] = _now_iso()
    with open(BETS_PATH, "w") as f:
        json.dump(record, f, indent=2, default=str)


def _tokens_match(input_tokens, candidate_tokens):
    """
    Every input token must be a prefix of the candidate's token in the
    same position -- handles nickname/shortened-first-name variations
    ("Nick Lopez" -> "Nicky Lopez", "Matt Chapman" -> "Matthew Chapman")
    that a plain substring match misses (a straight "%nick lopez%" LIKE
    doesn't match "Nicky Lopez" -- the "y" breaks the contiguous
    substring). Candidate can have MORE tokens than the input (a suffix
    like "Jr." shouldn't block a match), but not fewer.
    """
    if len(input_tokens) > len(candidate_tokens):
        return False
    return all(candidate_tokens[i].startswith(input_tokens[i]) for i in range(len(input_tokens)))


def resolve_player(conn, name):
    """
    Exact case-insensitive full_name match first; then a contains-match
    among active players for a bare last name (e.g. "Kirby"); then a
    token-prefix match for nickname/shortened-name variations the
    contains-match can't catch. Ambiguous matches use the first hit and
    print a warning rather than failing outright -- this is a personal
    tracking tool, not a system that should block you from logging a bet
    over a name lookup.
    """
    name = name.strip()
    row = conn.execute(
        "SELECT player_id, full_name FROM players WHERE lower(full_name) = lower(?)", (name,)
    ).fetchone()
    if row:
        return row["player_id"], row["full_name"]
    rows = conn.execute(
        "SELECT player_id, full_name FROM players WHERE lower(full_name) LIKE lower(?) AND active = 1",
        (f"%{name}%",),
    ).fetchall()
    if not rows:
        input_tokens = name.lower().split()
        all_active = conn.execute("SELECT player_id, full_name FROM players WHERE active = 1").fetchall()
        rows = [r for r in all_active if _tokens_match(input_tokens, r["full_name"].lower().split())]
    if len(rows) == 1:
        return rows[0]["player_id"], rows[0]["full_name"]
    if len(rows) > 1:
        names = ", ".join(r["full_name"] for r in rows)
        print(f"WARNING: '{name}' matched {len(rows)} players ({names}) -- using {rows[0]['full_name']}")
        return rows[0]["player_id"], rows[0]["full_name"]
    print(f"WARNING: no player found matching '{name}' -- leg recorded without a player_id (no grading/model comparison possible)")
    return None, name


def _model_snapshot(report, player_id, category_label):
    """Best-effort current model projection/line/lean for this exact player+category, for side-by-side display against what was actually bet. None if the player isn't in the current report window at all."""
    if not player_id:
        return None, None, None
    for g in report.get("games") or []:
        for side_key in ("home", "away"):
            side = g[side_key]
            entities = list(side.get("batters") or [])
            if side.get("probable_pitcher"):
                entities.append(side["probable_pitcher"])
            for entity in entities:
                if entity.get("player_id") != player_id:
                    continue
                for cat in entity.get("prop_categories") or []:
                    if cat["label"] == category_label:
                        return cat.get("today_projection"), cat.get("primary_line"), cat.get("lean")
    return None, None, None


def parse_legs(legs_text):
    legs = []
    for line in legs_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 4:
            raise ValueError(f"Malformed leg (expected 'Player | Category | Line | over/under'): {line!r}")
        player_name, category, line_str, direction = parts
        direction = direction.lower()
        if direction not in ("over", "under"):
            raise ValueError(f"Direction must be 'over' or 'under', got {direction!r} in: {line!r}")
        if category not in _CATEGORY_ROLE:
            raise ValueError(f"Unknown category {category!r} in: {line!r} -- must be one of {VALID_CATEGORIES}")
        legs.append({"player_name": player_name, "category": category, "line": float(line_str), "direction": direction})
    if not legs:
        raise ValueError("No legs parsed from --legs")
    return legs


def add_bet(conn, date, stake, to_win, odds, legs_text, sportsbook="FanDuel"):
    record = _load_bets()
    parsed = parse_legs(legs_text)
    report = build_report(conn, days_ahead=2)

    legs = []
    for leg in parsed:
        role = _CATEGORY_ROLE[leg["category"]]
        player_id, full_name = resolve_player(conn, leg["player_name"])
        proj, model_line, lean = _model_snapshot(report, player_id, leg["category"])
        legs.append({
            "player_name": full_name,
            "player_id": player_id,
            "role": role,
            "category": leg["category"],
            "line": leg["line"],
            "direction": leg["direction"],
            "game_pk": None,
            "status": "pending",
            "actual_value": None,
            "model_projection": proj,
            "model_line": model_line,
            "model_lean": lean,
        })

    bet_id = max((b["id"] for b in record["bets"]), default=0) + 1
    bet = {
        "id": bet_id,
        "placed_date": date,
        "sportsbook": sportsbook,
        "stake": stake,
        "to_win": to_win,
        "odds": odds,
        "status": "pending",
        "created_at": _now_iso(),
        "graded_at": None,
        "legs": legs,
    }
    record["bets"].append(bet)
    _save_bets(record)
    print(f"Logged bet #{bet_id}: {len(legs)} leg(s), ${stake} to win ${to_win}")
    return bet_id


def _leg_field(role, category):
    return (_BATTER_CATEGORY_FIELD if role == "batter" else _PITCHER_CATEGORY_FIELD).get(category)


def _grade_leg(conn, leg, placed_date):
    if leg["status"] != "pending" or not leg.get("player_id"):
        return
    game_pk = leg.get("game_pk") or _game_pk_for_date(conn, leg["role"], leg["player_id"], placed_date) or _game_pk_for_team_and_date(conn, leg["player_id"], placed_date)
    if not game_pk:
        return
    leg["game_pk"] = game_pk
    status = _game_status(conn, game_pk)
    is_final = status in FINAL_STATUSES
    game_result = (batter_game_result if leg["role"] == "batter" else pitcher_game_result)(conn, leg["player_id"], game_pk)
    category = {"label": leg["category"], "line": leg["line"]}
    outcome = pick_result(leg["role"], category, game_result, leg["direction"], is_final)
    if outcome in ("hit", "miss"):
        leg["status"] = outcome
        field = _leg_field(leg["role"], leg["category"])
        leg["actual_value"] = game_result.get(field) if game_result and field else None
    elif game_result is None and status not in (None, "Scheduled", "Pre-Game", "Preview", "Warmup"):
        leg["dnp"] = True


def regrade_all(conn):
    """Re-grades every pending leg of every bet -- idempotent, safe to call on every build_props.py run so bets settle automatically as games finish, not just when a new bet happens to be logged."""
    record = _load_bets()
    changed = False
    for bet in record["bets"]:
        if bet["status"] != "pending":
            continue
        for leg in bet["legs"]:
            before = leg["status"]
            _grade_leg(conn, leg, bet["placed_date"])
            if leg["status"] != before:
                changed = True
        statuses = [leg["status"] for leg in bet["legs"]]
        if any(s == "miss" for s in statuses):
            bet["status"] = "lost"
            bet["graded_at"] = _now_iso()
            changed = True
        elif all(s == "hit" for s in statuses):
            bet["status"] = "won"
            bet["graded_at"] = _now_iso()
            changed = True
    if changed:
        _save_bets(record)
    return record


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None, help="Game date, YYYY-MM-DD. Defaults to today.")
    p.add_argument("--stake", type=float, required=True)
    p.add_argument("--to-win", type=float, required=True, help="Profit if the bet wins (not total payout).")
    p.add_argument("--odds", default=None, help="American odds as shown on the sportsbook, e.g. +115")
    p.add_argument("--legs", required=True, help="One leg per line: 'Player | Category | Line | over/under'")
    p.add_argument("--sportsbook", default="FanDuel")
    args = p.parse_args()

    init_db()
    conn = get_conn()
    add_bet(conn, args.date or mlb_today(), args.stake, args.to_win, args.odds, args.legs, args.sportsbook)
    regrade_all(conn)
    conn.close()
