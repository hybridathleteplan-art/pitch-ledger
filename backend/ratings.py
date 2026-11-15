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

# Fixture-difficulty ratings blend two views of each past match:
#   RAW_WEIGHT      - what the team actually created/conceded (xG)
#   ADJUSTED_WEIGHT - the same number corrected for the opponent's quality
# 0.5 / 0.5 = "half what they actually did, half who they did it against".
RAW_WEIGHT = 0.5
ADJUSTED_WEIGHT = 0.5


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

    # ---- opponent adjustment -------------------------------------------
    # For every team: how much xG it normally creates (xgf_avg) and
    # allows (xga_avg) over the whole season, home+away combined.
    xgf_all, xga_all = {}, {}
    for r in rows:
        xgf_all.setdefault(r["team_id"], []).append(r["team_xg"] or 0)
        opp_xg = by_team_gw.get((r["opponent_team_id"], r["gameweek"]))
        if opp_xg is not None:
            xga_all.setdefault(r["team_id"], []).append(opp_xg)
    xgf_avg = {t: avg(v) for t, v in xgf_all.items()}
    xga_avg = {t: avg(v) for t, v in xga_all.items()}
    league_avg = avg([r["team_xg"] or 0 for r in rows]) or 1.0

    # Per match, scale the raw number by how the opponent normally performs:
    #   attack:  xG scored / what that opponent usually concedes
    #   defence: xG conceded / what that opponent usually creates
    # (multiplied back by the league average so it stays on the xG scale.)
    # Scoring 2.0 xG against a team that normally concedes 2.0 is worth
    # league-average; conceding 2.5 xG to a team that normally creates 2.5
    # is also only league-average, not a disaster.
    adj_samples = {tid: {"H_for": [], "H_against": [], "A_for": [], "A_against": []} for tid in teams}
    for r in rows:
        tid, gw, opp_id = r["team_id"], r["gameweek"], r["opponent_team_id"]
        if tid not in adj_samples:
            continue
        venue = "H" if r["was_home"] else "A"
        team_xg = r["team_xg"] or 0
        opp_conc = xga_avg.get(opp_id)
        adj_samples[tid][f"{venue}_for"].append(
            team_xg * league_avg / opp_conc if opp_conc else team_xg
        )
        opp_xg = by_team_gw.get((opp_id, gw))
        if opp_xg is not None:
            opp_create = xgf_avg.get(opp_id)
            adj_samples[tid][f"{venue}_against"].append(
                opp_xg * league_avg / opp_create if opp_create else opp_xg
            )

    def blend(raw, adj):
        if raw is None:
            return None
        if adj is None:
            return raw
        return RAW_WEIGHT * raw + ADJUSTED_WEIGHT * adj

    results = []
    for tid, t in teams.items():
        s = samples.get(tid, {
            "H_for": [], "H_against": [], "A_for": [], "A_against": [],
            "H_for_red_gws": [], "H_against_red_gws": [], "A_for_red_gws": [], "A_against_red_gws": [],
        })
        h_for, h_against = avg(s["H_for"]), avg(s["H_against"])
        a_for, a_against = avg(s["A_for"]), avg(s["A_against"])
        adj = adj_samples.get(tid, {"H_for": [], "H_against": [], "A_for": [], "A_against": []})
        h_for_b = blend(h_for, avg(adj["H_for"]))
        h_against_b = blend(h_against, avg(adj["H_against"]))
        a_for_b = blend(a_for, avg(adj["A_for"]))
        a_against_b = blend(a_against, avg(adj["A_against"]))
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
            # blended (70% raw + 30% opponent-adjusted) xG, used for the ratings
            "xg_for_home_blended": round(h_for_b, 2) if h_for_b is not None else None,
            "xg_against_home_blended": round(h_against_b, 2) if h_against_b is not None else None,
            "xg_for_away_blended": round(a_for_b, 2) if a_for_b is not None else None,
            "xg_against_away_blended": round(a_against_b, 2) if a_against_b is not None else None,
            "home_attack_rating": band_attack(h_for_b) if h_for_b is not None else None,
            "home_defense_rating": band_defense(h_against_b) if h_against_b is not None else None,
            "away_attack_rating": band_attack(a_for_b) if a_for_b is not None else None,
            "away_defense_rating": band_defense(a_against_b) if a_against_b is not None else None,
            # gameweeks (within the sampled window) where a red card - ours
            # or the opponent's - may be distorting the number above
            "home_for_red_card_gws": s["H_for_red_gws"],
            "home_against_red_card_gws": s["H_against_red_gws"],
            "away_for_red_card_gws": s["A_for_red_gws"],
            "away_against_red_card_gws": s["A_against_red_gws"],
        })
    return results
