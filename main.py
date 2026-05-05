import math
import os
import time
import json
import random
import requests
import threading
import netrc
from datetime import date as _date, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS

# =========================================================
# 🚀 APP INIT & ENV CHECK
# =========================================================
app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
if not API_KEY:
    raise RuntimeError("CRITICAL: FOOTBALL_API_KEY environment variable not set!")

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

COMPETITIONS = ["CL", "PL", "PD", "BL1", "SA", "FL1", "ELC", "DED", "PPL", "BSA"]

# League-specific average goals (used for atk/def normalisation)
# Source: historical averages per competition
LEAGUE_AVG_GOALS = {
    "BL1": 1.55,  # Bundesliga — high scoring
    "PL":  1.35,  # Premier League
    "PD":  1.25,  # La Liga — lower scoring
    "SA":  1.25,  # Serie A
    "FL1": 1.20,  # Ligue 1
    "CL":  1.30,  # Champions League
    "ELC": 1.40,  # Championship
    "DED": 1.30,  # Eredivisie
    "PPL": 1.25,  # Primeira Liga
    "BSA": 1.35,  # Brasileirao
}
DEFAULT_LEAGUE_AVG = 1.30

# Per-league home advantage multiplier (calibrated from historical data)
# 1.10 = 10% more goals scored at home vs away, on average
LEAGUE_HOME_ADV = {
    "BL1": 1.08,  # Bundesliga — home advantage weakened post-COVID
    "PL":  1.07,  # Premier League — lowest home advantage in top 5
    "PD":  1.10,  # La Liga
    "SA":  1.10,  # Serie A
    "FL1": 1.09,  # Ligue 1
    "CL":  1.08,  # Champions League — neutral-ish, elite away teams
    "ELC": 1.12,  # Championship — strong home crowd effect
    "DED": 1.10,  # Eredivisie
    "PPL": 1.10,  # Primeira Liga
    "BSA": 1.13,  # Brasileirao — strong home advantage
}
DEFAULT_HOME_ADV = 1.10

# =========================================================
# 🎲 ODDS API (The Odds API — the-odds-api.com)
# =========================================================
ODDS_API_KEY  = os.getenv("ODDS_API_KEY")   # add to Render env vars
ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"

# Map football-data.org competition codes → The Odds API sport keys
ODDS_SPORT_KEYS = {
    "PL":  "soccer_england_league1",
    "BL1": "soccer_germany_bundesliga",
    "PD":  "soccer_spain_la_liga",
    "SA":  "soccer_italy_serie_a",
    "FL1": "soccer_france_ligue_one",
    "CL":  "soccer_uefa_champions_league",
    "ELC": "soccer_england_league2",
    "DED": "soccer_netherlands_eredivisie",
    "PPL": "soccer_portugal_primeira_liga",
    "BSA": "soccer_brazil_campeonato",
}

# Bayesian blending weights
# 40% Poisson (your model) + 60% Market (bookmaker consensus)
# Market gets higher weight because it encodes team news and sharp money
# your model cannot access.
WEIGHT_POISSON = 0.40
WEIGHT_MARKET  = 0.60

# Odds cache: keyed by sport_key, stores list of events with odds
# Refreshed every 3 hours to stay within free tier (500 req/month)
odds_cache: dict = {}
ODDS_EXPIRY = 10800  # 3 hours
fetch_lock    = threading.Lock()
standings_cache: dict = {}
form_cache:     dict = {}
fixtures_store: dict = {}

CACHE_FILE        = "cache.json"
CACHE_MAX_AGE     = 3600   # 1 hour for fixtures
STANDINGS_EXPIRY  = 86400  # 24 hours
FORM_EXPIRY       = 3600   # 1 hour

# =========================================================
# 💾 DISK CACHE
# =========================================================
def load_cache_from_disk() -> bool:
    global fixtures_store
    if not os.path.exists(CACHE_FILE):
        return False
    try:
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
        age = time.time() - data.get("timestamp", 0)
        if age > CACHE_MAX_AGE * 6:          # discard if very stale (>6h)
            print(f"[DISK] Cache too old ({age/3600:.1f}h), ignoring")
            return False
        fixtures_store = data.get("fixtures", {})
        print(f"[DISK] Loaded cache ({age/60:.0f}m old). Dates: {list(fixtures_store.keys())}")
        return bool(fixtures_store)
    except Exception as e:
        print(f"[DISK] Load error: {e}")
        return False

def save_cache_to_disk(fixtures: dict):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"timestamp": time.time(), "fixtures": fixtures}, f)
        print("[DISK] Cache saved.")
    except Exception as e:
        print(f"[DISK] Save error: {e}")

def get_cache_age() -> float:
    if not os.path.exists(CACHE_FILE):
        return float("inf")
    try:
        with open(CACHE_FILE, "r") as f:
            return time.time() - json.load(f).get("timestamp", 0)
    except:
        return float("inf")

