"""
render_dashboard.py

Renders the report dict from build_props.py into a single static HTML
dashboard (output/index.html). No JS framework/build step -- plain HTML +
a small amount of vanilla JS for client-side filtering, inline CSS custom
properties, light+dark mode.

Written for a casual sportsbook user, not a sabermetrician: stat names are
spelled out (Total Bases, not TB), advanced/jargon terms (ERA, WHIP, BABIP)
get a one-line plain-English explanation in the Glossary panel instead of
being assumed knowledge, and every prop category shows the plain hit-rate
at common lines ("went over 1.5 total bases in 8 of the last 10 games")
rather than a raw stat dump -- this project doesn't pull real sportsbook
lines (see README), so that hit-rate is what you compare against whatever
number FanDuel/Sleeper shows you, not a claim that we know their line.

Layout: stat-tile summary row, a filter toolbar (date / batter-or-pitcher /
team-or-player search / confirmed-only), then games grouped by date as
collapsible cards -- today's games open by default, later dates collapsed.
Each player row expands (click the row) into a per-category prop
breakdown with a small recent-games bar chart.

`output/notes.md`, if present, is read (never written) and rendered as a
"Notes" panel -- a spot for manually-pasted expert-consensus takes that
survives regeneration because this script never touches that file.
"""

import html
import json
import os
from itertools import groupby

NOTES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "notes.md")

