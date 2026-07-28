"""
render_my_bets.py

Generates output/my-bets.html -- the personal bet tracker page. Unlike
every other page on this site, this one is NOT rebuilt from Python data
each cycle: everything on it (sign-in, the bet-entry form, the live bet
list, profit/loss) is driven client-side by Firebase (Auth + Firestore),
so this script only needs to run once, whenever the page's own code
changes. The server-side half (grading pending bets) lives in
sync_bets_firestore.py, called from build_props.py's own run().

Linked from the main dashboard and Track Record nav (the user's own
call -- this is a public static site with no page-level access control
either way, so linking it doesn't change the real security posture:
that's Firestore's own rules, which only let the signed-in user read
their own bets, and only the server-side Admin SDK, never the client,
update one after it's created).

Reuses the main dashboard's STYLE/SCRIPT so it doesn't look or behave
like a different site (same theme toggle, same color scheme). The
Firebase SDK is loaded as ES modules directly from Google's CDN --
no bundler/build step needed for a single static page like this.
"""

import html
import os

from render_dashboard import SCRIPT, STYLE

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")

# Public by Firebase's own design -- real security is enforced by
# Firestore security rules (see the setup docs), not by hiding this key.
FIREBASE_CONFIG = {
    "apiKey": "AIzaSyASxciCTPxSLDWt82n5DCUAhH_JdQ4xoP0",
    "authDomain": "mlb-props-1b47e.firebaseapp.com",
    "projectId": "mlb-props-1b47e",
    "storageBucket": "mlb-props-1b47e.firebasestorage.app",
    "messagingSenderId": "476096223505",
    "appId": "1:476096223505:web:d7c02d4393f59f579954b0",
}

FIREBASE_SDK_VERSION = "10.12.2"

PAGE_STYLE = """
<style>
  .bet-card { border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; background: var(--surface-2); }
  .bet-card-head { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .leg-row { padding: 6px 0; border-top: 1px solid var(--border); }
  .leg-row:first-of-type { border-top: none; }
  .leg-main { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .leg-prop { color: var(--text-secondary); }
  .leg-sub { margin-top: 2px; }
  #signin-view { max-width: 360px; margin: 40px auto; background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 24px; }
  #signin-view h2 { margin-top: 0; }
  #signin-view input { display: block; width: 100%; box-sizing: border-box; margin-bottom: 10px; padding: 9px 11px; border-radius: 8px; border: 1px solid var(--border); background: var(--surface-2); color: var(--text-primary); font-family: inherit; font-size: 14px; }
  #signin-view button { width: 100%; padding: 10px; border-radius: 8px; border: none; background: var(--series-1); color: #fff; font-weight: 600; cursor: pointer; font-family: inherit; font-size: 14px; }
  .bet-form-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  .bet-form-row input { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border); background: var(--surface-2); color: var(--text-primary); font-family: inherit; font-size: 13.5px; }
  .leg-input-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; align-items: center; }
  .leg-input-row input, .leg-input-row select { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border); background: var(--surface-2); color: var(--text-primary); font-family: inherit; font-size: 13.5px; }
  .leg-input-row .leg-entity { width: 100%; box-sizing: border-box; }
  .entity-search-wrap { position: relative; flex: 1; min-width: 160px; }
  .entity-suggestions { display: none; position: absolute; top: 100%; left: 0; right: 0; background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; z-index: 20; max-height: 220px; overflow-y: auto; margin-top: 2px; box-shadow: var(--shadow); }
  .entity-suggestion { padding: 8px 10px; cursor: pointer; font-size: 13.5px; }
  .entity-suggestion:hover, .entity-suggestion.active { background: var(--surface-3); }
  .entity-suggestion .sub { color: var(--text-secondary); font-size: 12px; }
  .leg-input-row.leg-resolved .entity-search-wrap .leg-entity { border-color: var(--status-good); }
  #bet-form button, #add-leg-btn { padding: 9px 16px; border-radius: 999px; border: none; background: var(--series-1); color: #fff; font-weight: 600; cursor: pointer; font-family: inherit; font-size: 13px; margin-right: 8px; margin-top: 4px; }
  #add-leg-btn { background: var(--surface-3); color: var(--text-primary); }
  #signout-btn { margin-top: 16px; padding: 8px 16px; border-radius: 999px; border: 1px solid var(--border); background: var(--surface-1); color: var(--text-primary); cursor: pointer; font-family: inherit; }
  .form-error { color: var(--status-critical); font-size: 13px; margin-top: 6px; }
  .bet-edit-btn, .bet-delete-btn { padding: 4px 10px; border-radius: 999px; border: 1px solid var(--border); background: var(--surface-1); color: var(--text-primary); cursor: pointer; font-family: inherit; font-size: 12px; margin-left: 6px; }
  .bet-delete-btn { color: var(--status-critical); }
  .bet-edit-form { margin: 10px 0; padding: 10px; border-radius: 8px; background: var(--surface-1); border: 1px solid var(--border); }
  .bet-edit-form input { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border); background: var(--surface-2); color: var(--text-primary); font-family: inherit; font-size: 13.5px; }
  .edit-save-btn, .edit-cancel-btn { padding: 7px 14px; border-radius: 999px; border: none; cursor: pointer; font-family: inherit; font-size: 12.5px; font-weight: 600; margin-top: 8px; margin-right: 6px; }
  .edit-save-btn { background: var(--series-1); color: #fff; }
  .edit-cancel-btn { background: var(--surface-3); color: var(--text-primary); }
  .bets-filter-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; }
  .bets-filter-row select, .bets-filter-row input { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border); background: var(--surface-2); color: var(--text-primary); font-family: inherit; font-size: 13px; }
  .bets-filter-row .sub { font-size: 12.5px; }
  #bet-filter-clear { padding: 8px 14px; border-radius: 999px; border: 1px solid var(--border); background: var(--surface-1); color: var(--text-primary); cursor: pointer; font-family: inherit; font-size: 12.5px; }
</style>
"""

CATEGORY_ROLE_JS = """
const CATEGORY_ROLE = {
  'Hits': 'batter', 'Total Bases': 'batter', 'Home Runs': 'batter',
  'RBIs': 'batter', 'Runs Scored': 'batter', 'Walks': 'batter',
  'Strikeouts': 'pitcher', 'Outs Recorded': 'pitcher', 'Runs Allowed': 'pitcher',
  'Hits Allowed': 'pitcher', 'Walks Allowed': 'pitcher',
};
const CATEGORIES = Object.keys(CATEGORY_ROLE);
"""

