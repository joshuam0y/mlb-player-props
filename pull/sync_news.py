"""
sync_news.py

Pulls two free, keyless, official/reliable context sources so the
dashboard shows more than raw stat lines:

1. Injuries -- MLB's own transactions feed. "Status Change" transactions
   whose description mentions the injured list are reliable, structured
   injury signal (no news-scraping/LLM summarization needed).
2. Headlines -- MLB.com's public news RSS feed, matched against tracked
   player names by simple substring match on the title.
"""

import re
from datetime import datetime, timedelta

import api
from db import get_conn, init_db, mlb_today


def _classify_status(description):
    d = (description or "").lower()
    if "injured list" in d and "activated" in d:
        return "activated"
    if "injured list" in d:
        return "IL"
    return None


def sync_injuries(conn, days_back=14):
    end = mlb_today()
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=days_back)).strftime("%Y-%m-%d")
    txns = api.get_transactions(start, end)

    rows = []
    for t in txns:
        if t.get("typeDesc") != "Status Change":
            continue
        status = _classify_status(t.get("description"))
        if not status:
            continue
        person = t.get("person") or {}
        team = t.get("toTeam") or t.get("fromTeam") or {}
        rows.append(
            (
                t["id"],
                person.get("id"),
                person.get("fullName"),
                team.get("id"),
                t.get("date"),
                status,
                t.get("description"),
            )
        )

    if rows:
        conn.executemany(
            """
            INSERT INTO injuries (transaction_id, player_id, player_name, team_id, date, status, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transaction_id) DO UPDATE SET status=excluded.status, description=excluded.description
            """,
            rows,
        )
        conn.commit()
    return len(rows)


def sync_headlines(conn):
    items = api.get_league_news()
    players = conn.execute(
        """
        SELECT p.player_id, p.full_name, t.name AS team_name
        FROM players p LEFT JOIN teams t ON t.team_id = p.current_team_id
        WHERE p.active = 1
        """
    ).fetchall()
    # crude but effective: match whole player names as a substring of the
    # headline title -- grouped by name (not a plain name->id dict) because
    # two active players can share an exact full name (a real, recurring
    # MLB occurrence); a dict comprehension would silently keep only
    # whichever one was iterated last and attribute every matching
    # headline to them, even when it's actually about the other player.
    by_name = {}
    for row in players:
        by_name.setdefault(row["full_name"], []).append((row["player_id"], row["team_name"]))

    rows = []
    for item in items:
        title = item["title"]
        matched = []
        for name, candidates in by_name.items():
            if not name or not re.search(re.escape(name), title):
                continue
            if len(candidates) == 1:
                matched.append(str(candidates[0][0]))
                continue
            # Shared name: only attribute the headline to a candidate whose
            # own team is also named in the title. If that's ambiguous too
            # (none or more than one team matches), there's no reliable way
            # to tell them apart from the title text alone -- skip rather
            # than guess and risk crediting the wrong player's news.
            team_matches = [pid for pid, team_name in candidates if team_name and re.search(re.escape(team_name), title)]
            if len(team_matches) == 1:
                matched.append(str(team_matches[0]))
        rows.append((item["link"], title, item["pub_date"], item["creator"], ",".join(matched)))

    if rows:
        conn.executemany(
            """
            INSERT INTO headlines (link, title, pub_date, creator, matched_player_ids)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(link) DO UPDATE SET title=excluded.title, matched_player_ids=excluded.matched_player_ids
            """,
            rows,
        )
        conn.commit()
    return len(rows)


def run():
    init_db()
    conn = get_conn()
    n_inj = sync_injuries(conn)
    n_news = sync_headlines(conn)
    conn.close()
    print(f"Synced {n_inj} injury transactions, {n_news} headlines.")


if __name__ == "__main__":
    run()