STYLE = """
<style>
  :root, .viz-root {
    color-scheme: light;
    --surface-1: #fcfcfb; --surface-2: #f9f9f7; --surface-3: #f2f1ee;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
    --gridline: #e1e0d9; --border: rgba(11,11,11,0.10);
    --status-good: #0ca30c; --status-warning: #fab219; --status-serious: #ec835a; --status-critical: #d03b3b;
    --badge-neutral-bg: #e1e0d9; --badge-neutral-text: #52514e;
    --series-1: #2a78d6; --series-1-bg: rgba(42,120,214,0.15);
    --shadow: 0 1px 2px rgba(11,11,11,0.04), 0 4px 12px rgba(11,11,11,0.04);
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface-1: #1a1a19; --surface-2: #0d0d0d; --surface-3: #222221;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
      --gridline: #2c2c2a; --border: rgba(255,255,255,0.10);
      --badge-neutral-bg: #2c2c2a; --badge-neutral-text: #c3c2b7;
      --series-1: #3987e5; --series-1-bg: rgba(57,135,229,0.18);
      --shadow: 0 1px 2px rgba(0,0,0,0.2), 0 4px 16px rgba(0,0,0,0.24);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--surface-2); color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .page { max-width: 1080px; margin: 0 auto; padding: 20px 20px 60px; }
  .top { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; margin-bottom: 4px; }
  h1 { font-size: 21px; margin: 0; letter-spacing: -0.01em; }
  .meta { color: var(--text-muted); font-size: 12.5px; }
  .notes, .glossary {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 16px; margin: 16px 0; font-size: 13px; color: var(--text-secondary);
    box-shadow: var(--shadow);
  }
  .notes { white-space: pre-wrap; }
  .glossary summary { cursor: pointer; font-weight: 600; color: var(--text-primary); }
  .glossary dl { margin: 12px 0 0; }
  .glossary dt { font-weight: 600; color: var(--text-primary); margin-top: 8px; }
  .glossary dd { margin: 2px 0 0; }

  .stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 18px 0; }
  @media (max-width: 640px) { .stat-row { grid-template-columns: repeat(2, 1fr); } }
  .stat-tile {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px; box-shadow: var(--shadow);
  }
  .stat-value { font-size: 22px; font-weight: 700; line-height: 1.1; font-variant-numeric: tabular-nums; }
  .stat-label { font-size: 11.5px; color: var(--text-muted); margin-top: 3px; }

  .toolbar {
    position: sticky; top: 0; z-index: 5; display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
    background: var(--surface-2); padding: 10px 0 14px; border-bottom: 1px solid var(--gridline); margin-bottom: 16px;
  }
  .toolbar select, .toolbar input[type="text"] {
    background: var(--surface-1); color: var(--text-primary); border: 1px solid var(--border);
    border-radius: 8px; padding: 7px 10px; font-size: 13px; font-family: inherit;
  }
  .toolbar input[type="text"] { min-width: 220px; }
  .toolbar label { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-secondary); }
  .toolbar .count { font-size: 12px; color: var(--text-muted); margin-left: auto; }

  .date-heading {
    font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--text-muted); margin: 22px 0 10px; padding-top: 4px;
  }
  .date-heading:first-child { margin-top: 0; }

  details.game-card {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
    margin-bottom: 10px; box-shadow: var(--shadow); overflow: hidden;
  }
  details.game-card[open] summary { border-bottom: 1px solid var(--gridline); }
  summary.game-summary {
    cursor: pointer; padding: 12px 16px; display: flex; justify-content: space-between;
    align-items: center; flex-wrap: wrap; gap: 8px; list-style: none;
  }
  summary.game-summary::-webkit-details-marker { display: none; }
  summary.game-summary::before {
    content: ""; display: inline-block; width: 0; height: 0; margin-right: 10px;
    border-top: 4px solid transparent; border-bottom: 4px solid transparent;
    border-left: 5px solid var(--text-muted); transition: transform 0.12s;
  }
  details[open] summary.game-summary::before { transform: rotate(90deg); }
  .matchup-title { font-size: 14.5px; font-weight: 600; }
  .game-meta { color: var(--text-muted); font-size: 12px; }
  .game-line { color: var(--text-secondary); font-size: 12.5px; width: 100%; margin-top: 2px; }
  .summary-flags { display: flex; gap: 6px; flex-wrap: wrap; }
  .game-body { padding: 4px 16px 16px; }

  .teams { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 8px; }
  @media (max-width: 720px) { .teams { grid-template-columns: 1fr; } }
  .team-col { border-top: 1px solid var(--gridline); padding-top: 10px; }
  .team-title { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 14px; margin-bottom: 8px; }
  .badge {
    display: inline-flex; align-items: center; gap: 4px; font-size: 10.5px; font-weight: 700;
    padding: 2px 7px; border-radius: 999px; line-height: 1.6; white-space: nowrap;
  }
  .badge-confirmed { background: var(--series-1); color: white; }
  .badge-projected { background: var(--badge-neutral-bg); color: var(--badge-neutral-text); }
  .badge-hot { background: var(--status-good); color: white; }
  .badge-cold { background: var(--status-critical); color: white; }
  .badge-injury { background: var(--status-warning); color: #1a1a19; }
  .badge-matchup { background: var(--series-1); color: white; }
  .badge-streak { background: var(--status-warning); color: #1a1a19; }
  .badge-caveat { background: var(--badge-neutral-bg); color: var(--text-secondary); }
  .pitcher-row { cursor: pointer; }
  .pitcher-line { font-size: 13px; margin-bottom: 6px; color: var(--text-secondary); }
  .pitcher-line b { color: var(--text-primary); }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th { text-align: left; color: var(--text-muted); font-weight: 500; font-size: 11px; padding: 5px 6px; border-bottom: 1px solid var(--gridline); }
  td { padding: 6px; border-bottom: 1px solid var(--gridline); vertical-align: top; }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr.player-row:nth-child(4n+1), tbody tr.player-row:nth-child(4n+2) { background: var(--surface-3); }
  tr.player-row { cursor: pointer; }
  tr.player-row:hover { background: var(--series-1-bg); }
  .name-cell { font-weight: 600; }
  .expand-arrow {
    display: inline-block; width: 0; height: 0; margin-right: 6px; vertical-align: middle;
    border-top: 3px solid transparent; border-bottom: 3px solid transparent;
    border-left: 4px solid var(--text-muted);
  }
  .sub { color: var(--text-muted); font-size: 11px; }
  .headline-link { color: var(--series-1); text-decoration: none; font-size: 11px; }
  .headline-link:hover { text-decoration: underline; }
  .empty { color: var(--text-muted); font-size: 13px; padding: 40px 20px; text-align: center; }

  .prop-detail td { background: var(--surface-2); }
  .prop-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; padding: 6px 2px; }
  .prop-cat { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
  .prop-cat-label { font-weight: 600; font-size: 12.5px; margin-bottom: 4px; }
  .prop-bars { display: flex; align-items: flex-end; gap: 2px; height: 32px; margin: 4px 0 6px; }
  .prop-bar { flex: 1; background: var(--series-1); border-radius: 2px 2px 0 0; min-height: 2px; }
  .prop-rate { font-size: 11.5px; color: var(--text-secondary); }
  .prop-avg { font-size: 11px; color: var(--text-muted); margin-top: 4px; }

  .picks-section { margin: 20px 0 24px; }
  .picks-heading { font-size: 15px; font-weight: 700; margin-bottom: 4px; }
  .picks-subheading { font-size: 12px; color: var(--text-muted); margin-bottom: 10px; }
  .picks-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; }
  .pick-card {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px; box-shadow: var(--shadow);
  }
  .pick-rank { font-size: 11px; color: var(--text-muted); font-weight: 700; }
  .pick-name { font-size: 14.5px; font-weight: 700; margin: 2px 0; }
  .pick-matchup { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
  .pick-reasons { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
  .pick-reasons li { margin-bottom: 2px; }
  .pick-category {
    font-size: 12.5px; font-weight: 600; background: var(--series-1-bg); color: var(--series-1);
    border-radius: 6px; padding: 6px 8px; margin-top: 6px;
  }
</style>
"""