APP_MODULE_SCRIPT = """
<script type="module">
import {{ initializeApp }} from "https://www.gstatic.com/firebasejs/{v}/firebase-app.js";
import {{
  getAuth, signInWithEmailAndPassword, onAuthStateChanged, signOut
}} from "https://www.gstatic.com/firebasejs/{v}/firebase-auth.js";
import {{
  getFirestore, collection, addDoc, doc, updateDoc, deleteDoc, query, where, onSnapshot
}} from "https://www.gstatic.com/firebasejs/{v}/firebase-firestore.js";

const firebaseConfig = {config};

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const db = getFirestore(app);

{category_role_js}

// Category label -> live box-score field, and the innings-pitched parser --
// identical to BATTER_LEAN_FIELD/PITCHER_LEAN_FIELD/outsFromInningsPitched/
// liveLeanValue/liveLeanResult in render_dashboard.py's own live tracker.
// Duplicated rather than shared (this is a separate static page with its
// own script, no bundler to share modules across the two) but must stay
// in lockstep with pick_result()'s asymmetric grading logic in
// build_props.py: a cleared OVER/UNDER is permanent the instant it
// happens, safe to show live; the reverse only locks in once Final.
const BATTER_LEAN_FIELD = {{
  'Hits': 'hits', 'Total Bases': 'totalBases', 'Home Runs': 'homeRuns',
  'RBIs': 'rbi', 'Runs Scored': 'runs', 'Walks': 'baseOnBalls',
}};
const PITCHER_LEAN_FIELD = {{
  'Strikeouts': 'strikeOuts', 'Runs Allowed': 'earnedRuns',
  'Hits Allowed': 'hits', 'Walks Allowed': 'baseOnBalls',
}};

function outsFromInningsPitched(ip) {{
  if (ip == null) return null;
  const parts = String(ip).split('.');
  const whole = parseInt(parts[0], 10) || 0;
  const thirds = parts[1] ? (parseInt(parts[1], 10) || 0) : 0;
  return whole * 3 + thirds;
}}

function liveLeanValue(role, category, batting, pitching) {{
  if (role === 'batter') {{
    if (!batting || batting.atBats == null) return null;
    const field = BATTER_LEAN_FIELD[category];
    return field ? (batting[field] == null ? 0 : batting[field]) : null;
  }}
  if (!pitching || pitching.inningsPitched == null) return null;
  if (category === 'Outs Recorded') return outsFromInningsPitched(pitching.inningsPitched);
  const field = PITCHER_LEAN_FIELD[category];
  return field ? (pitching[field] == null ? 0 : pitching[field]) : null;
}}

function liveLeanResult(value, line, direction, isFinal) {{
  const cleared = value > line;
  if (!cleared && !isFinal) return null;
  return (direction === 'over' ? cleared : !cleared) ? 'hit' : 'miss';
}}

let reportCacheByDate = {{}}; // date -> report JSON, so switching the bet-date field doesn't re-fetch on every keystroke
let playerIndex = []; // today's index -- used by backfillMissingGamePks() only
let teamIndex = [];
let currentIndexDate = null;
let formPlayerIndex = []; // follows the add-bet form's own date field -- used by the leg search
let formTeamIndex = [];
let formIndexDate = null;

function easternToday() {{
  return new Intl.DateTimeFormat('en-CA', {{ timeZone: 'America/New_York' }}).format(new Date());
}}

// Shown next to every player/team search suggestion specifically so a
// doubleheader (the same two teams, or the same player, showing up twice
// for one calendar date) is actually distinguishable in the dropdown --
// confirmed real case: a postponed game folded into a same-day
// doubleheader left two "Cincinnati Reds vs Cleveland Guardians" entries
// with no way to tell which one a bet meant, so the leg could never
// resolve to the right specific game.
function formatGameTime(gameTimeUtc) {{
  if (!gameTimeUtc) return '';
  const d = new Date(gameTimeUtc);
  if (isNaN(d.getTime())) return '';
  return new Intl.DateTimeFormat('en-US', {{
    timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  }}).format(d);
}}

// American odds -> profit on the given stake (standard formula: positive
// odds pay that much per $100 staked; negative odds require that much
// staked to win $100). Confirmed real bug this fixes: typing the raw
// odds number into the "to win" field by mistake (e.g. entering 392
// instead of computing 5 * 3.92 = 19.60) -- auto-computing removes that
// whole class of manual-arithmetic error, while staying editable
// afterward for a real promo-boosted payout that genuinely differs from
// the plain formula.
function computeToWinFromOdds(stake, oddsText) {{
  if (isNaN(stake) || !oddsText) return null;
  const odds = parseFloat(String(oddsText).replace(/[^0-9.+-]/g, ''));
  if (isNaN(odds) || odds === 0) return null;
  const profit = odds > 0 ? stake * (odds / 100) : stake * (100 / Math.abs(odds));
  return Math.round(profit * 100) / 100;
}}

// Wires "auto-fill to-win from stake+odds" onto a given set of
// stake/odds/to-win inputs -- shared by the add-bet form and each bet's
// inline edit form so both behave identically.
function wireAutoToWin(stakeEl, oddsEl, toWinEl) {{
  function recompute() {{
    const stake = parseFloat(stakeEl.value);
    const computed = computeToWinFromOdds(stake, oddsEl.value);
    if (computed != null) toWinEl.value = computed;
  }}
  stakeEl.addEventListener('input', recompute);
  oddsEl.addEventListener('input', recompute);
}}

function buildPlayerIndex(report) {{
  const seen = new Set();
  const index = [];
  (report.games || []).forEach(function (g) {{
    ['home', 'away'].forEach(function (sideKey) {{
      const side = g[sideKey];
      if (!side) return;
      const entities = [];
      (side.batters || []).forEach(function (b) {{ entities.push({{ entity: b, role: 'batter' }}); }});
      if (side.probable_pitcher) entities.push({{ entity: side.probable_pitcher, role: 'pitcher' }});
      entities.forEach(function (item) {{
        const key = item.entity.player_id + '-' + g.game_pk;
        if (seen.has(key) || !item.entity.player_id) return;
        seen.add(key);
        index.push({{
          player_id: item.entity.player_id,
          name: item.entity.name,
          role: item.role,
          team: side.team_name,
          game_pk: g.game_pk,
          game_time_utc: g.game_time_utc,
          prop_categories: item.entity.prop_categories || [],
        }});
      }});
    }});
  }});
  return index;
}}

// One entry per (team, game) -- side ('home'/'away') is recorded here so
// a game-prop leg can be graded (server-side) or live-overlaid
// (client-side) without a separate DB lookup: home_score/away_score plus
// which side this team was on is everything Moneyline/Run Line/Total
// grading needs.
function buildTeamIndex(report) {{
  const seen = new Set();
  const index = [];
  (report.games || []).forEach(function (g) {{
    ['home', 'away'].forEach(function (sideKey) {{
      const side = g[sideKey];
      const oppSide = g[sideKey === 'home' ? 'away' : 'home'];
      if (!side || !side.team_id) return;
      const key = side.team_id + '-' + g.game_pk;
      if (seen.has(key)) return;
      seen.add(key);
      index.push({{
        team_id: side.team_id,
        name: side.team_name,
        opponent: oppSide ? oppSide.team_name : '',
        game_pk: g.game_pk,
        game_time_utc: g.game_time_utc,
        side: sideKey,
      }});
    }});
  }});
  return index;
}}

// latest.json only ever covers today + a couple days ahead -- searching
// it for a player/team from an earlier date silently finds nothing,
// which is exactly what made logging a previous day's bet look broken.
// A past date instead reads that day's own frozen archive
// (props_{{date}}.json, written once and kept forever -- see
// build_props.py's own run()), the same file the rest of this site
// grades historical days from.
async function loadReportForDate(date) {{
  if (reportCacheByDate[date]) return reportCacheByDate[date];
  const isToday = date === easternToday();
  const url = isToday ? 'latest.json' : ('props_' + date + '.json');
  let report = null;
  try {{
    const res = await fetch(url, {{ cache: 'no-store' }});
    if (res.ok) report = await res.json();
  }} catch (e) {{ /* fall through to the latest.json fallback below */ }}
  if (!report && !isToday) {{
    // A date with no archive yet (e.g. today before this site's own
    // freeze hour, if picked before local midnight rolled over) --
    // latest.json is still a reasonable best-effort fallback.
    try {{
      const res = await fetch('latest.json', {{ cache: 'no-store' }});
      if (res.ok) report = await res.json();
    }} catch (e) {{ /* give up below */ }}
  }}
  reportCacheByDate[date] = report || {{ games: [] }};
  return reportCacheByDate[date];
}}

// Today's own index -- used by backfillMissingGamePks() to resolve a
// missing game_pk on an already-pending bet (almost always today's or a
// very recent game). Kept separate from the bet-entry form's own index
// below: those two need to reflect DIFFERENT dates at the same moment
// whenever someone's filling out a previous-day bet while today's games
// are still being live-polled.
async function ensureTodayIndex() {{
  const today = easternToday();
  if (currentIndexDate === today) return;
  const report = await loadReportForDate(today);
  playerIndex = buildPlayerIndex(report);
  teamIndex = buildTeamIndex(report);
  currentIndexDate = today;
}}

// This site's own "batters" list per game is the CONFIRMED starting
// lineup (or, when that never got synced, a "likely starters" guess) --
// never the full roster. A real bet can land on anyone who actually
// played (a bench bat, a mid-game defensive sub, a reliever), and a
// lineup-sync gap for one specific game can even drop a genuine starter
// (confirmed real case: Luis Rengifo batted 6th for the Padres on
// 2026-07-26, but that game's lineup was never captured, so this site's
// own report fell back to a guess that excluded him). MLB's own boxscore
// endpoint always has the true, complete roster for a game regardless of
// what this site's DB captured, so it's used here purely as a search
// fallback -- confirmed CORS-open the same way the live-score feed is.
let boxscoreCacheByGamePk = {{}};
async function fetchGameParticipants(gamePk) {{
  if (boxscoreCacheByGamePk[gamePk]) return boxscoreCacheByGamePk[gamePk];
  const participants = [];
  try {{
    const res = await fetch('https://statsapi.mlb.com/api/v1/game/' + gamePk + '/boxscore', {{ cache: 'no-store' }});
    if (res.ok) {{
      const data = await res.json();
      ['home', 'away'].forEach(function (sideKey) {{
        const side = data.teams && data.teams[sideKey];
        if (!side) return;
        const teamName = side.team ? side.team.name : '';
        Object.keys(side.players || {{}}).forEach(function (key) {{
          const p = side.players[key];
          if (!p.person) return;
          participants.push({{
            player_id: p.person.id,
            name: p.person.fullName,
            role: (p.position && p.position.code === '1') ? 'pitcher' : 'batter',
            team: teamName,
            game_pk: gamePk,
            prop_categories: [],
          }});
        }});
      }});
    }}
  }} catch (e) {{ /* best-effort enrichment only -- search still works from the starters-only index if this fails */ }}
  boxscoreCacheByGamePk[gamePk] = participants;
  return participants;
}}

// The bet-entry form's own index -- follows whatever date is currently
// selected in the add-bet form's date field, so player/team search works
// for a previous day's bet too, not just today's games. Enriched with
// every game's full boxscore roster (see fetchGameParticipants above) so
// a real bet on anyone who actually played can always be found, not just
// this site's own modeled "starters."
async function ensureFormIndexForDate(date) {{
  if (formIndexDate === date) return;
  const report = await loadReportForDate(date);
  const starterIndex = buildPlayerIndex(report);
  // Archives span a few days (this site's own days_ahead window at the
  // moment they froze), so restricted to games on the exact selected date
  // -- otherwise a "yesterday" search would also pull in noise from
  // tomorrow's not-yet-played games.
  const gamePks = Array.from(new Set((report.games || []).filter(function (g) {{ return g.date === date; }}).map(function (g) {{ return g.game_pk; }})));
  const boxscoreLists = await Promise.all(gamePks.map(fetchGameParticipants));
  const seen = new Set(starterIndex.map(function (p) {{ return p.player_id + '-' + p.game_pk; }}));
  const merged = starterIndex.slice();
  boxscoreLists.forEach(function (list) {{
    list.forEach(function (p) {{
      const key = p.player_id + '-' + p.game_pk;
      if (seen.has(key)) return;
      seen.add(key);
      merged.push(p);
    }});
  }});
  formPlayerIndex = merged;
  formTeamIndex = buildTeamIndex(report);
  formIndexDate = date;
}}

function searchPlayers(text) {{
  const wanted = text.trim().toLowerCase();
  if (!wanted) return [];
  return formPlayerIndex.filter(function (p) {{ return p.name.toLowerCase().indexOf(wanted) !== -1; }}).slice(0, 8);
}}

function searchTeams(text) {{
  const wanted = text.trim().toLowerCase();
  if (!wanted) return [];
  return formTeamIndex.filter(function (t) {{ return t.name.toLowerCase().indexOf(wanted) !== -1; }}).slice(0, 8);
}}

function escapeHtml(s) {{
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}}

function tileHtml(value, label) {{
  return '<div class="stat-tile"><div class="stat-value">' + value + '</div><div class="stat-label">' + escapeHtml(label) + '</div></div>';
}}

function badgeHtml(label, kind) {{
  return '<span class="badge badge-' + kind + '">' + escapeHtml(label) + '</span>';
}}

function legStatusBadge(leg) {{
  if (leg.status === 'hit') return badgeHtml('HIT', 'hit');
  if (leg.status === 'miss') return badgeHtml('MISS', 'miss');
  if (leg.dnp) return badgeHtml('DNP', 'dnp');
  return badgeHtml('PENDING', 'projected');
}}

function betStatusBadge(bet) {{
  if (bet.status === 'won') return badgeHtml('WON', 'hit');
  if (bet.status === 'lost') return badgeHtml('LOST', 'miss');
  return badgeHtml('PENDING', 'projected');
}}

function betProfit(bet) {{
  if (bet.status === 'won') return bet.to_win;
  if (bet.status === 'lost') return -bet.stake;
  return null;
}}

function legRowDisplayHtml(leg) {{
  if (leg.kind === 'game') {{
    const propTxt = leg.category === 'Moneyline'
      ? leg.category
      : leg.category + ' ' + (leg.direction ? leg.direction.toUpperCase() + ' ' : '') + leg.line;
    const oppTxt = leg.opponent_name ? ' vs ' + escapeHtml(leg.opponent_name) : '';
    const actualTxt = leg.actual_value != null ? ' &middot; actual margin/total: ' + leg.actual_value : '';
    return (
      '<div class="leg-row"><div class="leg-main"><b>' + escapeHtml(leg.team_name) + '</b>' + oppTxt + ' ' +
      '<span class="leg-prop">' + escapeHtml(propTxt) + '</span> ' +
      legStatusBadge(leg) + '</div>' +
      '<div class="leg-sub sub">Team prop -- grades only once the game is Final' + actualTxt + '</div></div>'
    );
  }}
  const modelTxt = leg.model_projection != null
    ? 'model: ' + leg.model_projection + ' proj vs ' + leg.model_line + ' line, leans ' + (leg.model_lean || '\\u2014')
    : 'model: no current projection for this player/category';
  const actualTxt = leg.actual_value != null ? ' &middot; actual: ' + leg.actual_value : '';
  return (
    '<div class="leg-row"><div class="leg-main"><b>' + escapeHtml(leg.player_name) + '</b> ' +
    '<span class="leg-prop">' + escapeHtml(leg.category) + ' ' + leg.direction.toUpperCase() + ' ' + leg.line + '</span> ' +
    legStatusBadge(leg) + '</div>' +
    '<div class="leg-sub sub">' + escapeHtml(modelTxt) + actualTxt + '</div></div>'
  );
}}

function editFormHtml(bet) {{
  return (
    '<div class="bet-form-row">' +
    '<input type="date" class="edit-date" value="' + escapeHtml(bet.placed_date) + '">' +
    '<input type="number" step="0.01" class="edit-stake" value="' + bet.stake + '" placeholder="Stake ($)" style="width:110px">' +
    '<input type="number" step="0.01" class="edit-to-win" value="' + bet.to_win + '" placeholder="To win ($)" style="width:150px">' +
    '<input type="text" class="edit-odds" value="' + escapeHtml(bet.odds || '') + '" placeholder="Odds (e.g. +115)" style="width:140px">' +
    '</div>' +
    '<button type="button" class="edit-save-btn">Save</button>' +
    '<button type="button" class="edit-cancel-btn">Cancel</button>' +
    '<div class="form-error edit-error"></div>'
  );
}}

// Edit/Delete only offered on still-pending bets -- a settled one is a
// real historical record at that point, not something to quietly rewrite.
// Editing intentionally only covers date/stake/odds/to-win, not the legs
// themselves (player/category/line) -- fixing a wrong leg means
// deleting and re-adding the bet, simpler than reusing the full
// player/team-search leg-row UI inline in an edit form.
function betCardHtml(bet) {{
  const profit = betProfit(bet);
  const profitTxt = profit != null ? ((profit >= 0 ? '+$' : '-$') + Math.abs(profit).toFixed(2)) : ('at stake: $' + Number(bet.stake).toFixed(2));
  const legsHtml = (bet.legs || []).map(legRowDisplayHtml).join('');
  const oddsTxt = bet.odds ? ' &middot; odds ' + escapeHtml(bet.odds) : '';
  const parlayTxt = (bet.legs || []).length > 1 ? ' (' + bet.legs.length + '-leg parlay)' : '';
  const actionsHtml = bet.status === 'pending'
    ? ' <button type="button" class="bet-edit-btn">Edit</button><button type="button" class="bet-delete-btn">Delete</button>'
    : '';
  return (
    '<div class="bet-card" data-bet-id="' + bet.id + '"><div class="bet-card-head">' +
    '<span><b>' + escapeHtml(bet.placed_date) + '</b>' + parlayTxt + ' &middot; ' + escapeHtml(bet.sportsbook || 'FanDuel') + oddsTxt +
    ' &middot; staked $' + Number(bet.stake).toFixed(2) + ' to win $' + Number(bet.to_win).toFixed(2) + '</span>' +
    '<span>' + betStatusBadge(bet) + ' <b>' + profitTxt + '</b>' + actionsHtml + '</span></div>' +
    '<div class="bet-edit-form" style="display:none"></div>' +
    legsHtml + '</div>'
  );
}}

// Newest-first / oldest-first fall back to the id as a tiebreak so two
// bets placed on the same date still land in a stable, deterministic
// order instead of shuffling on every re-render. Profit sort always
// pushes still-pending bets (profit unknown, not zero) to the bottom
// regardless of direction -- "highest to lowest" shouldn't be read as
// "pending counts as zero."
const BET_SORTERS = {{
  date_desc: function (a, b) {{ return (b.placed_date || '').localeCompare(a.placed_date || '') || String(b.id).localeCompare(String(a.id)); }},
  date_asc: function (a, b) {{ return (a.placed_date || '').localeCompare(b.placed_date || '') || String(a.id).localeCompare(String(b.id)); }},
  profit_desc: function (a, b) {{
    const pa = betProfit(a), pb = betProfit(b);
    if (pa == null) return pb == null ? 0 : 1;
    if (pb == null) return -1;
    return pb - pa;
  }},
  profit_asc: function (a, b) {{
    const pa = betProfit(a), pb = betProfit(b);
    if (pa == null) return pb == null ? 0 : 1;
    if (pb == null) return -1;
    return pa - pb;
  }},
  stake_desc: function (a, b) {{ return Number(b.stake) - Number(a.stake); }},
  stake_asc: function (a, b) {{ return Number(a.stake) - Number(b.stake); }},
}};

// Only affects which bets show (and in what order) in the "All bets"
// list below -- the summary tiles above always reflect every logged
// bet, since those are meant to be the honest running total, not a
// number that quietly changes shape when someone's just browsing.
let betFilters = {{ status: 'all', dateFrom: '', dateTo: '', sort: 'date_desc' }};

function applyBetFilters(bets) {{
  const filtered = bets.filter(function (b) {{
    if (betFilters.status !== 'all' && b.status !== betFilters.status) return false;
    if (betFilters.dateFrom && b.placed_date < betFilters.dateFrom) return false;
    if (betFilters.dateTo && b.placed_date > betFilters.dateTo) return false;
    return true;
  }});
  return filtered.sort(BET_SORTERS[betFilters.sort] || BET_SORTERS.date_desc);
}}

function renderBets(bets) {{
  const settled = bets.filter(function (b) {{ return b.status === 'won' || b.status === 'lost'; }});
  const pending = bets.filter(function (b) {{ return b.status === 'pending'; }});
  const totalProfit = settled.reduce(function (sum, b) {{ return sum + (b.status === 'won' ? b.to_win : -b.stake); }}, 0);
  const wins = settled.filter(function (b) {{ return b.status === 'won'; }}).length;
  const losses = settled.filter(function (b) {{ return b.status === 'lost'; }}).length;
  const pendingStake = pending.reduce(function (sum, b) {{ return sum + b.stake; }}, 0);

  document.getElementById('summary-tiles').innerHTML =
    tileHtml((totalProfit >= 0 ? '+$' : '-$') + Math.abs(totalProfit).toFixed(2), 'Total profit/loss (settled bets)') +
    tileHtml(wins + '-' + losses, 'Record (settled bets)') +
    tileHtml(String(pending.length), 'Pending ($' + pendingStake.toFixed(2) + ' at stake)');

  const visible = applyBetFilters(bets.slice());
  document.getElementById('bets-list').innerHTML = visible.length
    ? visible.map(betCardHtml).join('')
    : ('<div class="empty">' + (bets.length ? 'No bets match these filters.' : 'No bets logged yet.') + '</div>');
}}

function categoryOptionsHtml(role) {{
  const cats = role ? CATEGORIES.filter(function (c) {{ return CATEGORY_ROLE[c] === role; }}) : CATEGORIES;
  return '<option value="">Category</option>' + cats.map(function (c) {{ return '<option value="' + c + '">' + c + '</option>'; }}).join('');
}}

const GAME_CATEGORIES = ['Moneyline', 'Run Line', 'Total'];
function gameCategoryOptionsHtml() {{
  return '<option value="">Category</option>' + GAME_CATEGORIES.map(function (c) {{ return '<option value="' + c + '">' + c + '</option>'; }}).join('');
}}

function legRowHtml(idx) {{
  return (
    '<div class="leg-input-row" data-idx="' + idx + '" data-kind="player">' +
    '<select class="leg-kind"><option value="player">Player</option><option value="team">Team</option></select>' +
    '<div class="entity-search-wrap">' +
    '<input type="text" class="leg-entity" placeholder="Search player..." autocomplete="off" required>' +
    '<div class="entity-suggestions"></div>' +
    '</div>' +
    '<select class="leg-category">' + categoryOptionsHtml(null) + '</select>' +
    '<input type="number" step="0.5" class="leg-line" placeholder="Line" required style="width:80px">' +
    '<select class="leg-direction"><option value="over">Over</option><option value="under">Under</option></select>' +
    '</div>'
  );
}}

// Selecting a suggestion (player OR team, depending on the row's current
// "kind") stores the resolved id/role/game_pk/side as data-* on the row
// itself -- submission then trusts that resolution directly instead of
// re-guessing from typed text, and the category dropdown narrows to
// either that player's real categories (a batter can never accidentally
// get offered "Strikeouts") or the 3 game-prop categories.
function wireLegRow(rowEl) {{
  const kindSelect = rowEl.querySelector('.leg-kind');
  const input = rowEl.querySelector('.leg-entity');
  const suggestionsEl = rowEl.querySelector('.entity-suggestions');
  const categorySelect = rowEl.querySelector('.leg-category');
  const lineInput = rowEl.querySelector('.leg-line');
  const directionSelect = rowEl.querySelector('.leg-direction');

  function clearResolution() {{
    rowEl.dataset.playerId = '';
    rowEl.dataset.role = '';
    rowEl.dataset.teamId = '';
    rowEl.dataset.opponent = '';
    rowEl.dataset.side = '';
    rowEl.dataset.gamePk = '';
    rowEl.classList.remove('leg-resolved');
  }}

  // Moneyline has no line/direction at all (just which team); Run Line
  // needs a signed line (e.g. -1.5) but no direction (the team picked IS
  // the direction); Total needs both, same as a player prop.
  function updateFieldVisibility() {{
    const kind = kindSelect.value;
    const category = categorySelect.value;
    if (kind === 'player') {{
      lineInput.style.display = '';
      directionSelect.style.display = '';
      lineInput.required = true;
    }} else if (category === 'Moneyline') {{
      lineInput.style.display = 'none';
      directionSelect.style.display = 'none';
      lineInput.required = false;
    }} else if (category === 'Run Line') {{
      lineInput.style.display = '';
      directionSelect.style.display = 'none';
      lineInput.required = true;
    }} else {{
      lineInput.style.display = '';
      directionSelect.style.display = '';
      lineInput.required = true;
    }}
  }}

  kindSelect.addEventListener('change', function () {{
    const kind = kindSelect.value;
    rowEl.dataset.kind = kind;
    input.value = '';
    input.placeholder = kind === 'player' ? 'Search player...' : 'Search team...';
    clearResolution();
    categorySelect.innerHTML = kind === 'player' ? categoryOptionsHtml(null) : gameCategoryOptionsHtml();
    updateFieldVisibility();
  }});

  categorySelect.addEventListener('change', updateFieldVisibility);

  input.addEventListener('input', function () {{
    clearResolution();
    const kind = kindSelect.value;
    const matches = kind === 'player' ? searchPlayers(input.value) : searchTeams(input.value);
    if (!matches.length) {{
      suggestionsEl.style.display = 'none';
      suggestionsEl.innerHTML = '';
      return;
    }}
    suggestionsEl.dataset.matches = JSON.stringify(matches);
    suggestionsEl.innerHTML = matches.map(function (m, i) {{
      const base = kind === 'player' ? (m.team || '') : ('vs ' + (m.opponent || ''));
      const timeTxt = formatGameTime(m.game_time_utc);
      const sub = base + (timeTxt ? ' · ' + timeTxt : '');
      return '<div class="entity-suggestion" data-i="' + i + '">' + escapeHtml(m.name) +
        ' <span class="sub">' + escapeHtml(sub) + '</span></div>';
    }}).join('');
    suggestionsEl.style.display = 'block';
  }});

  suggestionsEl.addEventListener('mousedown', function (e) {{
    const item = e.target.closest('.entity-suggestion');
    if (!item) return;
    e.preventDefault();
    const matches = JSON.parse(suggestionsEl.dataset.matches || '[]');
    const picked = matches[parseInt(item.dataset.i, 10)];
    if (!picked) return;
    input.value = picked.name;
    const kind = kindSelect.value;
    if (kind === 'player') {{
      rowEl.dataset.playerId = picked.player_id;
      rowEl.dataset.role = picked.role;
      rowEl.dataset.gamePk = picked.game_pk;
      categorySelect.innerHTML = categoryOptionsHtml(picked.role);
    }} else {{
      rowEl.dataset.teamId = picked.team_id;
      rowEl.dataset.opponent = picked.opponent;
      rowEl.dataset.side = picked.side;
      rowEl.dataset.gamePk = picked.game_pk;
    }}
    rowEl.classList.add('leg-resolved');
    suggestionsEl.style.display = 'none';
    updateFieldVisibility();
  }});

  input.addEventListener('blur', function () {{
    setTimeout(function () {{ suggestionsEl.style.display = 'none'; }}, 150);
  }});

  updateFieldVisibility();
}}

let legIdx = 0;
function addLegRow() {{
  const container = document.getElementById('legs-container');
  const wrap = document.createElement('div');
  wrap.innerHTML = legRowHtml(legIdx++);
  const rowEl = wrap.firstChild;
  container.appendChild(rowEl);
  wireLegRow(rowEl);
}}
function resetLegRows() {{
  document.getElementById('legs-container').innerHTML = '';
  legIdx = 0;
  addLegRow();
}}

let unsubscribeBets = null;

let currentBets = [];
let livePollTimer = null;

// Live overlay on top of Firestore's own (authoritative but ~15-minutes-
// behind) grading: polls MLB's live feed directly for every game any
// pending leg is in, same source and same asymmetric grading logic as
// the main dashboard's own live tracker. This never writes to Firestore
// -- it's a read-only, in-memory preview layer for THIS page load; the
// server-side sync_bets_firestore.py is still the one thing that
// actually persists a bet's result.
// Backfills game_pk (in memory only, for this page load's own live-poll
// use -- the persistent fix is the server-side grading script resolving
// it the same way) on any pending leg that's missing it, e.g. one
// entered before this field was captured at submission time. Looked up
// from the same latest.json-derived index used for search/model-
// projection, so this only works for legs whose game is in the current
// report window (today forward) -- an older leg outside that window
// still needs the server-side resolution to ever settle.
function backfillMissingGamePks() {{
  currentBets.forEach(function (bet) {{
    if (bet.status !== 'pending') return;
    (bet.legs || []).forEach(function (leg) {{
      if (leg.status !== 'pending' || leg.game_pk) return;
      if (leg.kind === 'game' && leg.team_id) {{
        const entry = teamIndex.find(function (t) {{ return t.team_id === leg.team_id; }});
        if (entry) {{ leg.game_pk = entry.game_pk; leg.side = leg.side || entry.side; }}
      }} else if (leg.player_id) {{
        const entry = playerIndex.find(function (p) {{ return p.player_id === leg.player_id; }});
        if (entry) leg.game_pk = entry.game_pk;
      }}
    }});
  }});
}}

function pollAllBetGames() {{
  backfillMissingGamePks();
  const gamePks = new Set();
  currentBets.forEach(function (bet) {{
    if (bet.status !== 'pending') return;
    (bet.legs || []).forEach(function (leg) {{
      if (leg.status === 'pending' && leg.game_pk) gamePks.add(leg.game_pk);
    }});
  }});
  gamePks.forEach(function (pk) {{
    fetch('https://statsapi.mlb.com/api/v1.1/game/' + pk + '/feed/live')
      .then(function (r) {{ return r.json(); }})
      .then(function (data) {{ applyLiveDataToBets(pk, data); }})
      .catch(function () {{ /* transient network hiccup -- keep last known state, try again next poll */ }});
  }});
}}

function applyLiveDataToBets(gamePk, data) {{
  const isFinal = ((data.gameData || {{}}).status || {{}}).abstractGameState === 'Final';
  const box = ((data.liveData || {{}}).boxscore || {{}}).teams || {{}};
  const linescore = (data.liveData || {{}}).linescore || {{}};
  const homeRuns = ((linescore.teams || {{}}).home || {{}}).runs;
  const awayRuns = ((linescore.teams || {{}}).away || {{}}).runs;
  let anyChanged = false;
  currentBets.forEach(function (bet) {{
    if (bet.status !== 'pending') return;
    let betChanged = false;
    (bet.legs || []).forEach(function (leg) {{
      if (leg.status !== 'pending' || leg.game_pk !== gamePk) return;

      if (leg.kind === 'game') {{
        // Never grade a game-outcome prop before it's actually Final --
        // unlike a player stat, a game lead isn't a permanent fact until
        // the last out (see _grade_game_leg()'s own docstring server-side).
        if (!isFinal || homeRuns == null || awayRuns == null) return;
        const teamRuns = leg.side === 'home' ? homeRuns : awayRuns;
        const oppRuns = leg.side === 'home' ? awayRuns : homeRuns;
        let result = null;
        if (leg.category === 'Moneyline') {{
          result = teamRuns > oppRuns ? 'hit' : 'miss';
        }} else if (leg.category === 'Run Line') {{
          result = (teamRuns - oppRuns) > -leg.line ? 'hit' : 'miss';
        }} else if (leg.category === 'Total') {{
          const cleared = (homeRuns + awayRuns) > leg.line;
          result = (leg.direction === 'over' ? cleared : !cleared) ? 'hit' : 'miss';
        }}
        if (!result) return;
        leg.status = result;
        betChanged = true;
        return;
      }}

      if (!leg.player_id) return;
      let batting = null, pitching = null;
      ['home', 'away'].forEach(function (side) {{
        const players = (box[side] || {{}}).players || {{}};
        Object.keys(players).forEach(function (key) {{
          const p = players[key];
          if (p.person && p.person.id === leg.player_id) {{
            batting = p.stats && p.stats.batting;
            pitching = p.stats && p.stats.pitching;
          }}
        }});
      }});
      const value = liveLeanValue(leg.role, leg.category, batting, pitching);
      if (value == null) return;
      const result = liveLeanResult(value, leg.line, leg.direction, isFinal);
      if (!result) return;
      leg.status = result;
      betChanged = true;
    }});
    if (betChanged) {{
      anyChanged = true;
      const statuses = bet.legs.map(function (l) {{ return l.status; }});
      if (statuses.some(function (s) {{ return s === 'miss'; }})) bet.status = 'lost';
      else if (statuses.every(function (s) {{ return s === 'hit'; }})) bet.status = 'won';
    }}
  }});
  if (anyChanged) renderBets(currentBets);
}}

onAuthStateChanged(auth, function (user) {{
  const signinView = document.getElementById('signin-view');
  const appView = document.getElementById('app-view');
  if (user) {{
    signinView.style.display = 'none';
    appView.style.display = 'block';
    if (unsubscribeBets) unsubscribeBets();
    // Waits for latest.json (playerIndex/teamIndex) before attaching the
    // bets listener -- backfillMissingGamePks() needs those populated to
    // do anything useful, and without this, the very first poll (right
    // when the snapshot first fires) would run against empty indexes and
    // silently find nothing until the NEXT 30s tick caught up instead.
    ensureTodayIndex().then(function () {{
      // No orderBy here on purpose: combining it with the where() below
      // requires a Firestore composite index (a one-time manual step in
      // the Firebase console) -- sorting the small per-user result set
      // client-side avoids that entirely.
      const q = query(collection(db, 'bets'), where('userId', '==', user.uid));
      unsubscribeBets = onSnapshot(q, function (snap) {{
        const bets = [];
        snap.forEach(function (doc) {{ bets.push(Object.assign({{ id: doc.id }}, doc.data())); }});
        bets.sort(function (a, b) {{ return (b.placed_date || '').localeCompare(a.placed_date || '') || String(b.id).localeCompare(String(a.id)); }});
        currentBets = bets;
        renderBets(currentBets);
        pollAllBetGames();
        if (!livePollTimer) livePollTimer = setInterval(pollAllBetGames, 30000);
      }}, function (err) {{
        document.getElementById('bets-list').innerHTML = '<div class="empty">Error loading bets: ' + escapeHtml(err.message) + '</div>';
      }});
    }});
  }} else {{
    signinView.style.display = 'block';
    appView.style.display = 'none';
    if (unsubscribeBets) {{ unsubscribeBets(); unsubscribeBets = null; }}
    if (livePollTimer) {{ clearInterval(livePollTimer); livePollTimer = null; }}
    currentBets = [];
  }}
}});

document.getElementById('signin-btn').addEventListener('click', async function () {{
  const email = document.getElementById('email').value;
  const password = document.getElementById('password').value;
  const errorEl = document.getElementById('signin-error');
  errorEl.textContent = '';
  try {{
    await signInWithEmailAndPassword(auth, email, password);
  }} catch (err) {{
    errorEl.textContent = 'Sign-in failed: ' + err.message;
  }}
}});

document.getElementById('signout-btn').addEventListener('click', function () {{
  signOut(auth);
}});

// Event delegation on the whole list (not one listener per card) since
// renderBets() rebuilds the DOM from scratch on every Firestore/live-poll
// update -- per-card listeners would just leak and re-attach constantly.
document.getElementById('bets-list').addEventListener('click', async function (e) {{
  const card = e.target.closest('.bet-card');
  if (!card) return;
  const betId = card.dataset.betId;

  if (e.target.classList.contains('bet-delete-btn')) {{
    if (!confirm('Delete this bet? This cannot be undone.')) return;
    try {{
      await deleteDoc(doc(db, 'bets', betId));
    }} catch (err) {{
      alert('Error deleting: ' + err.message);
    }}
    return;
  }}

  if (e.target.classList.contains('bet-edit-btn')) {{
    const bet = currentBets.find(function (b) {{ return b.id === betId; }});
    if (!bet) return;
    const formEl = card.querySelector('.bet-edit-form');
    formEl.innerHTML = editFormHtml(bet);
    formEl.style.display = 'block';
    wireAutoToWin(formEl.querySelector('.edit-stake'), formEl.querySelector('.edit-odds'), formEl.querySelector('.edit-to-win'));
    return;
  }}

  if (e.target.classList.contains('edit-cancel-btn')) {{
    const formEl = card.querySelector('.bet-edit-form');
    formEl.style.display = 'none';
    formEl.innerHTML = '';
    return;
  }}

  if (e.target.classList.contains('edit-save-btn')) {{
    const formEl = card.querySelector('.bet-edit-form');
    const errorEl = formEl.querySelector('.edit-error');
    const date = formEl.querySelector('.edit-date').value;
    const stake = parseFloat(formEl.querySelector('.edit-stake').value);
    const toWin = parseFloat(formEl.querySelector('.edit-to-win').value);
    const odds = formEl.querySelector('.edit-odds').value || null;
    if (!date || isNaN(stake) || isNaN(toWin)) {{
      errorEl.textContent = 'Date, stake, and to-win are all required.';
      return;
    }}
    try {{
      await updateDoc(doc(db, 'bets', betId), {{ placed_date: date, stake: stake, to_win: toWin, odds: odds }});
      formEl.style.display = 'none';
      formEl.innerHTML = '';
    }} catch (err) {{
      errorEl.textContent = 'Error saving: ' + err.message;
    }}
  }}
}});

document.getElementById('add-leg-btn').addEventListener('click', addLegRow);

document.getElementById('bet-filter-status').addEventListener('change', function () {{ betFilters.status = this.value; renderBets(currentBets); }});
document.getElementById('bet-filter-from').addEventListener('change', function () {{ betFilters.dateFrom = this.value; renderBets(currentBets); }});
document.getElementById('bet-filter-to').addEventListener('change', function () {{ betFilters.dateTo = this.value; renderBets(currentBets); }});
document.getElementById('bet-sort').addEventListener('change', function () {{ betFilters.sort = this.value; renderBets(currentBets); }});
document.getElementById('bet-filter-clear').addEventListener('click', function () {{
  betFilters = {{ status: 'all', dateFrom: '', dateTo: '', sort: 'date_desc' }};
  document.getElementById('bet-filter-status').value = 'all';
  document.getElementById('bet-filter-from').value = '';
  document.getElementById('bet-filter-to').value = '';
  document.getElementById('bet-sort').value = 'date_desc';
  renderBets(currentBets);
}});

document.getElementById('bet-date').value = easternToday();
resetLegRows();
wireAutoToWin(document.getElementById('bet-stake'), document.getElementById('bet-odds'), document.getElementById('bet-to-win'));
ensureFormIndexForDate(document.getElementById('bet-date').value);
document.getElementById('bet-date').addEventListener('change', function () {{
  ensureFormIndexForDate(this.value);
}});

document.getElementById('bet-form').addEventListener('submit', async function (e) {{
  e.preventDefault();
  const errorEl = document.getElementById('bet-form-error');
  errorEl.textContent = '';
  try {{
    await ensureFormIndexForDate(document.getElementById('bet-date').value);
    const legRows = document.querySelectorAll('#legs-container .leg-input-row');
    const legs = [];
    legRows.forEach(function (row) {{
      const kind = row.querySelector('.leg-kind').value;
      const entityName = row.querySelector('.leg-entity').value.trim();
      const category = row.querySelector('.leg-category').value;
      const lineRaw = row.querySelector('.leg-line').value;
      const direction = row.querySelector('.leg-direction').value;
      if (!entityName || !category) return;

      if (kind === 'team') {{
        const teamId = row.dataset.teamId ? parseInt(row.dataset.teamId, 10) : null;
        const needsLine = category !== 'Moneyline';
        const line = lineRaw === '' ? null : parseFloat(lineRaw);
        if (needsLine && (line == null || isNaN(line))) return;
        legs.push({{
          kind: 'game',
          team_name: entityName,
          team_id: teamId,
          opponent_name: row.dataset.opponent || null,
          side: row.dataset.side || null,
          game_pk: row.dataset.gamePk ? parseInt(row.dataset.gamePk, 10) : null,
          category: category,
          line: needsLine ? line : null,
          direction: category === 'Total' ? direction : null,
          status: 'pending',
          actual_value: null,
        }});
        return;
      }}

      // Player leg. Trust the row's own resolved selection (from clicking
      // a search suggestion) over re-guessing from the typed text -- if
      // the row was never resolved (typed a name but never picked a
      // suggestion), fall back to an unresolved leg rather than blocking
      // submission entirely, same as the old text-only flow.
      const line = parseFloat(lineRaw);
      if (isNaN(line)) return;
      const playerId = row.dataset.playerId ? parseInt(row.dataset.playerId, 10) : null;
      const role = row.dataset.role || CATEGORY_ROLE[category];
      const gamePk = row.dataset.gamePk ? parseInt(row.dataset.gamePk, 10) : null;
      let modelProjection = null, modelLine = null, modelLean = null;
      if (playerId) {{
        const entry = formPlayerIndex.find(function (p) {{ return p.player_id === playerId; }});
        const cat = entry ? (entry.prop_categories || []).find(function (c) {{ return c.label === category; }}) : null;
        if (cat) {{ modelProjection = cat.today_projection; modelLine = cat.primary_line; modelLean = cat.lean; }}
      }}
      legs.push({{
        kind: 'player',
        player_name: entityName,
        player_id: playerId,
        role: role,
        category: category,
        line: line,
        direction: direction,
        game_pk: gamePk,
        status: 'pending',
        actual_value: null,
        dnp: false,
        model_projection: modelProjection,
        model_line: modelLine,
        model_lean: modelLean,
      }});
    }});
    if (!legs.length) {{ errorEl.textContent = 'Add at least one leg (pick a player or team from the search results, choose a category and line).'; return; }}
    const stake = parseFloat(document.getElementById('bet-stake').value);
    const toWin = parseFloat(document.getElementById('bet-to-win').value);
    if (isNaN(stake) || isNaN(toWin)) {{ errorEl.textContent = 'Stake and to-win must be numbers.'; return; }}
    await addDoc(collection(db, 'bets'), {{
      userId: auth.currentUser.uid,
      placed_date: document.getElementById('bet-date').value,
      sportsbook: 'FanDuel',
      stake: stake,
      to_win: toWin,
      odds: document.getElementById('bet-odds').value || null,
      status: 'pending',
      created_at: new Date().toISOString(),
      graded_at: null,
      legs: legs,
    }});
    document.getElementById('bet-form').reset();
    document.getElementById('bet-date').value = easternToday();
    resetLegRows();
    ensureFormIndexForDate(document.getElementById('bet-date').value);
  }} catch (err) {{
    errorEl.textContent = 'Error: ' + err.message;
  }}
}});
</script>
"""


