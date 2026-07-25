"""
render_dashboard.py

Renders the report dict from build_props.py into a single static HTML
dashboard (output/index.html). No JS framework/build step -- plain HTML +
a small amount of vanilla JS for client-side filtering, inline CSS custom
properties, light+dark mode (auto via OS preference, or a manual toggle
that overrides it, persisted in localStorage).

Written for a casual sportsbook user, not a sabermetrician: stat names are
spelled out (Total Bases, not TB), advanced/jargon terms (ERA, WHIP, BABIP)
get a one-line plain-English explanation in the Glossary panel instead of
being assumed knowledge, and every prop category shows the plain hit-rate
at common lines ("went over 1.5 total bases in 8 of the last 10 games")
rather than a raw stat dump -- this project doesn't pull real sportsbook
lines (see README), so that hit-rate is what you compare against whatever
number FanDuel/Sleeper shows you, not a claim that we know their line.

Layout: colored header band, stat-tile summary row, a collapsed Injury
Report, collapsed Top Overs/Top Unders leaderboards (batters AND
pitchers), a filter toolbar (date / batter-or-pitcher / prop category /
team-or-player search / confirmed-only), then games grouped by date as
collapsible cards -- today's games open by default, later dates collapsed.
Each player row expands (click the row) into a per-category prop
breakdown with a recent-games bar chart: a dashed line marks the common
betting line for that category, bars are green/red for over/under it, and
hovering a bar shows the exact number and date.

`output/notes.md`, if present, is read (never written) and rendered as a
"Notes" panel -- a spot for manually-pasted expert-consensus takes that
survives regeneration because this script never touches that file.
"""

import html
import os
import re
from datetime import datetime
from itertools import groupby

NOTES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "notes.md")

DEFAULT_BATTER_LABELS = ["Hits", "Total Bases", "Home Runs", "RBIs", "Runs Scored", "Walks"]
DEFAULT_PITCHER_LABELS = ["Strikeouts", "Runs Allowed", "Hits Allowed", "Walks Allowed"]

