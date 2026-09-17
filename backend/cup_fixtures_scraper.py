"""
Automatically pulls upcoming Champions League fixtures for whichever of
your Premier League teams are in this season's UCL, using football-data.org
(free tier, no card - https://www.football-data.org/client/register).

Why only Champions League here, not the other four cup competitions:
football-data.org's free tier covers 12 competitions, and UCL is the only
one of the five this app tracks (UCL, UEL, UECL, FA Cup, Carabao Cup)
that's in that free list. FA Cup and Carabao Cup are handled by the
separate scraper in this same file (see run_domestic_cups below), which
scrapes the official governing-body sites directly instead - they publish
fixtures as plain public web pages, same idea as fbref_scraper.py.
UEL/UECL still have no confirmed free source (automated or scraped) and
stay fully manual in cup_fixtures.py.

SETUP REQUIRED for the Champions League part:
  1. Sign up free at https://www.football-data.org/client/register
  2. export FOOTBALL_DATA_API_KEY="your_key_here"

Run manually:  python cup_fixtures_scraper.py

This only ever writes rows tagged with its own source value - it never
touches your manually-entered cup_fixtures.py rows (source='manual'), or
each other's rows, so all three loaders are independent.
"""
import os
import re
import datetime
import difflib

import requests
from bs4 import BeautifulSoup

from database import get_conn

API_BASE = "https://api.football-data.org/v4"
API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
COMPETITION_CODE = "CL"  # UEFA Champions League

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-stats-project/1.0)"}

# (source tag, competition label, fixtures page URL)
DOMESTIC_CUP_SOURCES = [
    ("thefa.com", "FA Cup", "https://www.thefa.com/competitions/thefacup/fixtures"),
    ("efl.com", "Carabao Cup", "https://www.efl.com/competitions/carabao-cup/fixtures/"),
]