SCRIPT = """
<script>
function applyFilters() {
  const date = document.getElementById('dateFilter').value;
  const role = document.getElementById('roleFilter').value;
  const search = document.getElementById('searchBox').value.trim().toLowerCase();
  const confirmedOnly = document.getElementById('confirmedOnly').checked;
  let visible = 0;
  document.querySelectorAll('.game-card').forEach(function (card) {
    let show = true;
    if (date !== 'all' && card.dataset.date !== date) show = false;
    if (confirmedOnly && card.dataset.confirmed !== 'true') show = false;
    if (search && card.dataset.search.indexOf(search) === -1) show = false;
    card.style.display = show ? '' : 'none';
    if (show) visible++;
    card.querySelectorAll('.pitcher-block').forEach(function (el) {
      el.style.display = (role === 'pitchers' || role === 'all') ? '' : 'none';
    });
    card.querySelectorAll('.batter-block').forEach(function (el) {
      el.style.display = (role === 'batters' || role === 'all') ? '' : 'none';
    });
  });
  document.querySelectorAll('[data-date-group]').forEach(function (group) {
    let anyVisible = false;
    group.querySelectorAll('.game-card').forEach(function (c) { if (c.style.display !== 'none') anyVisible = true; });
    group.style.display = anyVisible ? '' : 'none';
  });
  document.getElementById('resultCount').textContent = visible + ' game' + (visible === 1 ? '' : 's') + ' shown';
}
function toggleDetail(id) {
  const row = document.getElementById(id);
  if (row) row.style.display = (row.style.display === 'none' || !row.style.display) ? 'table-row' : 'none';
}
document.addEventListener('DOMContentLoaded', function () {
  document.getElementById('dateFilter').addEventListener('change', applyFilters);
  document.getElementById('roleFilter').addEventListener('change', applyFilters);
  document.getElementById('searchBox').addEventListener('input', applyFilters);
  document.getElementById('confirmedOnly').addEventListener('change', applyFilters);
  applyFilters();
});
</script>
"""

