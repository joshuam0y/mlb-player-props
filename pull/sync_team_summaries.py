"""
sync_team_summaries.py

Generates one short, AI-written "what to know before betting" blurb per
team currently in the report window (today through build_report()'s own
days_ahead), once per real calendar day -- not once per sync run. Every
fact handed to the model (record, batting/pitching averages vs league,
injuries, recent trades, recent headlines) is pulled straight from this
project's own already-synced data; the model's only job is turning real
numbers into a few sentences of prose, not inventing any of its own.

Idempotent by design: team_summaries has a (team_id, date) primary key,
so re-running this mid-day (it rides along in the same hourly workflow as
every other sync step) is a no-op for any team already generated today --
only a team with no row yet for today's date costs an actual API call.
This is what makes it safe to just add as another hourly.yml step instead
of needing its own separate daily-cron workflow.
"""

import argparse
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import anthropic

import api
from build_props import team_injury_report, team_streak_and_form
from db import get_conn, init_db, mlb_today

CURRENT_SEASON = datetime.now(timezone.utc).year
MODEL = "claude-haiku-4-5-20251001"
TRANSACTIONS_LOOKBACK_DAYS = 14
HEADLINES_LOOKBACK_DAYS = 10
HEADLINES_LIMIT = 5


def _season_record(conn, team_id, season, before_date):
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN home_team_id = ? AND home_score > away_score THEN 1
                    WHEN away_team_id = ? AND away_score > home_score THEN 1
                    ELSE 0 END) AS wins,
          SUM(CASE WHEN home_score IS NOT NULL THEN 1 ELSE 0 END) AS games
        FROM games
        WHERE (home_team_id = ? OR away_team_id = ?)
          AND official_date LIKE ? AND official_date < ?
          AND home_score IS NOT NULL
        """,
        (team_id, team_id, team_id, team_id, f"{season}%", before_date),
    ).fetchone()
    games = row["games"] or 0
    wins = row["wins"] or 0
    return {"wins": wins, "losses": games - wins, "games": games}


def _batting_totals(conn, team_id, season, before_date):
    """team_id=None aggregates across the whole league, as a comparison baseline."""
    where = "season = ? AND date < ?"
    params = [season, before_date]
    if team_id is not None:
        where = "team_id = ? AND " + where
        params = [team_id] + params
    row = conn.execute(
        f"SELECT SUM(hits) h, SUM(at_bats) ab, SUM(total_bases) tb, SUM(home_runs) hr, SUM(runs) r "
        f"FROM batting_game_logs WHERE {where}",
        params,
    ).fetchone()
    ab = row["ab"] or 0
    if not ab:
        return None
    return {
        "avg": (row["h"] or 0) / ab,
        "slg": (row["tb"] or 0) / ab,
        "hr": row["hr"] or 0,
        "runs": row["r"] or 0,
    }


def _pitching_totals(conn, team_id, season, before_date):
    where = "season = ? AND date < ?"
    params = [season, before_date]
    if team_id is not None:
        where = "team_id = ? AND " + where
        params = [team_id] + params
    row = conn.execute(
        f"SELECT SUM(earned_runs) er, SUM(outs) outs, SUM(hits) h, SUM(base_on_balls) bb "
        f"FROM pitching_game_logs WHERE {where}",
        params,
    ).fetchone()
    outs = row["outs"] or 0
    if not outs:
        return None
    return {
        "era": (row["er"] or 0) * 27 / outs,
        "whip": ((row["h"] or 0) + (row["bb"] or 0)) * 3 / outs,
    }


def _recent_trades(conn, team_id):
    """
    A single multi-player trade comes back from the transactions API as one
    row PER PLAYER moved, each carrying the exact same shared description
    text -- e.g. a 3-player deal shows up 3 times. Dedup on description so
    one real trade doesn't get restated to the model (and read back by the
    user) as though it happened multiple times.
    """
    end = mlb_today()
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=TRANSACTIONS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    txns = api.get_transactions(start, end)
    seen = set()
    out = []
    for t in txns:
        if t.get("typeDesc") != "Trade":
            continue
        from_team = (t.get("fromTeam") or {}).get("id")
        to_team = (t.get("toTeam") or {}).get("id")
        if team_id not in (from_team, to_team):
            continue
        desc = t.get("description") or ""
        if desc in seen:
            continue
        seen.add(desc)
        out.append(desc)
    return out


def _recent_headlines(conn, team_id):
    player_ids = {
        str(r["player_id"]) for r in conn.execute("SELECT player_id FROM players WHERE current_team_id = ?", (team_id,))
    }
    if not player_ids:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=HEADLINES_LOOKBACK_DAYS)
    rows = conn.execute("SELECT title, pub_date, matched_player_ids FROM headlines ORDER BY rowid DESC LIMIT 500").fetchall()
    out = []
    for r in rows:
        ids = set((r["matched_player_ids"] or "").split(","))
        if not (ids & player_ids):
            continue
        try:
            pub = parsedate_to_datetime(r["pub_date"])
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if pub < cutoff:
            continue
        out.append(r["title"])
        if len(out) >= HEADLINES_LIMIT:
            break
    return out


def gather_team_facts(conn, team_id, team_name):
    today = mlb_today()
    season_record = _season_record(conn, team_id, CURRENT_SEASON, today)
    last_10 = team_streak_and_form(conn, team_id, as_of_date=today)
    team_bat = _batting_totals(conn, team_id, CURRENT_SEASON, today)
    league_bat = _batting_totals(conn, None, CURRENT_SEASON, today)
    team_pitch = _pitching_totals(conn, team_id, CURRENT_SEASON, today)
    league_pitch = _pitching_totals(conn, None, CURRENT_SEASON, today)
    injuries = team_injury_report(conn, team_id)
    trades = _recent_trades(conn, team_id)
    headlines = _recent_headlines(conn, team_id)
    return {
        "team_name": team_name,
        "season_record": season_record,
        "last_10": last_10,
        "team_batting": team_bat,
        "league_batting": league_bat,
        "team_pitching": team_pitch,
        "league_pitching": league_pitch,
        "injuries": injuries,
        "trades": trades,
        "headlines": headlines,
    }


def _facts_to_prompt(facts):
    lines = [f"Team: {facts['team_name']}"]
    sr = facts["season_record"]
    if sr and sr["games"]:
        lines.append(f"Season record: {sr['wins']}-{sr['losses']} ({sr['games']} games)")
    l10 = facts["last_10"]
    if l10:
        lines.append(f"Last {l10['record_games']} games: {l10['wins']}-{l10['losses']}, run differential {l10['run_diff']:+d}")
        if l10["streak"] != 0:
            kind = "win" if l10["streak"] > 0 else "loss"
            lines.append(f"Current streak: {abs(l10['streak'])}-game {kind} streak")
    tb, lb = facts["team_batting"], facts["league_batting"]
    if tb and lb:
        lines.append(
            f"Team batting: .{int(tb['avg']*1000):03d} AVG / .{int(tb['slg']*1000):03d} SLG / {tb['hr']} HR "
            f"(league average: .{int(lb['avg']*1000):03d} AVG / .{int(lb['slg']*1000):03d} SLG)"
        )
    tp, lp = facts["team_pitching"], facts["league_pitching"]
    if tp and lp:
        lines.append(
            f"Team pitching: {tp['era']:.2f} ERA / {tp['whip']:.2f} WHIP "
            f"(league average: {lp['era']:.2f} ERA / {lp['whip']:.2f} WHIP)"
        )
    if facts["injuries"]:
        names = ", ".join(f"{i['player_name']} ({i['status']})" for i in facts["injuries"][:6])
        lines.append(f"Currently on the injured list: {names}")
    if facts["trades"]:
        lines.append("Recent trades: " + " | ".join(facts["trades"][:3]))
    if facts["headlines"]:
        lines.append("Recent headlines: " + " | ".join(facts["headlines"]))
    return "\n".join(lines)


PROMPT_TEMPLATE = """You are writing a short, factual pre-game briefing for a sports bettor about one MLB team. Use ONLY the facts listed below -- never invent a stat, injury, trade, or record that isn't given. If a fact is missing, just don't mention it.

