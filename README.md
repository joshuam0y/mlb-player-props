# MLB Player Props Helper

**Live dashboard:** https://joshuam0y.github.io/mlb-player-props/

A free, public-data research tool for MLB player props (hits, home runs, RBIs,
total bases, strikeouts, etc.) on markets like Sleeper and FanDuel. Pulls
everything from MLB's own public Stats API (no key, no cost) and MLB.com's
public news RSS feed -- no paid odds API, no scraping of betting sites.

## Changelog

_Updated with every change going forward._

- **2026-08-01** -- Fixed a real gap: a game that went Final showed the
  final score but no Moneyline/Run line/Total HIT/MISS verdict for
  however long it took until the NEXT full page rebuild (every ~15-60
  min) -- that grading logic only existed in the server-rendered HTML,
  which a game usually outlives (a ~3 hour game vs. a 15-60 min rebuild
  cycle almost always means the pre-game picks line has already been
  removed from the page by the time the game actually ends). The client-
  side live tracker now grades all three itself the moment MLB's feed
  reports Final, using the same rules as the two existing (server-side)
  copies of this logic, so they can never disagree.
- **2026-07-30** -- Extended the bullpen-game fix below to the actual
  score/win-probability model, not just the matchup badge: the probable
  starter's own run rate used to get a flat 60% weight in the run-
  environment calc regardless of whether he's a real workhorse or
  tonight's opener. Now capped at his own expected share of the game's
  outs (from his season average outs/appearance) -- a real bullpen-game
  arm now gets correctly little individual weight, with the team's actual
  bullpen rate picking up the rest. Never raises weight above the
  existing 60% for a normal starter. Verified against a full-season
  backtest before shipping: Brier score 0.2626 -> 0.2620, total-runs MAE
  3.85 -> 3.84 -- a small but real, repeatable improvement in both.
- **2026-07-30** -- Fixed a real problem with the batter-vs-pitcher
  matchup badge: it always read as if tonight's probable pitcher will
  face the lineup like a normal starter, even when he's actually a
  short-outing arm (an opener, piggyback/bulk arm, or bullpen game) who'll
  mostly hand the game off to several other, unpredictable relievers.
  Added a workload classifier (average outs per appearance this season,
  not just games-started) -- a "reliever"-tier probable pitcher now skips
  the Matchup edge/Tough matchup read entirely for every batter facing
  him, and gets a "BULLPEN GAME" badge on his own card explaining why.
- **2026-07-30** -- Favorites now clear automatically on a new day instead
  of persisting forever -- a player favorited yesterday (who also plays
  today) no longer silently shows as favorited today too, and a team
  pick's favorite no longer just accumulates as a permanent "missing"
  entry once that game's date passes. Checked on the first read after
  midnight (next click or page load), same-day activity unaffected.
- **2026-07-30** -- Added a starting-pitcher short-rest penalty to the game
  model: a starter working on fewer than 4 days' rest (a scratch start,
  bullpen game, or doubleheader reshuffle -- not the normal 5-man rotation)
  now gets a small, capped runs-allowed penalty instead of being projected
  identically to a normal-rest start. Verified against this season's real
  games first: only 9 of 3,142 starter-slots actually had short rest, so
  the season-aggregate backtest Brier score/MAE came back byte-identical
  before and after (too rare to move a season-wide average) -- correct and
  safe for the specific games it applies to, just not a broad accuracy
  swing. No bonus for extra rest -- the evidence there is much weaker.
- **2026-07-29** -- Live in-game win probability now uses MLB's own
  official win-probability feed (computed from real historical game-flow
  outcomes -- score, inning, outs, baserunners) instead of a hand-rolled
  normal-distribution approximation. Falls back to the old estimate only
  for the handful of polls where MLB's feed doesn't have a play yet (very
  start of a game) or that one fetch specifically fails -- labeled "(est.)"
  only in that fallback case, plain "Win probability" otherwise. Pre-game
  projections (Moneyline/Run line/Total, before first pitch) are
  unaffected -- there's no "game flow" yet for them to react to.