GLOSSARY_HTML = """
<details class="glossary">
  <summary>What do these terms mean?</summary>
  <dl>
    <dt>Confirmed vs. Projected lineup</dt>
    <dd>CONFIRMED means MLB has officially posted tonight's batting order (usually 1-2 hours before first pitch). PROJECTED means the game hasn't posted a lineup yet, so we're showing who's played most recently for that team as a best guess.</dd>
    <dt>HOT / COLD</dt>
    <dd>This player's batting average over their last 7 games is notably higher (HOT) or lower (COLD) than their season average. Backtested result: on its own this barely predicts what happens in the very next game -- short hot/cold streaks are mostly random noise, not a reliable signal by itself.</dd>
    <dt>"Likely luck" tag</dt>
    <dd>A hot streak driven by bloop hits falling in (measured by BABIP, the rate of batted balls that go for hits) rather than actually hitting the ball harder. These streaks tend to cool off faster than "real" hot streaks.</dd>
    <dt>Matchup edge</dt>
    <dd>This batter's handedness (lefty/righty) is a good matchup against tonight's specific opposing pitcher, who has historically struggled against that side.</dd>
    <dt>Bullpen taxed / rested</dt>
    <dd>How many innings that team's relief pitchers have thrown in the last 2 days. A taxed bullpen has been used a lot recently and may be less sharp late in the game.</dd>
    <dt>ERA (earned run average)</dt>
    <dd>Runs a pitcher allows per 9 innings pitched -- lower is better. Roughly: under 3.5 is very good, 4.5+ is below average.</dd>
    <dt>WHIP</dt>
    <dd>Walks + hits allowed per inning pitched -- lower is better, roughly measures how many baserunners a pitcher allows.</dd>
    <dt>Hit rate (in the prop breakdown)</dt>
    <dd>Out of this player's recent games, the percentage where they went OVER a given number. E.g. "80% over 1.5 total bases" means 8 of their last 10 games had 2+ total bases. We don't pull actual FanDuel/Sleeper lines, so compare this rate to whatever number the app shows you.</dd>
  </dl>
</details>
"""


def _badge(label, kind):
    return f'<span class="badge badge-{kind}">{html.escape(label)}</span>'


def _trend_badge(trend):
    if trend == "hot":
        return _badge("HOT", "hot")
    if trend == "cold":
        return _badge("COLD", "cold")
    return ""


def _injury_badge(injury):
    return _badge(f"INJURED ({injury['status']})", "injury") if injury else ""


def _matchup_badge(matchup):
    if not matchup or not matchup.get("favorable"):
        return ""
    return _badge("GOOD MATCHUP", "matchup")


def _streak_badge(streak):
    return _badge(f"{streak}-GAME HIT STREAK", "streak") if streak and streak >= 3 else ""


def _caveat_badge(trend_caveat):
    return _badge("LIKELY LUCK", "caveat") if trend_caveat == "babip_driven" else ""


def _prop_categories_html(categories, row_id):
    if not categories:
        return f'<tr id="{row_id}" class="prop-detail" style="display:none"><td colspan="5" class="sub">No recent-game data yet.</td></tr>'
    cats_html = []
    for cat in categories:
        values = cat["values"]
        max_v = max(values) or 1
        bars = "".join(f'<div class="prop-bar" style="height:{max(v / max_v * 100, 6):.0f}%" title="{v}"></div>' for v in values)
        rates = " &middot; ".join(f'{r["pct"]}% over {r["line"]}' for r in cat["hit_rates"])
        cats_html.append(f"""
        <div class="prop-cat">
          <div class="prop-cat-label">{html.escape(cat["label"])}</div>
          <div class="prop-bars">{bars}</div>
          <div class="prop-rate">{rates}</div>
          <div class="prop-avg">Averaging {cat["average"]} per game (last {len(values)})</div>
        </div>
        """)
    return f'<tr id="{row_id}" class="prop-detail" style="display:none"><td colspan="5"><div class="prop-grid">{"".join(cats_html)}</div></td></tr>'


