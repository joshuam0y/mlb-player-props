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
  .leg-input-row .leg-player { width: 100%; box-sizing: border-box; }
  .player-search-wrap { position: relative; flex: 1; min-width: 160px; }
  .player-suggestions { display: none; position: absolute; top: 100%; left: 0; right: 0; background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; z-index: 20; max-height: 220px; overflow-y: auto; margin-top: 2px; box-shadow: var(--shadow); }
  .player-suggestion { padding: 8px 10px; cursor: pointer; font-size: 13.5px; }
  .player-suggestion:hover, .player-suggestion.active { background: var(--surface-3); }
  .player-suggestion .sub { color: var(--text-secondary); font-size: 12px; }
  .leg-input-row.leg-resolved .player-search-wrap .leg-player { border-color: var(--status-good); }
  #bet-form button, #add-leg-btn { padding: 9px 16px; border-radius: 999px; border: none; background: var(--series-1); color: #fff; font-weight: 600; cursor: pointer; font-family: inherit; font-size: 13px; margin-right: 8px; margin-top: 4px; }
  #add-leg-btn { background: var(--surface-3); color: var(--text-primary); }
  #signout-btn { margin-top: 16px; padding: 8px 16px; border-radius: 999px; border: 1px solid var(--border); background: var(--surface-1); color: var(--text-primary); cursor: pointer; font-family: inherit; }
  .form-error { color: var(--status-critical); font-size: 13px; margin-top: 6px; }
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
  getFirestore, collection, addDoc, query, where, onSnapshot
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

let reportCache = null;
let playerIndex = []; // [{{player_id, name, role, team, game_pk, prop_categories}}]

function easternToday() {{
  return new Intl.DateTimeFormat('en-CA', {{ timeZone: 'America/New_York' }}).format(new Date());
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
          prop_categories: item.entity.prop_categories || [],
        }});
      }});
    }});
  }});
  playerIndex = index;
}}

async function loadReport() {{
  if (reportCache) return reportCache;
  try {{
    const res = await fetch('latest.json', {{ cache: 'no-store' }});
    reportCache = await res.json();
    buildPlayerIndex(reportCache);
  }} catch (e) {{
    reportCache = {{ games: [] }};
  }}
  return reportCache;
}}