- **2026-07-29** -- Fixed the rest of the full-codebase audit's real
  findings: (1) news headlines matching two active players with the exact
  same full name now disambiguate by team mentioned in the title instead
  of silently crediting whichever player the DB happened to return last --
  confirmed this isn't hypothetical, both Athletics' and Dodgers' Max
  Muncy are active right now; (2) a same-day doubleheader no longer lets
  the second game's matchup context silently overwrite the first's for a
  shared player when refreshing an already-frozen pick; (3) the data-
  completeness publish-guard now has a 24h cap on how long it can keep
  refusing to publish -- previously a genuine persistent regression (not
  just a transient recovery hiccup) would wedge the site at the same
  stale snapshot forever, silently; (4) corrected calibrate.py's docstring,
  which implied its Platt-scaling calibration feeds into live predictions
  -- it doesn't, nothing reads its output yet, left as a standalone
  research tool rather than wired in without validation.
- **2026-07-29** -- Fixed a real bug from a full-codebase audit: the live
  in-game tracker never started polling at all if you opened the dashboard
  before any of today's games had thrown a first pitch (a very common case
  -- checking props in the morning) -- it now always starts polling and
  re-checks which games have gone live on every 30s tick, instead of
  freezing that list once at page load.
- **2026-07-29** -- My Bets: a leg saved without picking a search suggestion
  (typed a name, hit submit before the dropdown resolved it) has no
  player/team id, so it could never auto-grade and silently sat there
  looking like an ordinary pending bet forever. Now shows a distinct
  "UNRESOLVED" badge (hover for why) so it's obvious it needs to be
  deleted and re-added correctly.
