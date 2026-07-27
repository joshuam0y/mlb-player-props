"""
db.py

SQLite schema and connection helper for the MLB player props database.
Run directly (`python db.py`) to create/upgrade the schema in place --
every statement is idempotent (CREATE TABLE IF NOT EXISTS), so it's safe
to call at the top of every sync script.

Note on "career" rows in the splits tables: SQLite treats NULL as distinct
from every other NULL in a UNIQUE/PRIMARY KEY, so a NULL `season` column
would let duplicate career rows slip in on re-sync. Season 0 is used as the
sentinel for "career-to-date" instead, so the primary key actually enforces
uniqueness.
"""

import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mlb_props.db")
CAREER_SEASON = 0  # sentinel used in *_splits.season to mean "career-to-date"

MLB_TZ = ZoneInfo("America/New_York")


def mlb_today():
    """
    "Today" the way MLB's own schedule means it: the current date in US
    Eastern time, not raw UTC. UTC crosses midnight 4-5 hours before the
    Eastern calendar day is actually over (8-9pm ET) -- using UTC's date
    directly made every "today"-scoped window (the schedule/report date
    range, Top Overs/Unders, the daily archive freeze, lineup/results
    syncing) silently flip over to "tomorrow" while a real chunk of
    tonight's games -- sometimes even East Coast ones -- were still being
    played, hours before the day was actually over for anyone watching.
    Not a perfect fix for the latest West Coast games (which can run past
    Eastern midnight too), but a real improvement over UTC, which was
    wrong by design for the entire US audience this runs for.
    """
    return datetime.now(MLB_TZ).strftime("%Y-%m-%d")

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    abbrev TEXT,
    league TEXT,
    division TEXT
);

CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    bat_side TEXT,
    pitch_hand TEXT,
    primary_position TEXT,
    is_pitcher INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    mlb_debut_season INTEGER,
    current_team_id INTEGER,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS games (
    game_pk INTEGER PRIMARY KEY,
    official_date TEXT NOT NULL,
    game_date_utc TEXT NOT NULL,
    status TEXT,
    home_team_id INTEGER,
    away_team_id INTEGER,
    home_probable_pitcher_id INTEGER,
    away_probable_pitcher_id INTEGER,
    venue_name TEXT,
    home_score INTEGER,
    away_score INTEGER
);

CREATE TABLE IF NOT EXISTS batting_game_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL,
    game_pk INTEGER NOT NULL,
    season INTEGER NOT NULL,
    date TEXT NOT NULL,
    team_id INTEGER,
    opponent_id INTEGER,
    is_home INTEGER,
    at_bats INTEGER, hits INTEGER, doubles INTEGER, triples INTEGER,
    home_runs INTEGER, rbi INTEGER, runs INTEGER, base_on_balls INTEGER,
    strike_outs INTEGER, total_bases INTEGER, hit_by_pitch INTEGER,
    stolen_bases INTEGER,
    UNIQUE(player_id, game_pk)
);
CREATE INDEX IF NOT EXISTS idx_batting_player_season ON batting_game_logs(player_id, season);
CREATE INDEX IF NOT EXISTS idx_batting_date ON batting_game_logs(date);

CREATE TABLE IF NOT EXISTS pitching_game_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL,
    game_pk INTEGER NOT NULL,
    season INTEGER NOT NULL,
    date TEXT NOT NULL,
    team_id INTEGER,
    opponent_id INTEGER,
    is_home INTEGER,
    innings_pitched TEXT, outs INTEGER, hits INTEGER, earned_runs INTEGER,
    runs INTEGER, base_on_balls INTEGER, strike_outs INTEGER,
    home_runs INTEGER, batters_faced INTEGER, wins INTEGER, losses INTEGER,
    games_started INTEGER,
    UNIQUE(player_id, game_pk)
);
CREATE INDEX IF NOT EXISTS idx_pitching_player_season ON pitching_game_logs(player_id, season);
CREATE INDEX IF NOT EXISTS idx_pitching_date ON pitching_game_logs(date);

