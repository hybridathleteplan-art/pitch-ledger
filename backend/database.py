"""
SQLite setup for the EPL stats app.
One file DB (epl.db) with three tables:
  teams              - one row per club
  players             - one row per player, current-season totals
  player_gameweeks    - one row per player per gameweek (history)
"""
import os
import sqlite3
from contextlib import contextmanager

# Resolve relative to this file, not the process's cwd - cwd varies between
# running locally, on Railway (root dir = backend/), and on Vercel (functions
# execute from wherever Vercel puts them). DB_PATH env var overrides this if
# you want the file somewhere else entirely.
DB_PATH = os.environ.get(
    "EPL_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "epl.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    short_name TEXT,
    strength INTEGER,
    played INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    draws INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    points INTEGER DEFAULT 0,
    goals_for INTEGER DEFAULT 0,
    goals_against INTEGER DEFAULT 0,
    xg_for REAL DEFAULT 0,
    xg_against REAL DEFAULT 0,
    touches_opp_box REAL DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY,
    fpl_id INTEGER UNIQUE,
    name TEXT NOT NULL,
    team_id INTEGER,
    position TEXT,
    minutes INTEGER DEFAULT 0,
    goals INTEGER DEFAULT 0,
    assists INTEGER DEFAULT 0,
    xg REAL DEFAULT 0,
    xa REAL DEFAULT 0,
    shots INTEGER DEFAULT 0,
    shots_on_target INTEGER DEFAULT 0,
    touches_opp_box REAL DEFAULT 0,
    progressive_carries REAL DEFAULT 0,
    progressive_passes REAL DEFAULT 0,
    ict_index REAL DEFAULT 0,
    price REAL,
    clean_sheets INTEGER DEFAULT 0,
    bonus INTEGER DEFAULT 0,
    defensive_contribution INTEGER DEFAULT 0,
    points_per_game REAL DEFAULT 0,
    total_points INTEGER DEFAULT 0,
    updated_at TEXT,
    FOREIGN KEY (team_id) REFERENCES teams(team_id)
);

CREATE TABLE IF NOT EXISTS player_gameweeks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER,
    gameweek INTEGER,
    was_home INTEGER,
    opponent_team_id INTEGER,
    minutes INTEGER DEFAULT 0,
    goals INTEGER DEFAULT 0,
    assists INTEGER DEFAULT 0,
    xg REAL DEFAULT 0,
    xa REAL DEFAULT 0,
    shots INTEGER DEFAULT 0,
    shots_on_target INTEGER DEFAULT 0,
    bonus INTEGER DEFAULT 0,
    defensive_contribution INTEGER DEFAULT 0,
    clean_sheet INTEGER DEFAULT 0,
    red_cards INTEGER DEFAULT 0,
    points INTEGER DEFAULT 0,
    UNIQUE(player_id, gameweek),
    FOREIGN KEY (player_id) REFERENCES players(player_id)
);

CREATE TABLE IF NOT EXISTS fixtures (
    fixture_id INTEGER PRIMARY KEY,
    gameweek INTEGER,
    team_h INTEGER,
    team_a INTEGER,
    kickoff_time TEXT,
    finished INTEGER DEFAULT 0,
    team_h_score INTEGER,
    team_a_score INTEGER,
    FOREIGN KEY (team_h) REFERENCES teams(team_id),
    FOREIGN KEY (team_a) REFERENCES teams(team_id)
);

CREATE TABLE IF NOT EXISTS other_fixtures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER,
    competition TEXT,          -- 'UCL', 'UEL', 'UECL', 'FA Cup', 'Carabao Cup'
    opponent_name TEXT,        -- free text - opponent may not be in our PL teams table (e.g. a Championship or European club)
    is_home INTEGER,
    match_date TEXT,           -- ISO date, used to slot it into the ticker near the right PL gameweek
    near_gameweek INTEGER,     -- which PL gameweek this midweek fixture falls closest to/before
    source TEXT DEFAULT 'manual', -- 'manual' (cup_fixtures.py) or 'football-data.org' (cup_fixtures_scraper.py) - each loader only touches its own rows
    FOREIGN KEY (team_id) REFERENCES teams(team_id)
);

CREATE TABLE IF NOT EXISTS match_odds (
    fixture_id INTEGER PRIMARY KEY,
    home_win_pct REAL,
    draw_pct REAL,
    away_win_pct REAL,
    btts_yes_pct REAL,         -- "both teams to score" - used as a goals-scored signal for both sides
    source TEXT,
    updated_at TEXT,
    FOREIGN KEY (fixture_id) REFERENCES fixtures(fixture_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added to the schema after a table already existed in the wild need
# an explicit migration entry here, or older database files will crash with
# "no such column" the first time new code reads/writes it - SQLite (unlike
# some other databases) never grows a table's columns on its own just
# because CREATE TABLE IF NOT EXISTS ran again with a longer definition.
#
# IMPORTANT: whenever a new column is added to a table in SCHEMA above that
# already ships in the wild, add a matching line here too (table, column
# name, full "ADD COLUMN" type/default clause). New databases get the
# column for free from CREATE TABLE; this list is what makes EXISTING
# database files catch up automatically the next time the app starts -
# nobody should ever need to run a manual ALTER TABLE again.
MIGRATIONS = [
    ("player_gameweeks", "red_cards", "INTEGER DEFAULT 0"),
    ("other_fixtures", "source", "TEXT DEFAULT 'manual'"),
]


def run_migrations(conn):
    """
    Adds any missing columns. Wrapped in try/except per-column on purpose:
    on a read-only filesystem (Vercel's Python functions at request time),
    ALTER TABLE fails with "attempt to write a readonly database" - unlike
    CREATE TABLE IF NOT EXISTS above, which is a safe no-op when the table
    already exists and never attempts a real write. If that happens, we log
    it and move on rather than crash the entire app: the fix at that point
    is to run the migration somewhere with real write access (locally, or
    via GitHub Actions) and commit the updated epl.db, not to keep retrying
    on every request.
    """
    for table, column, add_clause in MIGRATIONS:
        try:
            existing_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in existing_cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {add_clause}")
                print(f"[migration] added {table}.{column}")
        except sqlite3.OperationalError as e:
            print(f"[migration] skipped {table}.{column} (likely a read-only filesystem): {e}")


def init_db():
    try:
        with get_conn() as conn:
            conn.executescript(SCHEMA)
            run_migrations(conn)
    except sqlite3.OperationalError as e:
        # Same read-only-filesystem concern as above, belt-and-braces: if
        # even CREATE TABLE IF NOT EXISTS somehow fails (e.g. a brand new
        # empty file on a read-only mount, which genuinely can't be
        # created), don't take the whole app down over it.
        print(f"[init_db] schema setup skipped (likely a read-only filesystem): {e}")


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