- **2026-07-29** -- Smaller fixes from the same audit: `latest.md`'s Top
  Unders list was mislabeling every entry's hit rate as "X% over" instead
  of the correct "(100-X)% under"; the strike-zone chart's "current pitch"
  ring highlight never actually rendered (an SVG presentation attribute
  can't resolve a CSS variable); the glossary said 100,000 simulation
  trials instead of the real 1,000,000; home/away splits didn't exclude
  today's own game like every other rolling stat does; and a dead unused
  CSS selector was removed.
- **2026-07-29** -- Trimmed extraneous words from every game card's picks
  line and badges: "Projected score:" -> "Projected:", "to win"/"to cover"/
  "lean"/"leaned" dropped (team/line/side already say it), and "LINEUP
  CONFIRMED"/"PROJECTED (not yet announced)" shortened to plain CONFIRMED/
  PROJECTED to match the badge wording used everywhere else on the page.
- **2026-07-29** -- Fixed each game card's time/status/venue line wrapping
  onto a second line -- it now truncates with an ellipsis instead, keeping
  every collapsed card the same compact height regardless of venue name
  length or screen width.
- **2026-07-29** -- Fixed the dashboard defaulting to "All dates" (today +
  2 days ahead, ~40 games) on every page load -- now defaults to just
  today's slate ("All dates" is still one click away), cutting the
  collapsed-card scroll a mobile visitor hits by more than half. Also
  trimmed the confidence percentage and venue out of each collapsed
  card's summary on mobile (the pick/team/line itself still shows), and
  removed a harmless but sloppy duplicate `data-player-id` attribute on
  pick cards.
- **2026-07-29** -- Favorited moneyline/run line/total picks now show the
  matchup and game time (not just the pick itself) in the favorites panel,
  and every game card shows a "♥ N favorites" badge for how many of your
  favorites belong to it -- previously a favorited team pick just showed
  the bare pick text (e.g. "Total 6.5 UNDER") with no way to tell which
  game it was for.
- **2026-07-29** -- Added a remove ("x") button to each leg row in My Bets'
  "Add a bet" form, so accidentally clicking "+ Add leg" one extra time no
  longer blocks submission with an empty required field you can't get rid of.
- **2026-07-29** -- Fixed a real regression: an earlier same-day backfill
  fix accidentally re-graded all of 2026-07-25 against a stale local
  database, wiping out good pitcher/batter data production already had.
  Restored from the last known-good commit, keeping only the intended fix.
- **2026-07-29** -- Moneyline/Run line/Total picks can now be favorited too
  (previously player props only) -- same heart-toggle and floating panel,
  generalized to a string key so team picks and players share one system.
- **2026-07-29** -- Home Runs UNDER can no longer win a Top Overs/Unders or
  "Best prop" pick (near-guaranteed, low-value -- matches the exclusion
  "Predicted: X" already had, extended to the picks that were missing it).
  Other UNDER categories (Walks, RBIs, Runs Scored) are unaffected.
- **2026-07-29** -- Track Record's per-day recap now shows player+stat-line
  detail for batter trend/matchup and pitcher form (who landed HOT/COLD/
  etc. and what they did), plus which specific games hit/missed on
  moneyline/run line/total. Also surgically backfilled 2026-07-25's frozen
  archive, which predated Predicted-lean/Best-prop star and showed 0
  graded picks for both -- diff-verified as touching only those two fields.
- **2026-07-29** -- Track Record now shows the full 8-stat recap (Top
  Overs/Unders, Moneyline, Run line, Total O/U, score accuracy,
  Predicted-lean, Best-prop star) for every individual day, not just the
  all-time cumulative row -- applies retroactively to every already-
  tracked day too, since the underlying per-day data already existed.
- **2026-07-29** -- Added a favorites feature for props on the main
  dashboard -- a heart-toggle on every batter/pitcher and Top Overs/Unders
  pick card, persisted per-browser (localStorage, no account needed), with
  a floating panel to jump straight to a favorited player's expanded row.
- **2026-07-29** -- Added profit boost tokens (20/25/30/50%, applied to
  the profit portion only) and a "2-up early win" token to My Bets -- a
  Moneyline pick with the token locks in a win the instant the team leads
  by 2+ runs at any point, even if they later lose, graded both server-
  side (authoritative) and live client-side from MLB's per-inning score
  history.
- **2026-07-29** -- Refined the total-line fix below: instead of always
  rounding the median up (which just traded one systematic bias for
  another -- every game leaning UNDER), it now picks whichever of the two
  candidate lines is actually closer to a fair 50/50 split, verified
  unbiased across random matchups (mean over_prob 0.498).
- **2026-07-29** -- Fixed the total-runs line being silently off by a full
  run about half the time -- Python's banker's-rounding on exact .5 ties
  (the simulated median is always a whole run count) rounded the line down
  for an odd median and up for an even one, producing lopsided "leans"
  (e.g. 62% over) that were really just a rounding artifact, not a real
  signal (user-reported and confirmed: Braves @ Mets showing "Total 4.5"
  against a 5.88-run projection).
- **2026-07-28** -- Added each game's local time to Top Overs/Unders pick
  cards and to My Bets (both player and team-prop legs).
- **2026-07-28** -- Fixed the live HIT/MISS badge never appearing on a
  pitcher's "Predicted: X" line, even after the live box score had clearly
  cleared it -- the DOM lookup relied on nesting that only held for
  batters, not pitchers (confirmed real case: Slade Cecconi, 6 hits
  allowed vs. a 5.5 line, still showing no verdict).
- **2026-07-28** -- Added each player's position everywhere a name shows up
  (batter table rows, Top Overs/Unders pick cards, Track Record's pick
  lists, My Bets search/leg display) -- batters get their real per-game
  lineup position when confirmed, roster position as a fallback; pitchers
  get "P". Also fixed the LINEUP CONFIRMED/PROJECTED badge on pick cards
  being hardcoded batter-only, hiding pitchers' now-real confirmed status.
- **2026-07-28** -- Added a "Potential winnings" summary tile to My Bets --
  total profit across every still-pending bet if all of them hit.
- **2026-07-28** -- Fixed Top Overs/Unders structurally favoring earlier
  games: the confirmed-lineup penalty now only applies once a game is
  actually inside its own lineup-posting window (first pitch <=3 hours
  out) -- verified every West Coast night game was unfairly penalized at
  the daily freeze moment purely for starting late, regardless of pick
  quality.
- **2026-07-28** -- My Bets now allows editing/deleting settled (won/lost)
  bets too, not just pending ones -- for fixing a real mistake (wrong
  stake/date/odds, or a bet that graded against the wrong game).
- **2026-07-28** -- My Bets player/team search suggestions now show each
  game's date/time, so a doubleheader (two games between the same teams,
  or the same player appearing twice, on one calendar date) is always
  distinguishable before picking one -- confirmed real case: a postponed
  game folded into a same-day doubleheader left two identical-looking
  search results with no way to tell which one a leg meant.
- **2026-07-28** -- Probable pitchers now get a real CONFIRMED/PROJECTED
  status (own badge on the pitcher card, factored into Top Overs/Unders
  scoring) instead of being hardcoded "confirmed" everywhere -- a rotation
  change, rainout, or doubleheader reshuffle can still swap a probable
  starter (confirmed real case: the Guardians/Reds 7/28 doubleheader). MLB's
  API has no explicit flag for this, so it uses the game's own status
  (Scheduled/Preview vs. later) as a proxy.