CREATE TABLE IF NOT EXISTS batting_splits (
    player_id INTEGER NOT NULL,
    split_code TEXT NOT NULL,   -- 'vl' or 'vr'
    season INTEGER NOT NULL,    -- 0 = career-to-date
    games INTEGER, plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
    doubles INTEGER, triples INTEGER, home_runs INTEGER, rbi INTEGER,
    runs INTEGER, base_on_balls INTEGER, strike_outs INTEGER,
    total_bases INTEGER, avg REAL, obp REAL, slg REAL, ops REAL,
    PRIMARY KEY (player_id, split_code, season)
);

CREATE TABLE IF NOT EXISTS pitching_splits (
    player_id INTEGER NOT NULL,
    split_code TEXT NOT NULL,   -- 'vl' or 'vr' (opposing batter hand)
    season INTEGER NOT NULL,    -- 0 = career-to-date
    games INTEGER, innings_pitched TEXT, outs INTEGER, hits INTEGER,
    earned_runs INTEGER, runs INTEGER, base_on_balls INTEGER,
    strike_outs INTEGER, home_runs INTEGER, batters_faced INTEGER,
    era REAL, whip REAL, avg_against REAL,
    PRIMARY KEY (player_id, split_code, season)
);

CREATE TABLE IF NOT EXISTS injuries (
    transaction_id INTEGER PRIMARY KEY,
    player_id INTEGER,
    player_name TEXT,
    team_id INTEGER,
    date TEXT,
    status TEXT,        -- 'IL' (placed on injured list) or 'activated'
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_injuries_player ON injuries(player_id);

CREATE TABLE IF NOT EXISTS headlines (
    link TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    pub_date TEXT,
    creator TEXT,
    matched_player_ids TEXT   -- comma-separated player_ids mentioned in the title
);

CREATE TABLE IF NOT EXISTS lineups (
    game_pk INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    batting_order INTEGER,   -- 1-9, batting order slot
    position TEXT,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (game_pk, team_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_lineups_game ON lineups(game_pk);

CREATE TABLE IF NOT EXISTS game_projections (
    game_pk INTEGER NOT NULL,
    generated_at TEXT NOT NULL,   -- ISO timestamp this projection was made, so a later backtest
                                  -- can tell what was knowable *before* the game, not after
    model_version TEXT NOT NULL,
    home_exp_runs REAL,
    away_exp_runs REAL,
    home_win_prob REAL,
    spread_line REAL,            -- home perspective, e.g. -1.5 means home favored by 1.5
    spread_cover_prob REAL,       -- P(home covers spread_line)
    total_line REAL,
    over_prob REAL,               -- P(home_runs + away_runs > total_line)
    home_score_line REAL,          -- projected score, rounded to nearest .5 (sportsbook-style)
    away_score_line REAL,
    moneyline_pick TEXT,           -- 'home' or 'away'
    spread_favorite TEXT,          -- 'home' or 'away' -- whichever side is actually assigned -1.5
    spread_pick TEXT,              -- 'home' or 'away'
    spread_pick_prob REAL,
    total_pick TEXT,               -- 'over' or 'under'
    total_pick_prob REAL,
    PRIMARY KEY (game_pk, generated_at)
);
CREATE INDEX IF NOT EXISTS idx_projections_game ON game_projections(game_pk);

CREATE TABLE IF NOT EXISTS sync_state (
    player_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    stat_group TEXT NOT NULL,  -- 'hitting' or 'pitching'
    complete INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (player_id, season, stat_group)
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# CREATE TABLE IF NOT EXISTS only helps for genuinely new tables -- it's a
# no-op against a table that already exists with an older column set, so
# columns added after a table's first release need an explicit migration.
MIGRATIONS = {
    "games": [
        ("home_score", "INTEGER"),
        ("away_score", "INTEGER"),
    ],
    "pitching_game_logs": [
        ("games_started", "INTEGER"),
    ],
    "game_projections": [
        ("home_score_line", "REAL"),
        ("away_score_line", "REAL"),
        ("moneyline_pick", "TEXT"),
        ("spread_favorite", "TEXT"),
        ("spread_pick", "TEXT"),
        ("spread_pick_prob", "REAL"),
        ("total_pick", "TEXT"),
        ("total_pick_prob", "REAL"),
    ],
}


def _migrate(conn):
    for table, columns in MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, col_type in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Schema ready at {os.path.abspath(DB_PATH)}")
