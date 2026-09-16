# Pitch Ledger — Premier League stats app

Free, no-API-key Premier League stats: per-player and per-team G, A, xG, xA,
shots on target, touches in the opponent's box, progressive carries/passes,
minutes, ICT index — refreshed after each gameweek.

```
epl-stats/
├── api/
│   └── index.py            # Vercel entrypoint - imports the real app from backend/app.py
├── backend/
│   ├── database.py          # SQLite schema + connection helper
│   ├── fpl_scraper.py       # pulls G/A/xG/xA/minutes from official FPL API (free)
│   ├── fbref_scraper.py     # pulls touches-in-box, progressive actions from FBref (free)
│   ├── seed_sample.py       # 4 fake players, for testing without network
│   ├── app.py                # FastAPI: serves /api/players, /api/teams
│   └── requirements.txt
├── frontend/
│   └── index.html           # single-file dashboard, no build step
├── .github/workflows/
│   └── update-data.yml      # scheduled scraper run, commits refreshed epl.db
├── vercel.json               # routes / to the frontend, /api/* to the FastAPI function
└── requirements.txt          # copy of backend/requirements.txt - Vercel needs this at the root
```

## 1. Run it locally (5 minutes)

```bash
cd backend
pip install -r requirements.txt

# option A: real data (needs internet access to fantasy.premierleague.com and fbref.com)
python fpl_scraper.py          # takes a few minutes - it pulls per-player gameweek history too
python fbref_scraper.py        # optional, adds touches-in-box etc.

# option B: fake data, just to see the app work immediately
python seed_sample.py

# start the API
uvicorn app:app --reload --port 8000
```

Then just open `frontend/index.html` in a browser (double-click it, or
`python -m http.server` from the `frontend/` folder). Set `API_BASE` at the
top of the `<script>` block in `index.html` to `"http://127.0.0.1:8000"`
for this local setup — it's left as `""` (relative) by default because
that's what the actual Vercel deployment needs, where frontend and API
share one domain.

## 2. Deploying for real, with automatic per-gameweek updates

This repo already contains the config files for the free path below:
`.github/workflows/update-data.yml`, `vercel.json`, `api/index.py`, `.gitignore`.

**The approach:** one Vercel project hosts both the static frontend and the
FastAPI backend (as a Python serverless function) — same domain, no CORS
juggling. GitHub Actions runs the scrapers on a schedule and commits the
refreshed `epl.db` back to your repo; Vercel redeploys automatically on
every push, so the live app always has current data. $0/month.

**Honest caveat:** Vercel's Python functions have a read-only filesystem
at request time, so the API can only *read* `epl.db` — it can't write to
it live. That's fine here because all writes happen offline via the
scrapers + GitHub Actions, then get redeployed. If you later want features
that write at request time (saved teams, comments, etc.), that's the point
to move to Postgres (e.g. a free Supabase/Neon instance) instead of SQLite.

### Step 1 — Get this into a GitHub repo
```bash
cd pitch-ledger-epl-stats   # wherever you unzipped it
git init
git add .
git commit -m "Initial commit"
```
Create a new empty repo on github.com (no README/license — you already have files),
then:
```bash
git remote add origin https://github.com/<you>/pitch-ledger.git
git branch -M main
git push -u origin main
```

### Step 2 — Seed real data once, locally, before your first push
```bash
cd backend
pip install -r requirements.txt
python fpl_scraper.py        # pulls real data - takes a few minutes
python fbref_scraper.py      # optional
cd ..
git add backend/epl.db
git commit -m "Seed real data"
git push
```
(Do this once so the very first deploy has real numbers, not the empty
sample DB. After this, GitHub Actions keeps it updated automatically.)