STYLE = """
<style>
  :root, .viz-root {
    color-scheme: light;
    --surface-1: #ffffff; --surface-2: #f1f4f9; --surface-3: #e8edf5;
    --text-primary: #0b1e33; --text-secondary: #48566b; --text-muted: #8592a6;
    --gridline: #e1e7ef; --border: rgba(11,30,51,0.09);
    --status-good: #0f9d58; --status-warning: #f5a623; --status-serious: #ef6c4d; --status-critical: #e0333f;
    --badge-neutral-bg: #e6ebf3; --badge-neutral-text: #48566b;
    --series-1: #0057ff; --series-1-bg: rgba(0,87,255,0.10);
    --header-grad: linear-gradient(135deg, #002d80, #0057ff);
    --shadow: 0 1px 2px rgba(11,30,51,0.05), 0 6px 16px rgba(11,30,51,0.06);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface-1: #101d34; --surface-2: #0a1626; --surface-3: #16263f;
      --text-primary: #f4f7fb; --text-secondary: #b7c3d6; --text-muted: #7c8aa2;
      --gridline: #21324c; --border: rgba(255,255,255,0.09);
      --badge-neutral-bg: #1b2c46; --badge-neutral-text: #b7c3d6;
      --series-1: #3d8bff; --series-1-bg: rgba(61,139,255,0.18);
      --header-grad: linear-gradient(135deg, #041730, #0c2c5c);
      --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 8px 20px rgba(0,0,0,0.35);
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1: #101d34; --surface-2: #0a1626; --surface-3: #16263f;
    --text-primary: #f4f7fb; --text-secondary: #b7c3d6; --text-muted: #7c8aa2;
    --gridline: #21324c; --border: rgba(255,255,255,0.09);
    --badge-neutral-bg: #1b2c46; --badge-neutral-text: #b7c3d6;
    --series-1: #3d8bff; --series-1-bg: rgba(61,139,255,0.18);
    --header-grad: linear-gradient(135deg, #041730, #0c2c5c);
    --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 8px 20px rgba(0,0,0,0.35);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--surface-2); color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .page { max-width: 1100px; margin: 0 auto; padding: 20px 20px 60px; }

  .header-band {
    background: var(--header-grad); color: #ffffff; border-radius: 14px;
    padding: 18px 20px; margin-bottom: 16px; box-shadow: var(--shadow);
  }
  .header-top { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
  .header-band h1 { font-size: 21px; margin: 0; letter-spacing: -0.01em; color: #ffffff; }
  .header-band .meta { color: rgba(255,255,255,0.78); font-size: 12.5px; margin-top: 2px; }
  .header-actions { display: flex; align-items: center; gap: 10px; }
  .theme-toggle {
    background: rgba(255,255,255,0.14); color: #ffffff; border: 1px solid rgba(255,255,255,0.3);
    border-radius: 999px; padding: 7px 14px; font-size: 12.5px; font-weight: 600; cursor: pointer;
    font-family: inherit;
  }
  .theme-toggle:hover { background: rgba(255,255,255,0.22); }
  .nav-link {
    color: #ffffff; font-size: 12.5px; font-weight: 600; text-decoration: none;
    border: 1px solid rgba(255,255,255,0.3); border-radius: 999px; padding: 7px 14px;
  }
  .nav-link:hover { background: rgba(255,255,255,0.14); }

  .notes, .glossary {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
    padding: 14px 16px; margin: 14px 0; font-size: 13px; color: var(--text-secondary);
    box-shadow: var(--shadow);
  }
  .notes { white-space: pre-wrap; }
  .glossary summary { cursor: pointer; font-weight: 600; color: var(--text-primary); }
  .glossary dl { margin: 12px 0 0; }
  .glossary dt { font-weight: 600; color: var(--text-primary); margin-top: 8px; }
  .glossary dd { margin: 2px 0 0; }

  .stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin: 14px 0; }
  .stat-tile {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
    padding: 12px 14px; box-shadow: var(--shadow); border-top: 3px solid var(--series-1);
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
  .toolbar input[type="text"] { min-width: 200px; }
  .toolbar label { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-secondary); }
  .toolbar .count { font-size: 12px; color: var(--text-muted); margin-left: auto; }

  .date-heading {
    font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--text-muted); margin: 22px 0 10px; padding-top: 4px;
  }
  .date-heading:first-child { margin-top: 0; }

  details.game-card {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
    margin-bottom: 10px; box-shadow: var(--shadow); overflow: hidden;
  }
  details.game-card[open] summary { border-bottom: 1px solid var(--gridline); }
  summary.game-summary {
    cursor: pointer; padding: 12px 16px; display: flex; justify-content: space-between;
    align-items: center; flex-wrap: wrap; gap: 8px; list-style: none;
  }
  summary.game-summary::-webkit-details-marker, summary.picks-summary::-webkit-details-marker { display: none; }
  summary.game-summary::before, summary.picks-summary::before {
    content: ""; display: inline-block; width: 0; height: 0; margin-right: 10px; flex-shrink: 0;
    border-top: 4px solid transparent; border-bottom: 4px solid transparent;
    border-left: 5px solid var(--text-muted); transition: transform 0.12s;
  }
  details[open] summary.game-summary::before, details[open] summary.picks-summary::before { transform: rotate(90deg); }
  .matchup-title { font-size: 14.5px; font-weight: 600; }
  .game-meta { color: var(--text-muted); font-size: 12px; }
  .game-line { color: var(--text-secondary); font-size: 12.5px; width: 100%; margin-top: 4px; }
  .proj-score { font-weight: 600; color: var(--text-primary); }
  .proj-picks { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--text-secondary); margin-top: 3px; }
  .proj-picks b { color: var(--text-primary); }
  .summary-flags { display: flex; gap: 6px; flex-wrap: wrap; }
  .game-body { padding: 4px 16px 16px; }

  .teams { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 8px; }
  @media (max-width: 720px) { .teams { grid-template-columns: 1fr; } }
  .headline-mobile-only { display: none; }
  @media (max-width: 480px) {
    /* On a narrow phone screen, a 5-column batter table squeezes "Recent
       form" into a nearly-unreadable ribbon of wrapped text. News (most
       players show no headline at all) gives up the least by disappearing
       here -- a matched headline still shows up inside the expanded row
       instead (.headline-mobile-only), just not in the collapsed table. */
    .col-news { display: none; }
    .headline-mobile-only { display: block; margin-bottom: 8px; }
  }
  .team-col { border-top: 1px solid var(--gridline); padding-top: 10px; }
  .team-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-weight: 600; font-size: 14px; margin-bottom: 8px; }
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
  .badge-alert { background: var(--status-critical); color: white; }
  .game-time { font-variant-numeric: tabular-nums; }
  .pitcher-row { cursor: pointer; }
  .pitcher-line { font-size: 13px; margin-bottom: 4px; color: var(--text-secondary); }
  .pitcher-line b { color: var(--text-primary); }
  .team-summary { list-style: disc; margin: 0 0 8px; padding-left: 18px; font-size: 12.5px; color: var(--text-secondary); }
  .team-summary li { margin-bottom: 3px; }
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

  .best-prop { font-size: 11.5px; margin-top: 4px; padding: 3px 7px; border-radius: 6px; display: inline-block; }
  .best-prop-over { background: var(--series-1-bg); color: var(--series-1); }
  .best-prop-under { background: rgba(224,51,63,0.14); color: var(--status-critical); }
  .matchup-explain { font-size: 11.5px; margin-top: 6px; padding: 5px 8px; border-radius: 6px; }
  .matchup-explain-good { background: var(--series-1-bg); color: var(--series-1); }
  .matchup-explain-tough { background: rgba(224,51,63,0.14); color: var(--status-critical); }
  .matchup-explain-caveat { background: var(--badge-neutral-bg); color: var(--text-secondary); }
  .grade-row { font-size: 12.5px; padding: 6px 8px; border-radius: 6px; margin-top: 4px; display: flex; justify-content: space-between; gap: 10px; }
  .grade-hit { background: rgba(15,157,88,0.14); color: var(--status-good); }
  .grade-miss { background: rgba(224,51,63,0.14); color: var(--status-critical); }
  .grade-no_data { background: var(--badge-neutral-bg); color: var(--text-secondary); }
  .grade-neutral { background: var(--badge-neutral-bg); color: var(--text-secondary); }

  .prop-detail td { background: var(--surface-2); }
  .prop-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; padding: 6px 2px; }
  .prop-cat { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
  .prop-cat-label { font-weight: 600; font-size: 12.5px; margin-bottom: 4px; }
  .prop-bars { position: relative; display: flex; align-items: flex-end; gap: 3px; height: 36px; margin: 6px 0 6px; }
  .prop-bar { position: relative; z-index: 1; flex: 1; border-radius: 2px 2px 0 0; min-height: 3px; cursor: default; }
  .prop-bar-over { background: var(--status-good); }
  .prop-bar-under { background: var(--status-critical); opacity: 0.85; }
  .prop-baseline {
    position: absolute; left: -2px; right: -2px; bottom: var(--baseline); z-index: 2;
    border-top: 1px dashed var(--text-muted);
  }
  .prop-projection { font-size: 11.5px; color: var(--text-secondary); margin-bottom: 2px; }
  .prop-projection b { color: var(--text-primary); }
  .prop-lean-over { color: var(--status-good); font-weight: 700; }
  .prop-lean-under { color: var(--status-critical); font-weight: 700; }
  .prop-rate { font-size: 11.5px; color: var(--text-secondary); }
  .prop-avg { font-size: 10.5px; color: var(--text-muted); margin-top: 4px; }

  .injury-report { margin: 14px 0; }
  .injury-report table { margin-top: 8px; }

  .picks-section {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
    margin: 14px 0; box-shadow: var(--shadow); overflow: hidden;
  }
  .picks-section[open] summary.picks-summary { border-bottom: 1px solid var(--gridline); }
  summary.picks-summary {
    cursor: pointer; padding: 13px 16px; display: flex; align-items: center; gap: 10px; list-style: none;
  }
  .picks-heading { font-size: 15px; font-weight: 700; }
  .picks-count { font-size: 12px; color: var(--text-muted); margin-left: auto; }
  .picks-subheading { font-size: 12px; color: var(--text-muted); padding: 0 16px; margin-top: 10px; }
  .pick-group-label {
    font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--text-muted); padding: 12px 16px 0;
  }
  .picks-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; padding: 10px 16px 16px; }
  .pick-card {
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px;
  }
  .pick-card-over { border-left: 4px solid var(--status-good); }
  .pick-card-under { border-left: 4px solid var(--status-critical); }
  .pick-rank { font-size: 11px; color: var(--text-muted); font-weight: 700; }
  .pick-name { font-size: 14.5px; font-weight: 700; margin: 2px 0; }
  .pick-matchup { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
  .pick-badges { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 6px; }
  .pick-reasons { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; padding-left: 18px; }
  .pick-reasons li { margin-bottom: 2px; }
  .pick-category {
    font-size: 12.5px; font-weight: 600; border-radius: 6px; padding: 6px 8px; margin-top: 6px;
  }
  .pick-category-over { background: var(--series-1-bg); color: var(--series-1); }
  .pick-category-under { background: rgba(224,51,63,0.14); color: var(--status-critical); }
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
function applyCategoryFilter() {
  const cat = document.getElementById('categoryFilter').value;
  document.querySelectorAll('.prop-cat').forEach(function (el) {
    el.style.display = (cat === 'all' || el.dataset.category === cat) ? '' : 'none';
  });
  document.querySelectorAll('.prop-detail').forEach(function (row) {
    if (cat === 'all') {
      row.style.display = 'none';
      return;
    }
    const match = row.querySelector('.prop-cat[data-category="' + cat + '"]');
    row.style.display = match ? 'table-row' : 'none';
  });
}
function toggleDetail(id) {
  const row = document.getElementById(id);
  if (row) row.style.display = (row.style.display === 'none' || !row.style.display) ? 'table-row' : 'none';
}
function localizeGameTimes() {
  document.querySelectorAll('.game-time').forEach(function (el) {
    const iso = el.dataset.utc;
    if (!iso) return;
    const d = new Date(iso);
    if (isNaN(d.getTime())) return;
    el.textContent = d.toLocaleString([], {
      weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
    });
  });
}
function initTheme() {
  const saved = localStorage.getItem('theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  const btn = document.getElementById('themeToggle');
  if (!btn) return;
  function updateLabel() {
    const current = document.documentElement.getAttribute('data-theme');
    const isDark = current ? current === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
    btn.textContent = isDark ? 'Switch to light' : 'Switch to dark';
  }
  btn.addEventListener('click', function () {
    const current = document.documentElement.getAttribute('data-theme');
    const isDark = current ? current === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
    const next = isDark ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    updateLabel();
  });
  updateLabel();
}
document.addEventListener('DOMContentLoaded', function () {
  // Guarded as a whole, not element-by-element: this script is shared with
  // pages (like track-record.html) that have no filter toolbar at all --
  // any one of these calling .addEventListener on a missing element would
  // throw and abort the rest of this handler, silently breaking initTheme()
  // and localizeGameTimes() too even though neither depends on the toolbar.
  if (document.getElementById('dateFilter')) {
    document.getElementById('dateFilter').addEventListener('change', applyFilters);
    document.getElementById('roleFilter').addEventListener('change', applyFilters);
    document.getElementById('searchBox').addEventListener('input', applyFilters);
    document.getElementById('confirmedOnly').addEventListener('change', applyFilters);
    document.getElementById('categoryFilter').addEventListener('change', applyCategoryFilter);
    applyFilters();
    applyCategoryFilter();
  }
  initTheme();
  localizeGameTimes();
});
</script>
"""