# =========================================================
# 🎲 ODDS ENGINE (The Odds API — Bayesian prior source)
# =========================================================
def get_market_odds(comp: str) -> list:
    """
    Fetch pre-match 1X2 odds for a competition from The Odds API.
    Returns a list of event dicts each containing:
      home_team, away_team, home_odds, draw_odds, away_odds
    Uses in-memory cache with 3-hour expiry to protect free tier quota.
    Falls back to empty list if API key missing or request fails.
    """
    if not ODDS_API_KEY:
        return []

    sport_key = ODDS_SPORT_KEYS.get(comp)
    if not sport_key:
        return []

    now    = time.time()
    cached = odds_cache.get(comp)
    if cached and now - cached["t"] < ODDS_EXPIRY:
        return cached["d"]

    try:
        r = requests.get(
            f"{ODDS_API_BASE}/{sport_key}/odds",
            params={
                "apiKey":     ODDS_API_KEY,
                "regions":    "eu",
                # Fetch all four market types in a single API call
                # h2h = 1X2, totals = Over/Under, spreads = Asian Handicap,
                # double_chance = 1X / X2 / 12
                "markets":    "h2h,totals,spreads,double_chance",
                "oddsFormat": "decimal",
            },
            timeout=10
        )

        if r.status_code == 401:
            print("[ODDS API] Invalid API key")
            return []
        if r.status_code == 422:
            print(f"[ODDS API] Sport key not supported: {sport_key}")
            return []
        if r.status_code != 200:
            print(f"[ODDS API] {comp} returned {r.status_code}")
            return []

        events    = r.json()
        remaining = r.headers.get("x-requests-remaining", "?")
        print(f"[ODDS API] {comp} — {len(events)} events. Markets: h2h+totals+spreads+dc. Quota left: {remaining}")

        parsed = []
        for ev in events:
            try:
                home_name = ev["home_team"]
                away_name = ev["away_team"]

                # Collect odds per market type across all bookmakers
                h2h_h, h2h_d, h2h_a         = [], [], []
                dc_1x, dc_x2, dc_12          = [], [], []
                totals: dict                 = {}   # key: "over_X.X" / "under_X.X"
                spreads_h: dict              = {}   # key: handicap value (float)
                spreads_a: dict              = {}

                def median(lst):
                    if not lst: return None
                    s = sorted(lst); n = len(s)
                    return s[n//2] if n % 2 else (s[n//2-1] + s[n//2]) / 2

                for bookie in ev.get("bookmakers", []):
                    for mkt in bookie.get("markets", []):
                        key = mkt["key"]

                        if key == "h2h":
                            for o in mkt["outcomes"]:
                                if o["name"] == home_name:   h2h_h.append(o["price"])
                                elif o["name"] == away_name: h2h_a.append(o["price"])
                                else:                        h2h_d.append(o["price"])

                        elif key == "double_chance":
                            for o in mkt["outcomes"]:
                                n = o["name"]
                                if n in (home_name + "/" + "Draw", "1X"):
                                    dc_1x.append(o["price"])
                                elif n in ("Draw/" + away_name, "X2"):
                                    dc_x2.append(o["price"])
                                elif n in (home_name + "/" + away_name, "12"):
                                    dc_12.append(o["price"])
                                # Fallback: check partial
                                elif home_name[:4] in n and "Draw" in n:
                                    dc_1x.append(o["price"])
                                elif away_name[:4] in n and "Draw" in n:
                                    dc_x2.append(o["price"])

                        elif key == "totals":
                            for o in mkt["outcomes"]:
                                pt = o.get("point", "")
                                mk = f"{o['name'].lower()}_{pt}"
                                totals.setdefault(mk, []).append(o["price"])

                        elif key == "spreads":
                            for o in mkt["outcomes"]:
                                pt = o.get("point", 0)
                                if o["name"] == home_name:
                                    spreads_h.setdefault(pt, []).append(o["price"])
                                else:
                                    spreads_a.setdefault(pt, []).append(o["price"])

                entry = {
                    "home_team":  home_name,
                    "away_team":  away_name,
                    "commence":   ev.get("commence_time", ""),
                    # 1X2
                    "home_odds":  median(h2h_h),
                    "draw_odds":  median(h2h_d),
                    "away_odds":  median(h2h_a),
                    # Double Chance
                    "dc_1x_odds": median(dc_1x),
                    "dc_x2_odds": median(dc_x2),
                    "dc_12_odds": median(dc_12),
                    # Totals — keep all lines as dict
                    "totals":     {k: median(v) for k, v in totals.items()},
                    # Spreads — keep all lines as dict
                    "spreads_h":  {str(k): median(v) for k, v in spreads_h.items()},
                    "spreads_a":  {str(k): median(v) for k, v in spreads_a.items()},
                }

                if entry["home_odds"]:
                    parsed.append(entry)

            except Exception as e:
                print(f"[ODDS API] Parse error for event: {e}")
                continue

        odds_cache[comp] = {"t": now, "d": parsed}
        return parsed

    except Exception as e:
        print(f"[ODDS API ERROR] {comp}: {e}")
        return []


def find_match_odds(events: list, home: str, away: str) -> dict | None:
    """
    Find market odds for a specific fixture from the events list.
    Uses fuzzy name matching since team names differ between APIs.
    Returns dict with home_odds, draw_odds, away_odds or None if not found.
    """
    if not events:
        return None

    home_lower = home.lower().strip()
    away_lower = away.lower().strip()

    for ev in events:
        ev_home = ev["home_team"].lower().strip()
        ev_away = ev["away_team"].lower().strip()

        # Exact match first
        if ev_home == home_lower and ev_away == away_lower:
            return ev

        # Partial match — one team name contains the other
        # handles "Man United" vs "Manchester United" etc.
        if (home_lower in ev_home or ev_home in home_lower) and \
           (away_lower in ev_away or ev_away in away_lower):
            return ev

    return None


def calc_edge(fair_odds: float, api_odds: float) -> float:
    """
    Calculate value edge: how much the bookie price exceeds the fair price.
    Edge > 0 = value exists. Edge > 0.05 = meaningful value (5% threshold).
    Returns 0.0 if no api_odds available.
    """
    if not api_odds or api_odds <= 1.0 or not fair_odds or fair_odds <= 1.0:
        return 0.0
    return round((api_odds / fair_odds) - 1, 4)


def bayesian_blend(p_poisson: float, market_odds: float) -> float:
    """
    Blend Poisson probability with market-implied probability.
    Market probability is derived by removing the bookmaker margin (overround)
    and then weighting 40% Poisson + 60% Market.

    Args:
        p_poisson:    Raw Poisson probability (0–1)
        market_odds:  Decimal odds from bookmaker consensus

    Returns:
        Blended probability (0–1), normalised by caller
    """
    if not market_odds or market_odds <= 1.0:
        return p_poisson

    # Raw market implied probability (includes overround)
    raw_market_p = 1.0 / market_odds

    # We normalise across all three outcomes in the caller to remove overround
    return (WEIGHT_POISSON * p_poisson) + (WEIGHT_MARKET * raw_market_p)

# =========================================================
# 📊 POISSON MATH
# =========================================================
def poisson(k: int, lam: float) -> float:
    """Poisson PMF. lam is clamped to prevent extreme predictions."""
    lam = max(min(float(lam or DEFAULT_LEAGUE_AVG), 3.5), 0.3)
    try:
        return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)
    except (OverflowError, ValueError):
        return 0.0

# =========================================================
# 📈 STANDINGS ENGINE (home/away splits)
# =========================================================
def _parse_table(table: list) -> dict:
    """Parse a standings table into a dict keyed by team_id string."""
    return {
        str(t["team"]["id"]): {
            "rank":   t["position"],
            "played": max(t["playedGames"], 1),
            "gf":     t["goalsFor"]     / max(t["playedGames"], 1),
            "ga":     t["goalsAgainst"] / max(t["playedGames"], 1),
            "pts":    t["points"],
        }
        for t in table
    }

def get_standings(code: str) -> dict:
    """
    Fetch league standings with HOME and AWAY splits.
    Returns dict with three keys: 'total', 'home', 'away',
    each a dict keyed by team_id string.
    Falls back to total-only if splits are unavailable.
    """
    now = time.time()
    cached = standings_cache.get(code)
    if cached and now - cached["t"] < STANDINGS_EXPIRY:
        return cached["d"]

    try:
        r = requests.get(
            f"{BASE_URL}/competitions/{code}/standings",
            headers=HEADERS, timeout=10
        )
        if r.status_code == 429:
            print(f"[RATE LIMIT] standings {code} — using stale cache")
            return cached["d"] if cached else {"total": {}, "home": {}, "away": {}}
        if r.status_code != 200:
            print(f"[STANDINGS] {code} returned {r.status_code}")
            return cached["d"] if cached else {"total": {}, "home": {}, "away": {}}

        data = r.json()
        standings = data["standings"]

        # Pull all three table types — HOME and AWAY give venue-specific stats
        def get_table(stype):
            try:
                return _parse_table(
                    next(s for s in standings if s["type"] == stype)["table"]
                )
            except StopIteration:
                return {}

        out = {
            "total": get_table("TOTAL"),
            "home":  get_table("HOME"),
            "away":  get_table("AWAY"),
        }

        standings_cache[code] = {"t": now, "d": out}
        return out

    except Exception as e:
        print(f"[STANDINGS ERROR] {code}: {e}")
        return cached["d"] if cached else {"total": {}, "home": {}, "away": {}}

# =========================================================
# ⚽ FORM ENGINE (venue-specific, weighted, split atk/def)
# =========================================================
# Recent-game weights: most recent match gets highest weight.
FORM_WEIGHTS = [1.0, 0.85, 0.70, 0.55, 0.40]   # index 0 = most recent

def get_detailed_form(team_id: int, league_avg: float = DEFAULT_LEAGUE_AVG, venue: str = ""):
    """
    Returns (atk_mult, def_mult, form_string).

    venue=""     → all matches (fallback)
    venue="HOME" → home matches only  (use for the home team)
    venue="AWAY" → away matches only  (use for the away team)

    atk_mult > 1.0 → team scores more than league average at this venue
    def_mult > 1.0 → team concedes less than league average at this venue
    Both clamped to [0.90, 1.10] — form is a recency adjustment, not a base rate.
    """
    now = time.time()
    cache_key = (team_id, venue)
    cached = form_cache.get(cache_key)
    if cached and now - cached["t"] < FORM_EXPIRY:
        return cached["atk"], cached["def"], cached["s"]

    # Build URL — venue filter is optional
    url = f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5"
    if venue in ("HOME", "AWAY"):
        url += f"&venue={venue}"

    try:
        r = requests.get(url, headers=HEADERS, timeout=10)

        if r.status_code == 429:
            print(f"[RATE LIMIT] form team {team_id} {venue} — using stale cache")
            if cached:
                return cached["atk"], cached["def"], cached["s"]
            return 1.0, 1.0, "???"
        if r.status_code != 200:
            return 1.0, 1.0, "???"

        matches = r.json().get("matches", [])
        history  = []
        w_gf = w_ga = w_total = 0.0

        for idx, m in enumerate(matches):
            score = m["score"]["fullTime"]
            if score["home"] is None:
                continue
            hs, aw  = score["home"], score["away"]
            is_home = m["homeTeam"]["id"] == team_id
            gf, ga  = (hs, aw) if is_home else (aw, hs)

            w        = FORM_WEIGHTS[idx] if idx < len(FORM_WEIGHTS) else 0.30
            w_gf    += gf * w
            w_ga    += ga * w
            w_total += w

            history.append("W" if gf > ga else ("D" if gf == ga else "L"))

        if w_total == 0 or not history:
            return 1.0, 1.0, "???"

        avg_gf = w_gf / w_total
        avg_ga = max(w_ga / w_total, 0.1)  # floor prevents div explosion on clean-sheet runs

        # Form multipliers: recency adjustment on top of standings base (±10% max)
        atk  = avg_gf / league_avg if league_avg > 0 else 1.0
        def_ = league_avg / avg_ga

        atk  = max(min(atk,  1.10), 0.90)
        def_ = max(min(def_, 1.10), 0.90)

        form_str = "".join(history)
        form_cache[cache_key] = {"t": now, "atk": atk, "def": def_, "s": form_str}
        return atk, def_, form_str

    except Exception as e:
        print(f"[FORM ERROR] team {team_id} {venue}: {e}")
        if cached:
            return cached["atk"], cached["def"], cached["s"]
        return 1.0, 1.0, "???"

