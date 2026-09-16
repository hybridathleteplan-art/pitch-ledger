"""
Pulls Premier League match odds from The Odds API (odds-api.io) and stores
de-vigged (bookmaker-margin-removed) probabilities: home/draw/away win %,
and both-teams-to-score % as a goals-scored signal.

SETUP REQUIRED - this one needs a free API key, unlike the FPL/FBref
scrapers:
  1. Sign up at https://odds-api.io (free tier: 500 requests/day, no card).
  2. Set the key as an environment variable before running:
       export ODDS_API_KEY="your_key_here"
     (or add it to your GitHub Actions secrets / Vercel env vars for the
     automated version - see the README.)

Run manually:  python odds_scraper.py

Honest limitation: the free tier's exact market names (moneyline and BTTS)
aren't guaranteed to match the constants below - the marketing page shows
an "ML" market by name but doesn't fully document every market's exact
string. Check https://docs.odds-api.io against MARKET_NAME_MONEYLINE /
MARKET_NAME_BTTS below if this comes back empty, and adjust.
"""
import os
import datetime
import difflib

import requests

from database import get_conn

API_BASE = "https://api.odds-api.io/v3"
API_KEY = os.environ.get("ODDS_API_KEY")

# Verify these against https://docs.odds-api.io for your account - the
# public marketing page confirms "ML" (moneyline/1X2) and mentions "Both
# Teams to Score" exists as a market but doesn't give its exact string.
MARKET_NAME_MONEYLINE = "ML"
MARKET_NAME_BTTS = "BTTS"


def fetch_epl_events():
    r = requests.get(f"{API_BASE}/events", params={"apiKey": API_KEY, "sport": "football"}, timeout=30)
    r.raise_for_status()
    events = r.json()
    return [e for e in events if "premier" in (e.get("league", {}).get("name") or "").lower()
            and "efl" not in (e.get("league", {}).get("name") or "").lower()]


def fetch_event_odds(event_id):
    r = requests.get(f"{API_BASE}/odds", params={"apiKey": API_KEY, "eventId": event_id}, timeout=30)
    r.raise_for_status()
    return r.json()


def devig(prices: list[float]) -> list[float]:
    """Convert decimal odds to normalized (de-vigged) probabilities."""
    implied = [1 / p for p in prices if p]
    total = sum(implied)
    if not total:
        return [None] * len(prices)
    return [i / total for i in implied]


def average_market(bookmakers: dict, market_name: str, outcome_keys: list[str]):
    """Average a given market's prices across all bookmakers that offer it."""
    samples = {k: [] for k in outcome_keys}
    for _, markets in bookmakers.items():
        for m in markets:
            if m.get("name") != market_name:
                continue
            for row in m.get("odds", []):
                for k in outcome_keys:
                    if row.get(k):
                        try:
                            samples[k].append(float(row[k]))
                        except (TypeError, ValueError):
                            pass
    return {k: (sum(v) / len(v) if v else None) for k, v in samples.items()}


def match_team_name(name: str, teams_by_name: dict):
    """Fuzzy-match an odds-provider team name (e.g. 'Liverpool FC') against
    our stored team names/short names (e.g. 'Liverpool' / 'LIV')."""
    normalized = name.replace(" FC", "").replace(" AFC", "").strip().lower()
    candidates = list(teams_by_name.keys())
    match = difflib.get_close_matches(normalized, candidates, n=1, cutoff=0.6)
    return teams_by_name[match[0]] if match else None


def find_matching_fixture(conn, home_team_id, away_team_id, event_date_str):
    """Match an odds-provider event to our fixtures table by team pair,
    picking the closest kickoff date if there's ever more than one (there
    shouldn't be, teams play each other at most twice a season)."""
    rows = conn.execute(
        "SELECT fixture_id, kickoff_time FROM fixtures WHERE team_h = ? AND team_a = ?",
        (home_team_id, away_team_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]["fixture_id"]
    try:
        target = datetime.datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return rows[0]["fixture_id"]
    best = min(rows, key=lambda r: abs(
        (datetime.datetime.fromisoformat(r["kickoff_time"].replace("Z", "+00:00")) - target).total_seconds()
    ) if r["kickoff_time"] else float("inf"))
    return best["fixture_id"]


def run():
    if not API_KEY:
        print("ODDS_API_KEY not set - sign up free at https://odds-api.io and "
              "export ODDS_API_KEY, then re-run. Skipping.")
        return

    with get_conn() as conn:
        teams_by_name = {}
        for row in conn.execute("SELECT team_id, name, short_name FROM teams").fetchall():
            teams_by_name[row["name"].lower()] = row["team_id"]
            teams_by_name[row["short_name"].lower()] = row["team_id"]

        events = fetch_epl_events()
        print(f"Found {len(events)} upcoming Premier League events with odds")

        updated = 0
        for event in events:
            home_id = match_team_name(event.get("home", ""), teams_by_name)
            away_id = match_team_name(event.get("away", ""), teams_by_name)
            if not home_id or not away_id:
                print(f"  skip: couldn't match '{event.get('home')}' vs '{event.get('away')}' to known teams")
                continue

            fixture_id = find_matching_fixture(conn, home_id, away_id, event.get("date", ""))
            if not fixture_id:
                print(f"  skip: no fixture row for {event.get('home')} vs {event.get('away')} - run fpl_scraper.py first")
                continue

            odds_data = fetch_event_odds(event["id"])
            bookmakers = odds_data.get("bookmakers", {})

            ml = average_market(bookmakers, MARKET_NAME_MONEYLINE, ["home", "draw", "away"])
            btts = average_market(bookmakers, MARKET_NAME_BTTS, ["yes", "no"])

            home_pct = draw_pct = away_pct = btts_yes_pct = None
            if ml["home"] and ml["away"]:
                prices = [ml["home"], ml.get("draw") or 999999, ml["away"]]
                probs = devig(prices)
                home_pct, draw_pct, away_pct = probs
                if not ml.get("draw"):
                    draw_pct = None
            if btts["yes"] and btts["no"]:
                probs = devig([btts["yes"], btts["no"]])
                btts_yes_pct = probs[0]

            now = datetime.datetime.utcnow().isoformat()
            conn.execute(
                """INSERT INTO match_odds
                     (fixture_id, home_win_pct, draw_pct, away_win_pct, btts_yes_pct, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'odds-api.io', ?)
                   ON CONFLICT(fixture_id) DO UPDATE SET
                     home_win_pct=excluded.home_win_pct, draw_pct=excluded.draw_pct,
                     away_win_pct=excluded.away_win_pct, btts_yes_pct=excluded.btts_yes_pct,
                     updated_at=excluded.updated_at""",
                (fixture_id, home_pct, draw_pct, away_pct, btts_yes_pct, now),
            )
            updated += 1

        conn.commit()
    print(f"Updated odds for {updated} fixtures")


if __name__ == "__main__":
    run()
