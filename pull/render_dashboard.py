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
    /* No overflow:hidden here -- it would establish a clipping scroll
       container that breaks position:sticky on the summary below. Corners
       are rounded on the summary/body directly instead. */
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
    margin-bottom: 10px; box-shadow: var(--shadow);
  }
  details.game-card:not([open]) summary.game-summary { border-radius: 12px; }
  details.game-card[open] summary { border-bottom: 1px solid var(--gridline); }
  summary.game-summary {
    cursor: pointer; padding: 12px 16px; display: flex; justify-content: space-between;
    align-items: center; flex-wrap: wrap; gap: 8px; list-style: none;
    /* Sticky while a card is open and scrolled into -- the whole point of
       an open game card is its (long) body, so without this, closing it
       again meant scrolling all the way back up to find the header.
       top is the *toolbar's own height* (syncStickyOffset in SCRIPT below
       sets --sticky-offset), not 0 -- the toolbar above is ALSO sticky at
       top:0 with a higher z-index, so stacking both at top:0 let the
       toolbar paint over this summary's own title/score instead of
       sitting above it. Falls back to 0px on pages with no toolbar. */
    position: sticky; top: var(--sticky-offset, 0px); z-index: 3; background: var(--surface-1); border-radius: 12px 12px 0 0;
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
  .game-body { padding: 4px 16px 16px; border-radius: 0 0 12px 12px; overflow: hidden; }

  .teams { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 8px; }
  @media (max-width: 720px) { .teams { grid-template-columns: 1fr; } }
  .headline-mobile-only { display: none; }
  .team-col { border-top: 1px solid var(--gridline); padding-top: 10px; }
  .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }

  /* Below 640px, a 5-column batter table has no honest way to fit --
     shrinking columns squeezes "Recent form" into an unreadable ribbon,
     and letting the table scroll horizontally (an earlier attempt) just
     hid that same column off-screen, leaving a tall, apparently-empty gray
     row with no visible hint there was more to see. A real per-player CARD
     (each cell stacked full-width, relabeled via ::before since the column
     headers no longer apply) is the only version of this that's actually
     readable on a phone -- the same pattern MLB.com's own mobile boxscore
     uses instead of a cramped table. */
  @media (max-width: 640px) {
    .table-scroll { overflow-x: visible; }
    .table-scroll table { min-width: 0; }
    .col-news { display: none; }
    .headline-mobile-only { display: block; margin-bottom: 8px; }
    .batter-block table, .batter-block tbody { display: block; width: 100%; }
    .batter-block thead { display: none; }
    .batter-block tr.player-row {
      display: block; border: 1px solid var(--border); border-radius: 8px;
      padding: 10px 12px; margin-bottom: 8px; background: var(--surface-1) !important;
    }
    .batter-block tr.prop-detail { display: block; }
    .batter-block td { display: block; padding: 3px 0; border: none; }
    .batter-block td[data-label]::before {
      content: attr(data-label); display: block; font-size: 9.5px; font-weight: 700;
      color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.03em;
    }
    .batter-block td[data-label="Batter"]::before,
    .batter-block td[data-label="Order"]::before { content: none; }
    .batter-block td[data-label="Order"] { display: inline; margin-right: 6px; font-weight: 700; color: var(--text-muted); }
    .batter-block td[data-label="Batter"] { display: inline; }
    .batter-block td[data-label="Flags"]:empty { display: none; }
    /* .batter-block td's own display:block above outranks the plain
       .col-news rule (higher specificity), so it has to be re-stated here
       against the actual cell selector to still take effect. */
    .batter-block td.col-news { display: none; }
  }
  .team-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-weight: 600; font-size: 14px; margin-bottom: 2px; }
  .team-form { margin-bottom: 8px; }
  .team-form:empty { display: none; }
  .badge {
    display: inline-flex; align-items: center; gap: 4px; font-size: 10.5px; font-weight: 700;
    padding: 2px 7px; border-radius: 999px; line-height: 1.6; white-space: nowrap;
  }
  .badge-confirmed { background: var(--series-1); color: white; }
  .badge-projected { background: var(--badge-neutral-bg); color: var(--badge-neutral-text); }
  .badge-hot { background: var(--status-good); color: white; }
  .badge-cold { background: var(--status-critical); color: white; }
  .badge-injury { background: var(--status-warning); color: #1a1a19; }
  .badge-hit { background: var(--status-good); color: white; }
  .badge-miss { background: var(--status-critical); color: white; }
  .badge-dnp { background: var(--badge-neutral-bg); color: var(--badge-neutral-text); }
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
  /* Live-tracker highlights the batter/pitcher currently in the game --
     set/cleared by highlightActivePlayers() every poll, matched against
     these rows by the same person id the boxscore uses. A tinted row
     background plus a plain colored text label next to the name -- NOT
     another rounded .badge pill -- so "who's up right now" reads as a
     highlight on the row itself, distinct from the HOT/COLD/matchup
     bubbles rather than just one more bubble among them. */
  tr.player-row.is-batting { background: rgba(15,157,88,0.12); }
  tr.player-row.is-ondeck { background: rgba(245,166,35,0.12); }
  tr.player-row.is-batting td.name-cell, tr.player-row.is-ondeck td.name-cell { font-weight: 700; }
  .live-tag-text {
    margin-left: 6px; font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em;
  }
  tr.player-row.is-batting .live-tag-text { color: var(--status-good); }
  tr.player-row.is-ondeck .live-tag-text { color: var(--status-warning); }
  .pitcher-line.is-pitching { font-weight: 700; color: var(--text-primary); }
  .pitcher-line.is-pitching .live-tag-text { color: var(--status-good); }
  .expand-arrow {
    display: inline-block; width: 0; height: 0; margin-right: 6px; vertical-align: middle;
    border-top: 3px solid transparent; border-bottom: 3px solid transparent;
    border-left: 4px solid var(--text-muted);
  }
  .sub { color: var(--text-muted); font-size: 11px; }
  .headline-link { color: var(--series-1); text-decoration: none; font-size: 11px; }
  .headline-link:hover { text-decoration: underline; }
  .empty { color: var(--text-muted); font-size: 13px; padding: 40px 20px; text-align: center; }

  /* Relief pitchers / pinch-hitters / defensive subs never get a table row
     of their own -- only players with a prop projection (the confirmed/
     projected lineup + probable starter) do. Rather than silently drop
     their live stats, updatePlayerBoxScores() appends a compact line here
     for anyone who shows up in the live boxscore without one -- no props
     for them (nothing was projected), just their actual line. */
  .subs-list:empty { display: none; }
  .subs-list { margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--border); }
  .subs-list-heading { font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 6px; }
  /* Pitching always listed before Batting (upsertSubLine creates both up
     front, in this order); a group with nobody in it yet (e.g. no pinch
     hitters used, only relievers) just hides instead of showing an empty
     heading. */
  .subs-group:not(:has(.sub-player-line)) { display: none; }
  .subs-group + .subs-group { margin-top: 10px; }
  .subs-group-heading { font-size: 10.5px; font-weight: 600; color: var(--text-muted); margin-bottom: 6px; }
  .sub-player-line { margin-bottom: 8px; }
  .sub-player-name { font-size: 12.5px; font-weight: 600; margin-bottom: 4px; }
  .sub-player-name .sub { font-weight: 400; }

  .boxscore-line:empty { display: none; }
  .boxscore-line {
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 10px; margin-bottom: 6px; max-width: 220px;
  }
  .bx-header { margin-bottom: 6px; display: flex; align-items: center; gap: 6px; }
  .bx-volume { font-size: 11px; color: var(--text-muted); }
  .bx-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px 4px; text-align: center; }
  .bx-grid + .bx-grid { margin-top: 6px; }
  .bx-stat { display: flex; flex-direction: column; align-items: center; }
  .bx-stat b { font-size: 13.5px; line-height: 1.15; }
  .bx-stat small { font-size: 9px; color: var(--text-muted); letter-spacing: 0.03em; }
  .badge-live { background: var(--status-critical); color: white; }
  .badge-live::before {
    content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    background: white; animation: bx-pulse 1.2s ease-in-out infinite;
  }
  @keyframes bx-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }
  .badge-final { background: var(--badge-neutral-bg); color: var(--badge-neutral-text); }

  /* Client-side live tracker (live_tracker in SCRIPT below) -- populated by
     polling MLB's own live-feed API directly from the browser, not by our
     periodic build pipeline, so it can update on a ~30s cadence instead of
     waiting on the next scheduled sync. Empty (and hidden) until JS fills
     it in for a game that's actually in progress.

     Split into two containers, not one: .live-tracker lives in <summary>
     (sticky + visible even collapsed) and is deliberately kept SHORT --
     score/inning/diamond/count/win-prob only. The heavier detail (strike
     zone, full matchup, pitch-by-pitch, recent notable event) lives in
     .live-detail inside the card body instead. A sticky element with the
     full detail's height (400px+) would sit pinned over the lineup table
     as you scroll past it, hiding whatever scrolled up underneath -- this
     way only the genuinely short summary strip ever gets pinned. */
  .live-tracker:empty, .live-detail:empty { display: none; }
  .live-tracker, .live-detail {
    width: 100%; padding: 12px 14px; border-radius: 10px;
    background: var(--surface-2); border: 1px solid var(--border); font-size: 12.5px;
  }
  .live-tracker { margin-top: 8px; }
  .live-detail { margin-bottom: 14px; }
  .live-top-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
  .live-score-line { font-weight: 700; font-size: 19px; letter-spacing: -0.02em; }
  .live-inning { color: var(--text-secondary); white-space: nowrap; font-weight: 600; }
  .live-updated { color: var(--text-muted); font-size: 10.5px; margin-left: auto; white-space: nowrap; }

  .live-field-row { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  .live-state-panel {
    display: flex; flex-direction: column; align-items: center; gap: 6px;
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 12px; flex: none;
  }
  /* A thin outlined diamond path (the infield), not a filled background --
     a filled dirt-colored diamond behind similarly-sized base markers just
     blended into one cluttered shape instead of reading as "3 bases on a
     field." */
  .diamond { position: relative; width: 42px; height: 42px; flex: none; }
  .diamond::before {
    content: ""; position: absolute; top: 3px; left: 3px; right: 3px; bottom: 3px;
    border: 1.5px solid var(--border); transform: rotate(45deg);
  }
  .diamond .base {
    position: absolute; width: 11px; height: 11px; border: 2px solid var(--text-muted);
    background: var(--surface-2); transform: rotate(45deg); z-index: 1; box-sizing: border-box;
  }
  .diamond .base-2b { top: -3px; left: 15.5px; }
  .diamond .base-3b { top: 15.5px; left: -3px; }
  .diamond .base-1b { top: 15.5px; left: 34px; }
  .diamond .base-occupied { background: var(--status-warning); border-color: var(--status-warning); }
  .live-count-outs { display: flex; align-items: center; gap: 8px; }
  .live-count { font-variant-numeric: tabular-nums; font-weight: 700; color: var(--text-primary); white-space: nowrap; font-size: 13px; }
  .live-outs { display: inline-flex; gap: 3px; align-items: center; }
  .out-dot { width: 9px; height: 9px; border-radius: 50%; border: 1.5px solid var(--status-warning); display: inline-block; }
  .out-dot-filled { background: var(--status-warning); }

  /* Win probability: a statistical estimate from the live score/inning/
     outs/baserunner state (computeWinProbability in SCRIPT below), not a
     guarantee -- labeled "Est." throughout so it doesn't read as more
     certain than it is. */
  .wp-panel { display: flex; flex-direction: column; gap: 4px; min-width: 140px; flex: 1 1 140px; }
  .wp-panel-label { font-size: 9.5px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .wp-bar { display: flex; height: 8px; border-radius: 999px; overflow: hidden; background: var(--surface-3); }
  .wp-bar-away { background: var(--text-muted); }
  .wp-bar-home { background: var(--series-1); }
  .wp-teams { display: flex; justify-content: space-between; font-size: 11px; color: var(--text-secondary); white-space: nowrap; }
  .wp-teams b { color: var(--text-primary); }

  .sz-panel { display: flex; flex-direction: column; align-items: center; gap: 4px; flex: none; }
  .sz-panel-label { font-size: 9.5px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .strike-zone { flex: none; border-radius: 6px; }
  .sz-bg { fill: var(--surface-1); stroke: var(--border); stroke-width: 1; }
  .sz-zone { fill: rgba(127,127,127,0.06); stroke: var(--text-secondary); stroke-width: 1.75; }
  .pitch-dot-num { font-size: 8px; font-weight: 700; fill: white; pointer-events: none; }
  .pitch-dot { stroke: var(--surface-1); stroke-width: 1.5; }
  /* fill colors the SVG pitch-location dots; background colors the same
     classes reused as small HTML dots in the pitch-by-pitch list below --
     harmless on the element type that doesn't use it. */
  .pitch-dot-ball { fill: var(--series-1); background: var(--series-1); }
  .pitch-dot-strike { fill: var(--status-critical); background: var(--status-critical); }
  .pitch-dot-inplay { fill: var(--status-good); background: var(--status-good); }
  .pitch-dot-other { fill: var(--text-muted); background: var(--text-muted); }

  .pitch-seq { margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--border); display: flex; flex-direction: column; gap: 4px; }
  .pitch-seq-heading { font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 2px; }
  .pitch-seq-row { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--text-secondary); }
  .pitch-seq-num { color: var(--text-muted); font-variant-numeric: tabular-nums; width: 14px; flex: none; text-align: right; }
  .pitch-seq-dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
  .pitch-seq-speed { font-weight: 600; color: var(--text-primary); white-space: nowrap; }

  .live-matchup { display: flex; flex-direction: column; gap: 3px; font-size: 13px; }
  .live-matchup .sub { font-size: 12px; }
  .live-batter { color: var(--text-secondary); }
  .live-batter b { color: var(--text-primary); font-size: 14px; }
  .live-notable {
    display: block; width: 100%; font-style: italic; color: var(--text-secondary); font-size: 12px;
    margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--border);
  }
  .recent-plays:empty { display: none; }
  .recent-plays { margin-bottom: 14px; }
  .plays-heading { font-size: 13px; font-weight: 700; margin-bottom: 6px; }
  .play-row {
    font-size: 12.5px; padding: 5px 8px; border-radius: 6px; display: flex; gap: 8px;
    margin-bottom: 3px; background: var(--surface-2);
  }
  .play-inning { color: var(--text-muted); font-variant-numeric: tabular-nums; white-space: nowrap; text-transform: capitalize; min-width: 58px; }
  .play-action { background: var(--badge-neutral-bg); font-style: italic; color: var(--text-secondary); }
  /* A run actually scoring is the one event in this feed worth calling out
     at a glance -- MLB's own feed already flags it (about.isScoringPlay),
     no text-sniffing needed. */
  .play-row.play-scoring { background: rgba(245,166,35,0.22); }
  .play-row.play-scoring .play-inning { color: var(--text-primary); font-weight: 700; }
  .best-prop { font-size: 11.5px; margin-top: 4px; padding: 3px 7px; border-radius: 6px; display: inline-block; }
  .best-prop-over { background: var(--series-1-bg); color: var(--series-1); }
  .best-prop-under { background: rgba(224,51,63,0.14); color: var(--status-critical); }
  /* Deliberately a plain colored line, not a pill like .best-prop above --
     this is a different signal (today's matchup-adjusted lean, next to
     the actual box score once the game's started) and shouldn't be
     visually interchangeable with it. */
  .matchup-lean { font-size: 11.5px; font-weight: 600; margin: 4px 0; }
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
function toggleDetail(id) {
  const row = document.getElementById(id);
  // '' (not a hardcoded 'table-row') so the CSS cascade decides the shown
  // display value -- a mobile media query turns this same row into a
  // stacked block instead of a table-row, and a hardcoded value here would
  // silently win over that rule (inline styles beat stylesheet rules).
  // Checking ONLY '=== none' (not the old "|| falsy" fallback) matters: an
  // empty string is falsy too, so that fallback made every row un-closable
  // after the first click open -- both branches of the old OR were true
  // forever once display became ''.
  if (row) row.style.display = (row.style.display === 'none') ? '' : 'none';
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
// Accordion: opening one game card closes any other open one. With every
// card able to expand into a long batter/pitcher/live-tracker body, having
// several open at once made the page enormous and hard to navigate --
// only ever one open keeps the page scannable, and (with the sticky
// summary above) there's always exactly one header to find and click.
function initGameCardAccordion() {
  const cards = document.querySelectorAll('details.game-card');
  cards.forEach(function (card) {
    card.addEventListener('toggle', function () {
      if (!card.open) return;
      cards.forEach(function (other) {
        if (other !== card) other.open = false;
      });
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
// Live game tracker: polls MLB's own public live-feed API (CORS-open,
// verified: access-control-allow-origin: *) directly from the browser, on
// a short interval -- our own build only regenerates every ~15 minutes,
// which is far too slow for "is this at-bat still going." Only ever reads
// from MLB's API; never writes anything, so there's no auth/key needed.
const LIVE_POLL_MS = 30000;
let liveTrackedEls = [];

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

function initLiveTracker() {
  const now = Date.now();
  const hasStarted = function (el) {
    const timeEl = el.querySelector('.game-time');
    const t = timeEl ? timeEl.dataset.utc : null;
    if (!t) return false;
    const startMs = new Date(t).getTime();
    return !isNaN(startMs) && startMs <= now; // don't waste requests on a game that hasn't started yet
  };
  const allCards = Array.from(document.querySelectorAll('details.game-card[data-game-pk]'));
  liveTrackedEls = allCards.filter(function (el) { return el.dataset.final !== 'true' && hasStarted(el); });
  // A game already Final as of this build still gets exactly ONE poll of
  // MLB's real feed (not added to the recurring liveTrackedEls -- no point
  // re-polling a decided game every 30s forever). Our own server-rendered
  // box score only has a container for players who had a prop projection
  // to begin with (the starting lineup + probable pitcher) -- a reliever or
  // pinch-hitter who never got one needs this one real fetch for
  // updatePlayerBoxScores' subs-list handling to ever see them.
  allCards.filter(function (el) { return el.dataset.final === 'true' && hasStarted(el); }).forEach(pollLiveGame);
  if (!liveTrackedEls.length) return;
  pollAllLiveGames();
  setInterval(pollAllLiveGames, LIVE_POLL_MS);
  // A backgrounded tab shouldn't keep polling every game on its own timer;
  // catching back up the moment the tab is visible again feels just as
  // "live" without burning requests the whole time no one's looking.
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') pollAllLiveGames();
  });
}

function pollAllLiveGames() {
  if (document.visibilityState === 'hidden') return;
  liveTrackedEls.forEach(pollLiveGame);
}

function pollLiveGame(el) {
  if (el.dataset.liveDone === 'true') return; // confirmed Final by a previous poll -- stop hitting the API for it
  const pk = el.dataset.gamePk;
  fetch('https://statsapi.mlb.com/api/v1.1/game/' + pk + '/feed/live')
    .then(function (r) { return r.json(); })
    .then(function (data) { renderLiveState(el, data); })
    .catch(function () { /* transient network hiccup -- keep last known state, just try again next cycle */ });
}

function statOr0(v) {
  return v == null ? 0 : v;
}

function boxHeader(isFinal, volumeLabel, volumeValue) {
  const badge = isFinal ? '<span class="badge badge-final">FINAL</span>' : '<span class="badge badge-live">LIVE</span>';
  const volume = volumeLabel ? '<span class="bx-volume">' + volumeValue + ' ' + volumeLabel + '</span>' : '';
  return '<div class="bx-header">' + badge + volume + '</div>';
}

function statCellHtml(label, value) {
  return '<div class="bx-stat"><b>' + value + '</b><small>' + label + '</small></div>';
}

// Main grid leads with IP then exactly PITCHER_PROP_CATEGORIES (Strikeouts,
// Runs Allowed = earned runs, Hits Allowed, Walks Allowed) -- see
// _pitcher_boxscore_html()'s own docstring. Shared by the known-player path
// (a container already on the page) and the subs path (a player who never
// got one) below, so the two can't drift out of sync.
function pitchingGridHtml(pitching) {
  const grid = [
    statCellHtml('IP', pitching.inningsPitched), statCellHtml('SO', statOr0(pitching.strikeOuts)),
    statCellHtml('ER', statOr0(pitching.earnedRuns)), statCellHtml('H', statOr0(pitching.hits)), statCellHtml('BB', statOr0(pitching.baseOnBalls)),
  ].join('');
  const extraCells = [];
  if (statOr0(pitching.runs) !== statOr0(pitching.earnedRuns)) extraCells.push(statCellHtml('R', statOr0(pitching.runs)));
  if (pitching.homeRuns) extraCells.push(statCellHtml('HR', pitching.homeRuns));
  const extra = extraCells.length ? '<div class="bx-grid">' + extraCells.join('') + '</div>' : '';
  return '<div class="bx-grid">' + grid + '</div>' + extra;
}

// Main grid leads with AB then exactly BATTER_PROP_CATEGORIES (Hits, Total
// Bases, Home Runs, RBIs, Runs Scored, Walks).
function battingGridHtml(batting) {
  const grid = [
    statCellHtml('AB', statOr0(batting.atBats)), statCellHtml('H', statOr0(batting.hits)), statCellHtml('TB', statOr0(batting.totalBases)),
    statCellHtml('HR', statOr0(batting.homeRuns)), statCellHtml('RBI', statOr0(batting.rbi)), statCellHtml('R', statOr0(batting.runs)),
    statCellHtml('BB', statOr0(batting.baseOnBalls)),
  ].join('');
  const extraCells = [];
  if (batting.doubles) extraCells.push(statCellHtml('2B', batting.doubles));
  if (batting.triples) extraCells.push(statCellHtml('3B', batting.triples));
  if (batting.stolenBases) extraCells.push(statCellHtml('SB', batting.stolenBases));
  const extra = extraCells.length ? '<div class="bx-grid">' + extraCells.join('') + '</div>' : '';
  return '<div class="bx-grid">' + grid + '</div>' + extra;
}

// A reliever or pinch-hitter/defensive sub never got a table row -- only
// players with a prop projection (the confirmed/projected lineup + probable
// starter) do. Rather than silently drop their live stats, append a compact
// line to that side's .subs-list instead: no props (nothing was projected
// for them), just their actual line, so they're not just missing. Grouped
// into its own Pitching/Batting subsection (not one flat list) -- a reliever
// buried between two pinch-hitters read as a jumbled arrival-order list.
function upsertSubLine(el, side, pid, name, gridHtml, isFinal, kind) {
  const list = el.querySelector('.subs-list[data-side="' + side + '"]');
  if (!list) return;
  if (!list.querySelector('.subs-list-heading')) {
    // Both groups created together, in a fixed Pitching-then-Batting order,
    // regardless of which kind happens to be seen first in this poll.
    list.innerHTML =
      '<div class="subs-list-heading">Also appeared</div>' +
      '<div class="subs-group" data-kind="pitching"><div class="subs-group-heading">Pitching</div></div>' +
      '<div class="subs-group" data-kind="batting"><div class="subs-group-heading">Batting</div></div>';
  }
  const group = list.querySelector('.subs-group[data-kind="' + kind + '"]');
  let line = group.querySelector('.sub-player-line[data-player-id="' + pid + '"]');
  if (!line) {
    line = document.createElement('div');
    line.className = 'sub-player-line';
    line.dataset.playerId = pid;
    line.innerHTML = '<div class="sub-player-name">' + escapeHtml(name) + ' <span class="sub">(not projected)</span></div>';
    group.appendChild(line);
  }
  const nameDiv = line.querySelector('.sub-player-name').outerHTML;
  line.innerHTML = nameDiv + boxHeader(isFinal) + gridHtml;
}

// Every player in the boxscore (batting AND pitching stats keyed by
// person.id), re-rendered from the SAME fetch that drives the live strip
// above -- our own build only refreshes every ~15 minutes, so without
// this a player's card can sit on whatever partial line it had at the
// last build (see live_score's own docstring in build_props.py for a
// real example this bit us on) long after the real number moved on.
function updatePlayerBoxScores(el, data, isFinal) {
  const box = ((data.liveData || {}).boxscore || {}).teams || {};
  ['home', 'away'].forEach(function (side) {
    const players = (box[side] || {}).players || {};
    Object.keys(players).forEach(function (key) {
      const p = players[key];
      const pid = p.person && p.person.id;
      if (!pid) return;
      const pitching = p.stats && p.stats.pitching;
      const batting = p.stats && p.stats.batting;
      const container = el.querySelector('.boxscore-line[data-player-id="' + pid + '"]');
      const pitched = pitching && pitching.inningsPitched && (pitching.inningsPitched !== '0.0' || statOr0(pitching.battersFaced) > 0);
      const batted = batting && (batting.atBats != null) && (batting.atBats > 0 || statOr0(batting.plateAppearances) > 0);
      if (container) {
        if (pitched) container.innerHTML = boxHeader(isFinal) + pitchingGridHtml(pitching);
        else if (batting && batting.atBats != null) container.innerHTML = boxHeader(isFinal) + battingGridHtml(batting);
        return;
      }
      // No pre-rendered container for this person id -- a reliever, pinch
      // hitter, or defensive sub who wasn't in the projected/confirmed
      // lineup. Only worth a line once they've actually done something.
      if (pitched) upsertSubLine(el, side, pid, p.person.fullName, pitchingGridHtml(pitching), isFinal, 'pitching');
      else if (batted) upsertSubLine(el, side, pid, p.person.fullName, battingGridHtml(batting), isFinal, 'batting');
    });
  });
}

// Only the "something actually happened" action events -- MLB's feed logs
// a lot of routine noise alongside these (batter timeouts, mound visits,
// game-status advisories) that isn't worth a line in a recap feed.
function isNotableAction(event) {
  if (!event) return false;
  if (event.indexOf('Stolen Base') === 0 || event.indexOf('Caught Stealing') === 0) return true;
  return ['Pitching Substitution', 'Offensive Substitution', 'Defensive Sub', 'Defensive Switch', 'Wild Pitch', 'Passed Ball', 'Balk', 'Injury', 'Ejection'].indexOf(event) !== -1;
}

// Shared by the full "Recent plays" list (in the expanded card) and the
// always-visible live strip's "most recent notable event" line below --
// one pass over the same fetch, not two.
function collectGameEvents(data) {
  const allPlays = ((data.liveData || {}).plays || {}).allPlays || [];
  const events = [];
  allPlays.forEach(function (p) {
    const inning = (p.about.halfInning || '') + ' ' + (p.about.inning || '');
    (p.playEvents || []).forEach(function (pe) {
      if (pe.type === 'action' && pe.details && isNotableAction(pe.details.event) && pe.details.description) {
        events.push({ inning: inning, text: pe.details.description, action: true });
      }
    });
    if (p.result && p.result.description) {
      events.push({ inning: inning, text: p.result.description, action: false, scoring: !!p.about.isScoringPlay });
    }
  });
  return events;
}

function renderRecentPlays(el, events) {
  const container = el.querySelector('.recent-plays');
  if (!container || !events.length) return;
  const recent = events.slice(-15).reverse();
  container.innerHTML =
    '<div class="plays-heading">Recent plays</div>' +
    recent
      .map(function (e) {
        const cls = (e.action ? ' play-action' : '') + (e.scoring ? ' play-scoring' : '');
        return (
          '<div class="play-row' + cls + '">' +
          '<span class="play-inning">' + escapeHtml(e.inning) + '</span>' +
          '<span class="play-text">' + escapeHtml(e.text) + '</span></div>'
        );
      })
      .join('');
}

// The full history lives in the expanded card (renderRecentPlays), which
// most visitors won't have open -- a pitching change or pinch-hitter is
// exactly the kind of thing worth surfacing without clicking in, so the
// SINGLE most recent one also gets a line in the always-visible strip.
// Only counts as "current" if it's among the last few events overall --
// otherwise a wild pitch or pitching change from 3 batters ago just sits
// there looking like old news forever (nothing else notable has to have
// happened since for the OLD scan-back-to-the-start version to keep
// re-showing it).
function latestNotableEvent(events) {
  const recent = events.slice(-3);
  for (let i = recent.length - 1; i >= 0; i--) {
    if (recent[i].action) return recent[i];
  }
  return null;
}

// A foul is a strike (barring the 2-strike exception) -- MLB's own feed
// agrees: called strike, swinging strike, and foul all carry the exact
// same "ballColor" in the raw pitch data, distinct only from balls and
// balls in play. Matching that instead of inventing a 4th "foul" color.
// Exactly 3 colors: blue (ball, incl. hit-by-pitch -- batter didn't swing,
// not charged as a strike), red (any kind of strike, fouls included), green
// (ball in play). Not a 4th color for the rare HBP case.
function pitchDotClass(desc) {
  if (!desc) return 'pitch-dot-other';
  if (desc.indexOf('In play') === 0) return 'pitch-dot-inplay';
  if (desc.indexOf('Ball') === 0 || desc === 'Automatic Ball' || desc.indexOf('Hit By Pitch') === 0) return 'pitch-dot-ball';
  return 'pitch-dot-strike'; // Called Strike, Swinging Strike, Foul, Foul Tip, Foul Bunt, ...
}

// MLB's live feed carries full Statcast pitch coordinates (pX/pZ, feet from
// the plate's center/ground) for every pitch of the CURRENT at-bat, plus
// this batter's own strike zone top/bottom -- exactly what MLB.com's own
// Gameday pitch tracker plots. Re-fetched (and so reset to the new
// at-bat's pitches) every poll, same as everything else in the tracker.
function renderStrikeZone(currentPlay) {
  const pitchEvents = ((currentPlay || {}).playEvents || []).filter(function (pe) {
    return pe.pitchData && pe.pitchData.coordinates && pe.pitchData.coordinates.pX != null;
  });
  if (!pitchEvents.length) return '';
  const last = pitchEvents[pitchEvents.length - 1].pitchData;
  const top = last.strikeZoneTop || 3.5;
  const bottom = last.strikeZoneBottom || 1.5;
  const halfWidth = (last.strikeZoneWidth || 17) / 12 / 2; // inches -> feet -> half-width
  // viewBox is 5ft wide (-2.5..2.5) by 5ft tall (0..5, ground to well above
  // the zone), 20px/ft; SVG y grows downward while pZ grows upward, so flip.
  const toX = function (ft) { return (ft + 2.5) * 20; };
  const toY = function (ft) { return (5 - ft) * 20; };
  const zoneX = toX(-halfWidth), zoneY = toY(top);
  const zoneW = toX(halfWidth) - zoneX, zoneH = toY(bottom) - zoneY;
  const dots = pitchEvents
    .map(function (pe, i) {
      const c = pe.pitchData.coordinates;
      const cls = pitchDotClass(pe.details && pe.details.description);
      const x = toX(c.pX), y = toY(c.pZ);
      const isLast = i === pitchEvents.length - 1;
      return (
        '<circle cx="' + x + '" cy="' + y + '" r="' + (isLast ? 8 : 7) + '" class="pitch-dot ' + cls + '"' +
        (isLast ? ' stroke="var(--text-primary)" stroke-width="2"' : '') + '></circle>' +
        '<text x="' + x + '" y="' + y + '" class="pitch-dot-num" text-anchor="middle" dominant-baseline="central">' + (i + 1) + '</text>'
      );
    })
    .join('');
  return (
    '<svg class="strike-zone" viewBox="0 0 100 100" width="84" height="84">' +
    '<rect x="0" y="0" width="100" height="100" class="sz-bg"></rect>' +
    '<rect x="' + zoneX + '" y="' + zoneY + '" width="' + zoneW + '" height="' + zoneH + '" class="sz-zone"></rect>' +
    dots +
    '</svg>'
  );
}

// The same per-pitch data (type + velocity from pitchData/details, the
// call/result already used to color the strike-zone dots) as a plain-
// language list, oldest first -- MLB.com's own Gameday shows exactly this
// alongside its zone plot.
function renderPitchSequence(currentPlay) {
  const pitchEvents = ((currentPlay || {}).playEvents || []).filter(function (pe) {
    return pe.pitchData && pe.details;
  });
  if (!pitchEvents.length) return '';
  const rows = pitchEvents
    .map(function (pe, i) {
      const type = (pe.details.type && pe.details.type.description) || 'Pitch';
      const speed = pe.pitchData.startSpeed;
      const speedHtml = speed != null ? '<span class="pitch-seq-speed">' + Math.round(speed) + ' mph</span>' : '';
      const cls = pitchDotClass(pe.details.description);
      return (
        '<div class="pitch-seq-row"><span class="pitch-seq-num">' + (i + 1) + '.</span>' +
        '<span class="pitch-seq-dot ' + cls + '"></span>' +
        speedHtml +
        '<span>' + escapeHtml(type) + ' &mdash; ' + escapeHtml(pe.details.description || '') + '</span></div>'
      );
    })
    .join('');
  return '<div class="pitch-seq"><div class="pitch-seq-heading">Pitch by pitch</div>' + rows + '</div>';
}

// Marks the current batter/on-deck hitter/pitcher's row so it's obvious at
// a glance where in the order the game actually is, instead of having to
// cross-reference the "Pitching / At bat / On deck" names against the
// batter table by eye. Cleared and reapplied every poll (nothing tracks
// its own previous state -- simplest to just recompute from scratch). This
// is a highlight on the row itself (tinted background + a plain colored
// text label next to the name), not another rounded .badge pill -- it
// needs to read as distinct from the HOT/COLD/matchup bubbles, not as one
// more bubble alongside them.
function clearLiveTags(el) {
  el.querySelectorAll('.live-tag').forEach(function (n) { n.remove(); });
  el.querySelectorAll('.player-row.is-batting, .player-row.is-ondeck').forEach(function (row) {
    row.classList.remove('is-batting', 'is-ondeck');
  });
  el.querySelectorAll('.pitcher-row.is-pitching').forEach(function (row) { row.classList.remove('is-pitching'); });
}
function addLiveTag(cell, label) {
  if (!cell) return;
  cell.insertAdjacentHTML('beforeend', '<span class="live-tag-text live-tag">' + label + '</span>');
}
function highlightActivePlayers(el, batterId, onDeckId, pitcherId) {
  clearLiveTags(el);
  if (batterId) {
    const row = el.querySelector('.player-row[data-player-id="' + batterId + '"]');
    if (row) {
      row.classList.add('is-batting');
      addLiveTag(row.querySelector('td.name-cell'), 'AT BAT');
    }
  }
  if (onDeckId) {
    const row = el.querySelector('.player-row[data-player-id="' + onDeckId + '"]');
    if (row) {
      row.classList.add('is-ondeck');
      addLiveTag(row.querySelector('td.name-cell'), 'ON DECK');
    }
  }
  if (pitcherId) {
    const row = el.querySelector('.pitcher-row[data-player-id="' + pitcherId + '"]');
    if (row) {
      row.classList.add('is-pitching');
      addLiveTag(row, 'PITCHING');
    }
  }
}

// Standard normal CDF via the Abramowitz & Stegun erf approximation --
// no server round-trip, so this has to be self-contained in the browser.
function normCdf(x) {
  const sign = x < 0 ? -1 : 1;
  const ax = Math.abs(x) / Math.SQRT2;
  const a1 = 0.254829592, a2 = -0.284496736, a3 = 1.421413741, a4 = -1.453152027, a5 = 1.061405429, p = 0.3275911;
  const t = 1 / (1 + p * ax);
  const y = 1 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-ax * ax);
  return 0.5 * (1 + sign * y);
}

// A live in-game win-probability ESTIMATE, not MLB's own (proprietary) model
// -- built from first principles: treat each team's remaining scoring as
// normally distributed around the current lead, with variance shrinking as
// outs run out and a small fixed home-field edge (home teams win ~54% of
// season openers) that shrinks right along with it. Plus a small same-side
// bonus for the batting team's current base/out state (e.g. bases loaded,
// nobody out swings things back toward whoever's up). This is a real
// approximation, not a lookup from Statcast's actual win-expectancy table --
// good enough to be directionally right, not precise to the percentage point.
function computeWinProbability(linescore) {
  const inning = linescore.currentInning;
  if (!inning) return null;
  const isTop = linescore.isTopInning !== undefined ? linescore.isTopInning : (linescore.inningHalf || '') === 'Top';
  const outs = linescore.outs || 0;
  const home = (linescore.teams || {}).home || {};
  const away = (linescore.teams || {}).away || {};
  if (home.runs == null || away.runs == null) return null;
  const offense = linescore.offense || {};

  const outsPerSide = 3;
  const regulationOuts = 54; // 9 innings x 2 halves x 3 outs
  const outsCompleted = (inning - 1) * 6 + (isTop ? 0 : outsPerSide) + outs;
  const totalOuts = inning <= 9 ? regulationOuts : regulationOuts + (inning - 9) * 6;
  let outsRemaining = totalOuts - outsCompleted;
  const tied = home.runs === away.runs;
  if (outsRemaining <= 0 && tied) outsRemaining = 3; // heading to extras -- still very much live
  outsRemaining = Math.max(outsRemaining, 1);

  const baseBonus = (offense.first ? 0.15 : 0) + (offense.second ? 0.3 : 0) + (offense.third ? 0.35 : 0);
  const outMultiplier = outs === 0 ? 1 : outs === 1 ? 0.65 : 0.35;
  const runnerBonus = baseBonus * outMultiplier;

  const sigmaFull = 4.5; // ~typical SD of final run differential across a 9-inning game
  const sigma = Math.max(sigmaFull * Math.sqrt(outsRemaining / regulationOuts), 0.35);
  const homeEdgeFull = 0.452; // Phi(homeEdgeFull / sigmaFull) ~= 0.54, matching home teams' long-run win rate
  const homeEdge = homeEdgeFull * Math.sqrt(outsRemaining / regulationOuts);

  const lead = home.runs - away.runs + (isTop ? -runnerBonus : runnerBonus) + homeEdge;
  const homeWp = normCdf(lead / sigma);
  // Never claim near-certainty while the game's still live -- this is an
  // estimate, not an oracle.
  return Math.min(Math.max(homeWp, 0.02), 0.98);
}

function winProbHtml(linescore, gameData) {
  const wp = computeWinProbability(linescore);
  if (wp == null) return '';
  const homePct = Math.round(wp * 100);
  const awayPct = 100 - homePct;
  const teams = gameData.teams || {};
  const homeAbbr = (teams.home && (teams.home.abbreviation || teams.home.teamName)) || 'Home';
  const awayAbbr = (teams.away && (teams.away.abbreviation || teams.away.teamName)) || 'Away';
  return (
    '<div class="wp-panel"><span class="wp-panel-label">Win probability (est.)</span>' +
    '<div class="wp-bar"><div class="wp-bar-away" style="width:' + awayPct + '%"></div>' +
    '<div class="wp-bar-home" style="width:' + homePct + '%"></div></div>' +
    '<div class="wp-teams"><span>' + escapeHtml(awayAbbr) + ' <b>' + awayPct + '%</b></span>' +
    '<span>' + escapeHtml(homeAbbr) + ' <b>' + homePct + '%</b></span></div></div>'
  );
}

function renderLiveState(el, data) {
  const container = el.querySelector('.live-tracker'); // compact: sticky, in <summary>, visible even collapsed
  const detail = el.querySelector('.live-detail'); // heavier detail: NOT sticky, only in the expanded body
  const status = ((data.gameData || {}).status || {}).abstractGameState;
  const linescore = (data.liveData || {}).linescore || {};
  const events = collectGameEvents(data);

  if (status === 'Final') {
    el.dataset.liveDone = 'true';
    updatePlayerBoxScores(el, data, true);
    renderRecentPlays(el, events);
    highlightActivePlayers(el, null, null, null);
    if (detail) detail.innerHTML = '';
    if (!container) return;
    const home = (linescore.teams || {}).home, away = (linescore.teams || {}).away;
    if (home && away && home.runs != null && away.runs != null) {
      container.innerHTML =
        '<span class="badge badge-final">FINAL</span><span class="live-score-line">' + away.runs + ' - ' + home.runs + '</span>';
    }
    return;
  }
  if (status !== 'Live') return; // still Preview (Scheduled/Pre-Game/Warmup/Delayed) -- nothing live to show yet

  updatePlayerBoxScores(el, data, false);
  renderRecentPlays(el, events);

  const home = (linescore.teams || {}).home || {};
  const away = (linescore.teams || {}).away || {};
  const outs = linescore.outs || 0;
  const offense = linescore.offense || {};
  const defense = linescore.defense || {};
  const batter = offense.batter ? offense.batter.fullName : null;
  const onDeck = offense.onDeck ? offense.onDeck.fullName : null;
  const inHole = offense.inHole ? offense.inHole.fullName : null;
  const pitcher = defense.pitcher ? defense.pitcher.fullName : null;
  highlightActivePlayers(el, offense.batter && offense.batter.id, offense.onDeck && offense.onDeck.id, defense.pitcher && defense.pitcher.id);
  const outDots = [0, 1, 2]
    .map(function (i) { return '<span class="out-dot' + (i < outs ? ' out-dot-filled' : '') + '"></span>'; })
    .join('');
  const diamond =
    '<div class="diamond">' +
    '<div class="base base-2b' + (offense.second ? ' base-occupied' : '') + '"></div>' +
    '<div class="base base-3b' + (offense.third ? ' base-occupied' : '') + '"></div>' +
    '<div class="base base-1b' + (offense.first ? ' base-occupied' : '') + '"></div>' +
    '</div>';
  const updated = new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const latest = latestNotableEvent(events);
  const currentPlay = ((data.liveData || {}).plays || {}).currentPlay;
  const count = (currentPlay || {}).count || {};
  const countText = count.balls != null && count.strikes != null ? count.balls + '-' + count.strikes : '';
  const strikeZoneHtml = renderStrikeZone(currentPlay);
  const pitchSeqHtml = renderPitchSequence(currentPlay);
  const wpHtml = winProbHtml(linescore, data.gameData || {});

  if (container) {
    container.innerHTML =
      '<div class="live-top-row">' +
      '<span class="badge badge-live">LIVE</span>' +
      '<span class="live-score-line">' + away.runs + ' - ' + home.runs + '</span>' +
      '<span class="live-inning">' + escapeHtml(linescore.inningHalf || '') + ' ' + escapeHtml(linescore.currentInningOrdinal || '') + '</span>' +
      '<span class="live-updated">updated ' + updated + '</span>' +
      '</div>' +
      '<div class="live-field-row">' +
      '<div class="live-state-panel">' +
      diamond +
      '<div class="live-count-outs">' +
      (countText ? '<span class="live-count">' + countText + '</span>' : '') +
      '<span class="live-outs">' + outDots + '</span>' +
      '</div>' +
      '</div>' +
      wpHtml +
      '</div>';
  }

  if (detail) {
    detail.innerHTML =
      '<div class="live-field-row">' +
      (strikeZoneHtml ? '<div class="sz-panel"><span class="sz-panel-label">Pitch location</span>' + strikeZoneHtml + '</div>' : '') +
      (batter || pitcher
        ? '<div class="live-matchup">' +
          (pitcher ? '<span class="live-batter">Pitching: <b>' + escapeHtml(pitcher) + '</b></span>' : '') +
          (batter ? '<span class="live-batter">At bat: <b>' + escapeHtml(batter) + '</b></span>' : '') +
          (onDeck ? '<span class="live-batter sub">On deck: ' + escapeHtml(onDeck) + '</span>' : '') +
          (inHole ? '<span class="live-batter sub">In the hole: ' + escapeHtml(inHole) + '</span>' : '') +
          '</div>'
        : '') +
      '</div>' +
      pitchSeqHtml +
      (latest ? '<div class="live-notable">' + escapeHtml(latest.text) + '</div>' : '');
  }
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
    applyFilters();
  }
  initTheme();
  localizeGameTimes();
  initLiveTracker();
  initGameCardAccordion();
  syncStickyOffset();
  window.addEventListener('resize', syncStickyOffset);
});

// The filter toolbar (dropdowns/search) is its own sticky element pinned at
// top:0 -- an open game card's summary is ALSO sticky, and without this it
// would stack at the same top:0, letting the toolbar's higher z-index paint
// over the summary's own title/score. Measured live (not hardcoded) because
// the toolbar wraps to more rows, and gets taller, on narrow/mobile widths.
// No-op (falls back to the CSS default of 0px) on pages with no toolbar.
function syncStickyOffset() {
  const toolbar = document.querySelector('.toolbar');
  const height = toolbar ? toolbar.getBoundingClientRect().height : 0;
  document.documentElement.style.setProperty('--sticky-offset', height + 'px');
}
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
    <dd>Moneyline = which team our simulation (100,000 Monte Carlo trials per game) thinks is more likely to win outright. Run line = the standard MLB spread (always +/-1.5 runs, whichever side is the moneyline favorite) and which side is more likely to cover it. Total = the projected combined-runs line (the median of the simulated outcomes, rounded to a half-run so it can't push) and whether the model leans over or under it. All three come from the same simulation, which uses each team's season-long scoring record (home/away split), the probable starters' season stats, actual bullpen quality plus recent bullpen fatigue, and -- once a lineup is confirmed -- a small adjustment for how those specific 9 hitters have actually been hitting lately.</dd>
    <dt>Why does the +1.5 underdog get picked so often?</dt>
    <dd>Because that's genuinely how MLB run lines behave, not a bug: most games are decided by 1 run, so "lose by only 1" (which still covers +1.5) is a much lower bar than "win by 2+" (needed to cover -1.5). Real sportsbooks don't change the 1.5 number for this -- they price it in with worse moneyline odds on the +1.5 side. Since this project doesn't pull real odds, a run-line pick here is a directional lean only, not a claim that it's a great-value bet.</dd>
  </dl>
</details>
"""


NORMAL_STATUSES = {"Scheduled", "Pre-Game", "Warmup", "In Progress", "Final", "Game Over", "Completed Early"}
FINAL_STATUSES = {"Final", "Game Over", "Completed Early"}


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


def _team_form_badge(form):
    streak = (form or {}).get("streak") or 0
    if streak >= 3:
        return _badge(f"{streak}-GAME WIN STREAK", "hot")
    if streak <= -3:
        return _badge(f"{abs(streak)}-GAME LOSING STREAK", "cold")
    return ""


def _team_form_text(form):
    if not form or not form.get("record_games"):
        return ""
    diff = form["run_diff"]
    diff_txt = f"+{diff}" if diff >= 0 else str(diff)
    return f'<span class="sub">{form["wins"]}-{form["losses"]} last {form["record_games"]} ({diff_txt} run diff)</span>'


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


def _matchup_lean_html(entry):
    """
    The matchup-adjusted lean itself only needs a confirmed lineup + known
    opposing pitcher, both set well before first pitch -- no reason to
    withhold it pre-game just because "Best prop" above already headlines
    a different signal. Once the game's actually started, the box score
    sits right next to it, and this is the one thing it doesn't say for
    itself: which side of a line the model called beforehand.
    """
    lean = entry.get("matchup_lean")
    if not lean:
        return ""
    verb = "OVER" if lean["direction"] == "over" else "UNDER"
    # result (hit/miss) is computed in build_props.py's _lean_with_result(),
    # against this exact same line, the moment there's a real result to
    # check it against -- otherwise this line just restated the pre-game
    # call forever, even once the box score right next to it already
    # answered the obvious next question.
    result = lean.get("result")
    result_html = f" {_badge(result.upper(), result)}" if result else ""
    return f'<div class="matchup-lean prop-lean-{lean["direction"]}">Predicted: {html.escape(lean["label"])} {verb} {lean["line"]}{result_html}</div>'


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


def _game_status_badge(status):
    """
    LIVE vs FINAL for a specific player's box-score line -- a synced game
    log row can exist mid-game (the API reports partial in-progress stats,
    not just completed-game ones), so a hardcoded "Final" label was
    actively wrong for a game that hadn't ended yet. Anything not in
    FINAL_STATUSES but with a game log row already means play is underway.
    """
    return _badge("FINAL", "final") if status in FINAL_STATUSES else _badge("LIVE", "live")


def _boxscore_grid_html(pairs):
    cells = "".join(f'<div class="bx-stat"><b>{v}</b><small>{k}</small></div>' for k, v in pairs)
    return f'<div class="bx-grid">{cells}</div>'


def _boxscore_header_html(status, volume_label=None, volume_value=None):
    volume_html = f'<span class="bx-volume">{volume_value} {volume_label}</span>' if volume_label else ""
    return f'<div class="bx-header">{_game_status_badge(status)}{volume_html}</div>'


def _batter_boxscore_html(gr, status, player_id):
    """
    A compact MLB.com-style box (badge on its own line, a fixed 3-column
    stat grid below) for this player's actual game, instead of a run-on
    sentence -- a grid lays out the same regardless of this column's
    width, where flex-wrap alone wrapped unpredictably in the narrower
    "Recent form" column.

    Main grid leads with AB (essential context -- "how many at-bats") then
    exactly BATTER_PROP_CATEGORIES, same order: these are the stats the
    props/grading actually key off, so the box score a reader checks a
    pick against should show precisely those numbers, not a generic
    AB-R-H-RBI-BB-SO line that only partly overlaps them. 2B/3B/SB aren't
    graded prop categories here; kept as a secondary row, shown only when
    they actually happened, so a 0-for-4 night isn't padded with zeroes.

    The outer div (and its data-player-id) is always emitted, even with no
    game_result yet -- our own build only refreshes every ~15 minutes, so
    live_tracker in SCRIPT below re-fetches and re-fills this same element
    every ~30s directly from MLB's live feed; it needs a container to
    target regardless of whether this player had a stat line the last time
    our own pipeline ran.
    """
    inner = ""
    if gr:
        stats = [
            ("AB", gr["at_bats"]), ("H", gr["hits"]), ("TB", gr["total_bases"]),
            ("HR", gr["home_runs"]), ("RBI", gr["rbi"]), ("R", gr["runs"]), ("BB", gr["base_on_balls"]),
        ]
        extras = [(label, gr.get(key)) for key, label in (("doubles", "2B"), ("triples", "3B"), ("stolen_bases", "SB")) if gr.get(key)]
        extra_html = _boxscore_grid_html(extras) if extras else ""
        inner = f'{_boxscore_header_html(status)}{_boxscore_grid_html(stats)}{extra_html}'
    return f'<div class="boxscore-line" data-player-id="{player_id}">{inner}</div>'


def _pitcher_boxscore_html(gr, status, player_id):
    """Main grid leads with IP then exactly PITCHER_PROP_CATEGORIES (Strikeouts, Runs Allowed -- earned_runs, Hits Allowed, Walks Allowed), same reasoning as the batter box above."""
    inner = ""
    if gr:
        stats = [
            ("IP", gr["innings_pitched"]), ("SO", gr["strike_outs"]),
            ("ER", gr["earned_runs"]), ("H", gr["hits"]), ("BB", gr["base_on_balls"]),
        ]
        # R (total runs) and HR aren't graded prop categories here (Runs
        # Allowed is keyed off earned runs specifically) -- shown only when
        # they differ from what's already in the main grid, so an all-earned,
        # no-homer outing doesn't get cluttered with redundant zeroes.
        extras = []
        if gr["runs"] != gr["earned_runs"]:
            extras.append(("R", gr["runs"]))
        if gr.get("home_runs"):
            extras.append(("HR", gr["home_runs"]))
        extra_html = _boxscore_grid_html(extras) if extras else ""
        inner = f'{_boxscore_header_html(status)}{_boxscore_grid_html(stats)}{extra_html}'
    return f'<div class="boxscore-line" data-player-id="{player_id}">{inner}</div>'


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


def _pitcher_html(p, row_id, fatigue, status):
    if not p:
        return '<div class="pitcher-line sub">No probable pitcher announced</div>'
    l5 = p["l5"]
    l5_txt = (
        f"Last 5 starts: {l5['strike_outs']} strikeouts, {l5['earned_runs']} runs allowed, {l5['era']} ERA"
        if l5
        else "No recent starts on record yet"
    )
    badges = " ".join(x for x in [_pitcher_form_badge(p.get("form_trend")), _injury_badge(p["injury"])] if x)
    boxscore_html = _pitcher_boxscore_html(p.get("game_result"), status, p["player_id"])
    matchup_lean_html = _matchup_lean_html(p)

    bullets = [l5_txt]
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
    <div class="pitcher-line pitcher-row" data-player-id="{p["player_id"]}" onclick="toggleDetail('{row_id}')">
      <span class="expand-arrow"></span><b>{html.escape(p["name"])}</b> ({html.escape(p["pitch_hand"] or "?")}, throws) {badges}
    </div>
    {boxscore_html}
    {matchup_lean_html}
    <ul class="team-summary">{bullets_html}</ul>
    <table><tbody>{_prop_categories_html(p.get("prop_categories"), row_id)}</tbody></table>
    """


def _batter_rows(batters, id_prefix, status):
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
        boxscore_html = _batter_boxscore_html(b.get("game_result"), status, b["player_id"])
        matchup_lean_html = _matchup_lean_html(b)
        rows.append(
            f'<tr class="player-row" data-player-id="{b["player_id"]}" onclick="toggleDetail(\'{row_id}\')">'
            f'<td data-label="Order">{order}</td>'
            f'<td data-label="Batter" class="name-cell"><span class="expand-arrow"></span>{html.escape(b["name"])} '
            f'<span class="sub">({html.escape(b["bat_side"] or "?")} handed batter)</span></td>'
            f'<td data-label="Flags">{badges}</td>'
            f'<td data-label="Recent form">{boxscore_html}{matchup_lean_html}{html.escape(l7_txt)}<div class="sub">{season_txt}</div>{_best_prop_html(b)}</td>'
            f'<td data-label="News" class="col-news">{headline}</td></tr>'
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


def _team_col_html(side, id_prefix, status, side_key):
    tag_kind = "confirmed" if side["lineup_confirmed"] else "projected"
    tag_label = "LINEUP CONFIRMED" if side["lineup_confirmed"] else "PROJECTED (not yet announced)"
    form = side.get("form")
    return f"""
    <div class="team-col" data-side="{side_key}">
      <div class="team-title">{html.escape(side["team_name"] or "?")} {_badge(tag_label, tag_kind)}{_team_form_badge(form)}</div>
      <div class="team-form">{_team_form_text(form)}</div>
      <div class="pitcher-block">
        {_pitcher_html(side["probable_pitcher"], f"{id_prefix}-p", side.get("opponent_bullpen_fatigue"), status)}
      </div>
      <div class="batter-block">
        <div class="table-scroll">
        <table>
          <thead><tr><th>Order</th><th>Batter</th><th>Flags</th><th>Recent form</th><th class="col-news">News</th></tr></thead>
          <tbody>{_batter_rows(side["batters"], f"{id_prefix}-b", status)}</tbody>
        </table>
        </div>
      </div>
      <div class="subs-list" data-side="{side_key}"></div>
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

        # Same grading rules as grade_picks.py's _grade_games() (the
        # permanent Track Record verdict) -- mirrored exactly here so the
        # two can never disagree on what counts as a hit.
        margin = g["home_score"] - g["away_score"]  # MLB games never end tied
        ml_pick = p.get("moneyline_pick") or ("home" if p["home_win_prob"] >= 0.5 else "away")
        ml_team = g["home"]["team_name"] if ml_pick == "home" else g["away"]["team_name"]
        ml_hit = (ml_pick == "home") == (margin > 0)
        picks_html = f'<span>Moneyline: <b>{html.escape(ml_team or "?")}</b> {_badge("HIT" if ml_hit else "MISS", "hit" if ml_hit else "miss")}</span>'

        total_line = p.get("total_line")
        if total_line is not None:
            total_pick = p.get("total_pick") or ("over" if p.get("over_prob", 0.5) >= 0.5 else "under")
            total_hit = (actual_total > total_line) == (total_pick == "over")
            picks_html += (
                f'<span>Total {total_line}: leaned <b>{total_pick.upper()}</b> '
                f'{_badge("HIT" if total_hit else "MISS", "hit" if total_hit else "miss")}</span>'
            )

        return f"""
        <div class="game-line">
          {final_html}
          <div class="proj-picks"><span>Projected: <b>{p["away_exp_runs"]} &ndash; {p["home_exp_runs"]}</b> (total off by {diff_txt})</span></div>
          <div class="proj-picks">{picks_html}</div>
        </div>
        """

    # A game already in progress gets NO static score line here -- that's
    # exactly what used to bite us: this only ever reflects whatever our
    # own build last saw (every ~15-60 min), so it would sit there looking
    # frozen right next to the client-side .live-tracker below (SCRIPT's
    # initLiveTracker/renderLiveState), which actually re-fetches MLB's own
    # feed every ~30s. Two score displays, one stale and one live, reads as
    # "it's not updating" even when the real one is -- so for a game that's
    # started but isn't final yet, .live-tracker is the ONLY score shown.
    p = g.get("projection")
    if g.get("live_score"):
        return ""

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
    # data-final freezes the client-side tracker off entirely for a game
    # that was already Final as of this build -- no point in the browser
    # polling MLB's live feed for a decided game whose real box score
    # we've already got. Anything else (Scheduled through In Progress) gets
    # tracked; live_tracker.js itself decides, from the live feed's own
    # abstractGameState, when a Scheduled game has actually gone live.
    is_final = "true" if g.get("home_score") is not None else "false"
    return f"""
    <details class="game-card" data-date="{html.escape(g["date"])}" data-confirmed="{is_confirmed}"
              data-search="{_game_search_blob(g)}" data-game-pk="{g["game_pk"]}" data-final="{is_final}">
      <summary class="game-summary">
        <span class="matchup-title">{html.escape(g["away"]["team_name"] or "?")} @ {html.escape(g["home"]["team_name"] or "?")}</span>
        <span class="summary-flags">{_game_summary_flags(g)}{_status_badge(g["status"])}</span>
        <span class="game-meta">
          <span class="game-time" data-utc="{html.escape(g["game_time_utc"] or "")}">{html.escape(g["game_time_utc"] or "")}</span>
          {f" &middot; {html.escape(g['status'])}" if g["status"] and g["status"] != "Scheduled" else ""}
          &middot; {html.escape(g["venue"] or "")}
        </span>
        {_game_line_html(g)}
        <div class="live-tracker"></div>
      </summary>
      <div class="game-body">
        <div class="live-detail"></div>
        <div class="recent-plays"></div>
        <div class="teams">
          {_team_col_html(g["away"], f"{id_prefix}-a", g["status"], "away")}
          {_team_col_html(g["home"], f"{id_prefix}-h", g["status"], "home")}
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
    is_pitcher = pick.get("role") == "pitcher"
    badges = [_badge("PITCHER PROP", "matchup") if is_pitcher else _badge("BATTER PROP", "caveat")]
    if not is_pitcher:
        lineup_kind = "confirmed" if pick["lineup_confirmed"] else "projected"
        lineup_label = "LINEUP CONFIRMED" if pick["lineup_confirmed"] else "LINEUP PROJECTED"
        badges.append(_badge(lineup_label, lineup_kind))
    # Read straight off this player's own game_result (pick_result() in
    # build_props.py) -- once the game's actually happened, no reason to
    # make someone cross-reference this card against Track Record just to
    # see whether the pick that got them here actually hit.
    if pick.get("dnp"):
        badges.append(_badge("DNP", "dnp"))
    else:
        result = pick.get("result")
        if result:
            badges.append(_badge(result.upper(), result))
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
        body = _toolbar(games) + "".join(sections)

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