GLOSSARY_HTML = """
<details class="glossary">
  <summary>What do these terms mean?</summary>
  <dl>
    <dt>Confirmed vs. Projected lineup</dt>
    <dd>CONFIRMED means MLB has officially posted tonight's batting order (usually 1-2 hours before first pitch), and only the 9 players actually in that order are shown -- bench players are never included once a lineup is confirmed. PROJECTED means the game hasn't posted a lineup yet, so we're showing who's played most recently for that team as a best guess -- treat these as a bit less certain.</dd>
    <dt>HOT / COLD</dt>
    <dd>This player's batting average over their last 7 games is notably higher (HOT) or lower (COLD) than their season average. Backtested result: on its own this barely predicts what happens in the very next game -- short hot/cold streaks are mostly random noise, not a reliable signal by itself.</dd>
    <dt>"Likely luck" tag</dt>
    <dd>A hot streak driven by bloop hits falling in (measured by BABIP, the rate of batted balls that go for hits) rather than actually hitting the ball harder. These streaks tend to cool off faster than "real" hot streaks.</dd>
    <dt>Matchup edge</dt>
    <dd>This batter's handedness (lefty/righty) is a good matchup against tonight's specific opposing pitcher, who has historically struggled against that side.</dd>
    <dt>Best prop</dt>
    <dd>The single category where this player's last several games deviate the most from their own season norm -- e.g. "trending OVER 1.5 Total Bases (80% recently vs. 55% this season)". Not a guarantee, just the angle with the biggest recent shift.</dd>
    <dt>Bullpen taxed / rested</dt>
    <dd>How many innings that team's relief pitchers have thrown in the last 2 days. A taxed bullpen has been used a lot recently and may be less sharp late in the game -- this is factored into the game projection's run environment, not just shown as a note.</dd>
    <dt>Dominant form / Rough stretch (pitchers)</dt>
    <dd>This pitcher's ERA over his last several starts is notably better (DOMINANT) or worse (ROUGH) than his season ERA.</dd>
    <dt>Tough matchup for... (pitcher)</dt>
    <dd>Specific hitters in tonight's opposing lineup who are in an unfavorable spot against this pitcher (wrong-handed or facing a pitcher who dominates their side) -- the mirror image of a batter's own "matchup edge" tag.</dd>
    <dt>ERA (earned run average)</dt>
    <dd>Runs a pitcher allows per 9 innings pitched -- lower is better. Roughly: under 3.5 is very good, 4.5+ is below average.</dd>
    <dt>WHIP</dt>
    <dd>Walks + hits allowed per inning pitched -- lower is better, roughly measures how many baserunners a pitcher allows.</dd>
    <dt>Hit rate (in the prop breakdown)</dt>
    <dd>Out of this player's recent games, the percentage where they went OVER a given number. E.g. "80% over 1.5 total bases" means 8 of their last 10 games had 2+ total bases. We don't pull actual FanDuel/Sleeper lines, so compare this rate to whatever number the app shows you. In the bar chart, the dashed line marks that threshold -- green bars cleared it, red bars didn't -- and hovering a bar shows the exact number and date.</dd>
    <dt>Moneyline / Run line / Total pick</dt>
    <dd>Moneyline = which team our simulation (20,000+ Monte Carlo trials per game) thinks is more likely to win outright. Run line = the standard MLB spread (always +/-1.5 runs, whichever side is the moneyline favorite) and which side is more likely to cover it. Total = the projected combined-runs line (the median of the simulated outcomes, rounded to a half-run so it can't push) and whether the model leans over or under it. All three come from the same simulation, which uses each team's season-long scoring record (home/away split), the probable starters' season stats, actual bullpen quality plus recent bullpen fatigue, and -- once a lineup is confirmed -- a small adjustment for how those specific 9 hitters have actually been hitting lately.</dd>
    <dt>Why does the +1.5 underdog get picked so often?</dt>
    <dd>Because that's genuinely how MLB run lines behave, not a bug: most games are decided by 1 run, so "lose by only 1" (which still covers +1.5) is a much lower bar than "win by 2+" (needed to cover -1.5). Real sportsbooks don't change the 1.5 number for this -- they price it in with worse moneyline odds on the +1.5 side. Since this project doesn't pull real odds, a run-line pick here is a directional lean only, not a claim that it's a great-value bet.</dd>
  </dl>
</details>
"""


