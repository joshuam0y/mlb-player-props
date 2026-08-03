"""
api.py

Thin wrapper around the free, public, unauthenticated MLB Stats API
(https://statsapi.mlb.com/api/v1/). No API key required. This is an
unofficial/undocumented-but-public API with no published rate limit --
REQUEST_DELAY_SECONDS keeps us a polite, low-volume client.
"""

import time
import xml.etree.ElementTree as ET

import requests

BASE = "https://statsapi.mlb.com/api/v1"
LIVE_BASE = "https://statsapi.mlb.com/api/v1.1"
NEWS_RSS_URL = "https://www.mlb.com/feeds/news/rss.xml"
REQUEST_DELAY_SECONDS = 0.3
TIMEOUT = 30
MAX_RETRIES = 3

_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0 (mlb-player-props research script)"})


def _get(path, params=None, base=BASE):
    url = f"{base}{path}"
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = _session.get(url, params=params or {}, timeout=TIMEOUT)
            if resp.status_code == 200:
                time.sleep(REQUEST_DELAY_SECONDS)
                return resp.json()
            last_err = RuntimeError(f"HTTP {resp.status_code} for {url}: {resp.text[:300]}")
        except requests.RequestException as e:
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise last_err


def get_teams(sport_id=1):
    data = _get("/teams", {"sportId": sport_id})
    return data.get("teams", [])


def get_roster(team_id, roster_type="active"):
    data = _get(f"/teams/{team_id}/roster", {"rosterType": roster_type})
    return data.get("roster", [])


def get_person(person_id):
    data = _get(f"/people/{person_id}")
    people = data.get("people", [])
    return people[0] if people else None


def get_people(person_ids):
    """Batch bio lookup -- the API accepts a comma-separated personIds list."""
    if not person_ids:
        return []
    ids = ",".join(str(i) for i in person_ids)
    data = _get("/people", {"personIds": ids})
    return data.get("people", [])


def get_schedule(start_date, end_date, sport_id=1):
    data = _get(
        "/schedule",
        {
            "sportId": sport_id,
            "startDate": start_date,
            "endDate": end_date,
            "hydrate": "probablePitcher,team",
        },
    )
    return data.get("dates", [])


def get_game_log(person_id, season, group):
    """group is 'hitting' or 'pitching'."""
    data = _get(
        f"/people/{person_id}/stats",
        {"stats": "gameLog", "group": group, "season": season},
    )
    stats = data.get("stats", [])
    if not stats:
        return []
    return stats[0].get("splits", [])


def get_season_stats(person_id, season, group):
    """
    MLB's own official season aggregate (stats='season') for a player --
    the ground truth to check our summed-from-gameLog totals against,
    since both ultimately come from the same source of record.
    """
    data = _get(f"/people/{person_id}/stats", {"stats": "season", "group": group, "season": season})
    stats = data.get("stats", [])
    if not stats:
        return None
    splits = stats[0].get("splits", [])
    return splits[0].get("stat", {}) if splits else None


def get_splits_vs_hand(person_id, group, season=None):
    """
    Splits vs LHP/RHP (group='hitting') or vs LHB/RHB (group='pitching').
    Pass a season for that season's splits. Pass season=None for true
    career-to-date splits -- NOTE: 'statSplits' with no season param
    silently defaults to the *current* season (confirmed live), it is NOT
    career; true career totals require the separate 'careerStatSplits'
    stat type, which is what we use here when season is None.
    Returns a dict keyed by 'vl'/'vr' -> stat dict (missing keys if no data).
    """
    if season is None:
        params = {"stats": "careerStatSplits", "group": group, "sitCodes": "vl,vr"}
    else:
        params = {"stats": "statSplits", "group": group, "sitCodes": "vl,vr", "season": season}
    data = _get(f"/people/{person_id}/stats", params)
    stats = data.get("stats", [])
    if not stats:
        return {}
    out = {}
    for split in stats[0].get("splits", []):
        code = (split.get("split") or {}).get("code")
        if code in ("vl", "vr"):
            out[code] = split.get("stat", {})
    return out


def get_boxscore(game_pk):
    """
    Live/final boxscore for a game. teams.{home,away}.battingOrder is a list
    of player ids in batting order, empty until the lineup is officially
    posted (typically within ~1-2 hours of first pitch) -- that's the
    reliable signal for "is the starting lineup out yet", vs. `bench` which
    lists every not-yet-placed roster spot beforehand.
    """
    return _get(f"/game/{game_pk}/feed/live", base=LIVE_BASE).get("liveData", {}).get("boxscore", {})


def get_linescore(game_pk):
    """
    Per-inning score breakdown (linescore.innings, each a {home: {runs},
    away: {runs}} pair) -- unlike home_score/away_score in the `games`
    table (final only), this is the only way to reconstruct the running
    score at any point during the game, needed for a "team led by N runs
    at any point" check (the My Bets early-win token). Retained on MLB's
    own feed even for an already-completed game, so this works whether
    called live or well after the final out.
    """
    return _get(f"/game/{game_pk}/feed/live", base=LIVE_BASE).get("liveData", {}).get("linescore", {})


def get_game_content(game_pk):
    """
    MLB's own official editorial/highlights bundle for a game -- condensed-
    game recaps and key-play clips (home runs, big outs), each with a real
    playable MP4 URL served from MLB's own CDN (mlb-cuts-diamond.mlb.com),
    same official source this project already uses for player photos
    (img.mlbstatic.com) and everything else. Empty for a game that hasn't
    started yet (nothing's happened to clip).
    """
    return _get(f"/game/{game_pk}/content", base=BASE)


def get_transactions(start_date, end_date, sport_id=1):
    """Free-agent signings, trades, IL placements/activations, etc."""
    data = _get("/transactions", {"sportId": sport_id, "startDate": start_date, "endDate": end_date})
    return data.get("transactions", [])


def get_league_news():
    """
    MLB.com's public news RSS feed -- free, no key, no auth. Returns a list
    of {title, link, pub_date, creator} dicts, most recent first (as
    published by the feed).
    """
    resp = _session.get(NEWS_RSS_URL, timeout=TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        creator = (item.findtext("dc:creator", namespaces=ns) or "").strip()
        items.append({"title": title, "link": link, "pub_date": pub_date, "creator": creator})
    return items
