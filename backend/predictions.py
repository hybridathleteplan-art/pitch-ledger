"""
Predicted FPL points for the next N fixtures of every player.

For one player in one fixture the expected points are the sum of:

  appearance   1 pt for playing, 2 for 60+ minutes
  goals        xG/90 x expected minutes x opponent factor x (6 GK/DEF, 5 MID, 4 FWD)
  assists      xA/90 x expected minutes x opponent factor x 3
  clean sheet  P(clean sheet) x P(60+ min) x (4 GK/DEF, 1 MID, 0 FWD)
  goals conc.  GK/DEF only: -1 per 2 goals conceded, from the same Poisson model
  defensive    P(reaching the threshold) x P(60+ min) x 2
  contribution (threshold: 10 for DEF, 12 for MID/FWD; GK cannot earn it)
  bonus        bonus/90 x expected minutes

  cards        -1 per expected yellow, -3 per expected red
  suspension   accumulated-yellow bans (see below) remove the player from
               a fixture with the modelled probability

Yellow-card bans follow the Premier League's accumulated-caution rules:
  5 yellows by (and including) the team's 19th league match -> 1-match ban
  10 yellows by the 32nd league match                       -> 2-match ban
  15 yellows by the end of the season                       -> 3-match ban
The yellow count is tracked as a probability distribution across the
upcoming fixtures: each match he has a chance of being booked (his
season yellows per 90, pulled toward the position average); if that card
is the one that reaches a threshold inside its window, he is banned for
the next fixture (modelled as missing one match; the 2- and 3-match bans
are rare enough that only the first match of the ban is modelled).

  saves        goalkeepers only: saves/90 x expected minutes, scaled by how
               dangerous the opponent's attack is at that venue, / 3 points

Calibration (recomputed from the finished matches at every request):
  * expected goals against are scaled so the average modelled clean-sheet
    chance equals the real clean-sheet rate (xG runs above real goals)
  * each position's xG and xA rates are scaled so the totals match the
    goals and assists that really happened

Not included yet: own goals, penalties missed or saved.

Player rates blend 60% recent form (last 6 team games) and 40% season, then
are pulled toward the position average while the sample is small.

Opponent strength comes from ratings.compute_team_xg_ratings(), which is
already 50/50 raw and opponent-adjusted xG split by home/away.

Availability: FPL's chance_of_playing_next_round multiplies the first
fixture's minutes. The second fixture recovers halfway to full, the third
onward is full. Players with no flag are treated as fit.
"""
import math

from database import get_conn
from ratings import compute_team_xg_ratings

RECENT_GAMES = 6
RECENT_WEIGHT = 0.6
SEASON_WEIGHT = 0.4
MAX_FIXTURES = 10
MIN_EXPECTED_MINUTES = 10  # hide players who basically never play

GOAL_PTS = {"GK": 6, "DEF": 6, "MID": 5, "FWD": 4}
CS_PTS = {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0}
DEFCON_THRESHOLD = {"DEF": 10, "MID": 12, "FWD": 12}  # GK: not eligible
DEFCON_PTS = 2
ASSIST_PTS = 3

RATE_SHRINK_MINUTES = 900   # ~10 full games before a player's own rate dominates
DEFCON_SHRINK_GAMES = 5
FIXTURE_SHRINK_GAMES = 4    # venue games of the opponent before its numbers dominate


def _blend(recent, season):
    return RECENT_WEIGHT * recent + SEASON_WEIGHT * season