NORMAL_STATUSES = {"Scheduled", "Pre-Game", "Warmup", "In Progress", "Final", "Game Over", "Completed Early"}


def _slug(label):
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _fmt_date(d):
    if not d:
        return None
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%b %-d")
    except ValueError:
        return d


def _status_badge(status):
    if not status or status in NORMAL_STATUSES:
        return ""
    return _badge(status.upper(), "alert")


def _badge(label, kind):
    return f'<span class="badge badge-{kind}">{html.escape(label)}</span>'


def _trend_badge(trend):
    if trend == "hot":
        return _badge("HOT", "hot")
    if trend == "cold":
        return _badge("COLD", "cold")
    return ""


def _pitcher_form_badge(trend):
    if trend == "dominant":
        return _badge("DOMINANT FORM", "hot")
    if trend == "rough":
        return _badge("ROUGH STRETCH", "cold")
    return ""


def _injury_badge(injury):
    return _badge(f"INJURED ({injury['status']})", "injury") if injury else ""


def _matchup_badge(matchup):
    if not matchup:
        return ""
    if matchup.get("favorable"):
        return _badge("GOOD MATCHUP", "matchup")
    if matchup.get("unfavorable"):
        return _badge("TOUGH MATCHUP", "cold")
    return ""


_HAND_WORD = {"L": "left-handed", "R": "right-handed", "S": "switch-hitting"}


def _matchup_text(bat_side, matchup):
    """
    Plain-language reasoning behind the GOOD/TOUGH MATCHUP badge -- what
    actually makes it good or bad, not just the label. Built from the same
    two numbers the badge itself is computed from (platoon side + this
    specific pitcher's actual average-against that hand), so the
    explanation can never disagree with the badge.
    """
    if not matchup or matchup.get("favorable") is None:
        return None
    if not matchup.get("favorable") and not matchup.get("unfavorable"):
        return None
    side_word = _HAND_WORD.get(bat_side, "This")
    platoon_txt = (
        "has the platoon advantage (opposite-handed)" if matchup.get("platoon") == "opposite-hand"
        else "is on the same-handed side (no platoon advantage)"
    )
    avg = matchup.get("pitcher_avg_against")
    avg_txt = f"{avg:.3f}" if avg is not None else None
    if matchup.get("favorable"):
        base = f"{side_word.capitalize()} batter who {platoon_txt} against tonight's pitcher"
        if avg_txt:
            base += f", who has allowed a {avg_txt} average to that side this season"
        return base + "."
    base = f"{side_word.capitalize()} batter who {platoon_txt} against tonight's pitcher"
    if avg_txt:
        base += f", who has held that side to just a {avg_txt} average this season"
    return base + "."


def _streak_badge(streak):
    return _badge(f"{streak}-GAME HIT STREAK", "streak") if streak and streak >= 3 else ""


def _caveat_badge(trend_caveat):
    return _badge("LIKELY LUCK", "caveat") if trend_caveat == "babip_driven" else ""


def _best_prop_text(entry):
    prop = entry.get("best_prop")
    direction = entry.get("best_prop_direction")
    if not prop:
        return None
    verb = "OVER" if direction == "over" else "UNDER"
    pct = prop["pct"] if direction == "over" else 100 - prop["pct"]
    season_pct = prop.get("season_pct", prop["pct"])
    season_pct = season_pct if direction == "over" else 100 - season_pct
    return (
        f'Best prop: {html.escape(prop["label"])} &mdash; {verb} {prop["line"]} '
        f"({pct}% recently vs. {round(season_pct)}% this season)"
    )


