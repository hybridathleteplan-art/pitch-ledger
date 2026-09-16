"""
Thin read API over epl.db, served to the frontend.

Run:  uvicorn app:app --reload --port 8000
Then open frontend/index.html (it calls http://localhost:8000 by default).
"""
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from database import get_conn, init_db
from ratings import compute_team_xg_ratings

app = FastAPI(title="EPL Stats API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this to your actual frontend domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


@app.get("/api/health")
def health():
    with get_conn() as conn:
        gw = conn.execute("SELECT value FROM meta WHERE key='current_gameweek'").fetchone()
    return {"status": "ok", "current_gameweek": gw["value"] if gw else None}


@app.get("/api/teams")
def list_teams():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM teams ORDER BY name").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/fixtures")
def list_fixtures(team_id: int | None = None, upcoming_only: bool = True, limit: int = 200):
    query = """
        SELECT f.*, o.home_win_pct, o.draw_pct, o.away_win_pct, o.btts_yes_pct
        FROM fixtures f
        LEFT JOIN match_odds o ON o.fixture_id = f.fixture_id
        WHERE 1=1
    """
    params: list = []
    if upcoming_only:
        query += " AND f.finished = 0"
    if team_id is not None:
        query += " AND (f.team_h = ? OR f.team_a = ?)"
        params.extend([team_id, team_id])
    query += " ORDER BY f.gameweek ASC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/fixtures/cup")
def list_cup_fixtures(team_id: int | None = None):
    """Champions League / Europa / Conference / FA Cup / Carabao Cup fixtures.
    Maintained manually in cup_fixtures.py (see load_cup_fixtures.py) since
    there's no reliable free automated source for these tied to our team IDs."""
    query = "SELECT * FROM other_fixtures WHERE 1=1"
    params: list = []
    if team_id is not None:
        query += " AND team_id = ?"
        params.append(team_id)
    query += " ORDER BY near_gameweek ASC"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/teams/xg-ratings")
def teams_xg_ratings():
    """
    Home/away attack & defense ratings (1-5) computed from xG and xG-conceded
    instead of actual goals - see ratings.py for the methodology. Works for
    every team automatically once fpl_scraper.py has pulled gameweek history.
    """
    return compute_team_xg_ratings()


@app.get("/api/players")
def list_players(
    team_id: int | None = None,
    position: str | None = None,
    sort: str = Query("goals", description="column to sort by, e.g. xg, xa, touches_opp_box, pts_per_game_home"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = 100,
):
    allowed_sort = {
        "goals", "assists", "xg", "xa", "minutes", "shots_on_target",
        "touches_opp_box", "progressive_carries", "progressive_passes",
        "ict_index", "price", "name", "points_per_game", "clean_sheets",
        "bonus", "defensive_contribution", "games_played",
        "min_per_game", "defcon_per_game", "bonus_per_game", "cs_per_game",
        "pts_per_game_home", "pts_per_game_away",
    }
    if sort not in allowed_sort:
        sort = "goals"

    # Per-game and home/away split figures are computed on the fly from
    # player_gameweeks, so every player gets them automatically the moment
    # fpl_scraper.py has pulled their gameweek history - no per-player
    # special-casing needed.
    query = """
        SELECT p.*, t.name AS team_name, t.short_name AS team_short,
            COALESCE(gw.games_played, 0) AS games_played,
            COALESCE(gw.min_per_game, 0) AS min_per_game,
            COALESCE(gw.defcon_per_game, 0) AS defcon_per_game,
            COALESCE(gw.bonus_per_game, 0) AS bonus_per_game,
            COALESCE(gw.cs_per_game, 0) AS cs_per_game,
            gwh.pts_per_game_home,
            gwa.pts_per_game_away
        FROM players p
        LEFT JOIN teams t ON p.team_id = t.team_id
        LEFT JOIN (
            SELECT player_id,
                   COUNT(*) AS games_played,
                   AVG(minutes) AS min_per_game,
                   AVG(defensive_contribution) AS defcon_per_game,
                   AVG(bonus) AS bonus_per_game,
                   AVG(clean_sheet) AS cs_per_game
            FROM player_gameweeks WHERE minutes > 0 GROUP BY player_id
        ) gw ON gw.player_id = p.player_id
        LEFT JOIN (
            SELECT player_id, AVG(points) AS pts_per_game_home
            FROM player_gameweeks WHERE was_home = 1 AND minutes > 0 GROUP BY player_id
        ) gwh ON gwh.player_id = p.player_id
        LEFT JOIN (
            SELECT player_id, AVG(points) AS pts_per_game_away
            FROM player_gameweeks WHERE was_home = 0 AND minutes > 0 GROUP BY player_id
        ) gwa ON gwa.player_id = p.player_id
        WHERE 1=1
    """
    params: list = []
    if team_id is not None:
        query += " AND p.team_id = ?"
        params.append(team_id)
    if position is not None:
        query += " AND p.position = ?"
        params.append(position.upper())
    query += f" ORDER BY {sort} {order.upper()} LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/players/{player_id}/gameweeks")
def player_gameweeks(player_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM player_gameweeks WHERE player_id = ? ORDER BY gameweek",
            (player_id,),
        ).fetchall()
    return [dict(r) for r in rows]
