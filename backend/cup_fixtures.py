"""
Cup and European fixtures - maintained by hand.

Why manual: FPL's API only covers Premier League matches, and there's no
free API that reliably maps Champions League / Europa / Conference League /
FA Cup / Carabao Cup fixtures onto the same team IDs without a lot of
fragile name-matching. Draws for these competitions also happen in batches
(group stage draw, each cup round draw) rather than continuously, so
updating this file a handful of times a season is genuinely less work than
babysitting a scraper against 5 different competition sources.

Add an entry here whenever a new round is confirmed. team_short must match
a short_name already in your teams table (ARS, MCI, LIV, etc.) - that's
how load_cup_fixtures.py finds the right team_id.
"""

CUP_FIXTURES = [
    # {
    #     "team_short": "ARS",
    #     "competition": "UCL",             # UCL, UEL, UECL, "FA Cup", "Carabao Cup"
    #     "opponent_name": "Bayern Munich",  # free text - opponent may not be a PL team
    #     "is_home": True,
    #     "match_date": "2026-09-17",        # ISO date
    #     "near_gameweek": 5,                # nearest PL gameweek, so the ticker knows where to slot it
    # },
]