def _best_prop_html(entry):
    text = _best_prop_text(entry)
    direction = entry.get("best_prop_direction")
    return f'<div class="best-prop best-prop-{direction}">{text}</div>' if text else ""


def _pitcher_matchup_text(matchup):
    if not matchup:
        return []
    tough = matchup.get("tough_matchups") or []
    exploitable = matchup.get("exploitable_matchups") or []
    lines = []
    if tough:
        lines.append(f'Tough matchup tonight for: {html.escape(", ".join(tough))}')
    if exploitable:
        lines.append(f'Watch out for: {html.escape(", ".join(exploitable))} (batter has the edge)')
    return lines


def _fatigue_text(fatigue):
    if not fatigue:
        return None
    ratio = fatigue["fatigue_ratio"]
    if ratio >= 1.2:
        return f'Opposing bullpen has thrown a lot lately: {fatigue["recent_innings"]} innings in the last 2 days'
    if ratio <= 0.7:
        return f'Opposing bullpen is well-rested: only {fatigue["recent_innings"]} innings in the last 2 days'
    return None


def _batter_result_text(gr):
    """This player's own actual line for this specific game -- None until it's been played and synced."""
    if not gr:
        return None
    parts = [f"{gr['hits']}-for-{gr['at_bats']}"]
    for count, label in ((gr.get("doubles"), "2B"), (gr.get("triples"), "3B"), (gr.get("home_runs"), "HR")):
        if count:
            parts.append(f"{count} {label}")
    if gr.get("rbi"):
        parts.append(f"{gr['rbi']} RBI")
    if gr.get("runs"):
        parts.append(f"{gr['runs']} R")
    if gr.get("base_on_balls"):
        parts.append(f"{gr['base_on_balls']} BB")
    if gr.get("strike_outs"):
        parts.append(f"{gr['strike_outs']} K")
    if gr.get("stolen_bases"):
        parts.append(f"{gr['stolen_bases']} SB")
    return ", ".join(parts)


def _pitcher_result_text(gr):
    if not gr:
        return None
    parts = [f"{gr['innings_pitched']} IP", f"{gr['hits']} H", f"{gr['earned_runs']} ER", f"{gr['strike_outs']} K", f"{gr['base_on_balls']} BB"]
    if gr.get("home_runs"):
        parts.append(f"{gr['home_runs']} HR")
    return ", ".join(parts)


def _category_projection_html(cat):
    line = cat["primary_line"]
    today = cat.get("today_projection")
    if today is None:
        return f"Projected line: <b>{line}</b>"
    lean = cat.get("lean")
    if lean is None:
        return f"Projected line: <b>{line}</b> &middot; Today: {today} (even)"
    return f'Projected line: <b>{line}</b> &middot; Today: <span class="prop-lean-{lean}">{today} (lean {lean.upper()})</span>'


def _prop_categories_html(categories, row_id, headline_html=""):
    if not categories:
        return (
            f'<tr id="{row_id}" class="prop-detail" style="display:none">'
            f'<td colspan="5">{headline_html}<span class="sub">No recent-game data yet.</span></td></tr>'
        )
    cats_html = []
    for cat in categories:
        values = cat.get("values") or []
        dates = cat.get("dates") or [None] * len(values)
        line = cat["primary_line"]
        max_v = max([*values, line]) or 1
        baseline_pct = min(max(line / max_v * 100, 0), 100)
        bars = []
        for v, d in zip(values, dates):
            is_over = v > line
            bar_cls = "prop-bar-over" if is_over else "prop-bar-under"
            height = max(v / max_v * 100, 4)
            date_txt = _fmt_date(d)
            tooltip = f"{v} {cat['label'].lower()}" + (f" on {date_txt}" if date_txt else "")
            bars.append(f'<div class="prop-bar {bar_cls}" style="height:{height:.0f}%" title="{html.escape(tooltip)}"></div>')
        rates = " &middot; ".join(f'{r["pct"]}% over {r["line"]}' for r in cat["hit_rates"])
        slug = _slug(cat["label"])
        cats_html.append(f"""
        <div class="prop-cat" data-category="{slug}">
          <div class="prop-cat-label">{html.escape(cat["label"])}</div>
          <div class="prop-projection">{_category_projection_html(cat)}</div>
          <div class="prop-bars" style="--baseline:{baseline_pct:.0f}%">
            <div class="prop-baseline"></div>
            {"".join(bars)}
          </div>
          <div class="prop-rate">{rates} (last {len(values)})</div>
          <div class="prop-avg">Avg {cat["average"]}/game recently &middot; dashed line = this player's own projected line &middot; hover a bar for the exact game</div>
        </div>
        """)
    return (
        f'<tr id="{row_id}" class="prop-detail" style="display:none">'
        f'<td colspan="5">{headline_html}<div class="prop-grid">{"".join(cats_html)}</div></td></tr>'
    )