def _poisson_cs_and_conceded_penalty(lam):
    """P(0 goals) and E[floor(G/2)] for G ~ Poisson(lam)."""
    p0 = math.exp(-lam)
    e_pen = 0.0
    p = p0
    for g in range(0, 15):
        if g > 0:
            p *= lam / g
        e_pen += (g // 2) * p
    return p0, e_pen


def compute_predictions(max_games=MAX_FIXTURES):
    max_games = max(1, min(int(max_games), MAX_FIXTURES))

    with get_conn() as conn:
        players = [dict(r) for r in conn.execute("""
            SELECT p.*, t.short_name AS team_short, t.name AS team_name
            FROM players p JOIN teams t ON t.team_id = p.team_id
        """).fetchall()]
        teams = {t["team_id"]: dict(t) for t in conn.execute("SELECT * FROM teams").fetchall()}
        gw_rows = conn.execute("""
            SELECT player_id, gameweek, minutes, xg, xa, bonus, defensive_contribution
            FROM player_gameweeks
        """).fetchall()
        fixtures = [dict(r) for r in conn.execute("""
            SELECT fixture_id, gameweek, team_h, team_a, kickoff_time, finished,
                   team_h_score, team_a_score
            FROM fixtures WHERE gameweek IS NOT NULL
        """).fetchall()]

    ratings = {r["team_id"]: r for r in compute_team_xg_ratings()}

    # ---- team game calendars -------------------------------------------
    team_played_gws = {tid: [] for tid in teams}
    team_matches = {tid: 0 for tid in teams}
    upcoming = {tid: [] for tid in teams}
    for f in fixtures:
        if f["finished"]:
            for tid in (f["team_h"], f["team_a"]):
                if tid in team_matches:
                    team_matches[tid] += 1
            for tid in (f["team_h"], f["team_a"]):
                if tid in team_played_gws and f["gameweek"] not in team_played_gws[tid]:
                    team_played_gws[tid].append(f["gameweek"])
        else:
            if f["team_h"] in upcoming:
                upcoming[f["team_h"]].append((f["gameweek"], f["kickoff_time"] or "", f["team_a"], True))
            if f["team_a"] in upcoming:
                upcoming[f["team_a"]].append((f["gameweek"], f["kickoff_time"] or "", f["team_h"], False))
    for tid in team_played_gws:
        team_played_gws[tid].sort()
    for tid in upcoming:
        upcoming[tid].sort(key=lambda x: (x[0], x[1]))

    # ---- player history ------------------------------------------------
    hist = {}
    for r in gw_rows:
        hist.setdefault(r["player_id"], {})[r["gameweek"]] = r

    # ---- league averages for opponent factors ---------------------------
    def vals(key):
        return [r[key] for r in ratings.values() if r.get(key) is not None]
    conc_vals = vals("xg_against_home_blended") + vals("xg_against_away_blended")
    for_vals = vals("xg_for_home_blended") + vals("xg_for_away_blended")
    league_conc = sum(conc_vals) / len(conc_vals) if conc_vals else 1.4
    league_for = sum(for_vals) / len(for_vals) if for_vals else 1.4
    league = (league_conc + league_for) / 2

    def team_strength(tid, venue, kind):
        """Shrunk blended xG for/against for a team at a venue ('H'/'A')."""
        r = ratings.get(tid)
        if not r:
            return league
        games = r["home_games"] if venue == "H" else r["away_games"]
        key = ("xg_for_" if kind == "for" else "xg_against_") + ("home" if venue == "H" else "away") + "_blended"
        v = r.get(key)
        if v is None:
            return league
        k = games / (games + FIXTURE_SHRINK_GAMES)
        return league + k * (v - league)

    # ---- calibration to what really happened ----------------------------
    # Pick the scale on expected goals against so that the model's average
    # clean-sheet chance equals the clean-sheet rate that really happened.
    lam_list, cs_obs = [], []
    for f in fixtures:
        if not f["finished"] or f["team_h_score"] is None or f["team_a_score"] is None:
            continue
        h, a = f["team_h"], f["team_a"]
        lam_list.append(team_strength(a, "A", "for") * team_strength(h, "H", "against") / league)
        cs_obs.append(1 if f["team_a_score"] == 0 else 0)
        lam_list.append(team_strength(h, "H", "for") * team_strength(a, "A", "against") / league)
        cs_obs.append(1 if f["team_h_score"] == 0 else 0)
    cs_scale = 1.0
    if len(lam_list) >= 20:
        target = sum(cs_obs) / len(cs_obs)
        lo, hi = 0.5, 1.2
        for _ in range(40):
            mid = (lo + hi) / 2
            avg_p = sum(math.exp(-mid * l) for l in lam_list) / len(lam_list)
            if avg_p > target:
                lo = mid   # too many clean sheets predicted -> raise lambda
            else:
                hi = mid
        cs_scale = (lo + hi) / 2

    goal_cal, assist_cal = {}, {}
    for pos in ("GK", "DEF", "MID", "FWD"):
        gs = sum(p["goals"] or 0 for p in players if p["position"] == pos)
        xgs = sum(p["xg"] or 0 for p in players if p["position"] == pos)
        asts = sum(p["assists"] or 0 for p in players if p["position"] == pos)
        xas = sum(p["xa"] or 0 for p in players if p["position"] == pos)
        goal_cal[pos] = max(0.7, min(1.3, gs / xgs)) if xgs > 5 else 1.0
        assist_cal[pos] = max(0.7, min(2.5, asts / xas)) if xas > 5 else 1.0

    # goalkeeper saves per 90 prior
    gk = [p for p in players if p["position"] == "GK" and p.get("saves") is not None]
    gk_min = sum(p["minutes"] or 0 for p in gk)
    saves_prior = (sum(p["saves"] or 0 for p in gk) * 90.0 / gk_min) if gk_min else 0.0
    league_for_att = league_for if league_for else league

    # ---- position priors (per 90 rates, defcon hit rate) -----------------
    prior = {}
    for pos in ("GK", "DEF", "MID", "FWD"):
        mins = xg = xa = bonus = 0.0
        hit_n = hit_d = 0
        for p in players:
            if p["position"] != pos:
                continue
            for g in hist.get(p["player_id"], {}).values():
                m = g["minutes"] or 0
                if m <= 0:
                    continue
                mins += m
                xg += g["xg"] or 0
                xa += g["xa"] or 0
                bonus += g["bonus"] or 0
                if m >= 60 and pos in DEFCON_THRESHOLD:
                    hit_n += 1
                    hit_d += 1 if (g["defensive_contribution"] or 0) >= DEFCON_THRESHOLD[pos] else 0
        per90 = (lambda x: x * 90.0 / mins) if mins else (lambda x: 0.0)
        prior[pos] = {
            "xg": per90(xg), "xa": per90(xa), "bonus": per90(bonus),
            "defcon": (hit_d / hit_n) if hit_n else 0.0,
        }

    # yellow/red per-90 priors by position (None if the data isn't loaded yet)
    card_prior = {}
    for pos in ("GK", "DEF", "MID", "FWD"):
        mins = ys = rs = 0.0
        for p in players:
            if p["position"] == pos and p.get("yellow_cards") is not None:
                mins += p["minutes"] or 0
                ys += p.get("yellow_cards") or 0
                rs += p.get("red_cards") or 0
        card_prior[pos] = (ys * 90 / mins, rs * 90 / mins) if mins else (0.0, 0.0)

    out = []
    for p in players:
        pid, tid, pos = p["player_id"], p["team_id"], p["position"]
        if pos not in GOAL_PTS or tid not in teams:
            continue
        team_gws = team_played_gws.get(tid, [])
        if not team_gws:
            continue
        ph = hist.get(pid, {})
        recent_gws = team_gws[-RECENT_GAMES:]

        def mins_of(gw):
            g = ph.get(gw)
            return (g["minutes"] or 0) if g else 0

        # expected minutes / appearance probabilities (rotation-aware: a
        # gameweek the player missed counts as 0 minutes)
        all_m = [mins_of(gw) for gw in team_gws]
        rec_m = [mins_of(gw) for gw in recent_gws]
        exp_min = _blend(sum(rec_m) / len(rec_m), sum(all_m) / len(all_m))
        p_any = _blend(sum(1 for m in rec_m if m > 0) / len(rec_m), sum(1 for m in all_m if m > 0) / len(all_m))
        p60 = _blend(sum(1 for m in rec_m if m >= 60) / len(rec_m), sum(1 for m in all_m if m >= 60) / len(all_m))

        if exp_min < MIN_EXPECTED_MINUTES:
            continue

        # per-90 rates: recent vs season, shrunk to the position average
        def rate(stat):
            def per90(gws):
                tot_m = sum(mins_of(gw) for gw in gws)
                tot_s = sum((ph[gw][stat] or 0) for gw in gws if gw in ph)
                return (tot_s * 90.0 / tot_m) if tot_m else None, tot_m
            season, season_m = per90(team_gws)
            recent, _ = per90(recent_gws)
            pri = prior[pos][stat]
            if season is None:
                return pri
            if recent is None:
                recent = season
            own = _blend(recent, season)
            w = season_m / (season_m + RATE_SHRINK_MINUTES)
            return w * own + (1 - w) * pri

        xg90, xa90, bonus90 = rate("xg"), rate("xa"), rate("bonus")
        xg90 *= goal_cal[pos]
        xa90 *= assist_cal[pos]

        # goalkeeper saves per 90 (own season rate, shrunk to the GK average)
        saves90 = 0.0
        if pos == "GK" and p.get("saves") is not None:
            sm = p["minutes"] or 0
            own = (p["saves"] or 0) * 90.0 / sm if sm else saves_prior
            ws = sm / (sm + RATE_SHRINK_MINUTES)
            saves90 = ws * own + (1 - ws) * saves_prior

        # defensive contribution hit-rate (per 60+ minute game)
        p_def = 0.0
        if pos in DEFCON_THRESHOLD:
            thr = DEFCON_THRESHOLD[pos]
            full = [ph[gw] for gw in team_gws if gw in ph and (ph[gw]["minutes"] or 0) >= 60]
            rec = [ph[gw] for gw in recent_gws if gw in ph and (ph[gw]["minutes"] or 0) >= 60]
            def hit(lst):
                return sum(1 for g in lst if (g["defensive_contribution"] or 0) >= thr) / len(lst) if lst else None
            season_hit, recent_hit = hit(full), hit(rec)
            pri = prior[pos]["defcon"]
            if season_hit is None:
                p_def = pri
            else:
                own = _blend(recent_hit if recent_hit is not None else season_hit, season_hit)
                w = len(full) / (len(full) + DEFCON_SHRINK_GAMES)
                p_def = w * own + (1 - w) * pri

        chance = p.get("chance_of_playing")
        c = 1.0 if chance is None else max(0.0, min(1.0, chance / 100.0))

        # ---- yellow-card / suspension set-up ----------------------------
        yellows = p.get("yellow_cards")
        reds = p.get("red_cards") or 0
        cards_known = yellows is not None
        if cards_known:
            season_min = p["minutes"] or 0
            py90, pr90 = card_prior[pos]
            w_c = season_min / (season_min + RATE_SHRINK_MINUTES)
            y90 = w_c * (yellows * 90.0 / season_min if season_min else py90) + (1 - w_c) * py90
            r90 = w_c * (reds * 90.0 / season_min if season_min else pr90) + (1 - w_c) * pr90
            mins_per_app = min(90.0, exp_min / p_any) if p_any > 0.05 else 60.0
            p_book_given_play = min(0.5, y90 * mins_per_app / 90.0)
            dist = {(int(yellows), False): 1.0}
        matches_played = team_matches.get(tid, 0)

        fx_list = []
        for i, (gw, _ko, opp_id, is_home) in enumerate(upcoming.get(tid, [])[:max_games]):
            avail = c if i == 0 else (c + (1 - c) * 0.5 if i == 1 else 1.0)
            venue_me = "H" if is_home else "A"
            venue_opp = "A" if is_home else "H"

            em = exp_min * avail
            any_p = p_any * avail
            p60_f = p60 * avail

            opp_conc = team_strength(opp_id, venue_opp, "against")
            opp_for = team_strength(opp_id, venue_opp, "for")
            my_conc = team_strength(tid, venue_me, "against")

            goal_factor = opp_conc / league if league else 1.0
            lam = (opp_for * my_conc / league if league else 1.4) * cs_scale  # expected goals conceded

            ap_pts = any_p + p60_f
            g_pts = xg90 * em / 90.0 * goal_factor * GOAL_PTS[pos]
            a_pts = xa90 * em / 90.0 * goal_factor * ASSIST_PTS
            p_cs, e_pen = _poisson_cs_and_conceded_penalty(lam)
            cs_pts = CS_PTS[pos] * p_cs * p60_f
            gc_pts = -(e_pen * min(1.0, em / 90.0)) if pos in ("GK", "DEF") else 0.0
            dc_pts = DEFCON_PTS * p_def * p60_f if pos in DEFCON_THRESHOLD else 0.0
            b_pts = bonus90 * em / 90.0
            saves_pts = 0.0
            if pos == "GK" and saves90:
                att = opp_for / league_for_att if league_for_att else 1.0
                saves_pts = saves90 * em / 90.0 * att / 3.0

            # ---- cards and yellow-card suspension -------------------------
            p_susp = 0.0
            p_booked = 0.0
            card_pts = 0.0
            if cards_known:
                match_no = matches_played + i + 1
                q = any_p * p_book_given_play  # chance of a booking this match if not banned
                p_susp = sum(pr for (_k, pend), pr in dist.items() if pend)
                new = {}
                for (k, pend), pr in dist.items():
                    if pend:  # serves the ban in this match, no booking
                        new[(k, False)] = new.get((k, False), 0.0) + pr
                    else:
                        new[(k, False)] = new.get((k, False), 0.0) + pr * (1 - q)
                        nk = k + 1
                        ban = (nk == 5 and match_no <= 19) or (nk == 10 and match_no <= 32) or (nk == 15 and match_no <= 38)
                        new[(nk, ban)] = new.get((nk, ban), 0.0) + pr * q
                dist = new
                p_booked = q
                card_pts = -(q + 3.0 * r90 * em / 90.0)

            play = 1.0 - p_susp
            parts = [ap_pts, g_pts, a_pts, cs_pts, gc_pts, dc_pts, b_pts, card_pts, saves_pts]
            parts = [v * play for v in parts]
            total = sum(parts)
            opp = teams.get(opp_id, {})
            fx_list.append({
                "gw": gw,
                "opp": opp.get("short_name", "?"),
                "h": 1 if is_home else 0,
                "p": round(total, 2),
                "c": [round(v, 2) for v in parts],
                "s": round(p_susp, 3),
                "y": round(p_booked * play, 3),
            })
        if not fx_list:
            continue

        out.append({
            "player_id": pid,
            "name": p["name"],
            "team_id": tid,
            "team_short": p["team_short"],
            "code": p.get("code"),
            "position": pos,
            "minutes": p["minutes"] or 0,
            "exp_min": round(exp_min, 1),
            "chance": chance,
            "yellows": yellows,
            "news": p.get("news") or "",
            "fixtures": fx_list,
        })
    return out
