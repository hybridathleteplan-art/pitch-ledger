"""
Loads backend/cup_fixtures.py into the other_fixtures table.

Run this after editing cup_fixtures.py (new round drawn, etc.):
    python load_cup_fixtures.py

Safe to re-run - it clears and rewrites the whole table each time, so
cup_fixtures.py is always the single source of truth (no drift between
the file and the database).
"""
from database import get_conn, init_db
from cup_fixtures import CUP_FIXTURES


def run():
    init_db()
    with get_conn() as conn:
        teams_by_short = {
            row["short_name"]: row["team_id"]
            for row in conn.execute("SELECT team_id, short_name FROM teams").fetchall()
        }
        conn.execute("DELETE FROM other_fixtures")
        inserted, skipped = 0, 0
        for f in CUP_FIXTURES:
            team_id = teams_by_short.get(f["team_short"])
            if team_id is None:
                print(f"  skip: unknown team_short '{f['team_short']}' - run fpl_scraper.py first so teams exist")
                skipped += 1
                continue
            conn.execute(
                """INSERT INTO other_fixtures
                     (team_id, competition, opponent_name, is_home, match_date, near_gameweek)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    team_id,
                    f["competition"],
                    f["opponent_name"],
                    1 if f["is_home"] else 0,
                    f.get("match_date"),
                    f["near_gameweek"],
                ),
            )
            inserted += 1
    print(f"Loaded {inserted} cup/European fixtures ({skipped} skipped)")


if __name__ == "__main__":
    run()