def _pitcher_html(p, row_id, fatigue):
    if not p:
        return '<div class="pitcher-line sub">No probable pitcher announced</div>'
    l5 = p["l5"]
    l5_txt = (
        f"Last 5 starts: {l5['strike_outs']} strikeouts, {l5['earned_runs']} runs allowed, {l5['era']} ERA"
        if l5
        else "No recent starts on record yet"
    )
    badges = " ".join(x for x in [_pitcher_form_badge(p.get("form_trend")), _injury_badge(p["injury"])] if x)

    bullets = []
    result_text = _pitcher_result_text(p.get("game_result"))
    if result_text:
        bullets.append(f"<b>Final: {result_text}</b>")
    bullets.append(l5_txt)
    best_prop_text = _best_prop_text(p)
    if best_prop_text:
        bullets.append(best_prop_text)
    bullets.extend(_pitcher_matchup_text(p.get("opponent_matchup")))
    fatigue_text = _fatigue_text(fatigue)
    if fatigue_text:
        bullets.append(fatigue_text)
    if p.get("headlines"):
        h = p["headlines"][0]
        bullets.append(
            f'<a class="headline-link" href="{html.escape(h["link"])}" target="_blank" rel="noopener" '
            f'onclick="event.stopPropagation()">{html.escape(h["title"][:60])}</a>'
        )
    bullets_html = "".join(f"<li>{b}</li>" for b in bullets)

    return f"""
    <div class="pitcher-line pitcher-row" onclick="toggleDetail('{row_id}')">
      <span class="expand-arrow"></span><b>{html.escape(p["name"])}</b> ({html.escape(p["pitch_hand"] or "?")}, throws) {badges}
    </div>
    <ul class="team-summary">{bullets_html}</ul>
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
        result_text = _batter_result_text(b.get("game_result"))
        result_html = f"<b>Final: {result_text}</b><br>" if result_text else ""
        rows.append(
            f'<tr class="player-row" onclick="toggleDetail(\'{row_id}\')">'
            f"<td>{order}</td>"
            f'<td class="name-cell"><span class="expand-arrow"></span>{html.escape(b["name"])} '
            f'<span class="sub">({html.escape(b["bat_side"] or "?")} handed batter)</span></td>'
            f"<td>{badges}</td>"
            f'<td>{result_html}{html.escape(l7_txt)}<div class="sub">{season_txt}</div>{_best_prop_html(b)}</td>'
            f'<td class="col-news">{headline}</td></tr>'
        )
        # The News column is hidden on narrow screens (see .col-news media
        # query) -- .headline-mobile-only is the opposite (hidden except on
        # narrow screens), so a matched headline is still reachable there
        # instead of just disappearing along with the column.
        headline_detail = f'<div class="headline-mobile-only">{headline}</div>' if headline else ""
        matchup_text = _matchup_text(b["bat_side"], b.get("matchup"))
        matchup_kind = "good" if b["matchup"].get("favorable") else "tough"
        matchup_detail = f'<div class="matchup-explain matchup-explain-{matchup_kind}">{matchup_text}</div>' if matchup_text else ""
        rows.append(_prop_categories_html(b.get("prop_categories"), row_id, headline_html=headline_detail + matchup_detail))
    return "\n".join(rows)


def _team_col_html(side, id_prefix):
    tag_kind = "confirmed" if side["lineup_confirmed"] else "projected"
    tag_label = "LINEUP CONFIRMED" if side["lineup_confirmed"] else "PROJECTED (not yet announced)"
    return f"""
    <div class="team-col">
      <div class="team-title">{html.escape(side["team_name"] or "?")} {_badge(tag_label, tag_kind)}</div>
      <div class="pitcher-block">
        {_pitcher_html(side["probable_pitcher"], f"{id_prefix}-p", side.get("opponent_bullpen_fatigue"))}
      </div>
      <div class="batter-block">
        <table>
          <thead><tr><th>Order</th><th>Batter</th><th>Flags</th><th>Recent form</th><th class="col-news">News</th></tr></thead>
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
    taxed = sum(
        1 for side in (g["home"], g["away"])
        if side.get("opponent_bullpen_fatigue") and side["opponent_bullpen_fatigue"]["fatigue_ratio"] >= 1.2
    )
    if taxed:
        flags.append(_badge("BULLPEN TAXED", "caveat"))
    return "".join(flags)


def _game_line_html(g):
    if g.get("home_score") is not None:
        final_html = (
            f'<span class="proj-score">Final score: {html.escape(g["away"]["team_name"] or "?")} {g["away_score"]} '
            f'&ndash; {html.escape(g["home"]["team_name"] or "?")} {g["home_score"]}</span>'
        )
        p = g.get("projection")
        # p is this game's LAST projection before it went final -- comparing
        # it to the real score is exactly "how close was the model," so it's
        # kept alongside the final score rather than dropped once the
        # projection is no longer "live" information.
        if not p or p.get("home_exp_runs") is None or p.get("away_exp_runs") is None:
            return f'<div class="game-line">{final_html}</div>'
        proj_total = p["away_exp_runs"] + p["home_exp_runs"]
        actual_total = g["away_score"] + g["home_score"]
        diff = round(actual_total - proj_total, 1)
        diff_txt = f"+{diff}" if diff >= 0 else str(diff)
        return f"""
        <div class="game-line">
          {final_html}
          <div class="proj-picks"><span>Projected: <b>{p["away_exp_runs"]} &ndash; {p["home_exp_runs"]}</b> (total off by {diff_txt})</span></div>
        </div>
        """
    p = g.get("projection")
    if not p:
        return ""

    # A team can't literally score half a run -- unlike the total-runs LINE
    # (always .5, so it can't push), the projected score itself is just an
    # average outcome, so it's shown as the raw decimal (e.g. "3.2 - 4.7").
    home_score, away_score = p["home_exp_runs"], p["away_exp_runs"]

    home_win_prob = p["home_win_prob"]
    ml_pick = p.get("moneyline_pick") or ("home" if home_win_prob >= 0.5 else "away")
    ml_team = g["home"]["team_name"] if ml_pick == "home" else g["away"]["team_name"]
    ml_prob = home_win_prob if ml_pick == "home" else 1 - home_win_prob

    spread_line = p.get("spread_line", 1.5)
    # spread_favorite is who's actually assigned -1.5 (the moneyline
    # favorite, matching real sportsbook convention) -- NOT always home.
    # Legacy projection snapshots predating this fix don't have it; falling
    # back to home_win_prob (always present) derives the same answer rather
    # than assuming home, which was the actual bug being fixed here.
    spread_favorite = p.get("spread_favorite") or ("home" if home_win_prob >= 0.5 else "away")
    spread_pick = p.get("spread_pick")
    spread_prob = p.get("spread_pick_prob")
    if spread_pick is None:
        cover = p.get("spread_cover_prob") or 0
        spread_pick = "home" if cover >= 0.5 else "away"
        spread_prob = cover if spread_pick == "home" else 1 - cover
    spread_team = g["home"]["team_name"] if spread_pick == "home" else g["away"]["team_name"]
    spread_side = f"-{spread_line}" if spread_pick == spread_favorite else f"+{spread_line}"

    total_pick = p.get("total_pick") or ("over" if p["over_prob"] >= 0.5 else "under")
    total_prob = p.get("total_pick_prob")
    if total_prob is None:
        total_prob = max(p["over_prob"], 1 - p["over_prob"])

    return f"""
    <div class="game-line">
      <span class="proj-score">Projected score: {html.escape(g["away"]["team_name"] or "?")} {away_score} &ndash; {html.escape(g["home"]["team_name"] or "?")} {home_score}</span>
      <div class="proj-picks">
        <span>Moneyline: <b>{html.escape(ml_team or "?")}</b> to win ({round(ml_prob * 100)}%)</span>
        <span>Run line: <b>{html.escape(spread_team or "?")} {spread_side}</b> ({round(spread_prob * 100)}% to cover)</span>
        <span>Total {p['total_line']}: lean <b>{total_pick.upper()}</b> ({round(total_prob * 100)}%)</span>
      </div>
    </div>
    """


