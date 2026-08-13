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
from datetime import datetime, timedelta, timezone

import api

_firebase_app = None

EARLY_WIN_LEAD_THRESHOLD = 2  # runs -- the "2-up early win" token's own trigger


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


def _max_lead_from_linescore(linescore, side):
    """
    The biggest lead `side` ('home'/'away') ever held at any point in the
    game -- reconstructed by walking each half-inning's own runs (the only
    field games.home_score/away_score never captures, since that table
    only ever records the FINAL score). Needed for the "2-up early win"
    token: whether a team went up 2+ at some point is a fact about the
    game's whole history, not just how it ended.
    """
    innings = (linescore or {}).get("innings") or []
    home_cum = away_cum = 0
    max_lead = 0
    for inn in innings:
        away_runs = (inn.get("away") or {}).get("runs")
        if away_runs is not None:
            away_cum += away_runs
            max_lead = max(max_lead, (away_cum - home_cum) if side == "away" else (home_cum - away_cum))
        home_runs = (inn.get("home") or {}).get("runs")
        if home_runs is not None:
            home_cum += home_runs
            max_lead = max(max_lead, (home_cum - away_cum) if side == "home" else (away_cum - home_cum))
    return max_lead


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
    the game is genuinely Final, no early exception -- EXCEPT the
    "2-up early win" token below, a deliberate, explicit override: a real
    FanDuel promo mechanic where a Moneyline pick is locked in as a win
    the instant the picked team leads by 2+ runs at any point, even if
    they go on to lose. That's not a loophole in the reasoning above --
    it's the one case where a blown lead genuinely doesn't matter anymore,
    because the token already paid out on the lead itself.
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

    if leg["category"] == "Moneyline" and leg.get("early_win_token") and not leg.get("early_win_triggered"):
        try:
            linescore = api.get_linescore(game_pk)
        except Exception:
            linescore = None
        if linescore and _max_lead_from_linescore(linescore, side) >= EARLY_WIN_LEAD_THRESHOLD:
            leg["status"] = "hit"
            leg["early_win_triggered"] = True
            return True

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


def regrade_recent(conn, days=3):
    """
    Self-heals a real failure mode that regrade_all_pending() above can't
    touch: _grade_player_leg() reads the player's actual stat line from the
    production DB's batting_game_logs/pitching_game_logs, keyed off that
    same DB's games.status to decide is_final. If a game's status flips to
    Final slightly before that specific player's own complete box-score row
    finishes syncing (a real, confirmed sync-timing gap -- sync_schedule.py
    and sync_stats.py run as separate, independently-timed steps), a leg
    can get permanently locked in "hit" against a stat total that was only
    correct partway through the game. Confirmed on a real case: a batter's
    game was Final and the DB still held his total-bases count from after
    his FIRST hit, one hit short of his actual final total -- enough to
    flip a real bust into a wrongly-recorded hit forever, since
    regrade_all_pending() only ever re-examines bets still sitting at
    "pending" and this leg had already (wrongly) resolved.

    Re-checks every already-resolved player leg placed within the last
    `days` days on every run, regardless of its current hit/miss status --
    same "recompute the recent window fresh every time" principle
    grade_picks.py's own --backfill-days uses for the identical class of
    problem. Forces each leg back to "pending" before re-deriving its
    result so _grade_player_leg()'s own guard actually re-runs the full
    check against whatever the DB shows *now*; if the DB still can't
    produce a fresh hit/miss (e.g. a false alarm, or a mid-flight edge
    case), the leg's previous status/actual_value/dnp are restored
    untouched rather than left incorrectly stuck at "pending".
    """
    db = _get_firestore_client()
    if db is None:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    # Single-field filter only (status IN [...]) -- adding a second
    # .where() on placed_date here (a range filter on a DIFFERENT field)
    # would need a manually-provisioned Firestore composite index, which
    # this project has deliberately avoided needing anywhere else (see
    # render_my_bets.py's own onSnapshot query and its comment on exactly
    # this). Filtering by date in Python instead avoids that entirely --
    # a personal bet tracker's total bet count is small enough that
    # fetching every resolved bet and filtering here is cheap.
    all_resolved = list(db.collection("bets").where("status", "in", ["won", "lost"]).stream())
    docs = [doc for doc in all_resolved if doc.to_dict().get("placed_date", "") >= cutoff]
    corrected = 0
    for doc in docs:
        bet = doc.to_dict()
        legs = bet.get("legs") or []
        any_changed = False
        for leg in legs:
            # Only player-prop legs have this failure mode -- a game leg's
            # own grading (_grade_game_leg) keys off the game's single,
            # atomic final score, not a per-player stat row that can lag
            # the game's own status.
            if leg.get("kind") == "game" or leg.get("status") not in ("hit", "miss") or not leg.get("player_id"):
                continue
            previous_status = leg["status"]
            previous_actual = leg.get("actual_value")
            had_dnp = "dnp" in leg
            previous_dnp = leg.get("dnp")
            leg["status"] = "pending"
            _grade_player_leg(conn, leg, bet["placed_date"])
            if leg["status"] not in ("hit", "miss"):
                leg["status"] = previous_status
                leg["actual_value"] = previous_actual
                if had_dnp:
                    leg["dnp"] = previous_dnp
                else:
                    leg.pop("dnp", None)
                continue
            if leg["status"] != previous_status or leg.get("actual_value") != previous_actual:
                any_changed = True
                print(
                    f"regrade_recent: corrected {leg.get('player_name')} {leg.get('category')} "
                    f"{previous_status}/{previous_actual} -> {leg['status']}/{leg.get('actual_value')} "
                    f"(bet {doc.id})"
                )
        if not any_changed:
            continue
        corrected += 1
        statuses = [leg["status"] for leg in legs]
        new_status = "lost" if any(s == "miss" for s in statuses) else ("won" if all(s == "hit" for s in statuses) else bet["status"])
        update = {"legs": legs, "graded_at": datetime.now(timezone.utc).isoformat()}
        if new_status != bet["status"]:
            update["status"] = new_status
        doc.reference.update(update)
    if corrected:
        print(f"regrade_recent: corrected {corrected} bet(s) out of {len(docs)} checked (of {len(all_resolved)} resolved total).")