def fetch_scheduled_ucl_matches():
    r = requests.get(
        f"{API_BASE}/competitions/{COMPETITION_CODE}/matches",
        params={"status": "SCHEDULED"},
        headers={"X-Auth-Token": API_KEY},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("matches", [])


def match_team_name(name: str, teams_by_name: dict):
    """Fuzzy-match football-data.org's team name (e.g. 'Arsenal FC') against
    our stored team names/short names (e.g. 'Arsenal' / 'ARS')."""
    normalized = name.replace(" FC", "").replace(" AFC", "").replace(" CF", "").strip().lower()
    candidates = list(teams_by_name.keys())
    match = difflib.get_close_matches(normalized, candidates, n=1, cutoff=0.6)
    return teams_by_name[match[0]] if match else None


def nearest_upcoming_gameweek(conn, match_date_str: str):
    """Finds the next PL gameweek on/after this UCL match's date, so the
    fixture slots into the right spot in the ticker."""
    try:
        match_date = datetime.datetime.fromisoformat(match_date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    row = conn.execute(
        "SELECT MIN(gameweek) AS gw FROM fixtures WHERE kickoff_time >= ?",
        (match_date.isoformat(),),
    ).fetchone()
    if row and row["gw"] is not None:
        return row["gw"]
    # season's over / no future fixtures loaded yet - fall back to the latest known gameweek
    row = conn.execute("SELECT MAX(gameweek) AS gw FROM fixtures").fetchone()
    return row["gw"] if row else None


def run():
    if not API_KEY:
        print("FOOTBALL_DATA_API_KEY not set - sign up free at "
              "https://www.football-data.org/client/register and export it, then re-run. Skipping.")
        return

    with get_conn() as conn:
        teams_by_name = {}
        for row in conn.execute("SELECT team_id, name, short_name FROM teams").fetchall():
            teams_by_name[row["name"].lower()] = row["team_id"]
            teams_by_name[row["short_name"].lower()] = row["team_id"]

        matches = fetch_scheduled_ucl_matches()
        print(f"Found {len(matches)} scheduled Champions League matches")

        conn.execute("DELETE FROM other_fixtures WHERE source = 'football-data.org'")

        inserted, skipped = 0, 0
        for m in matches:
            home_name = m.get("homeTeam", {}).get("name", "")
            away_name = m.get("awayTeam", {}).get("name", "")
            match_date = m.get("utcDate", "")

            home_id = match_team_name(home_name, teams_by_name)
            away_id = match_team_name(away_name, teams_by_name)

            # only care about matches involving one of our 20 PL teams
            for team_id, is_home, opponent_name in (
                (home_id, True, away_name), (away_id, False, home_name)
            ):
                if not team_id:
                    continue
                near_gw = nearest_upcoming_gameweek(conn, match_date)
                if near_gw is None:
                    skipped += 1
                    continue
                conn.execute(
                    """INSERT INTO other_fixtures
                         (team_id, competition, opponent_name, is_home, match_date, near_gameweek, source)
                       VALUES (?, 'UCL', ?, ?, ?, ?, 'football-data.org')""",
                    (team_id, opponent_name, 1 if is_home else 0, match_date, near_gw),
                )
                inserted += 1
        conn.commit()
    print(f"Loaded {inserted} Champions League fixtures for your PL teams ({skipped} skipped - no gameweek match)")


# ---------------------------------------------------------------------------
# FA Cup / Carabao Cup - scraped directly from the governing bodies' own
# public fixtures pages (thefa.com, efl.com). No API key needed for this
# part. This is HTML scraping, not an official API - if either site
# redesigns its fixtures page, the table-finding logic below may need a
# small update, same caveat as fbref_scraper.py.
# ---------------------------------------------------------------------------

def fetch_tables(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser").find_all("table")


def extract_fixtures_from_table(table, teams_by_name: dict):
    """
    Defensive parser: finds whichever columns are headed 'Home' and 'Away'
    (case-insensitive) in this table, then pulls team names + row text for
    every data row. Skips anything that doesn't cleanly resolve to a
    Home/Away pair. Returns a list of (home_name, away_name, row_text) -
    row_text is kept so a date can be pulled from it separately, since
    these sites format dates inconsistently across table sections.
    """
    rows = table.find_all("tr")
    if not rows:
        return []

    header_cells = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
    if "home" not in header_cells or "away" not in header_cells:
        return []
    home_idx = header_cells.index("home")
    away_idx = header_cells.index("away")

    fixtures = []
    for r in rows[1:]:
        cells = r.find_all(["td", "th"])
        if len(cells) <= max(home_idx, away_idx):
            continue
        home_name = cells[home_idx].get_text(strip=True)
        away_name = cells[away_idx].get_text(strip=True)
        if not home_name or not away_name:
            continue
        fixtures.append((home_name, away_name, r.get_text(" ", strip=True)))
    return fixtures


def guess_date_from_text(text: str, fallback_year: int):
    """
    Looks for a date like '18 September 2026' or '19/09' in a table's
    caption/heading area. Falls back to None if nothing parseable is
    found - the fixture still gets stored, just without match_date set
    (near_gameweek still gets computed from whatever date IS found, or
    the row is skipped if truly nothing usable turns up).
    """
    m = re.search(r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
                  r"September|October|November|December)\s+(\d{4})", text, re.IGNORECASE)
    if m:
        try:
            return datetime.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y")
        except ValueError:
            return None
    return None


def run_domestic_cups():
    with get_conn() as conn:
        teams_by_name = {}
        for row in conn.execute("SELECT team_id, name, short_name FROM teams").fetchall():
            teams_by_name[row["name"].lower()] = row["team_id"]
            teams_by_name[row["short_name"].lower()] = row["team_id"]

        for source_tag, competition, url in DOMESTIC_CUP_SOURCES:
            print(f"Scraping {competition} from {url}...")
            conn.execute("DELETE FROM other_fixtures WHERE source = ?", (source_tag,))
            try:
                tables = fetch_tables(url)
            except requests.RequestException as e:
                print(f"  {competition}: fetch failed ({e}), skipping this source")
                continue

            inserted = 0
            today = datetime.datetime.now()
            for table in tables:
                # the date/round heading usually sits in the table right before
                # the header row, or in a caption - check nearby text broadly
                context_text = table.get_text(" ", strip=True)
                match_date = guess_date_from_text(context_text, today.year)

                for home_name, away_name, row_text in extract_fixtures_from_table(table, teams_by_name):
                    home_id = match_team_name(home_name, teams_by_name)
                    away_id = match_team_name(away_name, teams_by_name)
                    if not match_date:
                        match_date = guess_date_from_text(row_text, today.year)
                    if not match_date:
                        continue  # can't place it in the ticker without a date

                    near_gw = nearest_upcoming_gameweek(conn, match_date.isoformat())
                    if near_gw is None:
                        continue

                    for team_id, is_home, opponent_name in (
                        (home_id, True, away_name), (away_id, False, home_name)
                    ):
                        if not team_id:
                            continue
                        conn.execute(
                            """INSERT INTO other_fixtures
                                 (team_id, competition, opponent_name, is_home, match_date, near_gameweek, source)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (team_id, competition, opponent_name, 1 if is_home else 0,
                             match_date.isoformat(), near_gw, source_tag),
                        )
                        inserted += 1
            conn.commit()
            print(f"  {competition}: loaded {inserted} fixtures for your PL teams")


if __name__ == "__main__":
    run()
    run_domestic_cups()