# =========================================================
# ⚡ FIXTURE ENGINE (atomic swap + disk persistence)
# =========================================================
def fetch_all_fixtures() -> bool:
    global fixtures_store

    # Skip fetch if in-memory store AND disk cache are both fresh
    if fixtures_store and get_cache_age() < CACHE_MAX_AGE:
        print("[CACHE] Fixtures fresh, skipping fetch")
        return True

    if not fetch_lock.acquire(blocking=False):
        print("[CACHE] Fetch already in progress, skipping")
        return bool(fixtures_store)

    try:
        print("[CACHE] Fetching fixtures from API...")
        now        = time.time()
        start_date = time.strftime("%Y-%m-%d", time.gmtime(now - 86400))
        end_date   = time.strftime("%Y-%m-%d", time.gmtime(now + 5 * 86400))

        r = requests.get(
            f"{BASE_URL}/matches",
            headers=HEADERS,
            params={"dateFrom": start_date, "dateTo": end_date},
            timeout=25
        )

        if r.status_code == 429:
            print("[RATE LIMIT] fixtures — keeping existing store")
            return bool(fixtures_store)
        if r.status_code != 200:
            print(f"[FIXTURE] API returned {r.status_code}: {r.text[:120]}")
            return False

        temp: dict = {}
        for m in r.json().get("matches", []):
            comp      = m.get("competition", {})
            comp_code = comp.get("code")
            if comp_code not in COMPETITIONS:
                continue

            date  = m.get("utcDate", "")[:10]
            h_t   = m.get("homeTeam", {})
            a_t   = m.get("awayTeam", {})

            if not h_t.get("id") or not a_t.get("id"):
                continue

            temp.setdefault(date, []).append({
                "home":    h_t.get("name", "Unknown"),
                "home_id": h_t["id"],
                "away":    a_t.get("name", "Unknown"),
                "away_id": a_t["id"],
                "comp":    comp_code,
                "league":  comp.get("name", comp_code),
                "kickoff": m.get("utcDate", ""),   # full ISO datetime e.g. "2026-05-03T14:00:00Z"
            })

        fixtures_store = temp   # atomic swap
        save_cache_to_disk(temp)
        print(f"[CACHE] Loaded {sum(len(v) for v in temp.values())} matches across {len(temp)} days")
        return True

    except Exception as e:
        print(f"[FIXTURE ERROR] {e}")
        return False
    finally:
        fetch_lock.release()

# =========================================================
# ⚽ ROUTES
# =========================================================
@app.route("/fixtures")
def fixtures():
    date = request.args.get("date", "").split("T")[0]
    if not date:
        return jsonify([])

    # If memory store is empty, try disk then API
    if not fixtures_store:
        loaded = load_cache_from_disk()
        if not loaded:
            fetch_all_fixtures()
        if not fixtures_store:
            return jsonify({"status": "loading", "message": "Server is syncing match data..."})

    result = fixtures_store.get(date)
    if result is None:
        return jsonify({"status": "no_games", "data": [], "message": "No matches scheduled for this date."})

    return jsonify(result)


