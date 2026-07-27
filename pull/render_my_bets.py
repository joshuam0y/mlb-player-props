"""
render_my_bets.py

Renders output/user_bets.json (built by log_bet.py) into a static personal
bet tracker page -- output/my-bets.html. Deliberately NOT linked from the
main dashboard nav: this is personal wager/profit-loss data, and while
this site has no real access control (it's a public static page like
every other page here), there's no reason to make it casually
discoverable from the homepage either.

Reuses the main dashboard's STYLE/SCRIPT/badge classes so it doesn't look
like a different site -- WON/LOST reuse the existing hit/miss (green/red)
badge colors, PENDING reuses the existing neutral "projected" gray.
"""

import html
import json
import os

from render_dashboard import SCRIPT, STYLE, _badge

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
BETS_PATH = os.path.join(OUT_DIR, "user_bets.json")


def _money(x, sign=False):
    if x is None:
        return "&mdash;"
    prefix = "+" if sign and x > 0 else ""
    return f"{prefix}${x:,.2f}"


def _bet_profit(bet):
    if bet["status"] == "won":
        return bet["to_win"]
    if bet["status"] == "lost":
        return -bet["stake"]
    return None  # pending -- not yet realized


def _leg_status_badge(leg):
    if leg["status"] == "hit":
        return _badge("HIT", "hit")
    if leg["status"] == "miss":
        return _badge("MISS", "miss")
    if leg.get("dnp"):
        return _badge("DNP", "dnp")
    return _badge("PENDING", "projected")


def _bet_status_badge(bet):
    if bet["status"] == "won":
        return _badge("WON", "hit")
    if bet["status"] == "lost":
        return _badge("LOST", "miss")
    return _badge("PENDING", "projected")


def _leg_row_html(leg):
    model_bits = []
    if leg.get("model_projection") is not None:
        model_bits.append(f"model: {leg['model_projection']} proj vs {leg['model_line']} line, leans {leg.get('model_lean') or '&mdash;'}")
    else:
        model_bits.append("model: no current projection for this player/category")
    actual_bits = f"actual: {leg['actual_value']}" if leg.get("actual_value") is not None else ""
    return f"""
    <div class="leg-row">
      <div class="leg-main">
        <b>{html.escape(leg['player_name'])}</b>
        <span class="leg-prop">{html.escape(leg['category'])} {html.escape(leg['direction'].upper())} {leg['line']}</span>
        {_leg_status_badge(leg)}
      </div>
      <div class="leg-sub sub">{html.escape(' &middot; '.join(b for b in model_bits if b))} {f'&middot; {actual_bits}' if actual_bits else ''}</div>
    </div>
    """


def _bet_card_html(bet):
    profit = _bet_profit(bet)
    profit_txt = _money(profit, sign=True) if profit is not None else "at stake: " + _money(bet["stake"])
    legs_html = "".join(_leg_row_html(leg) for leg in bet["legs"])
    odds_txt = f" &middot; odds {html.escape(bet['odds'])}" if bet.get("odds") else ""
    parlay_txt = f" ({len(bet['legs'])}-leg parlay)" if len(bet["legs"]) > 1 else ""
    return f"""
    <div class="bet-card">
      <div class="bet-card-head">
        <span><b>{html.escape(bet['placed_date'])}</b>{parlay_txt} &middot; {html.escape(bet.get('sportsbook', 'FanDuel'))}{odds_txt} &middot; staked {_money(bet['stake'])} to win {_money(bet['to_win'])}</span>
        <span>{_bet_status_badge(bet)} <b>{profit_txt}</b></span>
      </div>
      {legs_html}
    </div>
    """


def render_html():
    if os.path.exists(BETS_PATH):
        with open(BETS_PATH) as f:
            record = json.load(f)
    else:
        record = {"bets": [], "updated_at": None}

    bets = sorted(record.get("bets") or [], key=lambda b: (b["placed_date"], b["id"]), reverse=True)

    settled = [b for b in bets if b["status"] in ("won", "lost")]
    pending = [b for b in bets if b["status"] == "pending"]
    total_profit = sum(_bet_profit(b) for b in settled) if settled else 0.0
    wins = sum(1 for b in settled if b["status"] == "won")
    losses = sum(1 for b in settled if b["status"] == "lost")
    pending_stake = sum(b["stake"] for b in pending)

    sign = "+" if total_profit >= 0 else ""
    tiles = f"""
      <div class="stat-tile">
        <div class="stat-value">{sign}{_money(total_profit)}</div>
        <div class="stat-label">Total profit/loss (settled bets)</div>
      </div>
      <div class="stat-tile">
        <div class="stat-value">{wins}-{losses}</div>
        <div class="stat-label">Record (settled bets)</div>
      </div>
      <div class="stat-tile">
        <div class="stat-value">{len(pending)}</div>
        <div class="stat-label">Pending ({_money(pending_stake)} at stake)</div>
      </div>
    """

    if not bets:
        body = '<div class="empty">No bets logged yet.</div>'
    else:
        body = "".join(_bet_card_html(b) for b in bets)

    updated = record.get("updated_at") or "never"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>MLB Player Props -- My Bets</title>
{STYLE}
<style>
  .bet-card {{ border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; background: var(--surface-2); }}
  .bet-card-head {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }}
  .leg-row {{ padding: 6px 0; border-top: 1px solid var(--border); }}
  .leg-row:first-of-type {{ border-top: none; }}
  .leg-main {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .leg-prop {{ color: var(--text-secondary); }}
  .leg-sub {{ margin-top: 2px; }}
</style>
</head>
<body>
  <div class="page">
    <div class="header-band">
      <div class="header-top">
        <div>
          <h1>My Bets</h1>
          <div class="meta">Personal wager tracker &middot; updated {html.escape(updated)}</div>
        </div>
        <div class="header-actions">
          <a class="nav-link" href="index.html">&larr; Back to Dashboard</a>
          <button id="themeToggle" class="theme-toggle" type="button">Switch to dark</button>
        </div>
      </div>
    </div>

    <div class="notes">
      <b>How to read this</b><br>
      Each leg is graded against real results using the exact same logic
      as every other pick on this site (an OVER that's already cleared
      shows HIT immediately, mid-game; a MISS only locks in once the game
      is Final). A bet only settles WON once every leg has hit, and
      settles LOST the moment any single leg misses -- same as a real
      parlay slip. "model" shows this site's own current projection/line
      for that exact player+category, for comparison against what was
      actually bet -- it's informational only, grading always uses YOUR
      actual line and direction, never the model's.
    </div>

    <div class="stat-row">{tiles}</div>

    <div class="date-heading">All bets</div>
    {body}
  </div>
{SCRIPT}
</body>
</html>"""


def run():
    os.makedirs(OUT_DIR, exist_ok=True)
    html_path = os.path.join(OUT_DIR, "my-bets.html")
    with open(html_path, "w") as f:
        f.write(render_html())
    print(f"Wrote my-bets page to {html_path}")


if __name__ == "__main__":
    run()
