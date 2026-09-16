"""
Loads a handful of realistic sample rows so you can run the app and see
it working immediately, before wiring up the live scrapers. Not needed
once fpl_scraper.py / fbref_scraper.py have populated real data.

Run: python seed_sample.py
"""
import datetime
from database import get_conn, init_db

now = datetime.datetime.utcnow().isoformat()

TEAMS = [
    (1, "Arsenal", "ARS", 4),
    (2, "Manchester City", "MCI", 5),
    (3, "Liverpool", "LIV", 5),
    (4, "Chelsea", "CHE", 4),
]

PLAYERS = [
    # fpl_id, name, team_id, position, minutes, goals, assists, xg, xa, sot, ict, price
    (1, "Erling Haaland", 2, "FWD", 720, 12, 2, 10.8, 1.9, 28, 145.2, 15.1),
    (2, "Mohamed Salah", 3, "MID", 705, 9, 8, 7.4, 6.1, 22, 168.9, 13.4),
    (3, "Bukayo Saka", 1, "MID", 690, 6, 7, 5.2, 5.8, 18, 132.5, 10.2),
    (4, "Cole Palmer", 4, "MID", 675, 8, 5, 6.9, 4.3, 24, 140.0, 11.0),
]

# gameweek, was_home, opponent_team_id, minutes, goals, assists, xg, bonus, defensive_contribution, clean_sheet, points
SAMPLE_GAMEWEEKS = {
    1: [
        (1, 1, 1, 90, 2, 0, 1.8, 3, 2, 1, 15),  # Haaland (MCI) home vs team1 (ARS)
        (2, 0, 4, 90, 1, 1, 1.1, 1, 3, 0, 9),   # Salah (LIV) away at team4 (CHE)
        (3, 0, 2, 90, 0, 0, 0.6, 0, 1, 0, 4),   # Saka (ARS) away at team2 (MCI)
    ],
    2: [
        (1, 0, 3, 90, 1, 0, 1.3, 1, 1, 0, 8),   # MCI away at team3 (LIV)
        (2, 1, 1, 90, 0, 1, 0.9, 2, 4, 1, 10),  # LIV home vs team1 (ARS)
        (3, 1, 4, 85, 1, 0, 1.4, 3, 0, 0, 11),  # ARS home vs team4 (CHE)
    ],
    3: [
        (1, 1, 4, 90, 1, 0, 1.6, 2, 2, 1, 12),  # MCI home vs team4 (CHE)
        (2, 1, 2, 90, 1, 1, 1.2, 0, 2, 1, 13),  # LIV home vs team2 (MCI) - synthetic test data, not a real double-home fixture
        (3, 1, 3, 90, 0, 1, 0.8, 1, 2, 1, 8),   # ARS home vs team3 (LIV)
    ],
}

# fixture_id, gameweek, team_h, team_a - a few upcoming fixtures for testing the ticker
UPCOMING_FIXTURES = [
    (101, 4, 1, 2), (102, 4, 3, 4),
    (103, 5, 2, 3), (104, 5, 4, 1),
    (105, 6, 1, 3), (106, 6, 2, 4),
]

# fixture_id, home_win_pct, draw_pct, away_win_pct, btts_yes_pct (0-1 range)
SAMPLE_ODDS = [
    (101, 0.28, 0.24, 0.48, 0.62),  # ARS vs MCI
    (102, 0.44, 0.27, 0.29, 0.58),  # LIV vs CHE
    (103, 0.61, 0.21, 0.18, 0.55),  # MCI vs LIV
]

# team_id, competition, opponent_name, is_home, near_gameweek
SAMPLE_CUP_FIXTURES = [
    (1, "UCL", "Bayern Munich", 1, 4),
    (2, "Carabao Cup", "Newcastle United", 0, 5),
    (3, "UEL", "Villarreal", 1, 6),
]


def run():
    init_db()
    with get_conn() as conn:
        for t in TEAMS:
            conn.execute(
                "INSERT OR REPLACE INTO teams (team_id, name, short_name, strength, updated_at) VALUES (?,?,?,?,?)",
                (*t, now),
            )
        for p in PLAYERS:
            conn.execute(
                """INSERT OR REPLACE INTO players
                   (fpl_id, name, team_id, position, minutes, goals, assists, xg, xa,
                    shots_on_target, ict_index, price, touches_opp_box,
                    progressive_carries, progressive_passes, shots, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*p, 45.0, 12.0, 20.0, 40, now),
            )
        for gw_num, rows in SAMPLE_GAMEWEEKS.items():
            for fpl_id, was_home, opponent_team_id, minutes, goals, assists, xg, bonus, defcon, cs, points in rows:
                conn.execute(
                    """INSERT OR REPLACE INTO player_gameweeks
                       (player_id, gameweek, was_home, opponent_team_id, minutes, goals, assists, xg, xa,
                        shots, shots_on_target, bonus, defensive_contribution, clean_sheet, points)
                       VALUES ((SELECT player_id FROM players WHERE fpl_id=?), ?,?,?,?,?,?,?,0,0,0,?,?,?,?)""",
                    (fpl_id, gw_num, was_home, opponent_team_id, minutes, goals, assists, xg, bonus, defcon, cs, points),
                )
        for fixture_id, gw_num, team_h, team_a in UPCOMING_FIXTURES:
            conn.execute(
                """INSERT OR REPLACE INTO fixtures
                   (fixture_id, gameweek, team_h, team_a, finished)
                   VALUES (?,?,?,?,0)""",
                (fixture_id, gw_num, team_h, team_a),
            )
        for fixture_id, home_pct, draw_pct, away_pct, btts_pct in SAMPLE_ODDS:
            conn.execute(
                """INSERT OR REPLACE INTO match_odds
                   (fixture_id, home_win_pct, draw_pct, away_win_pct, btts_yes_pct, source, updated_at)
                   VALUES (?,?,?,?,?,'sample',?)""",
                (fixture_id, home_pct, draw_pct, away_pct, btts_pct, now),
            )
        for team_id, competition, opponent, is_home, near_gw in SAMPLE_CUP_FIXTURES:
            conn.execute(
                """INSERT INTO other_fixtures
                   (team_id, competition, opponent_name, is_home, near_gameweek)
                   VALUES (?,?,?,?,?)""",
                (team_id, competition, opponent, is_home, near_gw),
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('current_gameweek', '7')"
        )
    print("Seeded sample data.")


if __name__ == "__main__":
    run()