function searchPlayers(text) {{
  const wanted = text.trim().toLowerCase();
  if (!wanted) return [];
  return playerIndex.filter(function (p) {{ return p.name.toLowerCase().indexOf(wanted) !== -1; }}).slice(0, 8);
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

function betCardHtml(bet) {{
  const profit = betProfit(bet);
  const profitTxt = profit != null ? ((profit >= 0 ? '+$' : '-$') + Math.abs(profit).toFixed(2)) : ('at stake: $' + Number(bet.stake).toFixed(2));
  const legsHtml = (bet.legs || []).map(legRowDisplayHtml).join('');
  const oddsTxt = bet.odds ? ' &middot; odds ' + escapeHtml(bet.odds) : '';
  const parlayTxt = (bet.legs || []).length > 1 ? ' (' + bet.legs.length + '-leg parlay)' : '';
  return (
    '<div class="bet-card"><div class="bet-card-head">' +
    '<span><b>' + escapeHtml(bet.placed_date) + '</b>' + parlayTxt + ' &middot; ' + escapeHtml(bet.sportsbook || 'FanDuel') + oddsTxt +
    ' &middot; staked $' + Number(bet.stake).toFixed(2) + ' to win $' + Number(bet.to_win).toFixed(2) + '</span>' +
    '<span>' + betStatusBadge(bet) + ' <b>' + profitTxt + '</b></span></div>' + legsHtml + '</div>'
  );
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

  document.getElementById('bets-list').innerHTML = bets.length
    ? bets.map(betCardHtml).join('')
    : '<div class="empty">No bets logged yet.</div>';
}}

function categoryOptionsHtml(role) {{
  const cats = role ? CATEGORIES.filter(function (c) {{ return CATEGORY_ROLE[c] === role; }}) : CATEGORIES;
  return '<option value="">Category</option>' + cats.map(function (c) {{ return '<option value="' + c + '">' + c + '</option>'; }}).join('');
}}

function legRowHtml(idx) {{
  return (
    '<div class="leg-input-row" data-idx="' + idx + '">' +
    '<div class="player-search-wrap">' +
    '<input type="text" class="leg-player" placeholder="Search player..." autocomplete="off" required>' +
    '<div class="player-suggestions"></div>' +
    '</div>' +
    '<select class="leg-category">' + categoryOptionsHtml(null) + '</select>' +
    '<input type="number" step="0.5" class="leg-line" placeholder="Line" required style="width:80px">' +
    '<select class="leg-direction"><option value="over">Over</option><option value="under">Under</option></select>' +
    '</div>'
  );
}}

// Selecting a suggestion stores the resolved player_id/role/game_pk as
// data-* on the row itself -- submission then trusts that resolution
// directly instead of re-guessing from typed text, and the category
// dropdown narrows to just that player's real categories (a batter can
// never accidentally get offered "Strikeouts").
function wireLegRow(rowEl) {{
  const input = rowEl.querySelector('.leg-player');
  const suggestionsEl = rowEl.querySelector('.player-suggestions');
  const categorySelect = rowEl.querySelector('.leg-category');

  input.addEventListener('input', function () {{
    rowEl.dataset.playerId = '';
    rowEl.dataset.role = '';
    rowEl.dataset.gamePk = '';
    rowEl.classList.remove('leg-resolved');
    const matches = searchPlayers(input.value);
    if (!matches.length) {{
      suggestionsEl.style.display = 'none';
      suggestionsEl.innerHTML = '';
      return;
    }}
    suggestionsEl.dataset.matches = JSON.stringify(matches);
    suggestionsEl.innerHTML = matches.map(function (p, i) {{
      return '<div class="player-suggestion" data-i="' + i + '">' + escapeHtml(p.name) +
        ' <span class="sub">' + escapeHtml(p.team || '') + '</span></div>';
    }}).join('');
    suggestionsEl.style.display = 'block';
  }});

  suggestionsEl.addEventListener('mousedown', function (e) {{
    const item = e.target.closest('.player-suggestion');
    if (!item) return;
    e.preventDefault();
    const matches = JSON.parse(suggestionsEl.dataset.matches || '[]');
    const picked = matches[parseInt(item.dataset.i, 10)];
    if (!picked) return;
    input.value = picked.name;
    rowEl.dataset.playerId = picked.player_id;
    rowEl.dataset.role = picked.role;
    rowEl.dataset.gamePk = picked.game_pk;
    rowEl.classList.add('leg-resolved');
    categorySelect.innerHTML = categoryOptionsHtml(picked.role);
    suggestionsEl.style.display = 'none';
  }});

  input.addEventListener('blur', function () {{
    setTimeout(function () {{ suggestionsEl.style.display = 'none'; }}, 150);
  }});
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
function pollAllBetGames() {{
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
  let anyChanged = false;
  currentBets.forEach(function (bet) {{
    if (bet.status !== 'pending') return;
    let betChanged = false;
    (bet.legs || []).forEach(function (leg) {{
      if (leg.status !== 'pending' || leg.game_pk !== gamePk || !leg.player_id) return;
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
    loadReport();
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

document.getElementById('add-leg-btn').addEventListener('click', addLegRow);

document.getElementById('bet-date').value = easternToday();
resetLegRows();

document.getElementById('bet-form').addEventListener('submit', async function (e) {{
  e.preventDefault();
  const errorEl = document.getElementById('bet-form-error');
  errorEl.textContent = '';
  try {{
    await loadReport();
    const legRows = document.querySelectorAll('#legs-container .leg-input-row');
    const legs = [];
    legRows.forEach(function (row) {{
      const playerName = row.querySelector('.leg-player').value.trim();
      const category = row.querySelector('.leg-category').value;
      const line = parseFloat(row.querySelector('.leg-line').value);
      const direction = row.querySelector('.leg-direction').value;
      if (!playerName || !category || isNaN(line)) return;
      // Trust the row's own resolved selection (from clicking a search
      // suggestion) over re-guessing from the typed text -- if the row
      // was never resolved (typed a name but never picked a suggestion),
      // fall back to an unresolved leg rather than blocking submission
      // entirely, same as the old text-only flow.
      const playerId = row.dataset.playerId ? parseInt(row.dataset.playerId, 10) : null;
      const role = row.dataset.role || CATEGORY_ROLE[category];
      const gamePk = row.dataset.gamePk ? parseInt(row.dataset.gamePk, 10) : null;
      let modelProjection = null, modelLine = null, modelLean = null;
      if (playerId) {{
        const entry = playerIndex.find(function (p) {{ return p.player_id === playerId; }});
        const cat = entry ? (entry.prop_categories || []).find(function (c) {{ return c.label === category; }}) : null;
        if (cat) {{ modelProjection = cat.today_projection; modelLine = cat.primary_line; modelLean = cat.lean; }}
      }}
      legs.push({{
        player_name: playerName,
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
    if (!legs.length) {{ errorEl.textContent = 'Add at least one leg (pick a player from the search results, choose a category and line).'; return; }}
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
        options narrow to their actual position.
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
