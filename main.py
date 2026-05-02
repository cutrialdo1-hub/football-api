import math
import os
import time
import json
import requests
import threading
import netrc  # prevent import deadlock in some environments
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
# 🔒 LOCKS & CACHE
# =========================================================
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
        p_btts      = 0.0
        p_over25    = 0.0
        # Store per-cell probabilities for scoreline selection after outcome is known
        matrix = {}

        for i in range(7):
            for j in range(7):
                p = poisson(i, h_lam) * poisson(j, a_lam)
                matrix[(i, j)] = p
                if   i > j: p_h += p
                elif i == j: p_d += p
                else:        p_a += p
                if i > 0 and j > 0:  p_btts   += p
                if i + j > 2:        p_over25  += p

        # --- Normalise 1X2 to 100% ---
        tot   = p_h + p_d + p_a

        # --- Scoreline: outcome-consistent if model has clear conviction ---
        # If the leading outcome is >5% ahead of the second-placed outcome,
        # restrict the scoreline to cells within that outcome bracket.
        # If the match is too close to call (<5% margin), use the full matrix —
        # the raw most-probable cell is the most honest answer in that case.
        sorted_probs = sorted([p_h, p_d, p_a], reverse=True)
        lead = sorted_probs[0] - sorted_probs[1]

        if lead >= 0.05:
            # Clear favourite — filter scoreline to winning outcome bracket
            if p_h >= p_d and p_h >= p_a:
                valid = lambda i, j: i > j   # home win cells only
            elif p_a >= p_h and p_a >= p_d:
                valid = lambda i, j: j > i   # away win cells only
            else:
                valid = lambda i, j: i == j  # draw cells only
        else:
            # Too close to call — use full matrix, no bracket filter
            valid = lambda i, j: True

        best  = "1-1"
        max_p = -1.0
        for (i, j), p in matrix.items():
            if valid(i, j) and p > max_p:
                max_p, best = p, f"{i}-{j}"
        h_pct = round((p_h / tot) * 100)
        d_pct = round((p_d / tot) * 100)
        a_pct = 100 - h_pct - d_pct  # guarantees sum = 100

        # --- Fair Decimal Odds ---
        def fair_odds(p: float) -> float:
            return round(1 / p, 2) if p > 0.04 else 25.0

        return jsonify({
            "score":   best,
            "probs":   {"home": h_pct, "draw": d_pct, "away": a_pct},
            "market":  {
                "home":    fair_odds(p_h / tot),
                "draw":    fair_odds(p_d / tot),
                "away":    fair_odds(p_a / tot),
                "btts":    fair_odds(p_btts),
                "over25":  fair_odds(p_over25),
            },
            "h_rank":  h_rank,
            "a_rank":  a_rank,
            "h_form":  h_form,
            "a_form":  a_form,
            "h_lam":   round(h_lam, 3),
            "a_lam":   round(a_lam, 3),
        })

    except Exception as e:
        print(f"[PREDICT ERROR] {e}")
        return jsonify({"error": "Prediction engine failed", "detail": str(e)}), 500


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
