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
    """
    Standings (W/D/L/Pts/GF/GA), xG for/against, and touches-in-box are all
    computed live here rather than read from static columns - those columns
    on the teams table are never populated by any scraper, so reading them
    directly always returned zeros. This computes the real numbers from data
    that IS populated: finished fixtures (results), player_gameweeks (xG),
    and players (touches, from fbref_scraper.py).
    """
    query = """
        SELECT
            t.team_id, t.name, t.short_name, t.strength,
            COALESCE(res.played, 0) AS played,
            COALESCE(res.wins, 0) AS wins,
            COALESCE(res.draws, 0) AS draws,
            COALESCE(res.losses, 0) AS losses,
            COALESCE(res.points, 0) AS points,
            COALESCE(res.goals_for, 0) AS goals_for,
            COALESCE(res.goals_against, 0) AS goals_against,
            COALESCE(xg.xg_for, 0) AS xg_for,
            COALESCE(xg.xg_against, 0) AS xg_against,
            COALESCE(touch.touches_opp_box, 0) AS touches_opp_box,
            t.updated_at
        FROM teams t
        LEFT JOIN (
            -- one row per finished fixture per team (home leg + away leg unioned),
            -- then aggregated into standard standings math
            SELECT team_id,
                   COUNT(*) AS played,
                   SUM(CASE WHEN result = 'W' THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN result = 'D' THEN 1 ELSE 0 END) AS draws,
                   SUM(CASE WHEN result = 'L' THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN result = 'W' THEN 3 WHEN result = 'D' THEN 1 ELSE 0 END) AS points,
                   SUM(goals_for) AS goals_for,
                   SUM(goals_against) AS goals_against
            FROM (
                SELECT team_h AS team_id, team_h_score AS goals_for, team_a_score AS goals_against,
                       CASE WHEN team_h_score > team_a_score THEN 'W'
                            WHEN team_h_score = team_a_score THEN 'D' ELSE 'L' END AS result
                FROM fixtures WHERE finished = 1
                UNION ALL
                SELECT team_a AS team_id, team_a_score AS goals_for, team_h_score AS goals_against,
                       CASE WHEN team_a_score > team_h_score THEN 'W'
                            WHEN team_a_score = team_h_score THEN 'D' ELSE 'L' END AS result
                FROM fixtures WHERE finished = 1
            ) results
            GROUP BY team_id
        ) res ON res.team_id = t.team_id
        LEFT JOIN (
            SELECT team_id, AVG(team_xg) AS xg_for, AVG(opp_xg) AS xg_against
            FROM (
                SELECT tg.team_id, tg.gameweek, tg.team_xg,
                       opp.team_xg AS opp_xg
                FROM (
                    -- one row per (team, gameweek): that team's total xG that gameweek
                    SELECT p.team_id AS team_id, pg.gameweek AS gameweek,
                           MAX(pg.opponent_team_id) AS opponent_team_id,
                           SUM(pg.xg) AS team_xg
                    FROM player_gameweeks pg JOIN players p ON p.player_id = pg.player_id
                    WHERE pg.minutes > 0
                    GROUP BY p.team_id, pg.gameweek
                ) tg
                LEFT JOIN (
                    SELECT p.team_id AS team_id, pg.gameweek AS gameweek, SUM(pg.xg) AS team_xg
                    FROM player_gameweeks pg JOIN players p ON p.player_id = pg.player_id
                    WHERE pg.minutes > 0
                    GROUP BY p.team_id, pg.gameweek
                ) opp ON opp.team_id = tg.opponent_team_id AND opp.gameweek = tg.gameweek
            ) per_gw
            GROUP BY team_id
        ) xg ON xg.team_id = t.team_id
        LEFT JOIN (
            SELECT team_id, SUM(touches_opp_box) AS touches_opp_box
            FROM players GROUP BY team_id
        ) touch ON touch.team_id = t.team_id
        ORDER BY t.name
    """
    with get_conn() as conn:
        rows = conn.execute(query).fetchall()
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