@app.route("/predict", methods=["POST"])
def predict():
    req = request.json
    if not req or "comp" not in req or "home_id" not in req or "away_id" not in req:
        return jsonify({"error": "Invalid request body"}), 400

    try:
        comp  = req["comp"]
        h_id  = req["home_id"]
        a_id  = req["away_id"]

        league_avg = LEAGUE_AVG_GOALS.get(comp, DEFAULT_LEAGUE_AVG)
        home_adv   = LEAGUE_HOME_ADV.get(comp, DEFAULT_HOME_ADV)

        # --- Standings (venue-specific) ---
        # Use home stats for the home team, away stats for the away team.
        # Fall back to total if the split table is empty (e.g. early season).
        all_stats  = get_standings(comp)
        home_stats = all_stats.get("home", {})
        away_stats = all_stats.get("away", {})
        total_stats = all_stats.get("total", {})

        fallback_h = {"gf": 1.2, "ga": 1.2, "rank": "N/A"}
        fallback_a = {"gf": 1.0, "ga": 1.3, "rank": "N/A"}

        # Home team — use home-venue stats; rank comes from total table
        h_venue = home_stats.get(str(h_id)) or total_stats.get(str(h_id), fallback_h)
        h_rank  = total_stats.get(str(h_id), fallback_h).get("rank", "N/A")

        # Away team — use away-venue stats; rank comes from total table
        a_venue = away_stats.get(str(a_id)) or total_stats.get(str(a_id), fallback_a)
        a_rank  = total_stats.get(str(a_id), fallback_a).get("rank", "N/A")

        # --- Form (venue-specific, weighted, split atk/def) ---
        # Home team's recent HOME matches, away team's recent AWAY matches
        h_atk, h_def, h_form = get_detailed_form(h_id, league_avg, venue="HOME")
        a_atk, a_def, a_form = get_detailed_form(a_id, league_avg, venue="AWAY")

        # --- Lambda Calculation ---
        # Base rate from venue-specific standings (long-run home/away ability).
        # Form multipliers apply a ±10% recency adjustment for recent venue momentum.
        # home_adv is per-league and already baked into home_stats gf/ga — we apply
        # a residual multiplier of sqrt(home_adv) to avoid double-counting.
        residual_adv = math.sqrt(home_adv)
        h_raw = h_venue["gf"] * (a_venue["ga"] / league_avg) * h_atk * (1.0 / a_def) * residual_adv
        a_raw = a_venue["gf"] * (h_venue["ga"] / league_avg) * a_atk * (1.0 / h_def)

        # Clamp λ to realistic range; log when extreme values hit the ceiling
        h_lam = max(min(h_raw, 3.2), 0.35)
        a_lam = max(min(a_raw, 3.2), 0.35)
        if h_raw != h_lam: print(f"[CLAMP] h_lam {h_raw:.3f}→{h_lam}")
        if a_raw != a_lam: print(f"[CLAMP] a_lam {a_raw:.3f}→{a_lam}")

        # --- Score Matrix (0–6 goals) ---
        p_h = p_d = p_a = 0.0
        p_btts   = 0.0
        p_over15 = 0.0
        p_over25 = 0.0
        p_over35 = 0.0
        matrix = {}

        # Asian Handicap accumulators
        # Half-ball lines: pure win/lose (no push)
        # Full-ball lines: push on exact margin
        # Quarter lines: stake split between two adjacent lines
        ah = {
            "hm15": 0.0,  # Home -1.5 (win by 2+)
            "hm1":  0.0,  # Home -1   (win by 2+ = win, win by 1 = push, else lose)
            "hm05": 0.0,  # Home -0.5 (win by 1+ = win, else lose)
            "h0":   0.0,  # Home 0    (win = win, draw = push, lose = lose)
            "hp05": 0.0,  # Home +0.5 (win or draw = win, lose = lose)
            "hp1":  0.0,  # Home +1   (win or draw = win, lose by 1 = push, lose by 2+ = lose)
            "hp15": 0.0,  # Home +1.5 (win, draw, or lose by 1 = win)
        }

        # Asian Total accumulators
        at = {
            "o05":  0.0,  # Over 0.5
            "o15":  0.0,  # Over 1.5
            "o25":  0.0,  # Over 2.5
            "o35":  0.0,  # Over 3.5
            "o45":  0.0,  # Over 4.5
            "u05":  0.0,  # Under 0.5
            "u15":  0.0,  # Under 1.5
            "u25":  0.0,  # Under 2.5
            "u35":  0.0,  # Under 3.5
        }

        for i in range(7):
            for j in range(7):
                p = poisson(i, h_lam) * poisson(j, a_lam)
                matrix[(i, j)] = p
                diff  = i - j
                total = i + j

                # 1X2
                if   i > j: p_h += p
                elif i == j: p_d += p
                else:        p_a += p

                # Goals markets
                if i > 0 and j > 0: p_btts  += p
                if total > 1: p_over15 += p
                if total > 2: p_over25 += p
                if total > 3: p_over35 += p

                # Asian Handicap (home perspective)
                # -1.5: home wins by 2+ goals
                if diff >= 2:  ah["hm15"] += p
                # -1: home wins by 2+ = full win; by 1 = push (×0.5); else = lose
                if diff >= 2:  ah["hm1"]  += p
                elif diff == 1: ah["hm1"] += p * 0.5   # push = half stake back
                # -0.5: home wins by 1+ = full win
                if diff >= 1:  ah["hm05"] += p
                # 0 (level ball): home wins = win; draw = push; away wins = lose
                if diff > 0:   ah["h0"]   += p
                elif diff == 0: ah["h0"]  += p * 0.5   # push
                # +0.5: home wins or draws = win
                if diff >= 0:  ah["hp05"] += p
                # +1: home wins or draws = win; loses by 1 = push; loses by 2+ = lose
                if diff >= 0:  ah["hp1"]  += p
                elif diff == -1: ah["hp1"] += p * 0.5  # push
                # +1.5: home wins, draws, or loses by 1 = win
                if diff >= -1: ah["hp15"] += p

                # Asian Totals
                if total > 0:  at["o05"]  += p
                if total > 1:  at["o15"]  += p
                if total > 2:  at["o25"]  += p
                if total > 3:  at["o35"]  += p
                if total > 4:  at["o45"]  += p
                if total < 1:  at["u05"]  += p
                if total < 2:  at["u15"]  += p
                if total < 3:  at["u25"]  += p
                if total < 4:  at["u35"]  += p

        # Quarter-ball Asian lines (stake split between two adjacent lines)
        # e.g. -0.75 = 50% on -0.5 and 50% on -1
        ah["hm075"] = (ah["hm05"] + ah["hm1"])  / 2
        ah["hm025"] = (ah["hm05"] + ah["h0"])   / 2
        ah["hp025"] = (ah["h0"]   + ah["hp05"]) / 2
        ah["hp075"] = (ah["hp05"] + ah["hp1"])  / 2
        ah["hp125"] = (ah["hp1"]  + ah["hp15"]) / 2

        # Asian Total quarter lines
        at["o175"] = (at["o15"] + at["o25"]) / 2
        at["o225"] = (at["o15"] + at["o25"]) / 2   # same split, shown as O2.25
        at["o275"] = (at["o25"] + at["o35"]) / 2
        at["o325"] = (at["o25"] + at["o35"]) / 2   # same split, shown as O3.25

        # Double Chance (pure maths — no Bayesian blend needed, derived from blended 1X2)
        # Computed after Bayesian blend below so they use final probabilities

        # --- Normalise raw Poisson 1X2 ---
        tot   = p_h + p_d + p_a
        p_h_raw = p_h / tot
        p_d_raw = p_d / tot
        p_a_raw = p_a / tot

        # --- Bayesian Blend with Market Odds ---
        # Fetch bookmaker consensus odds for this competition.
        # If market data is available for this fixture, blend 40% Poisson + 60% Market.
        # Goals markets (BTTS, O/U) are NOT blended — Poisson is more reliable there.
        # If no market data found, fall back to pure Poisson.
        market_events = get_market_odds(comp)
        match_odds    = find_match_odds(market_events, req.get("home", ""), req.get("away", ""))

        if match_odds:
            # Blend each outcome
            b_h = bayesian_blend(p_h_raw, match_odds["home_odds"])
            b_d = bayesian_blend(p_d_raw, match_odds["draw_odds"])
            b_a = bayesian_blend(p_a_raw, match_odds["away_odds"])

            # Normalise blended probabilities to sum to 1.0
            # (removes bookmaker overround embedded in market priors)
            b_tot = b_h + b_d + b_a
            p_h_final = b_h / b_tot
            p_d_final = b_d / b_tot
            p_a_final = b_a / b_tot

            blended = True
            print(f"[BAYES] {req.get('home')} vs {req.get('away')} — "
                  f"Poisson: H{p_h_raw:.2f}/D{p_d_raw:.2f}/A{p_a_raw:.2f} → "
                  f"Blended: H{p_h_final:.2f}/D{p_d_final:.2f}/A{p_a_final:.2f}")
        else:
            # No market data — use pure Poisson
            p_h_final = p_h_raw
            p_d_final = p_d_raw
            p_a_final = p_a_raw
            blended   = False
            print(f"[BAYES] No market odds found for {req.get('home')} vs {req.get('away')} — using Poisson only")

        # --- Scoreline: outcome-consistent if model has clear conviction ---
        sorted_probs = sorted([p_h_final, p_d_final, p_a_final], reverse=True)
        lead = sorted_probs[0] - sorted_probs[1]

        if lead >= 0.05:
            if p_h_final >= p_d_final and p_h_final >= p_a_final:
                valid = lambda i, j: i > j
            elif p_a_final >= p_h_final and p_a_final >= p_d_final:
                valid = lambda i, j: j > i
            else:
                valid = lambda i, j: i == j
        else:
            valid = lambda i, j: True

        best  = "1-1"
        max_p = -1.0
        for (i, j), p in matrix.items():
            if valid(i, j) and p > max_p:
                max_p, best = p, f"{i}-{j}"

        h_pct = round(p_h_final * 100)
        d_pct = round(p_d_final * 100)
        a_pct = 100 - h_pct - d_pct

        # --- Fair Decimal Odds helper ---
        def fair_odds(p: float) -> float:
            return round(1 / p, 2) if p > 0.04 else 25.0

        # --- Double Chance ---
        # 1X = home win or draw, X2 = draw or away win, 12 = home or away (no draw)
        p_1x = p_h_final + p_d_final
        p_x2 = p_d_final + p_a_final
        p_12 = p_h_final + p_a_final

        # --- Asian Handicap (from raw Poisson matrix, not blended) ---
        # AH uses the score matrix directly since it deals with goal margins.
        # Lines: home perspective. Positive = home giving goals, negative = receiving.
        # For each line we compute P(home covers), P(push), P(away covers).
        # Quarter lines split stake: e.g. -0.75 = half on -0.5, half on -1.

        def ah_prob(handicap: float) -> tuple:
            """
            Returns (p_home_cover, p_push, p_away_cover) for a given AH line.
            handicap > 0 means home team receives goals (e.g. +0.5 = home gets 0.5 start).
            handicap < 0 means home team gives goals (e.g. -1 = home must win by 2+).
            Quarter lines handled by splitting across two adjacent whole/half lines.
            """
            # Quarter lines: split into two adjacent lines
            frac = handicap % 0.5
            if abs(frac) == 0.25:
                # e.g. -0.75 = average of -0.5 and -1.0
                low  = handicap - 0.25
                high = handicap + 0.25
                ph_l, pp_l, pa_l = ah_prob(low)
                ph_h, pp_h, pa_h = ah_prob(high)
                return (
                    (ph_l + ph_h) / 2,
                    (pp_l + pp_h) / 2,
                    (pa_l + pa_h) / 2,
                )

            p_home_cover = 0.0
            p_push       = 0.0
            p_away_cover = 0.0

            for (i, j), p in matrix.items():
                margin = i - j  # positive = home winning by margin goals
                adjusted = margin + handicap  # home's effective margin with handicap applied

                if handicap % 1 == 0:
                    # Whole-number line: push is possible
                    if adjusted > 0:   p_home_cover += p
                    elif adjusted == 0: p_push       += p
                    else:              p_away_cover += p
                else:
                    # Half-line: no push possible
                    if adjusted > 0:   p_home_cover += p
                    else:              p_away_cover += p

            return p_home_cover, p_push, p_away_cover

        def ah_fair_odds(p_cover: float, p_push: float) -> float:
            """
            Fair odds for AH accounting for push (stake returned on push).
            Effective probability = p_cover / (1 - p_push)
            """
            effective_p = p_cover / (1 - p_push) if p_push < 1 else 0
            return fair_odds(effective_p)

        # Compute standard AH lines (home perspective)
        ah_lines = [-1.5, -1.0, -0.75, -0.5, -0.25, 0.0,
                     0.25,  0.5,  0.75,  1.0,  1.25, 1.5]
        ah_results = {}
        for line in ah_lines:
            ph_c, pp, pa_c = ah_prob(line)
            key = f"ah_{line:+.2f}".replace(".00", "").replace("+", "p").replace("-", "m").replace(".", "")
            ah_results[key] = {
                "line":        line,
                "home_cover":  round(ph_c * 100, 1),
                "push":        round(pp * 100, 1),
                "away_cover":  round(pa_c * 100, 1),
                "home_odds":   ah_fair_odds(ph_c, pp),
                "away_odds":   ah_fair_odds(pa_c, pp),
            }

        # --- Asian Totals (quarter lines) ---
        def at_prob(line: float) -> tuple:
            """
            Returns (p_over, p_push, p_under) for Asian Total line.
            Quarter lines split stake across two adjacent lines.
            """
            frac = line % 0.5
            if abs(frac) == 0.25:
                low  = line - 0.25
                high = line + 0.25
                po_l, pp_l, pu_l = at_prob(low)
                po_h, pp_h, pu_h = at_prob(high)
                return (
                    (po_l + po_h) / 2,
                    (pp_l + pp_h) / 2,
                    (pu_l + pu_h) / 2,
                )

            p_over = p_push = p_under = 0.0
            for (i, j), p in matrix.items():
                goals = i + j
                if line % 1 == 0:
                    # Whole line — push possible
                    if goals > line:   p_over  += p
                    elif goals == line: p_push  += p
                    else:              p_under += p
                else:
                    # Half line — no push
                    if goals > line:   p_over  += p
                    else:              p_under += p

            return p_over, p_push, p_under

        at_lines = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0,
                    2.25, 2.5, 2.75, 3.0, 3.25, 3.5]
        at_results = {}
        for line in at_lines:
            po, pp, pu = at_prob(line)
            key = f"at_{line:.2f}".replace(".", "")
            at_results[key] = {
                "line":       line,
                "over_pct":  round(po * 100, 1),
                "push_pct":  round(pp * 100, 1),
                "under_pct": round(pu * 100, 1),
                "over_odds":  ah_fair_odds(po, pp),
                "under_odds": ah_fair_odds(pu, pp),
            }

        # --- Pull real bookie odds from Odds API ---
        mo        = match_odds or {}
        totals    = mo.get("totals",    {})
        spreads_h = mo.get("spreads_h", {})

        api_home    = mo.get("home_odds")
        api_draw    = mo.get("draw_odds")
        api_away    = mo.get("away_odds")
        api_dc_1x   = mo.get("dc_1x_odds")
        api_dc_x2   = mo.get("dc_x2_odds")
        api_dc_12   = mo.get("dc_12_odds")
        api_o15     = totals.get("over_1.5")
        api_o25     = totals.get("over_2.5")
        api_o35     = totals.get("over_3.5")
        api_u25     = totals.get("under_2.5")
        api_ah_hm05 = spreads_h.get("-0.5")
        api_ah_hp05 = spreads_h.get("0.5")
        api_ah_hm15 = spreads_h.get("-1.5")
        api_ah_hp15 = spreads_h.get("1.5")

        # --- Edge per market ---
        fo_home  = fair_odds(p_h_final)
        fo_draw  = fair_odds(p_d_final)
        fo_away  = fair_odds(p_a_final)
        fo_dc_1x = fair_odds(p_1x)
        fo_dc_x2 = fair_odds(p_x2)
        fo_dc_12 = fair_odds(p_12)
        fo_o15   = fair_odds(p_over15)
        fo_o25   = fair_odds(p_over25)
        fo_o35   = fair_odds(p_over35)

        # AH key format produced by: f"ah_{line:+.2f}".replace(".00","").replace("+","p").replace("-","m").replace(".","")
        # -0.5 → "ahm050" → strip ".00" → no change → "ahm050" (but .50 → "50" not "050")
        # Correct: -0.5 → "ah_-0.50" → replace → "ahm050" ✓
        # AH key format verified: -0.5→"ah_m050", +0.5→"ah_p050", -1.5→"ah_m150", +1.5→"ah_p150"
        fo_ah_hm05 = ah_results.get("ah_m050", {}).get("home_odds", fair_odds(ah["hm05"]))
        fo_ah_hp05 = ah_results.get("ah_p050", {}).get("home_odds", fair_odds(ah["hp05"]))
        fo_ah_hm15 = ah_results.get("ah_m150", {}).get("home_odds", fair_odds(ah["hm15"]))
        fo_ah_hp15 = ah_results.get("ah_p150", {}).get("home_odds", fair_odds(ah["hp15"]))

        # --- Pull real bookie odds from Odds API ---
        # Smart fallback: if no real API price, display model's fair odds.
        # Edge is only computed against real API data — fallback odds produce edge=0.
        mo        = match_odds or {}
        totals    = mo.get("totals",    {})
        spreads_h = mo.get("spreads_h", {})

        # Raw API prices (None if not available)
        _api_home    = mo.get("home_odds")
        _api_draw    = mo.get("draw_odds")
        _api_away    = mo.get("away_odds")
        _api_dc_1x   = mo.get("dc_1x_odds")
        _api_dc_x2   = mo.get("dc_x2_odds")
        _api_dc_12   = mo.get("dc_12_odds")
        _api_o15     = totals.get("over_1.5")
        _api_o25     = totals.get("over_2.5")
        _api_o35     = totals.get("over_3.5")
        _api_ah_hm05 = spreads_h.get("-0.5")
        _api_ah_hp05 = spreads_h.get("0.5")
        _api_ah_hm15 = spreads_h.get("-1.5")
        _api_ah_hp15 = spreads_h.get("1.5")

        def api_or_fair(api_val, fair_val):
            """Return api_val if real bookie data, else fair_val so UI always shows a price."""
            return round(api_val, 2) if (api_val and api_val > 1.0) else fair_val

        # Display odds: real bookie where available, fair price as fallback
        api_home    = api_or_fair(_api_home,    fo_home)
        api_draw    = api_or_fair(_api_draw,    fo_draw)
        api_away    = api_or_fair(_api_away,    fo_away)
        api_dc_1x   = api_or_fair(_api_dc_1x,  fo_dc_1x)
        api_dc_x2   = api_or_fair(_api_dc_x2,  fo_dc_x2)
        api_dc_12   = api_or_fair(_api_dc_12,  fo_dc_12)
        api_o15     = api_or_fair(_api_o15,     fo_o15)
        api_o25     = api_or_fair(_api_o25,     fo_o25)
        api_o35     = api_or_fair(_api_o35,     fo_o35)
        api_ah_hm05 = api_or_fair(_api_ah_hm05, fo_ah_hm05)
        api_ah_hp05 = api_or_fair(_api_ah_hp05, fo_ah_hp05)
        api_ah_hm15 = api_or_fair(_api_ah_hm15, fo_ah_hm15)
        api_ah_hp15 = api_or_fair(_api_ah_hp15, fo_ah_hp15)

        # Edge uses raw API prices only — fallback odds correctly produce edge=0
        edges = {
            "home":    calc_edge(fo_home,    _api_home),
            "draw":    calc_edge(fo_draw,    _api_draw),
            "away":    calc_edge(fo_away,    _api_away),
            "dc_1x":   calc_edge(fo_dc_1x,  _api_dc_1x),
            "dc_x2":   calc_edge(fo_dc_x2,  _api_dc_x2),
            "dc_12":   calc_edge(fo_dc_12,  _api_dc_12),
            "over15":  calc_edge(fo_o15,    _api_o15),
            "over25":  calc_edge(fo_o25,    _api_o25),
            "over35":  calc_edge(fo_o35,    _api_o35),
            "ah_hm05": calc_edge(fo_ah_hm05, _api_ah_hm05),
            "ah_hp05": calc_edge(fo_ah_hp05, _api_ah_hp05),
            "ah_hm15": calc_edge(fo_ah_hm15, _api_ah_hm15),
            "ah_hp15": calc_edge(fo_ah_hp15, _api_ah_hp15),
        }

        # --- Top Recommended Market ---
        # Priority 1: real edge > 5% from Odds API (ranked by edge desc)
        # Priority 2: highest model confidence across ALL markets (not just 1X2)
        # This ensures Goals, DC, and AH markets surface when they are stronger signals.
        #
        # Confidence score per market = probability × odds_suitability
        # Confidence score = implied probability only (1/fair_odds)
        # No odds suitability weighting here — that biased against goals markets
        # (Over 1.5 at 80% was scoring lower than Home Win at 55% due to short odds penalty)
        # The top pick should reflect the strongest mathematical signal across ALL markets.
        def mkt_confidence(prob, fair):
            """Score a market purely by its implied probability strength."""
            if fair <= 1.0:
                return 0.0
            return round(1 / fair, 4)  # higher probability = higher score, no odds bias

        candidates = [
            {"label": f"{req.get('home','Home')} Win", "code": "H",       "fair": fo_home,    "api": api_home,    "edge": edges["home"],    "type": "1X2",   "conf": mkt_confidence(p_h_final, fo_home)},
            {"label": "Draw",                          "code": "D",       "fair": fo_draw,    "api": api_draw,    "edge": edges["draw"],    "type": "1X2",   "conf": mkt_confidence(p_d_final, fo_draw)},
            {"label": f"{req.get('away','Away')} Win", "code": "A",       "fair": fo_away,    "api": api_away,    "edge": edges["away"],    "type": "1X2",   "conf": mkt_confidence(p_a_final, fo_away)},
            {"label": "1X (Home or Draw)",             "code": "1X",      "fair": fo_dc_1x,   "api": api_dc_1x,   "edge": edges["dc_1x"],   "type": "DC",    "conf": mkt_confidence(p_1x, fo_dc_1x)},
            {"label": "X2 (Draw or Away)",             "code": "X2",      "fair": fo_dc_x2,   "api": api_dc_x2,   "edge": edges["dc_x2"],   "type": "DC",    "conf": mkt_confidence(p_x2, fo_dc_x2)},
            {"label": "12 (Home or Away)",             "code": "12",      "fair": fo_dc_12,   "api": api_dc_12,   "edge": edges["dc_12"],   "type": "DC",    "conf": mkt_confidence(p_12, fo_dc_12)},
            {"label": "Over 1.5",                      "code": "O15",     "fair": fo_o15,     "api": api_o15,     "edge": edges["over15"],  "type": "Goals", "conf": mkt_confidence(p_over15, fo_o15)},
            {"label": "Over 2.5",                      "code": "O25",     "fair": fo_o25,     "api": api_o25,     "edge": edges["over25"],  "type": "Goals", "conf": mkt_confidence(p_over25, fo_o25)},
            {"label": "Over 3.5",                      "code": "O35",     "fair": fo_o35,     "api": api_o35,     "edge": edges["over35"],  "type": "Goals", "conf": mkt_confidence(p_over35, fo_o35)},
            {"label": "AH Home -0.5",                  "code": "AH_HM05", "fair": fo_ah_hm05, "api": api_ah_hm05, "edge": edges["ah_hm05"], "type": "AH",    "conf": mkt_confidence(ah["hm05"], fo_ah_hm05)},
            {"label": "AH Home +0.5",                  "code": "AH_HP05", "fair": fo_ah_hp05, "api": api_ah_hp05, "edge": edges["ah_hp05"], "type": "AH",    "conf": mkt_confidence(ah["hp05"], fo_ah_hp05)},
            {"label": "AH Home -1.5",                  "code": "AH_HM15", "fair": fo_ah_hm15, "api": api_ah_hm15, "edge": edges["ah_hm15"], "type": "AH",    "conf": mkt_confidence(ah["hm15"], fo_ah_hm15)},
            {"label": "AH Home +1.5",                  "code": "AH_HP15", "fair": fo_ah_hp15, "api": api_ah_hp15, "edge": edges["ah_hp15"], "type": "AH",    "conf": mkt_confidence(ah["hp15"], fo_ah_hp15)},
        ]

        # Priority 1: real edge > 5% ranked by edge
        has_edge = sorted([c for c in candidates if c["edge"] > 0.05], key=lambda x: -x["edge"])

        # Priority 2: highest confidence score across ALL market types
        conf_fallback = max(candidates, key=lambda x: x["conf"], default=candidates[0])

        top_pick = has_edge[0] if has_edge else conf_fallback

        return jsonify({
            "score":   best,
            "probs":   {"home": h_pct, "draw": d_pct, "away": a_pct},
            "market":  {
                "home":    fo_home,  "draw":   fo_draw,  "away":   fo_away,
                "dc_1x":  fo_dc_1x, "dc_x2":  fo_dc_x2, "dc_12":  fo_dc_12,
                "btts":   fair_odds(p_btts),
                "over15": fo_o15,   "over25": fo_o25,   "over35": fo_o35,
            },
            "api_odds": {
                "home":    api_home,   "draw":   api_draw,   "away":   api_away,
                "dc_1x":   api_dc_1x,  "dc_x2":  api_dc_x2,  "dc_12":  api_dc_12,
                "over15":  api_o15,    "over25": api_o25,    "over35": api_o35,
                "ah_hm05": api_ah_hm05,"ah_hp05":api_ah_hp05,
                "ah_hm15": api_ah_hm15,"ah_hp15":api_ah_hp15,
            },
            "edges":       edges,
            "top_pick":    top_pick,
            "all_markets": candidates,
            "ah":          ah_results,
            "at":          at_results,
            "h_rank":  h_rank,  "a_rank": a_rank,
            "h_form":  h_form,  "a_form": a_form,
            "h_lam":   round(h_lam, 3),
            "a_lam":   round(a_lam, 3),
            "blended": blended,
        })

    except Exception as e:
        print(f"[PREDICT ERROR] {e}")
        return jsonify({"error": "Prediction engine failed", "detail": str(e)}), 500