def _game_card_html(g):
    is_confirmed = "true" if (g["home"]["lineup_confirmed"] or g["away"]["lineup_confirmed"]) else "false"
    id_prefix = f"g{g['game_pk']}"
    return f"""
    <details class="game-card" data-date="{html.escape(g["date"])}" data-confirmed="{is_confirmed}"
              data-search="{_game_search_blob(g)}">
      <summary class="game-summary">
        <span class="matchup-title">{html.escape(g["away"]["team_name"] or "?")} @ {html.escape(g["home"]["team_name"] or "?")}</span>
        <span class="summary-flags">{_game_summary_flags(g)}{_status_badge(g["status"])}</span>
        <span class="game-meta">
          <span class="game-time" data-utc="{html.escape(g["game_time_utc"] or "")}">{html.escape(g["game_time_utc"] or "")}</span>
          {f" &middot; {html.escape(g['status'])}" if g["status"] and g["status"] != "Scheduled" else ""}
          &middot; {html.escape(g["venue"] or "")}
        </span>
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
    injury_count = _count_team_injuries(games)
    tiles = [
        (str(total_games), "Games in range"),
        (str(confirmed_sides), "Confirmed lineups"),
        (str(hot_count), "Hot batters flagged"),
        (str(matchup_count), "Good platoon matchups"),
        (str(injury_count), "Injury flags"),
    ]
    return "".join(
        f'<div class="stat-tile"><div class="stat-value">{v}</div><div class="stat-label">{html.escape(l)}</div></div>'
        for v, l in tiles
    )


def _team_injuries(games):
    """Deduped (team, player) list across every side in this date range -- a team playing multiple games in range would otherwise double-count its own injured players."""
    seen = set()
    rows = []
    for g in games:
        for side in (g["home"], g["away"]):
            for inj in side.get("injuries") or []:
                key = (side["team_name"], inj["player_name"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append((side["team_name"], inj))
    return rows


def _count_team_injuries(games):
    return len(_team_injuries(games))


def _injury_report_html(games):
    rows = _team_injuries(games)
    if not rows:
        return ""
    rows.sort(key=lambda r: r[1]["date"], reverse=True)
    body = "".join(
        f"<tr><td>{html.escape(team or '?')}</td><td>{html.escape(inj['player_name'])}</td>"
        f'<td>{_badge(inj["status"], "injury")}</td><td class="sub">{html.escape(inj.get("description") or "")}</td></tr>'
        for team, inj in rows
    )
    return f"""
    <details class="injury-report picks-section">
      <summary class="picks-summary">
        <span class="picks-heading">Injury Report</span>
        <span class="picks-count">{len(rows)} player(s)</span>
      </summary>
      <table>
        <thead><tr><th>Team</th><th>Player</th><th>Status</th><th>Note</th></tr></thead>
        <tbody>{body}</tbody>
      </table>
    </details>
    """


def _toolbar(games, batter_labels, pitcher_labels):
    dates = sorted({g["date"] for g in games})
    options = ['<option value="all">All dates</option>']
    for d in dates:
        options.append(f'<option value="{html.escape(d)}">{html.escape(d)}</option>')

    batter_cat_options = "".join(
        f'<option value="{_slug(label)}">{html.escape(label)}</option>' for label in batter_labels
    )
    pitcher_cat_options = "".join(
        f'<option value="{_slug(label)}">{html.escape(label)}</option>' for label in pitcher_labels
    )

    return f"""
    <div class="toolbar">
      <select id="dateFilter">{"".join(options)}</select>
      <select id="roleFilter">
        <option value="all">Batters &amp; Pitchers</option>
        <option value="batters">Batters only</option>
        <option value="pitchers">Pitchers only</option>
      </select>
      <select id="categoryFilter">
        <option value="all">All prop categories</option>
        <optgroup label="Batting">{batter_cat_options}</optgroup>
        <optgroup label="Pitching">{pitcher_cat_options}</optgroup>
      </select>
      <input id="searchBox" type="text" placeholder="Search team or player...">
      <label><input type="checkbox" id="confirmedOnly"> Confirmed lineups only</label>
      <span class="count" id="resultCount"></span>
    </div>
    """


def _pick_card_html(pick, rank, direction):
    reasons_html = "".join(f"<li>{html.escape(r)}</li>" for r in pick["reasons"])
    category_html = ""
    if pick["best_category"]:
        c = pick["best_category"]
        verb = "over" if direction == "over" else "under"
        pct = c["pct"] if direction == "over" else 100 - c["pct"]
        category_html = (
            f'<div class="pick-category pick-category-{direction}">Best angle: {html.escape(c["label"])} '
            f'&mdash; {verb} {c["line"]} in {pct}% of the last {c["n"]} games</div>'
        )
    elif pick.get("fallback_angle"):
        # This pick qualified on a signal other than a specific prop category
        # (matchup edge, hot/cold trend) -- prop_category_delta() needs 8+
        # recent games (4+ for pitchers) AND a real deviation from the
        # player's own norm, so it can legitimately come back empty even for
        # a real pick. Showing the number behind whichever signal DID get
        # them here beats leaving the card with reasons but no hard number.
        category_html = f'<div class="pick-category pick-category-{direction}">Best angle: {html.escape(pick["fallback_angle"])}</div>'
    is_pitcher = pick.get("role") == "pitcher"
    badges = [_badge("PITCHER PROP", "matchup") if is_pitcher else _badge("BATTER PROP", "caveat")]
    if not is_pitcher:
        lineup_kind = "confirmed" if pick["lineup_confirmed"] else "projected"
        lineup_label = "LINEUP CONFIRMED" if pick["lineup_confirmed"] else "LINEUP PROJECTED"
        badges.append(_badge(lineup_label, lineup_kind))
    tag = "OVER" if direction == "over" else "UNDER"
    return f"""
    <div class="pick-card pick-card-{direction}">
      <div class="pick-rank">#{rank} {tag}</div>
      <div class="pick-name">{html.escape(pick["name"])}</div>
      <div class="pick-matchup">{html.escape(pick["team"] or "?")} vs. {html.escape(pick["opponent"] or "?")}</div>
      <div class="pick-badges">{"".join(badges)}</div>
      <ul class="pick-reasons">{reasons_html}</ul>
      {category_html}
    </div>
    """


def _pick_group_html(group_label, picks, direction):
    if not picks:
        return ""
    cards = "".join(_pick_card_html(p, i, direction) for i, p in enumerate(picks, 1))
    return f"""
    <div class="pick-group-label">{html.escape(group_label)}</div>
    <div class="picks-grid">{cards}</div>
    """


def _picks_section_html(heading, subheading, picks, direction):
    """
    `picks` is {"batters": [...], "pitchers": [...]} -- rendered as two
    separately-numbered rankings (each its own #1, #2, ...) rather than one
    merged 1-N list. Batter and pitcher scores aren't on a comparable point
    scale, so sorting them together would silently rank every pitcher below
    every batter regardless of actual confidence -- looking like "pitchers
    are always the worst picks" when it's really just a scale mismatch.
    """
    if not picks or not (picks.get("batters") or picks.get("pitchers")):
        return ""
    total = len(picks.get("batters") or []) + len(picks.get("pitchers") or [])
    return f"""
    <details class="picks-section">
      <summary class="picks-summary">
        <span class="picks-heading">{html.escape(heading)}</span>
        <span class="picks-count">{total} picks</span>
      </summary>
      <div class="picks-subheading">{html.escape(subheading)}</div>
      {_pick_group_html("Batters", picks.get("batters"), direction)}
      {_pick_group_html("Pitchers", picks.get("pitchers"), direction)}
    </details>
    """


def _top_picks_html(top_overs, top_unders):
    common = "Injured players excluded. Players whose lineup spot isn't confirmed yet are marked PROJECTED. Includes both batter props and pitcher props. Not a guarantee, just where the signals point."
    return _picks_section_html(
        "Today's Top Overs", f"Bet the OVER on these. {common}", top_overs, "over"
    ) + _picks_section_html(
        "Today's Top Unders", f"Bet the UNDER on these (cold bats, tough matchups, or a pitcher expected to limit runs/hits). {common}", top_unders, "under"
    )


def render_html(report):
    notes_html = ""
    if os.path.exists(NOTES_PATH):
        with open(NOTES_PATH) as f:
            notes = f.read().strip()
        if notes:
            notes_html = f'<div class="notes"><b>Notes</b>\n{html.escape(notes)}</div>'

    games = report["games"]
    batter_labels = report.get("batter_prop_labels") or DEFAULT_BATTER_LABELS
    pitcher_labels = report.get("pitcher_prop_labels") or DEFAULT_PITCHER_LABELS

    if not games:
        body = '<div class="empty">No games in range.</div>'
    else:
        sections = []
        for date, group in groupby(games, key=lambda g: g["date"]):
            group_games = list(group)
            # All game cards collapsed by default now, today included -- with
            # a full slate of games each expanding to a two-team batter/pitcher
            # breakdown, leaving today's games auto-expanded made the page
            # enormous on first load instead of a scannable list to click into.
            cards = "".join(_game_card_html(g) for g in group_games)
            sections.append(f'<div data-date-group><div class="date-heading">{html.escape(date)}</div>{cards}</div>')
        body = _toolbar(games, batter_labels, pitcher_labels) + "".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- This page regenerates every 15 min; without these, browsers (and some
     intermediate caches) can hold onto a stale copy well past that, which
     looks exactly like "the site isn't updating" even when it is. GitHub
     Pages' own CDN edge cache is a separate layer this can't reach -- a
     fresh deploy can still take several minutes to propagate there -- so
     if this page looks stale, check the "Generated" timestamp below the
     title before assuming something's broken. -->
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>MLB Player Props Dashboard</title>
{STYLE}
</head>
<body>
  <div class="page">
    <div class="header-band">
      <div class="header-top">
        <div>
          <h1>MLB Player Props Dashboard</h1>
          <div class="meta">Generated {html.escape(report["generated_at"])}</div>
        </div>
        <div class="header-actions">
          <a class="nav-link" href="track-record.html">Track Record</a>
          <button id="themeToggle" class="theme-toggle" type="button">Switch to dark</button>
        </div>
      </div>
    </div>
    <div class="stat-row">{_stat_tiles(games)}</div>
    {_injury_report_html(games)}
    {_top_picks_html(report.get("top_overs"), report.get("top_unders"))}
    {GLOSSARY_HTML}
    {notes_html}
    {body}
  </div>
{SCRIPT}
</body>
</html>"""