def _pitcher_html(p, row_id):
    if not p:
        return '<div class="pitcher-line sub">No probable pitcher announced</div>'
    l5 = p["l5"]
    l5_txt = (
        f"Last 5 starts: {l5['strike_outs']} strikeouts, {l5['earned_runs']} runs allowed, {l5['era']} ERA"
        if l5
        else "No recent starts on record yet"
    )
    return f"""
    <div class="pitcher-line pitcher-row" onclick="toggleDetail('{row_id}')">
      <span class="expand-arrow"></span><b>{html.escape(p["name"])}</b> ({html.escape(p["pitch_hand"] or "?")}, throws)
      {_injury_badge(p["injury"])}<br>{html.escape(l5_txt)}
    </div>
    <table><tbody>{_prop_categories_html(p.get("prop_categories"), row_id)}</tbody></table>
    """


def _batter_rows(batters, id_prefix):
    if not batters:
        return '<tr><td colspan="5" class="sub">No batter data yet</td></tr>'
    rows = []
    for i, b in enumerate(batters):
        row_id = f"{id_prefix}-{i}"
        order = f"#{b['batting_order']}" if b["batting_order"] else "-"
        l7 = b["l7"]
        l7_txt = (
            f"Last 7 games: {l7['hits']} hits, {l7['home_runs']} home runs, {l7['rbi']} RBIs, batting {l7['avg']}"
            if l7
            else "No recent games on record yet"
        )
        season = b["season"]
        season_txt = f"{season['avg']} batting average this season" if season and season["avg"] is not None else "-"
        headline = (
            f'<a class="headline-link" href="{html.escape(b["headlines"][0]["link"])}" target="_blank" rel="noopener" onclick="event.stopPropagation()">'
            f'{html.escape(b["headlines"][0]["title"][:60])}</a>'
            if b["headlines"]
            else ""
        )
        badges = " ".join(
            x
            for x in [
                _trend_badge(b["trend"]),
                _caveat_badge(b.get("trend_caveat")),
                _matchup_badge(b.get("matchup")),
                _streak_badge(b.get("hit_streak")),
                _injury_badge(b["injury"]),
            ]
            if x
        )
        rows.append(
            f'<tr class="player-row" onclick="toggleDetail(\'{row_id}\')">'
            f"<td>{order}</td>"
            f'<td class="name-cell"><span class="expand-arrow"></span>{html.escape(b["name"])} '
            f'<span class="sub">({html.escape(b["bat_side"] or "?")} handed batter)</span></td>'
            f"<td>{badges}</td>"
            f'<td>{html.escape(l7_txt)}<div class="sub">{season_txt}</div></td>'
            f"<td>{headline}</td></tr>"
        )
        rows.append(_prop_categories_html(b.get("prop_categories"), row_id))
    return "\n".join(rows)


def _fatigue_html(fatigue):
    if not fatigue:
        return ""
    ratio = fatigue["fatigue_ratio"]
    if ratio >= 1.2:
        return f'<div class="sub">Opposing bullpen has thrown a lot lately: {fatigue["recent_innings"]} innings in the last 2 days</div>'
    if ratio <= 0.7:
        return f'<div class="sub">Opposing bullpen is well-rested: only {fatigue["recent_innings"]} innings in the last 2 days</div>'
    return ""


def _team_col_html(side, id_prefix):
    tag_kind = "confirmed" if side["lineup_confirmed"] else "projected"
    tag_label = "LINEUP CONFIRMED" if side["lineup_confirmed"] else "PROJECTED (not yet announced)"
    return f"""
    <div class="team-col">
      <div class="team-title">{html.escape(side["team_name"] or "?")} {_badge(tag_label, tag_kind)}</div>
      <div class="pitcher-block">
        {_pitcher_html(side["probable_pitcher"], f"{id_prefix}-p")}
        {_fatigue_html(side.get("opponent_bullpen_fatigue"))}
      </div>
      <div class="batter-block">
        <table>
          <thead><tr><th>Order</th><th>Batter</th><th>Flags</th><th>Recent form</th><th>News</th></tr></thead>
          <tbody>{_batter_rows(side["batters"], f"{id_prefix}-b")}</tbody>
        </table>
      </div>
    </div>
    """