# =========================================================
# 🔍 SCAN — rank all fixtures across a date range by confidence
# =========================================================
@app.route("/scan")
def scan():
    """
    Returns all fixtures across a date range, each enriched with
    Poisson outcome probabilities and a confidence score.
    Used by the Acca Builder to auto-rank legs.

    Query params:
        date_from  — YYYY-MM-DD (inclusive)
        date_to    — YYYY-MM-DD (inclusive, max 5 days from today)
    """
    try:
        date_from = request.args.get("date_from", "").split("T")[0]
        date_to   = request.args.get("date_to",   "").split("T")[0]

        if not date_from or not date_to:
            return jsonify({"error": "date_from and date_to required"}), 400

        if not fixtures_store:
            load_cache_from_disk()
        if not fixtures_store:
            fetch_all_fixtures()
        if not fixtures_store:
            return jsonify({"status": "loading", "message": "Data syncing, try again shortly."})

        try:
            d_from = _date.fromisoformat(date_from)
            d_to   = _date.fromisoformat(date_to)
        except ValueError:
            return jsonify({"error": "Invalid date format"}), 400

        # Cap at 5 days from today
        today   = _date.today()
        max_day = today + timedelta(days=5)
        d_to    = min(d_to, max_day)

        ranked = []
        current = d_from
        while current <= d_to:
            ds       = current.isoformat()
            matches  = fixtures_store.get(ds, [])
            days_out = (current - today).days

            for m in matches:
                try:
                    comp  = m.get("comp")
                    h_id  = m.get("home_id")
                    a_id  = m.get("away_id")
                    if not comp or not h_id or not a_id:
                        continue

                    league_avg = LEAGUE_AVG_GOALS.get(comp, DEFAULT_LEAGUE_AVG)
                    home_adv   = LEAGUE_HOME_ADV.get(comp, DEFAULT_HOME_ADV)

                    all_stats   = get_standings(comp)
                    home_stats  = all_stats.get("home",  {})
                    away_stats  = all_stats.get("away",  {})
                    total_stats = all_stats.get("total", {})

                    fallback_h = {"gf": 1.2, "ga": 1.2, "rank": "N/A"}
                    fallback_a = {"gf": 1.0, "ga": 1.3, "rank": "N/A"}

                    h_venue = home_stats.get(str(h_id))  or total_stats.get(str(h_id), fallback_h)
                    a_venue = away_stats.get(str(a_id))  or total_stats.get(str(a_id), fallback_a)

                    # Use cached form only — do NOT make live API calls per team
                    # during a scan (would hit rate limits across 50+ teams).
                    # Fall back to neutral 1.0 multipliers if not cached yet.
                    h_cache = form_cache.get((h_id, "HOME"))
                    a_cache = form_cache.get((a_id, "AWAY"))
                    h_atk = h_cache["atk"] if h_cache else 1.0
                    h_def = h_cache["def"] if h_cache else 1.0
                    a_atk = a_cache["atk"] if a_cache else 1.0
                    a_def = a_cache["def"] if a_cache else 1.0

                    residual_adv = math.sqrt(home_adv)
                    h_lam = max(min(h_venue["gf"] * (a_venue["ga"] / league_avg) * h_atk * (1.0 / a_def) * residual_adv, 3.2), 0.35)
                    a_lam = max(min(a_venue["gf"] * (h_venue["ga"] / league_avg) * a_atk * (1.0 / h_def),                  3.2), 0.35)

                    p_h = p_d = p_a = 0.0
                    for i in range(7):
                        for j in range(7):
                            p = poisson(i, h_lam) * poisson(j, a_lam)
                            if   i > j: p_h += p
                            elif i == j: p_d += p
                            else:        p_a += p

                    tot   = p_h + p_d + p_a
                    p_h  /= tot;  p_d /= tot;  p_a /= tot

                    # Confidence = gap between top and second outcome
                    probs_sorted = sorted([p_h, p_d, p_a], reverse=True)
                    confidence   = probs_sorted[0] - probs_sorted[1]

                    # Determine best pick
                    if p_h >= p_d and p_h >= p_a:
                        pick, pick_prob = "H", p_h
                        pick_label = f"{m['home']} Win"
                    elif p_a >= p_h and p_a >= p_d:
                        pick, pick_prob = "A", p_a
                        pick_label = f"{m['away']} Win"
                    else:
                        pick, pick_prob = "D", p_d
                        pick_label = "Draw"

                    # Confidence tier
                    if confidence >= 0.30:   tier = "HIGH"
                    elif confidence >= 0.15: tier = "MED"
                    else:                    tier = "LOW"

                    ranked.append({
                        "date":       ds,
                        "days_out":   days_out,
                        "home":       m["home"],
                        "away":       m["away"],
                        "home_id":    h_id,
                        "away_id":    a_id,
                        "comp":       comp,
                        "league":     m.get("league", comp),
                        "pick":       pick,
                        "pick_label": pick_label,
                        "pick_prob":  round(pick_prob * 100, 1),
                        "confidence": round(confidence, 4),
                        "tier":       tier,
                        "h_lam":      round(h_lam, 3),
                        "a_lam":      round(a_lam, 3),
                        "probs": {
                            "home": round(p_h * 100, 1),
                            "draw": round(p_d * 100, 1),
                            "away": round(p_a * 100, 1),
                        },
                        "fair_odds":  round(1 / pick_prob, 2) if pick_prob > 0.04 else 25.0,
                    })

                except Exception as e:
                    print(f"[SCAN] Skipped {m.get('home','?')} vs {m.get('away','?')}: {e}")
                    continue

            current += timedelta(days=1)

        # Sort by confidence descending
        ranked.sort(key=lambda x: x["confidence"], reverse=True)
        return jsonify(ranked)

    except Exception as e:
        print(f"[SCAN ERROR] {e}")
        return jsonify({"error": str(e)}), 500


