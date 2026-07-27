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
  .leg-input-row .leg-player { flex: 1; min-width: 140px; }
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
  getFirestore, collection, addDoc, query, where, orderBy, onSnapshot
}} from "https://www.gstatic.com/firebasejs/{v}/firebase-firestore.js";

const firebaseConfig = {config};

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const db = getFirestore(app);

{category_role_js}

let reportCache = null;

function easternToday() {{
  return new Intl.DateTimeFormat('en-CA', {{ timeZone: 'America/New_York' }}).format(new Date());
}}

async function loadReport() {{
  if (reportCache) return reportCache;
  try {{
    const res = await fetch('latest.json', {{ cache: 'no-store' }});
    reportCache = await res.json();
  }} catch (e) {{
    reportCache = {{ games: [] }};
  }}
  return reportCache;
}}

function findPlayerAndProjection(report, name, category) {{
  const wanted = name.trim().toLowerCase();
  const tokens = wanted.split(/\\s+/);
  let best = null;
  for (const g of report.games || []) {{
    for (const sideKey of ['home', 'away']) {{
      const side = g[sideKey];
      if (!side) continue;
      const entities = [];
      (side.batters || []).forEach(function (b) {{ entities.push({{ entity: b, role: 'batter' }}); }});
      if (side.probable_pitcher) entities.push({{ entity: side.probable_pitcher, role: 'pitcher' }});
      for (const item of entities) {{
        if (CATEGORY_ROLE[category] !== item.role) continue;
        const entity = item.entity;
        const fullName = (entity.name || '').toLowerCase();
        const nameTokens = fullName.split(/\\s+/);
        let match = fullName === wanted || fullName.indexOf(wanted) !== -1;
        if (!match && tokens.length <= nameTokens.length) {{
          match = tokens.every(function (t, i) {{ return nameTokens[i] && nameTokens[i].indexOf(t) === 0; }});
        }}
        if (match) {{
          const cat = (entity.prop_categories || []).find(function (c) {{ return c.label === category; }});
          best = {{
            player_id: entity.player_id,
            player_name: entity.name,
            role: item.role,
            model_projection: cat ? cat.today_projection : null,
            model_line: cat ? cat.primary_line : null,
            model_lean: cat ? cat.lean : null,
          }};
          if (fullName === wanted) return best;
        }}
      }}
    }}
  }}
  return best;
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

function legRowHtml(idx) {{
  const options = CATEGORIES.map(function (c) {{ return '<option value="' + c + '">' + c + '</option>'; }}).join('');
  return (
    '<div class="leg-input-row" data-idx="' + idx + '">' +
    '<input type="text" class="leg-player" placeholder="Player name" required>' +
    '<select class="leg-category">' + options + '</select>' +
    '<input type="number" step="0.5" class="leg-line" placeholder="Line" required style="width:80px">' +
    '<select class="leg-direction"><option value="over">Over</option><option value="under">Under</option></select>' +
    '</div>'
  );
}}

let legIdx = 0;
function addLegRow() {{
  const container = document.getElementById('legs-container');
  const wrap = document.createElement('div');
  wrap.innerHTML = legRowHtml(legIdx++);
  container.appendChild(wrap.firstChild);
}}
function resetLegRows() {{
  document.getElementById('legs-container').innerHTML = '';
  legIdx = 0;
  addLegRow();
}}

let unsubscribeBets = null;

onAuthStateChanged(auth, function (user) {{
  const signinView = document.getElementById('signin-view');
  const appView = document.getElementById('app-view');
  if (user) {{
    signinView.style.display = 'none';
    appView.style.display = 'block';
    if (unsubscribeBets) unsubscribeBets();
    const q = query(collection(db, 'bets'), where('userId', '==', user.uid), orderBy('placed_date', 'desc'));
    unsubscribeBets = onSnapshot(q, function (snap) {{
      const bets = [];
      snap.forEach(function (doc) {{ bets.push(Object.assign({{ id: doc.id }}, doc.data())); }});
      renderBets(bets);
    }}, function (err) {{
      document.getElementById('bets-list').innerHTML = '<div class="empty">Error loading bets: ' + escapeHtml(err.message) + '</div>';
    }});
  }} else {{
    signinView.style.display = 'block';
    appView.style.display = 'none';
    if (unsubscribeBets) {{ unsubscribeBets(); unsubscribeBets = null; }}
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
    const report = await loadReport();
    const legRows = document.querySelectorAll('#legs-container .leg-input-row');
    const legs = [];
    legRows.forEach(function (row) {{
      const playerName = row.querySelector('.leg-player').value.trim();
      const category = row.querySelector('.leg-category').value;
      const line = parseFloat(row.querySelector('.leg-line').value);
      const direction = row.querySelector('.leg-direction').value;
      if (!playerName || isNaN(line)) return;
      const resolved = findPlayerAndProjection(report, playerName, category);
      legs.push({{
        player_name: resolved ? resolved.player_name : playerName,
        player_id: resolved ? resolved.player_id : null,
        role: resolved ? resolved.role : CATEGORY_ROLE[category],
        category: category,
        line: line,
        direction: direction,
        game_pk: null,
        status: 'pending',
        actual_value: null,
        dnp: false,
        model_projection: resolved ? resolved.model_projection : null,
        model_line: resolved ? resolved.model_line : null,
        model_lean: resolved ? resolved.model_lean : null,
      }});
    }});
    if (!legs.length) {{ errorEl.textContent = 'Add at least one leg.'; return; }}
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
        parlay slip. Grading runs automatically every ~15 minutes on the
        server; "model" shows this site's own current projection/line for
        that exact player+category, purely for comparison against what was
        actually bet.
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