Write 3-5 sentences (under 100 words): cover their current form/record, how their batting and pitching compare to league average, and anything notable from injuries/trades/headlines that a bettor should factor in. Plain, direct tone -- no hype, no filler like "let's dive in". Do not use markdown formatting.

Facts:
{facts}
"""


def generate_summary(client, facts):
    prompt = PROMPT_TEMPLATE.format(facts=_facts_to_prompt(facts))
    resp = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def _teams_in_window(conn, days_ahead=2):
    today = mlb_today()
    end = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT DISTINCT home_team_id AS team_id FROM games WHERE official_date BETWEEN ? AND ? "
        "UNION SELECT DISTINCT away_team_id FROM games WHERE official_date BETWEEN ? AND ?",
        (today, end, today, end),
    ).fetchall()
    return [r["team_id"] for r in rows if r["team_id"]]


def run(days_ahead=2):
    init_db()
    conn = get_conn()
    today = mlb_today()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set -- skipping team summary generation.")
        conn.close()
        return 0
    client = anthropic.Anthropic(api_key=api_key)

    team_ids = _teams_in_window(conn, days_ahead=days_ahead)
    generated = 0
    for team_id in team_ids:
        existing = conn.execute(
            "SELECT 1 FROM team_summaries WHERE team_id = ? AND date = ?", (team_id, today)
        ).fetchone()
        if existing:
            continue
        team_row = conn.execute("SELECT name FROM teams WHERE team_id = ?", (team_id,)).fetchone()
        if not team_row:
            continue
        try:
            facts = gather_team_facts(conn, team_id, team_row["name"])
            summary = generate_summary(client, facts)
        except Exception as e:
            # A single team's generation failing (rate limit, API hiccup) shouldn't
            # block every other team -- this whole feature is a nice-to-have on top
            # of the real, free-data-driven dashboard, not something worth a hard failure.
            print(f"Failed to generate summary for team {team_id}: {e}")
            continue
        conn.execute(
            """
            INSERT INTO team_summaries (team_id, date, summary, generated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(team_id, date) DO UPDATE SET summary=excluded.summary, generated_at=excluded.generated_at
            """,
            (team_id, today, summary, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        generated += 1
    conn.close()
    print(f"Generated {generated} new team summaries ({len(team_ids)} teams in window).")
    return generated


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days-ahead", type=int, default=2)
    args = parser.parse_args()
    run(days_ahead=args.days_ahead)