# =========================================================
# 🎲 ACCA — Monte Carlo simulation across selected legs
# =========================================================
@app.route("/acca", methods=["POST"])
def acca():
    """
    Runs a Monte Carlo simulation across selected acca legs.
    Each leg provides h_lam, a_lam, and the user's pick (H/D/A).
    Returns combined probability and fair acca odds.

    Body: { "legs": [ { "h_lam": 1.2, "a_lam": 0.9, "pick": "H", "label": "..." }, ... ] }
    """
    try:
        body = request.json
        if not body or "legs" not in body or len(body["legs"]) < 2:
            return jsonify({"error": "At least 2 legs required"}), 400

        legs   = body["legs"]
        n_sims = 10000
        wins   = 0

        # Define Poisson draw once outside the loop — not 10,000 times
        def pois_draw(lam):
            lam   = max(min(lam, 3.2), 0.35)
            u     = random.random()
            p_cum = 0.0
            k     = 0
            while k < 10:
                p_cum += (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)
                if u < p_cum:
                    return k
                k += 1
            return k

        for _ in range(n_sims):
            acca_won = True
            for leg in legs:
                h_lam = float(leg["h_lam"])
                a_lam = float(leg["a_lam"])
                pick  = leg["pick"]

                h_goals = pois_draw(h_lam)
                a_goals = pois_draw(a_lam)
                diff    = h_goals - a_goals
                total   = h_goals + a_goals

                # Evaluate pick against simulated scoreline
                won = False
                if   pick == "H":      won = diff > 0
                elif pick == "D":      won = diff == 0
                elif pick == "A":      won = diff < 0
                elif pick == "1X":     won = diff >= 0
                elif pick == "X2":     won = diff <= 0
                elif pick == "12":     won = diff != 0
                elif pick == "BTTS":   won = h_goals > 0 and a_goals > 0
                elif pick == "O15":    won = total > 1
                elif pick == "O25":    won = total > 2
                elif pick == "O35":    won = total > 3
                elif pick == "U25":    won = total < 3
                elif pick == "U35":    won = total < 4
                # Asian Handicap — quarter lines use half-win logic
                elif pick == "AH_HM15":  won = diff >= 2
                elif pick == "AH_HM10":  won = diff >= 2 or (diff == 1 and random.random() < 0.5)  # push = coin flip for simulation
                elif pick == "AH_HM075": won = diff >= 2 or (diff == 1 and random.random() < 0.25)
                elif pick == "AH_HM05":  won = diff >= 1
                elif pick == "AH_HM025": won = diff >= 1 or (diff == 0 and random.random() < 0.5)
                elif pick == "AH_H0":    won = diff > 0 or (diff == 0 and random.random() < 0.5)
                elif pick == "AH_HP025": won = diff >= 0 or (diff == -1 and random.random() < 0.5)
                elif pick == "AH_HP05":  won = diff >= 0
                elif pick == "AH_HP075": won = diff >= 0 or (diff == -1 and random.random() < 0.25)
                elif pick == "AH_HP10":  won = diff >= 0 or (diff == -1 and random.random() < 0.5)
                elif pick == "AH_HP15":  won = diff >= -1
                # Asian Totals
                elif pick == "AT_O125": won = total > 1 or (total == 1 and random.random() < 0.5)
                elif pick == "AT_O15":  won = total > 1
                elif pick == "AT_O175": won = total > 2 or (total == 2 and random.random() < 0.5)
                elif pick == "AT_O20":  won = total > 2 or (total == 2 and random.random() < 0.5)
                elif pick == "AT_O225": won = total > 2 or (total == 2 and random.random() < 0.5)
                elif pick == "AT_O25":  won = total > 2
                elif pick == "AT_O275": won = total > 3 or (total == 3 and random.random() < 0.5)
                elif pick == "AT_O30":  won = total > 3 or (total == 3 and random.random() < 0.5)
                elif pick == "AT_O325": won = total > 3 or (total == 3 and random.random() < 0.5)
                elif pick == "AT_O35":  won = total > 3
                elif pick == "AT_U25":  won = total < 3 or (total == 3 and random.random() < 0.5)
                elif pick == "AT_U35":  won = total < 4 or (total == 4 and random.random() < 0.5)
                else:
                    won = diff > 0  # default to home win if unknown pick

                if not won:
                    acca_won = False
                    break

            if acca_won:
                wins += 1

        prob      = wins / n_sims
        fair_odds = round(1 / prob, 2) if prob > 0.005 else 200.0

        # Also compute expected value if bookie odds provided
        bookie_odds = body.get("bookie_odds")
        ev = None
        if bookie_odds and float(bookie_odds) > 1:
            bo = float(bookie_odds)
            ev = round((prob * (bo - 1)) - (1 - prob), 4)

        return jsonify({
            "probability":  round(prob * 100, 2),
            "fair_odds":    fair_odds,
            "wins":         wins,
            "simulations":  n_sims,
            "ev":           ev,
        })

    except Exception as e:
        print(f"[ACCA ERROR] {e}")
        return jsonify({"error": str(e)}), 500


