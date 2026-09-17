"""
Pulls the stats FPL's API doesn't have - touches in the attacking penalty
area, progressive carries/passes - from FBref's free public stat tables.

This is HTML scraping, not an official API, so:
  - Be polite: FBref asks for max ~1 request every 3 seconds per their
    terms. This script sleeps between requests. Don't remove that.
  - It's slower-moving than FPL's feed - update it weekly, not hourly.
  - If FBref changes their page layout, this will need small fixes to
    the column-matching logic below.

Run manually:  python fbref_scraper.py
"""
import time
import datetime
import re

import pandas as pd
import requests

from database import get_conn

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://fbref.com/en/comps/9/Premier-League-Stats",
}

# FBref's Premier League "Possession" table has touches broken down by zone,
# including "Att Pen" (touches in the attacking penalty area).
POSSESSION_URL = "https://fbref.com/en/comps/9/possession/Premier-League-Stats"
# The "Standard Stats" table has shots, shots on target, npxG, per-90s.
STANDARD_URL = "https://fbref.com/en/comps/9/stats/Premier-League-Stats"


def _get_tables(url: str) -> list[pd.DataFrame]:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    # FBref wraps some tables in HTML comments to hide them from basic
    # scrapers; pull those out before letting pandas parse the page.
    html = resp.text
    html = re.sub(r"<!--|-->", "", html)
    return pd.read_html(html)


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[-1] if c[-1] and "Unnamed" not in c[-1] else c[0] for c in df.columns]
    return df


def scrape_possession() -> pd.DataFrame:
    tables = _get_tables(POSSESSION_URL)
    # the big per-player table is the largest one on the page
    df = max(tables, key=len)
    df = _flatten_columns(df)
    df = df[df["Player"] != "Player"]  # drop repeated header rows
    keep = {"Player": "name", "Squad": "team", "Att Pen": "touches_opp_box",
            "PrgC": "progressive_carries", "PrgP": "progressive_passes"}
    df = df[[c for c in keep if c in df.columns]].rename(columns=keep)
    for col in ("touches_opp_box", "progressive_carries", "progressive_passes"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def scrape_standard() -> pd.DataFrame:
    tables = _get_tables(STANDARD_URL)
    df = max(tables, key=len)
    df = _flatten_columns(df)
    df = df[df["Player"] != "Player"]
    keep = {"Player": "name", "Squad": "team", "Sh": "shots", "SoT": "shots_on_target"}
    df = df[[c for c in keep if c in df.columns]].rename(columns=keep)
    for col in ("shots", "shots_on_target"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def merge_into_db(possession: pd.DataFrame, standard: pd.DataFrame):
    merged = possession.merge(standard.drop(columns=["team"], errors="ignore"),
                               on="name", how="outer")
    now = datetime.datetime.utcnow().isoformat()
    updated, unmatched = 0, 0
    with get_conn() as conn:
        for _, row in merged.iterrows():
            # Match by name - FBref and FPL naming can differ slightly
            # (accents, nicknames). A fuzzy-match pass is a good next
            # improvement; exact match covers most players fine.
            cur = conn.execute(
                "SELECT player_id FROM players WHERE name = ?", (row["name"],)
            ).fetchone()
            if not cur:
                unmatched += 1
                continue
            conn.execute(
                """UPDATE players SET
                     touches_opp_box = ?, progressive_carries = ?,
                     progressive_passes = ?, shots = ?, shots_on_target = ?, updated_at = ?
                   WHERE player_id = ?""",
                (
                    float(row.get("touches_opp_box", 0) or 0),
                    float(row.get("progressive_carries", 0) or 0),
                    float(row.get("progressive_passes", 0) or 0),
                    int(row.get("shots", 0) or 0),
                    int(row.get("shots_on_target", 0) or 0),
                    now,
                    cur["player_id"],
                ),
            )
            updated += 1
    print(f"FBref merge: updated {updated} players, {unmatched} unmatched names")


def run():
    print("Scraping FBref possession table (touches in box, progressive actions)...")
    possession = scrape_possession()
    time.sleep(3)  # be polite to FBref between requests
    print("Scraping FBref standard stats table (shots)...")
    standard = scrape_standard()
    merge_into_db(possession, standard)


if __name__ == "__main__":
    run()
