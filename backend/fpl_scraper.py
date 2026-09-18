"""
Pulls current-season Premier League data from the official, free,
no-auth-required Fantasy Premier League API.

Gives us: goals, assists, xG, xA, minutes, shots, ICT index, price,
per-gameweek history - for every player and team, auto-updated by
FPL as soon as each gameweek's stats are confirmed.

Run manually:  python fpl_scraper.py
Run on a schedule (recommended): once per day during the season, or
right after each gameweek's matches finish (Mon/Tue).
"""
import datetime
import requests

from database import get_conn, init_db

BASE = "https://fantasy.premierleague.com/api"


def fetch_bootstrap():
    r = requests.get(f"{BASE}/bootstrap-static/", timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_player_history(fpl_id: int):
    r = requests.get(f"{BASE}/element-summary/{fpl_id}/", timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_fixtures():
    r = requests.get(f"{BASE}/fixtures/", timeout=30)
    r.raise_for_status()
    return r.json()


def upsert_fixtures(conn, fixtures):
    for f in fixtures:
        conn.execute(
            """INSERT INTO fixtures
                 (fixture_id, gameweek, team_h, team_a, kickoff_time, finished,
                  team_h_score, team_a_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(fixture_id) DO UPDATE SET
                 gameweek=excluded.gameweek, team_h=excluded.team_h, team_a=excluded.team_a,
                 kickoff_time=excluded.kickoff_time, finished=excluded.finished,
                 team_h_score=excluded.team_h_score, team_a_score=excluded.team_a_score""",
            (
                f["id"],
                f.get("event"),
                f["team_h"],
                f["team_a"],
                f.get("kickoff_time"),
                1 if f.get("finished") else 0,
                f.get("team_h_score"),
                f.get("team_a_score"),
            ),
        )


def current_gameweek(events):
    for e in events:
        if e.get("is_current"):
            return e["id"]
    # season not started / between gameweeks - use most recently finished
    finished = [e["id"] for e in events if e.get("finished")]
    return max(finished) if finished else 1


def upsert_teams(conn, teams):
    now = datetime.datetime.utcnow().isoformat()
    for t in teams:
        conn.execute(
            """INSERT INTO teams (team_id, name, short_name, strength, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(team_id) DO UPDATE SET
                 name=excluded.name, short_name=excluded.short_name,
                 strength=excluded.strength, updated_at=excluded.updated_at""",
            (t["id"], t["name"], t["short_name"], t.get("strength", 0), now),
        )


POSITION_MAP = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def upsert_players(conn, elements):
    now = datetime.datetime.utcnow().isoformat()
    for p in elements:
        conn.execute(
            """INSERT INTO players
                 (fpl_id, name, team_id, position, minutes, goals, assists,
                  xg, xa, ict_index, price, clean_sheets, bonus,
                  defensive_contribution, points_per_game, total_points, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(fpl_id) DO UPDATE SET
                 name=excluded.name, team_id=excluded.team_id,
                 position=excluded.position, minutes=excluded.minutes,
                 goals=excluded.goals, assists=excluded.assists,
                 xg=excluded.xg, xa=excluded.xa,
                 ict_index=excluded.ict_index, price=excluded.price,
                 clean_sheets=excluded.clean_sheets, bonus=excluded.bonus,
                 defensive_contribution=excluded.defensive_contribution,
                 points_per_game=excluded.points_per_game,
                 total_points=excluded.total_points,
                 updated_at=excluded.updated_at""",
            (
                p["id"],
                f'{p["first_name"]} {p["second_name"]}'.strip(),
                p["team"],
                POSITION_MAP.get(p["element_type"], "?"),
                p.get("minutes", 0),
                p.get("goals_scored", 0),
                p.get("assists", 0),
                float(p.get("expected_goals", 0) or 0),
                float(p.get("expected_assists", 0) or 0),
                float(p.get("ict_index", 0) or 0),
                (p.get("now_cost", 0) or 0) / 10,
                p.get("clean_sheets", 0) or 0,
                p.get("bonus", 0) or 0,
                p.get("defensive_contribution", 0) or 0,
                float(p.get("points_per_game", 0) or 0),
                p.get("total_points", 0) or 0,
                now,
            ),
        )
    # Note: FPL's bootstrap-static no longer exposes a per-player "shots_on_target"
    # field directly (it was dropped from the API). shots / shots_on_target get
    # filled in by fbref_scraper.py instead, which pulls them from FBref.


def upsert_gameweek_history(conn, player_id, fpl_id, history):
    for gw in history:
        conn.execute(
            """INSERT INTO player_gameweeks
                 (player_id, gameweek, was_home, opponent_team_id, minutes, goals, assists, xg, xa,
                  shots, shots_on_target, bonus, defensive_contribution, clean_sheet, red_cards, points)
               VALUES ((SELECT player_id FROM players WHERE fpl_id=?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(player_id, gameweek) DO UPDATE SET
                 was_home=excluded.was_home, opponent_team_id=excluded.opponent_team_id,
                 minutes=excluded.minutes, goals=excluded.goals,
                 assists=excluded.assists, xg=excluded.xg, xa=excluded.xa,
                 shots=excluded.shots, shots_on_target=excluded.shots_on_target,
                 bonus=excluded.bonus, defensive_contribution=excluded.defensive_contribution,
                 clean_sheet=excluded.clean_sheet, red_cards=excluded.red_cards, points=excluded.points""",
            (
                fpl_id,
                gw["round"],
                1 if gw.get("was_home") else 0,
                gw.get("opponent_team"),
                gw.get("minutes", 0),
                gw.get("goals_scored", 0),
                gw.get("assists", 0),
                float(gw.get("expected_goals", 0) or 0),
                float(gw.get("expected_assists", 0) or 0),
                gw.get("shots", 0) or 0,
                gw.get("shots_on_target", 0) or 0,
                gw.get("bonus", 0) or 0,
                gw.get("defensive_contribution", 0) or 0,
                1 if gw.get("clean_sheets") else 0,
                gw.get("red_cards", 0) or 0,
                gw.get("total_points", 0),
            ),
        )


def run(with_history: bool = True, history_limit: int | None = None):
    init_db()
    data = fetch_bootstrap()
    gw = current_gameweek(data["events"])
    print(f"Current gameweek: {gw}")

    with get_conn() as conn:
        upsert_teams(conn, data["teams"])
        upsert_players(conn, data["elements"])
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('current_gameweek', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(gw),),
        )
    print(f"Upserted {len(data['teams'])} teams, {len(data['elements'])} players")

    print("Fetching fixtures...")
    fixtures = fetch_fixtures()
    with get_conn() as conn:
        upsert_fixtures(conn, fixtures)
    print(f"Upserted {len(fixtures)} fixtures")

    if with_history:
        elements = data["elements"]
        if history_limit:
            elements = elements[:history_limit]
        with get_conn() as conn:
            for i, p in enumerate(elements):
                try:
                    hist = fetch_player_history(p["id"])["history"]
                    upsert_gameweek_history(conn, p["id"], p["id"], hist)
                except requests.RequestException as e:
                    print(f"  skip player {p['id']}: {e}")
                if i % 100 == 0:
                    print(f"  history {i}/{len(elements)}")
        print("Gameweek history updated")


if __name__ == "__main__":
    import sys

    # pass a number as argv[1] to limit history pulls while testing, e.g.
    # `python fpl_scraper.py 20`
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(with_history=True, history_limit=limit)
