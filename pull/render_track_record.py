"""
render_track_record.py

Renders output/track_record.json (built by grade_picks.py) into a static
"how good is this actually" results page -- output/track-record.html.
Deliberately a separate page from the main dashboard: this is a
retrospective record (what was picked vs. what happened), not a live
prop-decision tool, and mixing the two would bury the live picks under an
ever-growing history.

Reuses the main dashboard's STYLE/SCRIPT so it doesn't look or behave like
a different site (same theme toggle, same collapsible-card look).
"""

import html
import json
import os

from render_dashboard import SCRIPT, STYLE

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
TRACK_RECORD_PATH = os.path.join(OUT_DIR, "track_record.json")

BATTER_COLS = ("AVG", "TB/game")
PITCHER_COLS = ("ERA", "K/start")


def _pct(x):
    return f"{x:.0%}" if x is not None else "&mdash;"


def _rate_row(label, bucket, kind):
    bucket = bucket or {}
    games = bucket.get("games", 0)
    if not games:
        return f'<tr><td>{html.escape(label)}</td><td colspan="3" class="sub">No graded games yet</td></tr>'
    if kind == "batter":
        return f"<tr><td>{html.escape(label)}</td><td>{games}</td><td>{bucket['avg']}</td><td>{bucket['tb_per_game']}</td></tr>"
    return f"<tr><td>{html.escape(label)}</td><td>{games}</td><td>{bucket['era']}</td><td>{bucket['k_per_game']}</td></tr>"


def _rate_table(heading, bucket_dict, labels, kind):
    cols = BATTER_COLS if kind == "batter" else PITCHER_COLS
    rows = "".join(_rate_row(label, (bucket_dict or {}).get(key), kind) for label, key in labels)
    return f"""
    <div class="picks-heading" style="margin-top:16px">{html.escape(heading)}</div>
    <table><thead><tr><th></th><th>Games</th><th>{cols[0]}</th><th>{cols[1]}</th></tr></thead>
    <tbody>{rows}</tbody></table>
    """


def _picks_tile_html(title, bucket):
    bucket = bucket or {}
    graded = (bucket.get("hits") or 0) + (bucket.get("misses") or 0)
    if not graded:
        return f'<div class="stat-tile"><div class="stat-value">&mdash;</div><div class="stat-label">{html.escape(title)}</div></div>'
    dnp_txt = f", {bucket['no_data']} DNP" if bucket.get("no_data") else ""
    return f"""
    <div class="stat-tile">
      <div class="stat-value">{_pct(bucket.get("hit_rate"))}</div>
      <div class="stat-label">{html.escape(title)} ({bucket['hits']}/{graded}{dnp_txt})</div>
    </div>
    """


def _game_pick_tile_html(title, bucket):
    bucket = bucket or {}
    n = bucket.get("n") or 0
    if not n:
        return f'<div class="stat-tile"><div class="stat-value">&mdash;</div><div class="stat-label">{html.escape(title)}</div></div>'
    return f"""
    <div class="stat-tile">
      <div class="stat-value">{_pct(bucket.get("hit_rate"))}</div>
      <div class="stat-label">{html.escape(title)} ({bucket['hits']}/{n})</div>
    </div>
    """


def _pick_list_html(bucket, key="picks"):
    picks = (bucket or {}).get(key) or []
    if not picks:
        return '<div class="sub">No picks that day.</div>'
    rows = []
    for p in picks:
        actual = p["actual"] if p["actual"] is not None else "DNP"
        rows.append(
            f'<div class="grade-row grade-{p["outcome"]}">'
            f'<span><b>{html.escape(p["name"])}</b> ({html.escape(p.get("role", "batter"))}) '
            f'&mdash; {html.escape(p["category"] or "?")} {p["line"]}</span>'
            f'<span>actual: {actual} &middot; {p["outcome"].replace("_", " ").upper()}</span></div>'
        )
    return "".join(rows)


def _game_bucket_row(label, bucket):
    bucket = bucket or {}
    n = bucket.get("n") or 0
    if not n:
        return f'<tr><td>{html.escape(label)}</td><td colspan="2" class="sub">No graded games yet</td></tr>'
    return f"<tr><td>{html.escape(label)}</td><td>{bucket['hits']}/{n}</td><td>{_pct(bucket['hit_rate'])}</td></tr>"


def _games_table(heading, games_bucket):
    games_bucket = games_bucket or {}
    labels = [("Moneyline (correct winner)", "moneyline"), ("Run line (spread)", "run_line"), ("Total (over/under)", "total")]
    rows = "".join(_game_bucket_row(label, games_bucket.get(key)) for label, key in labels)
    return f"""
    <div class="picks-heading" style="margin-top:16px">{html.escape(heading)}</div>
    <table><thead><tr><th></th><th>Record</th><th>Hit rate</th></tr></thead>
    <tbody>{rows}</tbody></table>
    """