def render_html():
    app_script = APP_MODULE_SCRIPT.format(
        v=FIREBASE_SDK_VERSION,
        config=str(FIREBASE_CONFIG).replace("'", '"'),
        category_role_js=CATEGORY_ROLE_JS,
    )

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
{PAGE_STYLE}
</head>
<body>
  <div class="page">
    <div class="header-band">
      <div class="header-top">
        <div>
          <h1>My Bets</h1>
          <div class="meta">Personal wager tracker &middot; sign in to view</div>
        </div>
        <div class="header-actions">
          <a class="nav-link" href="index.html">&larr; Back to Dashboard</a>
          <button id="themeToggle" class="theme-toggle" type="button">Switch to dark</button>
        </div>
      </div>
    </div>

    <div id="signin-view">
      <h2>Sign in</h2>
      <input type="email" id="email" placeholder="Email" autocomplete="username">
      <input type="password" id="password" placeholder="Password" autocomplete="current-password">
      <button id="signin-btn" type="button">Sign In</button>
      <div id="signin-error" class="form-error"></div>
    </div>

    <div id="app-view" style="display:none">
      <div class="notes">
        <b>How to read this</b><br>
        Each leg is graded against real results using the exact same logic
        as every other pick on this site (an OVER that's already cleared
        shows HIT immediately, mid-game; a MISS only locks in once the game
        is Final). A bet only settles WON once every leg has hit, and
        settles LOST the moment any single leg misses -- same as a real
        parlay slip. This page polls MLB's live feed directly (same as the
        main dashboard) for every pending leg's game, so a bet can settle
        right here in real time -- the server-side grading (every ~15
        minutes) is what actually persists it. "model" shows this site's
        own current projection/line for that exact player+category, purely
        for comparison against what was actually bet. Start typing a
        player's name to search -- pick them from the list so the category
        options narrow to their actual position. Switch a leg to "Team" for
        a moneyline/run line/total game prop instead of a player prop --
        those never grade early even if a team is currently leading, since
        (unlike a player's own stat total) a lead can still get blown before
        the final out.
      </div>

      <div class="stat-row" id="summary-tiles"></div>

      <div class="picks-section" style="padding:16px">
        <div class="picks-subheading" style="margin-bottom:10px">Add a bet</div>
        <form id="bet-form">
          <div class="bet-form-row">
            <input type="date" id="bet-date" required>
            <input type="number" step="0.01" id="bet-stake" placeholder="Stake ($)" required style="width:110px">
            <input type="number" step="0.01" id="bet-to-win" placeholder="To win ($, profit only)" required style="width:170px">
            <input type="text" id="bet-odds" placeholder="Odds (e.g. +115, optional)" style="width:180px">
          </div>
          <div id="legs-container"></div>
          <button type="button" id="add-leg-btn">+ Add leg</button>
          <button type="submit">Log bet</button>
          <div id="bet-form-error" class="form-error"></div>
        </form>
      </div>

      <div class="date-heading">All bets</div>
      <div class="bets-filter-row">
        <select id="bet-filter-status">
          <option value="all">All bets</option>
          <option value="pending">Pending</option>
          <option value="won">Won</option>
          <option value="lost">Lost</option>
        </select>
        <input type="date" id="bet-filter-from" title="From date">
        <span class="sub">to</span>
        <input type="date" id="bet-filter-to" title="To date">
        <select id="bet-sort">
          <option value="date_desc">Date: newest first</option>
          <option value="date_asc">Date: oldest first</option>
          <option value="profit_desc">Profit: high to low</option>
          <option value="profit_asc">Profit: low to high</option>
          <option value="stake_desc">Stake: high to low</option>
          <option value="stake_asc">Stake: low to high</option>
        </select>
        <button type="button" id="bet-filter-clear">Clear filters</button>
      </div>
      <div id="bets-list"></div>
      <button id="signout-btn" type="button">Sign out</button>
    </div>
  </div>
{SCRIPT}
{app_script}
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
