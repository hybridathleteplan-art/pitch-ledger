"""
Team attack/defense ratings (1-5) computed from xG instead of actual goals.

Why xG instead of actual goals: actual goals in a handful of games are
noisy - one fluky 4-0 or a missed penalty swings a team's "goals per game"
a lot. xG smooths that out and reflects the quality of chances a team
creates/allows, which is what analysts usually want for a forward-looking
fixture difficulty rating.

Methodology:
- A team's attacking xG for a given gameweek = sum of that gameweek's xG
  across all of that team's players who got minutes (this is exactly what
  a "team xG" stat means - total quality of chances the team's players
  were involved in).
- A team's defensive xG-conceded for that same gameweek = the opponent's
  attacking xG in that match. This is definitional: the shots a team
  faced ARE the shots that generate the opponent's attacking xG, so there
  is no separate "xGC" data source needed - it's just the opponent's own
  attacking number for that fixture, looked up via opponent_team_id.
- Home and away are tracked separately throughout, same as the
  goals-based ratings this replaces.

Bands: reuses the same 1-5 thresholds originally defined for actual
goals/game, now applied to xG/game instead. There's no different
"official" xG banding, so this is a judgment call — easy to retune below
if you'd rather use tighter/wider bands for xG specifically (xG per game
tends to have a narrower spread than actual goals per game across a
season, so you may eventually want to compress these bands).

Red card notes: a team's xG-conceded (or an opponent's inflated xG-for)
in one game can be distorted by a red card - a team down to 10 men
concedes more/creates less, and that shows up as noise in a small
sample. This module also flags which gameweeks (within the sampled
window) had a red card for either side, so the raw-data table can show
"why" a number looks off rather than just presenting it silently.
"""

from database import get_conn


def band_attack(xg_per_game: float) -> int:
    if xg_per_game >= 2.5:
        return 5
    if xg_per_game > 2:
        return 4
    if xg_per_game > 1.5:
        return 3
    if xg_per_game >= 1:
        return 2
    return 1


def band_defense(xgc_per_game: float) -> int:
    if xgc_per_game <= 1:
        return 5
    if xgc_per_game <= 1.5:
        return 4
    if xgc_per_game <= 2:
        return 3
    if xgc_per_game < 2.5:
        return 2
    return 1


def compute_team_xg_ratings():
    """
    Returns one dict per team with home/away attacking and defensive xG
    ratings (1-5), plus the underlying raw xG/game numbers for transparency.
    Teams with no gameweek data yet (season hasn't started / scraper
    hasn't run) get null ratings rather than a misleading default.
    """
    with get_conn() as conn:
        teams = {t["team_id"]: dict(t) for t in conn.execute("SELECT * FROM teams").fetchall()}

        # One row per (team, gameweek): that team's total xG that gameweek,
        # whether they were home, who the opponent was, and whether this
        # team had a red card that gameweek (own_red_card).
        rows = conn.execute("""
            SELECT p.team_id AS team_id,
                   pg.gameweek AS gameweek,
                   MAX(pg.was_home) AS was_home,
                   MAX(pg.opponent_team_id) AS opponent_team_id,
                   SUM(pg.xg) AS team_xg,
                   SUM(pg.red_cards) AS own_red_cards
            FROM player_gameweeks pg
            JOIN players p ON p.player_id = pg.player_id
            WHERE pg.minutes > 0
            GROUP BY p.team_id, pg.gameweek
        """).fetchall()

    # index by (team_id, gameweek) so we can look up "what was team X's xG
    # (and red cards) in gameweek Y" - used below both for xG-conceded
    # (opponent's xG in the same gameweek) and for red-card notes.
    by_team_gw = {(r["team_id"], r["gameweek"]): r["team_xg"] for r in rows}
    reds_by_team_gw = {(r["team_id"], r["gameweek"]): (r["own_red_cards"] or 0) > 0 for r in rows}

    # accumulate xg_for/xg_against samples plus red-card gameweek lists, split by venue
    samples = {tid: {
        "H_for": [], "H_against": [], "A_for": [], "A_against": [],
        "H_for_red_gws": [], "H_against_red_gws": [], "A_for_red_gws": [], "A_against_red_gws": [],
    } for tid in teams}

    for r in rows:
        tid, gw, was_home, opp_id, team_xg = (
            r["team_id"], r["gameweek"], r["was_home"], r["opponent_team_id"], r["team_xg"],
        )
        if tid not in samples:
            continue
        opp_xg = by_team_gw.get((opp_id, gw))  # None if opponent's data isn't in yet
        venue = "H" if was_home else "A"
        samples[tid][f"{venue}_for"].append(team_xg or 0)
        # this team had a red card this gameweek -> flags THIS team's own
        # xg_for as possibly inflated (played most of the game vs 10 men)
        # or, from the opponent's perspective when we process their row,
        # explains an inflated xg_against for the opponent - handled below
        # via reds_by_team_gw lookups rather than duplicating logic here.
        if reds_by_team_gw.get((opp_id, gw)):
            samples[tid][f"{venue}_for_red_gws"].append(gw)  # opponent had a red card -> our xG-for that game may be inflated
        if opp_xg is not None:
            samples[tid][f"{venue}_against"].append(opp_xg)
            if reds_by_team_gw.get((tid, gw)):
                samples[tid][f"{venue}_against_red_gws"].append(gw)  # WE had a red card -> our xG-against that game may be inflated

    def avg(lst):
        return sum(lst) / len(lst) if lst else None

    results = []
    for tid, t in teams.items():
        s = samples.get(tid, {
            "H_for": [], "H_against": [], "A_for": [], "A_against": [],
            "H_for_red_gws": [], "H_against_red_gws": [], "A_for_red_gws": [], "A_against_red_gws": [],
        })
        h_for, h_against = avg(s["H_for"]), avg(s["H_against"])
        a_for, a_against = avg(s["A_for"]), avg(s["A_against"])
        results.append({
            "team_id": tid,
            "name": t["name"],
            "short_name": t["short_name"],
            "home_games": len(s["H_for"]),
            "away_games": len(s["A_for"]),
            "xg_for_home": round(h_for, 2) if h_for is not None else None,
            "xg_against_home": round(h_against, 2) if h_against is not None else None,
            "xg_for_away": round(a_for, 2) if a_for is not None else None,
            "xg_against_away": round(a_against, 2) if a_against is not None else None,
            "home_attack_rating": band_attack(h_for) if h_for is not None else None,
            "home_defense_rating": band_defense(h_against) if h_against is not None else None,
            "away_attack_rating": band_attack(a_for) if a_for is not None else None,
            "away_defense_rating": band_defense(a_against) if a_against is not None else None,
            # gameweeks (within the sampled window) where a red card - ours
            # or the opponent's - may be distorting the number above
            "home_for_red_card_gws": s["H_for_red_gws"],
            "home_against_red_card_gws": s["H_against_red_gws"],
            "away_for_red_card_gws": s["A_for_red_gws"],
            "away_against_red_card_gws": s["A_against_red_gws"],
        })
    return results