def _score_accuracy_tile(bucket):
    bucket = bucket or {}
    if not bucket.get("n"):
        return '<div class="stat-tile"><div class="stat-value">&mdash;</div><div class="stat-label">Projected score accuracy</div></div>'
    sign = "+" if (bucket.get("bias") or 0) >= 0 else ""
    return f"""
    <div class="stat-tile">
      <div class="stat-value">&plusmn;{bucket['mae']}</div>
      <div class="stat-label">Runs off per game, avg (projected vs actual total, {bucket['n']} games, bias {sign}{bucket.get('bias')})</div>
    </div>
    """


def _game_score_examples_html(examples):
    examples = examples or []
    if not examples:
        return '<div class="sub">No graded games that day.</div>'
    rows = []
    for e in examples:
        rows.append(
            f'<div class="grade-row grade-neutral">'
            f'<span><b>{html.escape(e["matchup"])}</b></span>'
            f'<span>projected {e["projected_away"]}&ndash;{e["projected_home"]} &middot; actual {e["actual_away"]}&ndash;{e["actual_home"]}</span></div>'
        )
    return "".join(rows)


def _projection_table(heading, category_stats):
    category_stats = category_stats or {}
    rows = []
    for label, s in sorted(category_stats.items(), key=lambda kv: -(kv[1].get("n") or 0)):
        if not s.get("n"):
            continue
        rows.append(
            f"<tr><td>{html.escape(label)}</td><td>{s['n']}</td>"
            f"<td>{s.get('avg_projected')}</td><td>{s.get('avg_actual')}</td>"
            f"<td>&plusmn;{s['mae']}</td></tr>"
        )
    if not rows:
        return f'<div class="picks-heading" style="margin-top:16px">{html.escape(heading)}</div><div class="sub">No graded projections yet.</div>'
    return f"""
    <div class="picks-heading" style="margin-top:16px">{html.escape(heading)}</div>
    <table><thead><tr><th>Stat</th><th>N</th><th>Avg projected</th><th>Avg actual</th><th>Off by, avg</th></tr></thead>
    <tbody>{"".join(rows)}</tbody></table>
    """


def _projection_examples_html(examples):
    examples = examples or []
    if not examples:
        return '<div class="sub">No graded projections that day.</div>'
    rows = []
    for e in examples:
        rows.append(
            f'<div class="grade-row grade-neutral">'
            f'<span><b>{html.escape(e["name"])}</b> ({html.escape(e["role"])}) &mdash; {html.escape(e["category"])}</span>'
            f'<span>projected {e["projected"]} &middot; actual {e["actual"]}</span></div>'
        )
    return "".join(rows)


def _day_card_html(day, open_by_default):
    date = day["date"]
    to, tu = day["top_overs"], day["top_unders"]
    to_graded = to["hits"] + to["misses"]
    tu_graded = tu["hits"] + tu["misses"]
    open_attr = " open" if open_by_default else ""
    games = day.get("games") or {}
    return f"""
    <details class="game-card"{open_attr}>
      <summary class="game-summary">
        <span class="matchup-title">{html.escape(date)}</span>
        <span class="game-meta">
          Top Overs: {_pct(to["hit_rate"])} ({to["hits"]}/{to_graded}) &middot;
          Top Unders: {_pct(tu["hit_rate"])} ({tu["hits"]}/{tu_graded})
        </span>
      </summary>
      <div class="game-body">
        <div class="picks-heading">Top Overs picks</div>
        {_pick_list_html(to)}
        <div class="picks-heading" style="margin-top:14px">Top Unders picks</div>
        {_pick_list_html(tu)}
        {_rate_table("Batter trend that day (every flagged batter, not just Top Picks)", day.get("batter_trend"), [("HOT", "hot"), ("COLD", "cold"), ("NEUTRAL", "neutral")], "batter")}
        {_rate_table("Batter matchup edge that day", day.get("batter_matchup"), [("Favorable", "favorable"), ("Unfavorable", "unfavorable"), ("Neutral", "neutral")], "batter")}
        {_rate_table("Pitcher form trend that day", day.get("pitcher_form"), [("Dominant", "dominant"), ("Rough", "rough"), ("Neutral", "neutral")], "pitcher")}
        {_games_table("Game picks that day (moneyline / run line / total)", games)}
        <div class="picks-heading" style="margin-top:16px">Projected score vs actual, that day</div>
        {_game_score_examples_html(games.get("score_examples"))}
        <div class="picks-heading" style="margin-top:16px">"Predicted: X" matchup-lean picks, that day (biggest misses/hits sample, not every pick)</div>
        {_pick_list_html(day.get("matchup_leans"), key="examples")}
        <div class="picks-heading" style="margin-top:16px">Best-prop star picks, that day</div>
        {_pick_list_html(day.get("best_prop_stars"), key="examples")}
        {_projection_table("Projected stat vs actual, that day (every player shown, by category)", day.get("projection_accuracy"))}
        <div class="picks-heading" style="margin-top:16px">Biggest single-player projection misses that day</div>
        {_projection_examples_html(day.get("projection_examples"))}
      </div>
    </details>
    """