def _game_search_blob(g):
    names = [g["home"]["team_name"] or "", g["away"]["team_name"] or ""]
    for side in (g["home"], g["away"]):
        if side["probable_pitcher"]:
            names.append(side["probable_pitcher"]["name"])
        names.extend(b["name"] for b in side["batters"])
    return html.escape(" ".join(names).lower())


def _game_summary_flags(g):
    flags = []
    if g["home"]["lineup_confirmed"] or g["away"]["lineup_confirmed"]:
        flags.append(_badge("CONFIRMED", "confirmed"))
    hot_count = sum(1 for side in (g["home"], g["away"]) for b in side["batters"] if b["trend"] == "hot")
    if hot_count:
        flags.append(_badge(f"{hot_count} HOT", "hot"))
    matchup_count = sum(
        1 for side in (g["home"], g["away"]) for b in side["batters"] if b["matchup"] and b["matchup"].get("favorable")
    )
    if matchup_count:
        flags.append(_badge(f"{matchup_count} GOOD MATCHUPS", "matchup"))
    return "".join(flags)


def _game_line_html(g):
    if g.get("home_score") is not None:
        return (
            f'<div class="game-line">Final score: {html.escape(g["away"]["team_name"] or "?")} {g["away_score"]} '
            f'&ndash; {html.escape(g["home"]["team_name"] or "?")} {g["home_score"]}</div>'
        )
    p = g.get("projection")
    if not p:
        return ""
    home_pct = round(p["home_win_prob"] * 100)
    away_pct = 100 - home_pct
    return (
        f'<div class="game-line">Our projection: {html.escape(g["away"]["team_name"] or "?")} {away_pct}% '
        f'&ndash; {html.escape(g["home"]["team_name"] or "?")} {home_pct}% to win &middot; '
        f'projected total {p["total_line"]} runs ({round(p["over_prob"] * 100)}% chance of going over)</div>'
    )


def _game_card_html(g, open_by_default):
    is_confirmed = "true" if (g["home"]["lineup_confirmed"] or g["away"]["lineup_confirmed"]) else "false"
    open_attr = " open" if open_by_default else ""
    id_prefix = f"g{g['game_pk']}"
    return f"""
    <details class="game-card" data-date="{html.escape(g["date"])}" data-confirmed="{is_confirmed}"
              data-search="{_game_search_blob(g)}"{open_attr}>
      <summary class="game-summary">
        <span class="matchup-title">{html.escape(g["away"]["team_name"] or "?")} @ {html.escape(g["home"]["team_name"] or "?")}</span>
        <span class="summary-flags">{_game_summary_flags(g)}</span>
        <span class="game-meta">{html.escape(g["status"] or "")} &middot; {html.escape(g["venue"] or "")}</span>
        {_game_line_html(g)}
      </summary>
      <div class="game-body">
        <div class="teams">
          {_team_col_html(g["away"], f"{id_prefix}-a")}
          {_team_col_html(g["home"], f"{id_prefix}-h")}
        </div>
      </div>
    </details>
    """


def _stat_tiles(games):
    total_games = len(games)
    confirmed_sides = sum(1 for g in games for side in (g["home"], g["away"]) if side["lineup_confirmed"])
    hot_count = sum(1 for g in games for side in (g["home"], g["away"]) for b in side["batters"] if b["trend"] == "hot")
    matchup_count = sum(
        1
        for g in games
        for side in (g["home"], g["away"])
        for b in side["batters"]
        if b["matchup"] and b["matchup"].get("favorable")
    )
    tiles = [
        (str(total_games), "Games in range"),
        (str(confirmed_sides), "Confirmed lineups"),
        (str(hot_count), "Hot batters flagged"),
        (str(matchup_count), "Good platoon matchups"),
    ]
    return "".join(
        f'<div class="stat-tile"><div class="stat-value">{v}</div><div class="stat-label">{html.escape(l)}</div></div>'
        for v, l in tiles
    )


