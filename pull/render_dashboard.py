"""
render_dashboard.py

Renders the report dict from build_props.py into a single static HTML
dashboard (output/index.html). No JS framework/build step -- plain HTML,
inline CSS custom properties, light+dark mode. Palette/roles follow the
project's dataviz skill: status colors (hot=good green, cold=critical red)
always ship with an icon+label, never color alone; lineup-confirmed vs
projected uses a neutral categorical badge, not a status color, since it
isn't a good/bad signal.

`output/notes.md`, if present, is read (never written) and rendered as a
"Notes" panel -- a spot for manually-pasted expert-consensus takes that
survives regeneration because this script never touches that file.
"""

import html
import os

NOTES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "notes.md")

STYLE = """
<style>
  :root, .viz-root {
    color-scheme: light;
    --surface-1: #fcfcfb; --surface-2: #f9f9f7;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
    --gridline: #e1e0d9; --border: rgba(11,11,11,0.10);
    --status-good: #0ca30c; --status-warning: #fab219; --status-serious: #ec835a; --status-critical: #d03b3b;
    --badge-neutral-bg: #e1e0d9; --badge-neutral-text: #52514e;
    --series-1: #2a78d6;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface-1: #1a1a19; --surface-2: #0d0d0d;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
      --gridline: #2c2c2a; --border: rgba(255,255,255,0.10);
      --badge-neutral-bg: #2c2c2a; --badge-neutral-text: #c3c2b7;
      --series-1: #3987e5;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px; background: var(--surface-2); color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .meta { color: var(--text-secondary); font-size: 13px; margin-bottom: 20px; }
  .notes {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
    padding: 14px 16px; margin-bottom: 20px; font-size: 13px; color: var(--text-secondary);
    white-space: pre-wrap;
  }
  .game-card {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px; margin-bottom: 16px;
  }
  .game-header { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; }
  .game-header h2 { font-size: 16px; margin: 0; }
  .game-meta { color: var(--text-muted); font-size: 12px; }
  .teams { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 12px; }
  @media (max-width: 720px) { .teams { grid-template-columns: 1fr; } }
  .team-col { border-top: 1px solid var(--gridline); padding-top: 10px; }
  .team-title { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 14px; margin-bottom: 8px; }
  .badge {
    display: inline-flex; align-items: center; gap: 4px; font-size: 11px; font-weight: 600;
    padding: 2px 7px; border-radius: 999px; line-height: 1.6;
  }
  .badge-confirmed { background: var(--series-1); color: white; }
  .badge-projected { background: var(--badge-neutral-bg); color: var(--badge-neutral-text); }
  .badge-hot { background: var(--status-good); color: white; }
  .badge-cold { background: var(--status-critical); color: white; }
  .badge-injury { background: var(--status-warning); color: #1a1a19; }
  .badge-matchup { background: var(--series-1); color: white; }
  .pitcher-line { font-size: 13px; margin-bottom: 8px; color: var(--text-secondary); }
  .pitcher-line b { color: var(--text-primary); }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th { text-align: left; color: var(--text-muted); font-weight: 500; font-size: 11px; padding: 4px 6px; border-bottom: 1px solid var(--gridline); }
  td { padding: 5px 6px; border-bottom: 1px solid var(--gridline); vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  .name-cell { font-weight: 600; }
  .sub { color: var(--text-muted); font-size: 11px; }
  .headline-link { color: var(--series-1); text-decoration: none; font-size: 11px; }
  .headline-link:hover { text-decoration: underline; }
  .empty { color: var(--text-muted); font-size: 13px; padding: 20px; text-align: center; }
</style>
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
    return _badge(f"INJ: {injury['status']}", "injury") if injury else ""


def _matchup_badge(matchup):
    if not matchup or not matchup.get("favorable"):
        return ""
    avg = matchup.get("pitcher_avg_against")
    label = f"MATCHUP ({avg} avg-against)" if avg is not None else "MATCHUP EDGE"
    return _badge(label, "matchup")


def _pitcher_html(p):
    if not p:
        return '<div class="pitcher-line sub">No probable pitcher announced</div>'
    l5 = p["l5"]
    l5_txt = (
        f"L5: {l5['innings_pitched']} IP, {l5['strike_outs']} K, {l5['earned_runs']} ER, {l5['era']} ERA, {l5['whip']} WHIP"
        if l5
        else "L5: no data yet"
    )
    return (
        f'<div class="pitcher-line"><b>{html.escape(p["name"])}</b> ({html.escape(p["pitch_hand"] or "?")}) '
        f'{_injury_badge(p["injury"])}<br>{html.escape(l5_txt)}</div>'
    )


def _batter_rows(batters):
    if not batters:
        return '<tr><td colspan="5" class="sub">No batter data yet</td></tr>'
    rows = []
    for b in batters:
        order = f"#{b['batting_order']}" if b["batting_order"] else "-"
        l7 = b["l7"]
        l7_txt = f"{l7['hits']}H {l7['home_runs']}HR {l7['rbi']}RBI {l7['total_bases']}TB ({l7['avg']})" if l7 else "no data"
        season = b["season"]
        season_txt = f"{season['avg']} avg" if season and season["avg"] is not None else "-"
        headline = (
            f'<a class="headline-link" href="{html.escape(b["headlines"][0]["link"])}" target="_blank" rel="noopener">'
            f'{html.escape(b["headlines"][0]["title"][:60])}</a>'
            if b["headlines"]
            else ""
        )
        badges = " ".join(
            x for x in [_trend_badge(b["trend"]), _matchup_badge(b.get("matchup")), _injury_badge(b["injury"])] if x
        )
        rows.append(
            f"<tr><td>{order}</td>"
            f'<td class="name-cell">{html.escape(b["name"])} <span class="sub">({html.escape(b["bat_side"] or "?")})</span></td>'
            f"<td>{badges}</td>"
            f"<td>{html.escape(l7_txt)}<div class=\"sub\">season: {season_txt}</div></td>"
            f"<td>{headline}</td></tr>"
        )
    return "\n".join(rows)


def _team_col_html(side):
    tag_kind = "confirmed" if side["lineup_confirmed"] else "projected"
    tag_label = "LINEUP CONFIRMED" if side["lineup_confirmed"] else "PROJECTED (unconfirmed)"
    return f"""
    <div class="team-col">
      <div class="team-title">{html.escape(side["team_name"] or "?")} {_badge(tag_label, tag_kind)}</div>
      {_pitcher_html(side["probable_pitcher"])}
      <table>
        <thead><tr><th>Ord</th><th>Batter</th><th>Flags</th><th>Recent form</th><th>News</th></tr></thead>
        <tbody>{_batter_rows(side["batters"])}</tbody>
      </table>
    </div>
    """


def _game_card_html(g):
    return f"""
    <div class="game-card">
      <div class="game-header">
        <h2>{html.escape(g["away"]["team_name"] or "?")} @ {html.escape(g["home"]["team_name"] or "?")}</h2>
        <span class="game-meta">{html.escape(g["date"])} &middot; {html.escape(g["status"] or "")} &middot; {html.escape(g["venue"] or "")}</span>
      </div>
      <div class="teams">
        {_team_col_html(g["away"])}
        {_team_col_html(g["home"])}
      </div>
    </div>
    """


def render_html(report):
    notes_html = ""
    if os.path.exists(NOTES_PATH):
        with open(NOTES_PATH) as f:
            notes = f.read().strip()
        if notes:
            notes_html = f'<div class="notes"><b>Notes</b>\n{html.escape(notes)}</div>'

    games_html = "".join(_game_card_html(g) for g in report["games"]) or '<div class="empty">No games in range.</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MLB Player Props Dashboard</title>
{STYLE}
</head>
<body>
  <h1>MLB Player Props Dashboard</h1>
  <div class="meta">Generated {html.escape(report["generated_at"])} &middot; range {report["range"][0]} to {report["range"][1]}</div>
  {notes_html}
  {games_html}
</body>
</html>"""
