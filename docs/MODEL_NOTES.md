# Model notes: what the graded history actually says

Every number here comes from this project's own records — `output/track_record.json`
(9 graded days) and the archived daily snapshots `output/props_*.json` — not from
outside research. Reproduced 2026-08-03.

The code comments in `build_props.py` point here rather than restating the
numbers in five places. **If you're about to restore a signal this document
says is dead, read the relevant section first.**

## How the data was reconstructed

The archived snapshots are written hourly, so most of them catch a slate
mid-day and their `game_result` fields are mostly empty. But each player's
`prop_categories` carries their own last-10-game log with dates, and those
logs are `as_of_date`-filtered to exclude the game being previewed. Unioning
those logs across all ten archives yields complete per-game actuals for
every player who appeared, which can then be joined back to the
point-in-time inputs (`trend`, `matchup`, `hit_streak`, each category's
line and `today_projection`) from the archive of that same date.

That gives **2,233 gradable batter-games / 13,398 player-game-categories**
with no leakage: the inputs were computed strictly before each date, and the
actuals come from archives written later. A leakage check confirmed zero
category game-logs contained their own preview date.

## The thing that makes every hit rate misleading

These lines are the model's own — `round_to_half(0.4 × recent + 0.6 × season)`,
see `category_baselines()` — not a sportsbook's. `round_to_half` floors to
`x.5`, and per-game counting stats are right-skewed, so **the line sits above
the median outcome**. How often an OVER clears at all:

| Category | Line | n | OVER clears |
|---|---|---|---|
| Hits | 0.5 | 1750 | 54.5% |
| Total Bases | 1.5 | 1929 | 33.7% |
| Runs Scored | 0.5 | 2230 | 35.6% |
| Walks | 0.5 | 2233 | 27.6% |
| RBIs | 0.5 | 2221 | 26.7% |
| Hits | 1.5 | 483 | 26.1% |
| Home Runs | 0.5 | 2233 | 9.5% |

Three consequences, all of which we walked into:

1. **A Top Unders hit rate is not comparable to a Top Overs hit rate.** Overs
   looking worse is mostly the category mix.
2. **Neither is comparable to a −110 breakeven.** Nobody offers −110 on our
   own line, so reading 52.4% as the bar to clear is a category error.
3. **Whether a pick list is any good is invisible in its hit rate.** Top
   Unders graded 61.9% while a blind pick of the same props returned 67.2% —
   a list that looked passable was losing to random.

This is why `grade_picks.py` now records **lift** against a same-day,
same-(category, line, side) base rate. Lift is the part of the hit rate the
ranking is responsible for. Per-day rather than pooled, so a good list can't
be flattered by a high-scoring slate.

## Signals, measured

Clear rate for the OVER, by signal bucket, across 13,398 graded
player-game-categories:

| Signal | Bucket | n | OVER clears | vs. | z |
|---|---|---|---|---|---|
| form trend | hot | 2790 | 27.1% | cold 32.9% | **−4.75** |
| form trend | neutral | 7788 | 30.4% | | |
| BABIP caveat | "real" hot | 1230 | 26.7% | babip hot 27.4% | −0.45 |
| matchup edge | favorable | 2796 | 33.5% | unfavorable 28.2% | **+3.59** |
| hit streak | ≥5 games | 942 | 31.4% | 0–2 games 30.2% | +0.79 |
| lean | over | 2506 | 49.9% | under 19.2% | **+28.89** |

### The form trend is inverted, not merely weak

A batter flagged **hot** clears his OVER *less* often than one flagged
**cold** — 27.1% vs 32.9%, z = −4.75. The old `batter_over_score` awarded
`hot` **+2.0 toward the OVER**, its joint-largest term. It was betting the
wrong side of a real effect.

Two hypotheses tested, one of which failed:

- **Talent composition — DISPROVED.** HOT/COLD is defined against the
  player's own season average, so it could have been selecting weak hitters
  into HOT. It isn't: season AVG is flat across buckets (hot .246 / cold .248
  / neutral .247, ~500 player-games each).
- **Line inflation — real but small.** Because the line is 40% recent form, a
  hot streak *raises the bar the OVER must clear*. Measured: 7.0% of a hot
  batter's categories have their line pushed up versus a season-only line
  (0.0% down); for cold batters, 2.5% down and 0.0% up. Directionally exactly
  this mechanism, but too rare to explain a 6-point gap on its own.

The residual is ordinary mean reversion, which the README already cited from
the sabermetric literature and the project's own earlier `backtest_props.py`
run had already seen (HOT .235 vs COLD .249). That finding was documented but
never removed from the scoring.

### The BABIP-luck caveat does nothing

`trend_caveat()` splits a hot streak into "real" (ISO rising) and
`babip_driven` (lucky bloops). The split has no discriminating power at all:
26.7% vs 27.4%, z = −0.45, with "real" streaks marginally *worse*. It remains
on the player's row as a description; it no longer gates a score.

### The projection-vs-line margin is the one strong signal

Monotonic across all ten deciles — no bucket out of order:

| decile | margin | n | OVER clears |
|---|---|---|---|
| 1 | −1.28 … −0.42 | 1339 | 13.8% |
| 2 | −0.42 … −0.35 | 1339 | 13.5% |
| 3 | −0.35 … −0.27 | 1339 | 19.0% |
| 4 | −0.27 … −0.19 | 1339 | 24.9% |
| 5 | −0.19 … −0.12 | 1339 | 29.2% |
| 6 | −0.12 … −0.04 | 1339 | 31.5% |
| 7 | −0.04 … +0.03 | 1339 | 35.5% |
| 8 | +0.03 … +0.13 | 1339 | 36.3% |
| 9 | +0.13 … +0.30 | 1339 | 45.1% |
| 10 | +0.30 … +0.80 | 1347 | 53.4% |