- **2026-07-28** -- Top Overs/Best-prop star were hitting worse than a coin
  flip (~34%/43% across the first 4 tracked Track Record days) -- traced to
  their category-of-the-day being chosen purely by "biggest recent-vs-season
  hot streak," a signal `backtest_props.py` had already shown barely
  predicts anything. A batter's pick now also has to agree with that
  category's matchup-adjusted `lean` (the signal behind "Predicted-lean hit
  rate," this site's best-performing metric at 65%) before it can headline;
  an uncorroborated hot streak falls back to a safer default category
  instead.
- **2026-07-28** -- Total Bases prop lines never show 0.5 anymore -- that
  was mathematically identical to a Hits 0.5 line (both true iff the player
  gets >=1 hit), so it carried no real signal. Floored at 1.5 now.
- **2026-07-28** -- Fixed a bug where a postponed/rescheduled game stayed
  stuck under its original date forever: `sync_schedule.py`'s upsert wasn't
  updating `official_date`/`game_date_utc` on an already-known `game_pk`, so
  a game MLB pushed to a new date (e.g. a rainout becoming a same-day
  doubleheader) silently never showed up once the calendar moved past its
  old date.
- **2026-07-27** -- Added filter/sort controls (status, date range, sort by
  date/profit/stake) to the My Bets list.
- **2026-07-27** -- My Bets player search now also falls back to MLB's live
  boxscore, so a real bet on anyone who actually played can be logged, not
  just this site's own modeled "starters" (surfaced a real lineup-sync gap
  for one specific game in the process).