def render_html():
    if os.path.exists(TRACK_RECORD_PATH):
        with open(TRACK_RECORD_PATH) as f:
            record = json.load(f)
    else:
        record = {"days": {}, "cumulative": {}, "updated_at": None}

    days = sorted(record.get("days", {}).values(), key=lambda d: d["date"], reverse=True)
    cum = record.get("cumulative") or {}

    if not days:
        body = '<div class="empty">No days graded yet -- check back after today\'s games are final.</div>'
    else:
        body = "".join(_day_card_html(d, open_by_default=(i == 0)) for i, d in enumerate(days))

    cum_games = cum.get("games") or {}
    cum_tiles = (
        _picks_tile_html("Top Overs hit rate, all days", cum.get("top_overs"))
        + _picks_tile_html("Top Unders hit rate, all days", cum.get("top_unders"))
        + _game_pick_tile_html("Moneyline hit rate, all days", cum_games.get("moneyline"))
        + _game_pick_tile_html("Run line hit rate, all days", cum_games.get("run_line"))
        + _game_pick_tile_html("Total (O/U) hit rate, all days", cum_games.get("total"))
        + _score_accuracy_tile(cum_games.get("score_accuracy"))
        + _game_pick_tile_html("Predicted-lean hit rate, all days", cum.get("matchup_leans"))
        + _game_pick_tile_html("Best-prop star hit rate, all days", cum.get("best_prop_stars"))
    )
    cum_rates = (
        _rate_table("All days: batter trend", cum.get("batter_trend"), [("HOT", "hot"), ("COLD", "cold"), ("NEUTRAL", "neutral")], "batter")
        + _rate_table("All days: batter matchup edge", cum.get("batter_matchup"), [("Favorable", "favorable"), ("Unfavorable", "unfavorable"), ("Neutral", "neutral")], "batter")
        + _rate_table("All days: pitcher form trend", cum.get("pitcher_form"), [("Dominant", "dominant"), ("Rough", "rough"), ("Neutral", "neutral")], "pitcher")
        + _projection_table("All days: projected stat vs actual, by category", cum.get("projection_accuracy"))
    )
    days_tracked = cum.get("days_tracked", 0)
    updated = record.get("updated_at") or "never"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>MLB Player Props -- Track Record</title>
{STYLE}
</head>
<body>
  <div class="page">
    <div class="header-band">
      <div class="header-top">
        <div>
          <h1>Track Record</h1>
          <div class="meta">How the picks and signals actually performed, day by day &middot; {days_tracked} day(s) graded &middot; updated {html.escape(updated)}</div>
        </div>
        <div class="header-actions">
          <a class="nav-link" href="index.html">&larr; Back to Dashboard</a>
          <a class="nav-link" href="my-bets.html">My Bets</a>
          <button id="themeToggle" class="theme-toggle" type="button">Switch to dark</button>
        </div>
      </div>
    </div>

    <div class="notes">
      <b>How to read this</b><br>
      Each day is graded from the very first report generated that day --
      frozen before that day's games start, never a later snapshot -- so
      nothing here can be quietly informed by that same day's own
      results. Top Overs/Unders hit rate excludes picks where the player
      didn't end up playing (DNP), shown separately rather than counted
      as a miss. The trend/matchup/pitcher-form breakdowns cover every
      flagged player that day, not just the curated Top Picks -- a
      broader, more honest read on whether those signals predict
      anything. Small samples (especially a single day) bounce around a
      lot; judge the signals by the "all days" totals below, not any one
      day in isolation. Moneyline/run line/total grade each game's pick
      against the actual final score. "Projected stat vs actual" is a
      different, more granular cut than hit/miss -- it shows the actual
      NUMBER projected for every player and category (e.g. "8.0 projected
      strikeouts") next to what really happened (e.g. "12"), so you can
      see not just whether a line was cleared but how close the model's
      numbers actually were.
    </div>

    <div class="stat-row">{cum_tiles}</div>

    <div class="picks-section">
      <div class="picks-subheading" style="padding:14px 16px 0">Across every day graded so far</div>
      <div style="padding: 0 16px 16px">{cum_rates}</div>
    </div>

    <div class="date-heading">Day by day</div>
    {body}
  </div>
{SCRIPT}
</body>
</html>"""


def run():
    os.makedirs(OUT_DIR, exist_ok=True)
    html_path = os.path.join(OUT_DIR, "track-record.html")
    with open(html_path, "w") as f:
        f.write(render_html())
    print(f"Wrote track record page to {html_path}")


if __name__ == "__main__":
    run()
