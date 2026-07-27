"""
sync_bets_firestore.py

Server-side half of the personal bet tracker. The client (my-bets.html)
signs in with Firebase Auth and writes new bets directly to Firestore,
each tagged with the signed-in user's UID; this script is the ONLY thing
that ever grades them, reusing pick_result()/batter_game_result()/
pitcher_game_result() directly from build_props.py so a bet leg can
never disagree with what the live dashboard says about the same
player+category+line. Firestore's own security rules (see the setup
docs given to the user) deny client-side updates entirely -- only this
script, authenticated via a service account (Admin SDK access bypasses
security rules), may ever change a bet's status once it's created.

Requires the FIREBASE_SERVICE_ACCOUNT environment variable: the full
JSON contents of a Firebase service account key (Project Settings ->
Service accounts -> Generate new private key). Never committed to the
repo -- passed in only as a GitHub Actions secret, never seen by this
codebase's own author. If it's not set (e.g. running this pipeline
locally without Firebase configured), every function here is a no-op --
the bet tracker is optional, not a hard dependency of the rest of the
pipeline (mirrors continue-on-error's own reasoning elsewhere in this
project: one optional piece being unavailable should never block
anything else from running).
"""

import json
import os
from datetime import datetime, timezone

_firebase_app = None


def _get_firestore_client():
    """Lazily initializes the Firebase Admin SDK. Returns None (not an error) if FIREBASE_SERVICE_ACCOUNT isn't set -- see module docstring."""
    global _firebase_app
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not raw:
        return None
    import firebase_admin
    from firebase_admin import credentials, firestore

    if _firebase_app is None:
        cred = credentials.Certificate(json.loads(raw))
        _firebase_app = firebase_admin.initialize_app(cred)
    return firestore.client()


def _leg_field(role, category):
    from build_props import _BATTER_CATEGORY_FIELD, _PITCHER_CATEGORY_FIELD

    return (_BATTER_CATEGORY_FIELD if role == "batter" else _PITCHER_CATEGORY_FIELD).get(category)


def _grade_player_leg(conn, leg, placed_date):
    """Mutates leg in place; returns True if anything about it changed."""
    from build_props import (
        FINAL_STATUSES,
        _game_pk_for_date,
        _game_pk_for_team_and_date,
        _game_status,
        batter_game_result,
        pick_result,
        pitcher_game_result,
    )

    if leg.get("status") != "pending" or not leg.get("player_id"):
        return False
    game_pk = (
        leg.get("game_pk")
        or _game_pk_for_date(conn, leg["role"], leg["player_id"], placed_date)
        or _game_pk_for_team_and_date(conn, leg["player_id"], placed_date)
    )
    if not game_pk:
        return False
    changed = leg.get("game_pk") != game_pk
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
        return True
    if game_result is None and status not in (None, "Scheduled", "Pre-Game", "Preview", "Warmup") and not leg.get("dnp"):
        leg["dnp"] = True
        return True
    return changed


def _game_pk_and_side_for_team(conn, team_id, date):
    row = conn.execute(
        "SELECT game_pk, home_team_id FROM games WHERE official_date = ? AND ? IN (home_team_id, away_team_id)",
        (date, team_id),
    ).fetchone()
    if not row:
        return None, None
    return row["game_pk"], ("home" if row["home_team_id"] == team_id else "away")


def _grade_game_leg(conn, leg, placed_date):
    """
    Mutates leg in place; returns True if anything about it changed.
    Deliberately does NOT use pick_result()'s "cleared is permanent, safe
    to show early" asymmetry -- that relies on the graded stat only ever
    increasing over a game (a hit total can't go down), which is true for
    every player prop category but NOT true for a team's game outcome: a
    5th-inning lead can still get blown, a run differential can shrink,
    so "currently ahead" is never a safe permanent fact the way "already
    has 2 hits" is. Every game-prop category here only ever grades once
    the game is genuinely Final, no early exception.
    """
    from build_props import FINAL_STATUSES

    if leg.get("status") != "pending" or not leg.get("team_id"):
        return False
    game_pk, side = leg.get("game_pk"), leg.get("side")
    if not game_pk or not side:
        game_pk, side = _game_pk_and_side_for_team(conn, leg["team_id"], placed_date)
        if not game_pk:
            return False
    changed = leg.get("game_pk") != game_pk
    leg["game_pk"], leg["side"] = game_pk, side
    row = conn.execute("SELECT home_score, away_score, status FROM games WHERE game_pk = ?", (game_pk,)).fetchone()
    if not row or row["status"] not in FINAL_STATUSES or row["home_score"] is None or row["away_score"] is None:
        return changed
    home_score, away_score = row["home_score"], row["away_score"]
    team_score = home_score if side == "home" else away_score
    opp_score = away_score if side == "home" else home_score
    category = leg["category"]
    if category == "Moneyline":
        leg["status"] = "hit" if team_score > opp_score else "miss"
        leg["actual_value"] = team_score - opp_score
    elif category == "Run Line":
        margin = team_score - opp_score
        leg["status"] = "hit" if margin > -leg["line"] else "miss"
        leg["actual_value"] = margin
    elif category == "Total":
        total = home_score + away_score
        cleared = total > leg["line"]
        leg["status"] = "hit" if (cleared if leg["direction"] == "over" else not cleared) else "miss"
        leg["actual_value"] = total
    else:
        return changed
    return True


def _grade_leg(conn, leg, placed_date):
    if leg.get("kind") == "game":
        return _grade_game_leg(conn, leg, placed_date)
    return _grade_player_leg(conn, leg, placed_date)


def regrade_all_pending(conn):
    """
    Regrades every pending leg of every still-pending bet in Firestore --
    idempotent, safe to call on every build_props.py run, same reasoning
    as grade_picks.py re-grading recent days on every hourly run. No-op
    (not an error) if Firebase isn't configured for this environment.
    """
    db = _get_firestore_client()
    if db is None:
        return
    docs = list(db.collection("bets").where("status", "==", "pending").stream())
    for doc in docs:
        bet = doc.to_dict()
        legs = bet.get("legs") or []
        any_changed = False
        for leg in legs:
            if _grade_leg(conn, leg, bet["placed_date"]):
                any_changed = True
        statuses = [leg["status"] for leg in legs]
        new_status = bet["status"]
        if any(s == "miss" for s in statuses):
            new_status = "lost"
        elif statuses and all(s == "hit" for s in statuses):
            new_status = "won"
        if any_changed or new_status != bet["status"]:
            update = {"legs": legs}
            if new_status != bet["status"]:
                update["status"] = new_status
                update["graded_at"] = datetime.now(timezone.utc).isoformat()
            doc.reference.update(update)