- **2026-07-27** -- Fixed My Bets player/team search not working for
  previous-day bets (it only ever searched today's data).
- **2026-07-27** -- Added auto-computed to-win-from-odds, and the ability to
  edit/delete a still-pending bet, to My Bets.
- **2026-07-27** -- Added team-level props (Moneyline, Run Line, Total) to
  the bet tracker, on top of player props.
- **2026-07-27** -- Rebuilt My Bets as a real personal bet tracker: sign-in
  + cross-device sync (Firebase Auth/Firestore), player/team search with
  autocomplete, and live hit/miss grading against the same box-score feed
  the main dashboard uses.

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
- **BABIP/ISO luck check** -- flags when a "hot" streak looks BABIP-driven
  (likely to regress) vs. a real power uptick (ISO actually up).
- **Hit streaks** and **home/away splits**.
- **Team win/loss streak and last-10 form** -- shown as context next to each
  team name (e.g. "3-GAME WIN STREAK", "8-2 last 10 (+15 run diff)"). Kept
  separate from the actual game projection, which already blends a
  recency-weighted scoring rate more rigorously (see Game-outcome simulation
  below) -- a streak counter would just be a noisier restatement of the same
  games.
- **Recent-pitcher strikeout carryover** -- if a team's batters struck out at
  an elevated clip over their last couple of games (a tough starter ran
  through the lineup), the next opposing pitcher's strikeout prop gets a real
  scoring boost and a called-out reason, on the theory that a lineup that
  just got overmatched is live for it again.
- **Bullpen fatigue** -- relief innings thrown in the last 2 days vs. that
  team's own season-average workload, since a taxed pen matters for late-game
  at-bats even when the starter matchup looks fine.
- **Park factor tier** -- a static, illustrative hitter/pitcher/neutral tag
  per venue (Coors Field-type parks vs. pitcher's parks).
- **Pitcher props** -- strikeouts/runs-earned/hits-allowed/walks-allowed hit
  rates, a DOMINANT/ROUGH recent-form flag (L5 ERA vs. season ERA), and a
  matchup summary built from the *opposing* lineup's own platoon edges
  ("tough matchup tonight for: ...") -- surfaced both on the pitcher's own
  card and in the Top Overs/Unders leaderboards, not just batters.
- **"Best prop" per player** -- the single category (batting or pitching)
  where the last several games deviate the most from that player's own
  season norm, shown inline on every player row, not just in the leaderboard.

## Live, in-game tracking

Once a game goes live, each game card polls MLB's own live-feed API
(`statsapi.mlb.com`, CORS-open) directly from your browser every ~30 seconds
-- no server round-trip, so it updates far faster than the ~15-minute
dashboard rebuild underneath it. Only one game card is open at a time (the
"accordion" behavior; opening another closes the last one), and the header of
an open card stays pinned to the top of the screen while you scroll its body,
so closing it back up never means scrolling all the way back to the top.

- **Live score, inning, and a base/out diamond** -- always visible even with
  the card collapsed, so you can scan every live score on mobile without
  opening anything.
- **Live win probability (estimate)** -- a from-first-principles statistical
  model (not MLB's own proprietary one): each team's remaining scoring is
  treated as normally distributed around the current lead, with the variance
  shrinking as outs run out, plus a small home-field edge that shrinks right
  alongside it, and a same-side bonus for the batting team's current
  base/out state. Directionally right, not precise to the percentage point --
  labeled "(est.)" throughout so it doesn't read as more certain than it is.
- **Live count, a real strike-zone pitch plot** (using the same Statcast
  pitch coordinates MLB.com's own Gameday uses), and a **pitch-by-pitch list**
  with type and velocity for the current at-bat.
- **Recent plays feed** -- the play-by-play recap, including substitutions,
  pitching changes, and inning transitions, not just hits and outs.
- **Live per-player box scores** for every batter/pitcher on the page,
  updating in real time -- and for anyone who wasn't in the projected/
  confirmed lineup to begin with (a reliever, pinch hitter, or defensive
  sub), a compact "Also appeared" line gets added automatically, marked
  not-projected, in the same box-score style as everyone else, so a
  mid-game substitution's stats are never silently dropped.
- **Batting-order highlighting** -- the current batter, on-deck hitter, and
  pitcher are tagged directly in the lineup table (AT BAT / ON DECK /
  PITCHING), so it's obvious where in the order the game actually is without
  cross-referencing names by eye.

## Track Record

**https://joshuam0y.github.io/mlb-player-props/track-record.html**

Every pick this tool has actually made -- player-prop hit rates (HOT/COLD,
matchup-edge, pitcher-form flags) and full game-level picks (moneyline, run
line, total) -- graded against what really happened, day by day. Each day is
frozen from the very first report generated that morning, before that day's
games start, so nothing there can be quietly informed by that same day's
results. It also shows "projected stat vs. actual" -- the literal number
projected for every player/category next to what really happened, a more
granular cut than plain hit/miss. Same honesty standard as the Backtesting
section below: this shows what the signals actually did, not a curated
highlight reel.

## Game-outcome simulation

`game_model.py` + `simulate_games.py` project win probability, a run line
(standard MLB -1.5/+1.5), and a total-runs line for upcoming games -- a
log5-style blend of each team's own season runs-scored/allowed (home/away
split), the probable starter's ERA, and actual bullpen ERA (separated from
starters via `games_started`) adjusted for recent bullpen fatigue, fed into
a Negative-Binomial Monte Carlo (100,000 trials/game, fit against this
season's real run distribution, not assumed constants). Each team's offense index also blends in that team's own scoring rate
specifically against the probable opposing starter's handedness (weight 0.3,
once 15+ games vs. that hand exist to trust) -- verified by the same
backtest: Brier score 0.2639 with the split off, 0.2634 at weight 0.2, 0.2631
at weight 0.3, monotonically better as the weight increases, the same "real,
repeatable, not noise" pattern as the recency-weighting result below. Once a
game's lineup is confirmed, the specific 9 starters' recent form nudges the
offense index a small, capped amount -- before that, the projection runs on
team-level rates alone. Every projection is snapshotted with a timestamp in
`game_projections` (including a plain moneyline/run-line/total "pick") so a
later backtest can check what the model would have said *before* the game,
not after, and the dashboard shows the projected score rounded to the
nearest half-run, the way sportsbook lines are actually quoted.

**This is explicitly not pitched as beating Vegas.** There's no bullpen
usage/role data beyond aggregate recent workload, no Statcast, no
play-by-play. The goal is a calibrated baseline with a known, measured gap
from the market -- not a claim of edge. See Backtesting below.

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
6. `sync_results.py` -- backfills final scores for already-played games this
   season (ground truth for the game-sim calibration and backtest).
7. `build_props.py` -- builds the context report: `output/latest.json`,
   `output/latest.md`, `output/index.html` (the dashboard).
8. `verify_data.py` -- data-integrity check: confirms our summed game logs
   exactly match MLB's own official season totals. Catches ingestion bugs,
   not prediction errors.
9. `simulate_games.py` -- projects win probability/run line/total for
   upcoming games (see Game-outcome simulation above) into `game_projections`.

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
python pull/sync_results.py
python pull/build_props.py
python pull/simulate_games.py
open output/index.html
```

## Automation

Two workflows, on separate schedules:

- **`.github/workflows/hourly.yml`** -- the full pipeline, once an hour:
  team/roster sync, the full stats backfill, injuries/news, results, and a
  data-integrity check. Every sync step is `continue-on-error: true`, so a
  problem in any one source can't block the rest of the pipeline (or the
  deploy) from running with whatever data is available.
- **`.github/workflows/quick-refresh.yml`** -- schedule, confirmed lineups,
  re-simulation, and a dashboard rebuild every 15 minutes, so a lineup
  posting or a postponement doesn't sit stale for up to an hour. Reads the
  same database cache but never writes it back, so it can't race the
  hourly job's own cache save.

Both commit the refreshed `output/` files back to the repo and redeploy
Pages, so `output/index.html` on `main` stays close to current either way.
`build_props.py` refuses to publish a real regression in data completeness
(e.g. if a build ever runs against a still-recovering database), so a
partial sync can't silently push worse data live -- see its `run()`
docstring for the guard.

`mlb_props.db` itself is *not* committed -- it's a large, constantly-
mutating binary that git can't delta-compress, so it's persisted between
runs via a GitHub Actions cache instead (see `hourly.yml` for why, and for
the size-gated save that keeps a bad run from ever overwriting a good
cache with a corrupted one).

## Backtesting

`backtest.py` reconstructs what `game_model.py` would have projected
*before* each past game (point-in-time: only data dated strictly before
that game's date, so nothing leaks from the future), then scores it
against what actually happened.

```
python pull/backtest.py --start 2026-03-25 --end 2026-07-23
```

Result on the full 2026 season to date (1525 completed games, 2026-03-25
through 2026-07-23): **Brier score ~0.264, versus ~0.250 for a naive
"always guess the ~54% long-run home-win rate" baseline** -- worse than
that trivial baseline over the season as a whole. But that full-season
number hides a real, useful pattern: broken out by month, the model is
actively bad early (April: 0.293 vs. a 0.249 baseline -- team-level rates
built from only a handful of games are too noisy to trust yet) and
genuinely good later (July: 0.243 vs. a 0.251 baseline -- it beats the
baseline once each team has real season-long sample size). Point being:
judge this model's day-to-day usefulness by recent performance, not a
season-long average that's dragged down by the first month's cold start,
and don't over-read any single day's results either -- a 14-game slate is
a small enough sample that a genuinely well-calibrated model can still
have a rough night by chance.

Team-level run rates ARE recency-weighted (last 20 games, 35% weight,
blended with the season/context rate) -- verified with this same backtest
rather than assumed: 0.2656 with no recency blend vs. 0.2641 with it,
tested at a couple of weight/window settings to confirm the direction was
real and not noise, not just a one-off.

`calibrate.py` went a step further and tried Platt scaling (a fitted
logistic recalibration of the win probability, `sigmoid(A*logit(p)+B)`,
trained on an 80% chronological split and evaluated on the held-out 20%
it never saw -- the 308 most recent games at time of writing). **Result:
it doesn't help.** The held-out raw model already scores 0.242 against a
0.252 baseline on that recent slice (consistent with the July-specific
result above), and the fitted slope comes out close to flat (A ≈ 0.13),
meaning there isn't enough *additional* signal in the raw probability for
a recalibration to add on top of what's already there. Calibration is
only applied (writing `output/calibration.json`, which `simulate_games.py`
would then read) if it actually beats the raw probability on the held-out
set; right now it doesn't, so raw probabilities are used as-is.

`backtest_props.py` does the same point-in-time check for the player-prop
signals: does HOT/COLD, the BABIP-luck caveat, or the matchup-edge flag
actually predict what happens in the very next game? Result, on ~5,000
player-games: essentially no single-game predictive power on their own
(HOT .235 AVG vs. COLD .249 AVG vs. NEUTRAL .252 AVG -- barely different,
and not in the "obvious" direction). This matches the sabermetric
literature's well-known finding that short hot/cold streaks are mostly
noise -- confirmed here, not just cited.

None of this is hidden because it isn't flattering -- it's the actual
point of backtesting. See the module docstrings in `backtest.py`,
`backtest_props.py`, and `calibrate.py` for the full methodology.
