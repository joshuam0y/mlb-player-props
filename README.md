# MLB Player Props Helper

A free, public-data research tool for MLB player props (hits, home runs, RBIs,
total bases, strikeouts, etc.) on markets like Sleeper and FanDuel. Pulls
everything from MLB's own public Stats API (no key, no cost) and MLB.com's
public news RSS feed -- no paid odds API, no scraping of betting sites.

**This is a research/context tool, not a prediction engine.** It does not
pull actual sportsbook lines, so it can't tell you "our number beats their
number." What it does do: aggregate, in one place, the things that actually
move a prop decision -- confirmed starting lineups, injury status, recent
form, and platoon (lefty/righty) matchups -- refreshed hourly, so you're not
tab-hopping multiple sites before locking in a bet.

## What it tracks per upcoming game

- **Confirmed starting lineup** (not just "probable") -- a batter is only
  marked CONFIRMED once MLB posts the actual batting order, typically
  1-2 hours before first pitch. Until then, batters are shown as PROJECTED
  based on recent appearances, clearly labeled as unconfirmed, so a hot bench
  bat never gets mistaken for a real starter.
- **Recent form** -- L7 and L15 rolling counting stats (H, HR, RBI, TB, BB,
  K), plus a HOT/COLD flag when L7 average diverges meaningfully from season
  average.
- **Platoon matchup edge** -- cross-references a batter's handedness against
  the *specific* opposing pitcher's own vs-lefty/vs-righty split (not just
  the generic "batter has the platoon advantage" fact). This is the kind of
  matchup-specific signal that generic prop-market pricing doesn't always
  reflect.
- **Injury status** -- sourced from MLB's own transactions feed (IL
  placements/activations), not third-party scraping.
- **Recent headlines** -- MLB.com news RSS, matched to tracked players.

## What it deliberately does NOT do

- No actual FanDuel/Sleeper odds ingestion (no free, ToS-safe API for this).
- No automated scraping of expert-pick/prop-betting sites (RotoWire, Action
  Network, etc. explicitly prohibit automated scraping in their ToS, and this
  runs as a public, hourly automated job -- exactly the pattern those clauses
  target). `output/notes.md`, if you create it, is read (never written) into
  the dashboard as a spot for your own manually-pasted notes.
- No claim of predictive accuracy. Short-window "hot streaks" are well known
  in sabermetrics to be mostly BABIP noise. See Backtesting below.

## Pipeline

Scripts in `pull/`, run in this order:

1. `sync_teams_and_roster.py` -- all 30 teams + active rosters, player bios.
2. `sync_schedule.py` -- next 5 days of games + probable pitchers.
3. `sync_lineups.py` -- confirmed starting lineups for games in the next day.
4. `sync_stats.py` -- game logs (current season first, then backfills full
   career) + platoon splits. Resumable: safe to interrupt and rerun, tracked
   in the `sync_state` table.
5. `sync_news.py` -- injuries (from transactions) + news headlines.
6. `build_props.py` -- builds the context report: `output/latest.json`,
   `output/latest.md`, `output/index.html` (the dashboard).
7. `verify_data.py` -- data-integrity check: confirms our summed game logs
   exactly match MLB's own official season totals. Catches ingestion bugs,
   not prediction errors.

Everything reads/writes `mlb_props.db` (SQLite) in the repo root.

## Running locally

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python pull/sync_teams_and_roster.py
python pull/sync_schedule.py
python pull/sync_lineups.py
python pull/sync_stats.py          # slow on first run -- backfills full career history
python pull/sync_news.py
python pull/build_props.py
open output/index.html
```

## Automation

`.github/workflows/hourly.yml` runs the full pipeline every hour on GitHub
Actions and commits the refreshed `output/` files back to the repo, so
`output/index.html` on `main` is always close to current. `mlb_props.db`
itself is *not* committed -- it's a large, constantly-mutating binary that
git can't delta-compress, so it's persisted between runs via GitHub Actions
cache instead (see the workflow file for why).

## Backtesting

Planned next: replay the pipeline against past game dates and check the
HOT/COLD and matchup-edge flags against what actually happened, to see
whether they carry real predictive signal or are just noise dressed up
nicely. Results (once run) will live in `output/backtest/`.