def _toolbar(games):
    dates = sorted({g["date"] for g in games})
    options = ['<option value="all">All dates</option>']
    for d in dates:
        options.append(f'<option value="{html.escape(d)}">{html.escape(d)}</option>')
    return f"""
    <div class="toolbar">
      <select id="dateFilter">{"".join(options)}</select>
      <select id="roleFilter">
        <option value="all">Batters &amp; Pitchers</option>
        <option value="batters">Batters only</option>
        <option value="pitchers">Pitchers only</option>
      </select>
      <input id="searchBox" type="text" placeholder="Search team or player...">
      <label><input type="checkbox" id="confirmedOnly"> Confirmed lineups only</label>
      <span class="count" id="resultCount"></span>
    </div>
    """


def _top_picks_html(picks):
    if not picks:
        return ""
    cards = []
    for i, pick in enumerate(picks, 1):
        reasons_html = "".join(f"<li>{html.escape(r)}</li>" for r in pick["reasons"])
        category_html = ""
        if pick["best_category"]:
            c = pick["best_category"]
            category_html = (
                f'<div class="pick-category">Best angle: {html.escape(c["label"])} '
                f'&mdash; over {c["hit_rates"][0]["line"]} in {c["hit_rates"][0]["pct"]}% of last {c["hit_rates"][0]["n"]} games</div>'
            )
        lineup_kind = "confirmed" if pick["lineup_confirmed"] else "projected"
        lineup_label = "LINEUP CONFIRMED" if pick["lineup_confirmed"] else "LINEUP PROJECTED"
        cards.append(f"""
        <div class="pick-card">
          <div class="pick-rank">#{i} PICK</div>
          <div class="pick-name">{html.escape(pick["name"])}</div>
          <div class="pick-matchup">{html.escape(pick["team"] or "?")} vs. {html.escape(pick["opponent"] or "?")} &middot; {_badge(lineup_label, lineup_kind)}</div>
          <ul class="pick-reasons">{reasons_html}</ul>
          {category_html}
        </div>
        """)
    return f"""
    <div class="picks-section">
      <div class="picks-heading">Today's Best Picks</div>
      <div class="picks-subheading">Ranked across every game by real signal strength -- injured players excluded. Players whose lineup spot isn't confirmed yet are marked PROJECTED. Not a guarantee, just where the strongest combination of signals points.</div>
      <div class="picks-grid">{"".join(cards)}</div>
    </div>
    """


def render_html(report):
    notes_html = ""
    if os.path.exists(NOTES_PATH):
        with open(NOTES_PATH) as f:
            notes = f.read().strip()
        if notes:
            notes_html = f'<div class="notes"><b>Notes</b>\n{html.escape(notes)}</div>'

    games = report["games"]
    if not games:
        body = '<div class="empty">No games in range.</div>'
    else:
        first_date = games[0]["date"]
        sections = []
        for date, group in groupby(games, key=lambda g: g["date"]):
            group_games = list(group)
            cards = "".join(_game_card_html(g, open_by_default=(date == first_date)) for g in group_games)
            sections.append(f'<div data-date-group><div class="date-heading">{html.escape(date)}</div>{cards}</div>')
        body = _toolbar(games) + "".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MLB Player Props Dashboard</title>
{STYLE}
</head>
<body>
  <div class="page">
    <div class="top">
      <h1>MLB Player Props Dashboard</h1>
      <div class="meta">Generated {html.escape(report["generated_at"])}</div>
    </div>
    <div class="stat-row">{_stat_tiles(games)}</div>
    {_top_picks_html(report.get("top_picks"))}
    {GLOSSARY_HTML}
    {notes_html}
    {body}
  </div>
{SCRIPT}
</body>
</html>"""