The validated matchup edge is already inside this number, because
`today_projection = blended_average × batter_matchup_factor(matchup)`. Adding
a separate matchup bonus on top measured as no further gain — it would be
double-counting.

## What changed, and what it's worth

Backtested over the 9 graded days, top 15 batters per day per direction,
grading each variant against the base rate for the prop it actually picked:

| ranking | overs: hit / base / lift | z | unders: hit / base / lift | z |
|---|---|---|---|---|
| trend + matchup + streak (old) | 42.2% / 44.3% / **−2.0%** | −0.49 | 75.4% / 70.1% / **+5.3%** | +1.31 |
| matchup only | 52.5% / 47.0% / +5.5% | +1.33 | 75.7% / 70.4% / +5.3% | +1.37 |
| **margin (shipped)** | 56.9% / 47.7% / **+9.3%** | **+2.27** | 84.1% / 69.4% / **+14.7%** | **+3.31** |
| margin + matchup bonus | 56.9% / 47.7% / +9.2% | +2.26 | 83.2% / 69.4% / +13.8% | +3.20 |
| margin + inverted trend | 57.2% / 47.6% / +9.6% | +2.36 | 82.6% / 69.5% / +13.2% | +3.07 |

Re-run against the actual shipped `batter_over_score`/`batter_under_score`
(not a reimplementation): overs +8.3% (z = +2.03), unders +14.4% (z = +3.29).

**Inverting the trend was deliberately not shipped.** It buys +0.3% on overs
and *loses* 1.5% on unders — inside the noise, and betting a fitted
coefficient on nine days of data. Dropping the term is the honest change;
re-adding it with the sign flipped is curve-fitting.

## The Best-prop star was ranking on an anti-signal

`best_prop_star()` picked whichever player had the largest `|delta|`, where
delta is *recent hit-rate% minus season hit-rate%* at the same line. Graded
across 2,076 of those calls, a **bigger** deviation predicts a **worse**
outcome, monotonically:

| \|delta\| | n | best_prop direction hit |
|---|---|---|
| ≥ 25 pts | 514 | 49.2% |
| 15–24 pts | 856 | 56.4% |
| < 15 pts | 706 | 61.6% |

z = −4.31 between the outer buckets. Ranking by *max* |delta| was selecting,
on purpose, the least reliable call on the team — hence the star grading
46.4%, below every other tracked signal. It now selects on the strongest
`matchup_lean` margin, and `grade_picks.py` grades it on that same quantity
so selection and metric can't drift apart again.

`best_prop`/`delta` stay on the player's row as a descriptive
"recent stretch vs. season" stat, which is all it ever reliably was.

Note the cumulative star number will be a blend for a while: days graded
before this change were graded on `best_prop`. Re-grading old days doesn't
fully fix it either — the archived `star_player_id` was chosen by |delta| at
the time, so a re-grade gives old selection with new grading. Only days built
after 2026-08-03 are clean.

## Pitchers: no validated signal, kept and labelled

Pitcher picks came in **below a blind pick of the same prop in both
directions**: overs 30.3% vs 41.5% base (lift −11.2%, z = −1.35), unders
34.0% vs 51.5% base (lift −17.4%, z = **−2.49**).

Nothing here ranks them better:

- **Margin is flat for pitchers**, unlike batters — 49.2% clear rate in the
  bottom quintile, 50.8% in the top, non-monotonic in between. This is why
  the batter fix was *not* copied across.
- **`form_trend` is confounded.** "Dominant" pitchers clear their overs more
  often (59.3% vs 45.9% for "rough"), but a dominant recent stretch also
  *deflates* that pitcher's own runs/hits-allowed line, making the over
  mechanically easier without predicting anything. The graded season-level
  cut shows no real gap either (dominant 4.58 K/game, 4.55 ERA; rough 4.88
  K/game, 3.86 ERA).
- **Polarity incoherence was real but not the cause.** A pitcher could rank
  onto Top Overs for *pitching well* and then headline "Hits Allowed OVER" —
  the pick betting against its own stated reason. That hit 18 of 60 over
  picks and 11 of 55 unders. Fixed (`_pitcher_thesis_categories()`), but it
  doesn't rescue the hit rate: the contradictory picks actually graded
  slightly *better* (37.9% vs 32.6%, z = −0.53).

Two pitchers per game over nine days is a thin sample to delete a feature on,
so the lists stay — shortened to 4 per side and captioned with
`PITCHER_PICKS_CAVEAT` on every pick — until there's enough graded history to
find a real signal or drop them.

## Not touched, and why

- **`RECENT_WEIGHT = 0.4`.** The line-inflation effect above argues for
  lowering it, but the projections themselves are well calibrated (Hits bias
  −0.01 on 2,138 games, Total Bases −0.07, Runs Scored −0.03), and the margin
  ranking already handles the line where it lands. Changing it would move
  every historical hit-rate bar on the site for a second-order effect.
- **The game model.** Moneyline 57.0%, run line 61.7%, total 56.2% over 128
  games. Working; left alone.
- **`matchup_lean`.** Already the margin signal, already the best-performing
  tracked metric (64.5% overall, +5.2% lift on the graded batter sample).

## Reproducing

The analysis scripts were scratch, not committed. To redo it: union the
`prop_categories` `dates`/`values` arrays across `output/props_*.json` into
`(player_id, date, label) -> actual`, join to the same archives'
point-in-time entries for their own date, and compare against
`round_to_half`-derived lines. The lift benchmark in `grade_picks.py`
(`_slate_base_rates`) is the same computation, done per day at grading time,
so from here on the numbers accumulate on their own.
