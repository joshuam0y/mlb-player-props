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
    players = conn.execute("SELECT player_id, full_name FROM players WHERE active = 1").fetchall()
    # crude but effective: match whole player names as a substring of the headline title
    name_to_id = {row["full_name"]: row["player_id"] for row in players}

    rows = []
    for item in items:
        title = item["title"]
        matched = [str(pid) for name, pid in name_to_id.items() if name and re.search(re.escape(name), title)]
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