# =========================================================
# 🎯 SESSION — cherry-pick top sequential Masaniello picks
# =========================================================
@app.route("/session")
def session():
    """
    Scans fixtures across a user-defined date range, scores every
    market signal, and returns sequential picks in strict
    chronological order suitable for a Masaniello session.

    Query params:
        date_from  — YYYY-MM-DD (default: today)
        date_to    — YYYY-MM-DD (default: today + 5 days, max 5 days from today)

    Rules:
    - Picks returned in strict DATE ORDER — no going back in time
    - One pick per fixture (best-scoring market only)
    - No two picks from the same fixture
    - All markets scored: 1X2, BTTS, Over 1.5 / 2.5 / 3.5
    - Scored by: confidence × probability × odds_suitability
    - Returns top 10 sequential picks + reserve list
    """
    try:
        if not fixtures_store:
            load_cache_from_disk()
        if not fixtures_store:
            fetch_all_fixtures()
        if not fixtures_store:
            return jsonify({"status": "loading", "message": "Data syncing, try again shortly."})

        # ── Date range from query params ──
        today   = _date.today()
        max_day = today + timedelta(days=5)

        raw_from = request.args.get("date_from", "").split("T")[0] or today.isoformat()
        raw_to   = request.args.get("date_to",   "").split("T")[0] or max_day.isoformat()

        try:
            d_from = _date.fromisoformat(raw_from)
            d_to   = _date.fromisoformat(raw_to)
        except ValueError:
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

        # Clamp to today → today+5, but strictly respect user's d_from/d_to
        d_from = max(d_from, today)
        d_to   = min(d_to, max_day)

        if d_from > d_to:
            return jsonify({"error": "date_from must be before date_to"}), 400

        # ── Score function for Masaniello suitability ──
        def mas_score(prob, fair_odds, confidence):
            if fair_odds < 1.2 or fair_odds > 5.0:
                odds_suit = 0.0
            elif fair_odds <= 2.0:
                odds_suit = (fair_odds - 1.2) / 0.8
            elif fair_odds <= 3.5:
                odds_suit = 1.0 - ((fair_odds - 2.0) / 2.5)
            else:
                odds_suit = max(0, 1.0 - ((fair_odds - 3.5) / 3.0))
            return round(prob * confidence * odds_suit, 6)

        # ── Step 1: collect best pick PER FIXTURE, keyed by (date, home, away) ──
        # This ensures we only ever have one market per fixture in the session.
        # Within each fixture, we keep the highest-scoring market signal.
        fixture_best = {}   # key: (date, home, away) → best pick dict

        current = d_from
        while current <= d_to:
            ds      = current.isoformat()
            matches = fixtures_store.get(ds, [])
            days_out = (current - today).days

            for m in matches:
                try:
                    comp  = m.get("comp")
                    h_id  = m.get("home_id")
                    a_id  = m.get("away_id")
                    if not comp or not h_id or not a_id:
                        continue

                    league_avg  = LEAGUE_AVG_GOALS.get(comp, DEFAULT_LEAGUE_AVG)
                    home_adv    = LEAGUE_HOME_ADV.get(comp, DEFAULT_HOME_ADV)
                    all_stats   = get_standings(comp)
                    home_stats  = all_stats.get("home",  {})
                    away_stats  = all_stats.get("away",  {})
                    total_stats = all_stats.get("total", {})

                    fallback_h = {"gf": 1.2, "ga": 1.2, "rank": "N/A"}
                    fallback_a = {"gf": 1.0, "ga": 1.3, "rank": "N/A"}
                    h_venue = home_stats.get(str(h_id)) or total_stats.get(str(h_id), fallback_h)
                    a_venue = away_stats.get(str(a_id)) or total_stats.get(str(a_id), fallback_a)

                    h_cache = form_cache.get((h_id, "HOME"))
                    a_cache = form_cache.get((a_id, "AWAY"))
                    h_atk = h_cache["atk"] if h_cache else 1.0
                    h_def = h_cache["def"] if h_cache else 1.0
                    a_atk = a_cache["atk"] if a_cache else 1.0
                    a_def = a_cache["def"] if a_cache else 1.0

                    residual_adv = math.sqrt(home_adv)
                    h_lam = max(min(h_venue["gf"] * (a_venue["ga"] / league_avg) * h_atk * (1.0 / a_def) * residual_adv, 3.2), 0.35)
                    a_lam = max(min(a_venue["gf"] * (h_venue["ga"] / league_avg) * a_atk * (1.0 / h_def), 3.2), 0.35)

                    p_h = p_d = p_a = 0.0
                    p_btts = p_o15 = p_o25 = p_o35 = 0.0
                    for i in range(7):
                        for j in range(7):
                            p = poisson(i, h_lam) * poisson(j, a_lam)
                            if   i > j: p_h += p
                            elif i == j: p_d += p
                            else:        p_a += p
                            if i > 0 and j > 0: p_btts += p
                            if i + j > 1: p_o15 += p
                            if i + j > 2: p_o25 += p
                            if i + j > 3: p_o35 += p

                    tot  = p_h + p_d + p_a
                    p_h /= tot; p_d /= tot; p_a /= tot

                    probs_sorted = sorted([p_h, p_d, p_a], reverse=True)
                    confidence   = probs_sorted[0] - probs_sorted[1]

                    def fair(p): return round(1/p, 2) if p > 0.04 else 25.0

                    markets = [
                        ("Home Win",  "1X2",   p_h,    fair(p_h),    confidence),
                        ("Draw",      "1X2",   p_d,    fair(p_d),    confidence * 0.7),
                        ("Away Win",  "1X2",   p_a,    fair(p_a),    confidence),
                        ("BTTS",      "Goals", p_btts, fair(p_btts), 0.25),
                        ("Over 1.5",  "Goals", p_o15,  fair(p_o15),  0.30),
                        ("Over 2.5",  "Goals", p_o25,  fair(p_o25),  0.28),
                        ("Over 3.5",  "Goals", p_o35,  fair(p_o35),  0.20),
                    ]

                    # Find the single best market for this fixture
                    best_for_fixture = None
                    for label, mkt_type, prob, fo, conf in markets:
                        score = mas_score(prob, fo, conf)
                        if score <= 0 or prob < 0.40:
                            continue
                        if best_for_fixture is None or score > best_for_fixture["mas_score"]:
                            best_for_fixture = {
                                "date":       ds,
                                "kickoff":    m.get("kickoff", ""),   # full UTC datetime
                                "home":       m["home"],
                                "away":       m["away"],
                                "home_id":    h_id,
                                "away_id":    a_id,
                                "comp":       comp,
                                "league":     m.get("league", comp),
                                "market":     label,
                                "mkt_type":   mkt_type,
                                "prob":       round(prob * 100, 1),
                                "fair_odds":  fo,
                                "confidence": round(conf, 4),
                                "mas_score":  score,
                                "h_lam":      round(h_lam, 3),
                                "a_lam":      round(a_lam, 3),
                                "days_out":   days_out,
                            }

                    if best_for_fixture:
                        key = (ds, m["home"], m["away"])
                        fixture_best[key] = best_for_fixture

                except Exception as e:
                    print(f"[SESSION] Skipped {m.get('home','?')} vs {m.get('away','?')}: {e}")
                    continue

            current += timedelta(days=1)

        # ── Step 2: sort ALL fixture picks by full kickoff datetime ──
        # Using kickoff (full ISO string e.g. "2026-05-03T14:00:00Z") as sort key
        # guarantees strict chronological order including time within the same day.
        # Falls back to date string if kickoff is missing.
        all_picks = sorted(
            fixture_best.values(),
            key=lambda x: (x["kickoff"] or x["date"], -x["mas_score"])
        )

        # ── Step 3: take top 10 in chronological order as session picks ──
        # Remaining go to reserves, sorted by score (best replacements first)
        selected = all_picks[:10]
        reserves = sorted(all_picks[10:], key=lambda x: -x["mas_score"])

        return jsonify({
            "session":  selected,
            "reserves": reserves[:15],
        })

    except Exception as e:
        print(f"[SESSION ERROR] {e}")
        return jsonify({"error": str(e)}), 500


# =========================================================
# 🚀 BACKGROUND SCHEDULER
# =========================================================
def preload_standings():
    print("[BOOT] Preloading standings cache...")
    for comp in COMPETITIONS:
        get_standings(comp)
        time.sleep(7)   # stay under 10 req/min rate limit
    print("[BOOT] Standings preload complete")

def run_scheduler():
    """Boot fetch, then refresh every hour."""
    fetch_all_fixtures()
    # Preload standings in a separate thread so fixtures are immediately available
    threading.Thread(target=preload_standings, daemon=True).start()
    while True:
        time.sleep(3600)
        print("[SCHEDULER] Hourly refresh...")
        fetch_all_fixtures()
        preload_standings()

# Gunicorn-safe single-start guard
_started = False
def start_once():
    global _started
    if not _started:
        _started = True
        print("[INIT] Starting background scheduler...")
        threading.Thread(target=run_scheduler, daemon=True).start()

# Load disk cache immediately so first requests are served even before API responds
load_cache_from_disk()
start_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