### Step 3 — Deploy on Vercel
1. Go to [vercel.com](https://vercel.com), sign in with GitHub, **Add New → Project** → pick your repo.
2. Leave **Root Directory** as the repo root (not `frontend` or `backend` — `vercel.json` handles routing both parts from there).
3. Framework preset: **Other**. Vercel auto-detects `api/index.py` as a Python serverless function and picks up `requirements.txt` at the root.
4. Deploy. You'll get a URL like `pitch-ledger.vercel.app`.
5. Test it: open `https://<your-domain>/api/health` — you should see `{"status":"ok", "current_gameweek": ...}`. Open `https://<your-domain>/` — you should see the dashboard, already pointed at the right API since `API_BASE` is relative.

That's it — one deploy, both pieces live, nothing to wire together manually.

### Step 4 — Turn on automatic updates
`.github/workflows/update-data.yml` is already in the repo and scheduled
for Monday mornings (UTC) — adjust the cron line if you want a
different cadence, or just push and it works as-is. To confirm it works
before waiting for the schedule: on GitHub, go to **Actions → Update EPL
data → Run workflow** to trigger it manually once. When it commits a new
`epl.db`, Vercel redeploys automatically within a minute or two.

### If you'd rather split it (Railway/Render + Vercel)
Nothing above requires it, but if you ever want the backend on an
always-on server instead of serverless (e.g. you outgrow the read-only
filesystem limitation), `backend/railway.toml` and `backend/Procfile` are
still in the project for that path — deploy `backend/` there as its own
service, then set `API_BASE` in `frontend/index.html` to that service's
URL and deploy `frontend/` on Vercel separately.



## 3. Data notes / honesty check

- **FPL API** (`fantasy.premierleague.com/api`): free, no key, official,
  reliable. Source for goals, assists, xG, xA, minutes, shots on target,
  ICT index, price, per-gameweek history.
- **FBref**: free, no key, but it's a scrape of their public stat pages,
  not an official API. It's the only free source for touches-in-box and
  progressive actions. It's a bit more fragile — if FBref redesigns their
  page, `fbref_scraper.py`'s column-matching will need a small update.
  Name-matching between FBref and FPL is exact-string for now; a handful
  of players with accented names or nicknames may not match and will
  just keep their FPL-only stats until you improve the matching.
- If this ever needs to be bulletproof/commercial, that's when a paid
  Opta-based feed (SportMonks etc., from ~€30-50/mo) earns its cost — it
  removes the scraping fragility entirely. Not needed to get started.

## 4. Per-game averages & home/away splits (all players, automatic)

`/api/players` now also returns, computed live from every player's gameweek
history — no per-player special-casing, this works for the full ~700-player
database the moment `fpl_scraper.py` has run:

- `min_per_game`, `defcon_per_game`, `bonus_per_game`, `cs_per_game` — simple
  averages across gameweeks where the player actually got minutes.
- `points_per_game` — FPL's own official season figure.
- `pts_per_game_home` / `pts_per_game_away` — average FPL points split by
  whether the player's team was home or away that gameweek. `null` means
  the player hasn't featured in a home (or away) game yet this season.

This relies on `was_home`, `bonus`, `defensive_contribution`, and
`clean_sheet` being present in each gameweek's history row, which
`fpl_scraper.py` now stores per-gameweek (see `player_gameweeks` in
`database.py`). Re-run `fpl_scraper.py` after upgrading if you're working
from an older copy of `epl.db`, since the schema changed.

## 5. Fixture difficulty ticker (xG-based, all teams, automatic)

The "Fixtures" tab in the dashboard shows attacking and defensive fixture
tickers, same as before, but now rated on **xG instead of actual goals** —
`GET /api/teams/xg-ratings` computes this for every team from data you
already have:

- A team's attacking rating comes from their own players' **xG**, summed
  per gameweek they played.
- A team's defensive rating comes from the **opponent's xG** in that same
  gameweek — by definition, the chances a team faced are the chances that
  generate the opponent's attacking xG, so there's no separate "xGC" field
  to source; it's just a lookup via each gameweek's `opponent_team_id`.
- Home and away are tracked separately, same 1-5 bands as before (see
  `backend/ratings.py`), just fed by xG/game instead of goals/game.

This needs two new pieces of data to work: `player_gameweeks.opponent_team_id`
(added to the schema — tells you who a team played each gameweek, so their
xG-conceded can be looked up) and a new `fixtures` table (upcoming
matchups, pulled from `fantasy.premierleague.com/api/fixtures/`, needed so
the ticker knows what's coming up, not just what already happened).
`fpl_scraper.py` now pulls both automatically — nothing extra to run.

**Why xG over actual goals:** a team's goals-per-game over a handful of
early-season games is noisy — one fluky scoreline swings it a lot. xG
reflects chance quality more than finishing luck, which tends to be more
predictive going forward. The bands themselves are a judgment call reused
from the goals-based version (there's no "official" xG banding) — worth
retuning in `ratings.py` if your own testing suggests different cutoffs.

## 6. Cup/European fixtures and bookmaker odds

**Cup and European fixtures** (grey chips in the ticker): there's no free
API that reliably maps Champions League / Europa / Conference League / FA
Cup / Carabao Cup fixtures onto the same team IDs, and these draws happen
in occasional batches rather than continuously, so this is maintained by
hand in `backend/cup_fixtures.py`. Add an entry whenever a new round is
confirmed, then run:
```bash
cd backend && python load_cup_fixtures.py
```
This is also wired into the GitHub Actions workflow, so once you've
committed an update to `cup_fixtures.py`, the next scheduled run picks it
up automatically — you don't need to run it manually except to preview
locally. These fixtures are shown unrated (grey) since they're a different
competition, not folded into the xG difficulty system — the point is just
visibility into midweek fixture congestion.

**Bookmaker odds** (win % / BTTS % on each ticker chip): pulled from
[The Odds API](https://odds-api.io) (free tier, 500 requests/day, no card
required — sign up and grab a key). Setup:
```bash
export ODDS_API_KEY="your_key_here"
cd backend && python odds_scraper.py
```
For the automated version, add `ODDS_API_KEY` as a GitHub Actions secret
(repo → Settings → Secrets and variables → Actions) — the workflow already
references it and skips gracefully if it's not set.

One honest limitation: **team matching is fuzzy** (odds providers use
names like "Liverpool FC" where we store "Liverpool") —
`odds_scraper.py` uses approximate string matching, which works for
standard club names but is worth spot-checking after your first run.

(We dropped clean-sheet % from this — it wasn't a documented market on
the free tier, so it was always coming back empty. Win % and BTTS % are
the two signals that reliably populate.)

## 7. Extending it

- Add fixture difficulty / upcoming opponent to the players table (FPL's
  `bootstrap-static` already includes fixtures — `fpl_scraper.py` doesn't
  pull them yet, but the data's right there).
- Add a player detail page using `/api/players/{id}/gameweeks` — the
  backend already exposes full gameweek-by-gameweek history.
- Add team-level xG scraping from FBref the same way `fbref_scraper.py`
  does it for players (there's a squad-level table on the same pages).
