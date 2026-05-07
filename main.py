import math
import os
import time
import json
import random
import requests
import threading
import netrc
import functools
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

LEAGUE_AVG_GOALS = {
    "BL1": 1.55, "PL": 1.35, "PD": 1.25, "SA": 1.25,
    "FL1": 1.20, "CL": 1.30, "ELC": 1.40, "DED": 1.30,
    "PPL": 1.25, "BSA": 1.35,
}
DEFAULT_LEAGUE_AVG = 1.30

LEAGUE_HOME_ADV = {
    "BL1": 1.08, "PL": 1.07, "PD": 1.10, "SA": 1.10,
    "FL1": 1.09, "CL": 1.08, "ELC": 1.12, "DED": 1.10,
    "PPL": 1.10, "BSA": 1.13,
}
DEFAULT_HOME_ADV = 1.10

# =========================================================
# 🎲 ODDS API
# =========================================================
ODDS_API_KEY  = os.getenv("ODDS_API_KEY")
ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"

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

WEIGHT_POISSON = 0.40
WEIGHT_MARKET  = 0.60

odds_cache: dict = {}
ODDS_EXPIRY = 10800  # 3 hours

fetch_lock             = threading.Lock()
football_api_lock      = threading.Lock()
football_api_last_call = 0.0
standings_cache: dict  = {}
form_cache: dict       = {}
fixtures_store: dict   = {}

FOOTBALL_API_MIN_INTERVAL = 6.5
CACHE_FILE        = "cache.json"
CACHE_MAX_AGE     = 3600
STANDINGS_EXPIRY  = 86400
FORM_EXPIRY       = 3600

def football_data_get(url: str, **kwargs):
    global football_api_last_call
    with football_api_lock:
        elapsed = time.monotonic() - football_api_last_call
        if elapsed < FOOTBALL_API_MIN_INTERVAL:
            time.sleep(FOOTBALL_API_MIN_INTERVAL - elapsed)
        r = requests.get(url, **kwargs)
        football_api_last_call = time.monotonic()
        return r

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
        if age > CACHE_MAX_AGE * 6:
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
# 🎲 ODDS ENGINE — expanded to h2h + totals + spreads + double_chance
# =========================================================
def get_market_odds(comp: str) -> list:
    """
    Fetch pre-match odds from The Odds API for a competition.
    Fetches h2h (1X2), totals (O/U), spreads (AH), double_chance in one call.
    Uses median across bookmakers. Caches 3h to protect free tier quota.
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
                "markets":    "h2h,totals,spreads,double_chance",
                "oddsFormat": "decimal",
            },
            timeout=10
        )

        if r.status_code == 401:
            print("[ODDS API] Invalid API key"); return []
        if r.status_code == 422:
            print(f"[ODDS API] Sport key not supported: {sport_key}"); return []
        if r.status_code != 200:
            print(f"[ODDS API] {comp} returned {r.status_code}"); return []

        events    = r.json()
        remaining = r.headers.get("x-requests-remaining", "?")
        print(f"[ODDS API] {comp} — {len(events)} events. Markets: h2h+totals+spreads+dc. Quota left: {remaining}")

        def median(lst):
            if not lst: return None
            s = sorted(lst); n = len(s)
            return s[n//2] if n % 2 else (s[n//2-1] + s[n//2]) / 2

        parsed = []
        for ev in events:
            try:
                home_name = ev["home_team"]
                away_name = ev["away_team"]

                h2h_h, h2h_d, h2h_a  = [], [], []
                dc_1x, dc_x2, dc_12  = [], [], []
                totals: dict          = {}
                spreads_h: dict       = {}
                spreads_a: dict       = {}

                for bookie in ev.get("bookmakers", []):
                    for mkt in bookie.get("markets", []):
                        key = mkt["key"]

                        if key == "h2h":
                            for o in mkt["outcomes"]:
                                n = o["name"]
                                if n == home_name:   h2h_h.append(o["price"])
                                elif n == away_name: h2h_a.append(o["price"])
                                else:                h2h_d.append(o["price"])

                        elif key == "double_chance":
                            for o in mkt["outcomes"]:
                                n = o["name"]
                                # The Odds API double_chance outcome names vary by bookie.
                                # Match by checking if both team fragments present.
                                hn = home_name[:5].lower()
                                an = away_name[:5].lower()
                                nl = n.lower()
                                if "draw" in nl and (hn in nl or "1x" in nl):
                                    dc_1x.append(o["price"])
                                elif "draw" in nl and (an in nl or "x2" in nl):
                                    dc_x2.append(o["price"])
                                elif hn in nl and an in nl:
                                    dc_12.append(o["price"])
                                # Fallback: positional if exactly 3 outcomes
                            # Simpler fallback: if lists still empty, try name-based
                            if not dc_1x and not dc_x2 and not dc_12:
                                outs = mkt["outcomes"]
                                if len(outs) == 3:
                                    dc_1x.append(outs[0]["price"])
                                    dc_x2.append(outs[1]["price"])
                                    dc_12.append(outs[2]["price"])

                        elif key == "totals":
                            for o in mkt["outcomes"]:
                                pt = o.get("point", "")
                                mk = f"{o['name'].lower()}_{pt}"
                                totals.setdefault(mk, []).append(o["price"])

                        elif key == "spreads":
                            for o in mkt["outcomes"]:
                                pt = o.get("point", 0)
                                if o["name"] == home_name:
                                    spreads_h.setdefault(str(pt), []).append(o["price"])
                                else:
                                    spreads_a.setdefault(str(pt), []).append(o["price"])

                if not h2h_h or not h2h_a or not h2h_d:
                    continue

                entry = {
                    "home_team":  home_name,
                    "away_team":  away_name,
                    "commence":   ev.get("commence_time", ""),
                    "home_odds":  median(h2h_h),
                    "draw_odds":  median(h2h_d),
                    "away_odds":  median(h2h_a),
                    "dc_1x_odds": median(dc_1x),
                    "dc_x2_odds": median(dc_x2),
                    "dc_12_odds": median(dc_12),
                    "totals":     {k: median(v) for k, v in totals.items()},
                    "spreads_h":  {k: median(v) for k, v in spreads_h.items()},
                    "spreads_a":  {k: median(v) for k, v in spreads_a.items()},
                }
                parsed.append(entry)

            except Exception as e:
                print(f"[ODDS API] Parse error: {e}")
                continue

        odds_cache[comp] = {"t": now, "d": parsed}
        return parsed

    except Exception as e:
        print(f"[ODDS API ERROR] {comp}: {e}")
        return []


def find_match_odds(events: list, home: str, away: str) -> dict | None:
    """Fuzzy name match — handles 'Man United' vs 'Manchester United' etc."""
    if not events:
        return None
    hl = home.lower().strip()
    al = away.lower().strip()
    for ev in events:
        eh = ev["home_team"].lower().strip()
        ea = ev["away_team"].lower().strip()
        if eh == hl and ea == al:
            return ev
        if (hl in eh or eh in hl) and (al in ea or ea in al):
            return ev
    return None


def calc_edge(fair_odds: float, api_odds) -> float:
    """Edge = (api_odds / fair_odds) - 1. Returns 0 if no real api price."""
    if not api_odds or api_odds <= 1.0 or not fair_odds or fair_odds <= 1.0:
        return 0.0
    return round((api_odds / fair_odds) - 1, 4)


def bayesian_blend(p_poisson: float, market_odds: float) -> float:
    """40% Poisson + 60% Market. Normalised by caller to remove overround."""
    if not market_odds or market_odds <= 1.0:
        return p_poisson
    return (WEIGHT_POISSON * p_poisson) + (WEIGHT_MARKET * (1.0 / market_odds))

# =========================================================
# 📊 POISSON MATH — cached PMF + fast compute_probs
# =========================================================
@functools.lru_cache(maxsize=1024)
def _pmf_vec(lam_q: int) -> tuple:
    """
    Cached Poisson PMF for k=0..10. Extended from k=6 to eliminate truncation
    bias — for λ=3.2, P(k≥7)≈7.6% was silently discarded before.
    lam_q = round(lam * 1000) — quantised to 3dp for cache efficiency.
    """
    lam = max(min(lam_q / 1000.0, 3.5), 0.3)
    p   = math.exp(-lam)
    out = [p]
    for k in range(1, 11):
        p = p * lam / k
        out.append(p)
    return tuple(out)


def poisson_vec(lam: float) -> tuple:
    return _pmf_vec(round(lam * 1000))


def poisson(k: int, lam: float) -> float:
    """Backward-compatible point lookup."""
    vec = poisson_vec(float(lam or DEFAULT_LEAGUE_AVG))
    return vec[k] if 0 <= k < 7 else 0.0


def compute_probs(h_lam: float, a_lam: float):
    """
    O(n) probability computation using prefix-sum identities.
    Returns (p_h, p_d, p_a, p_btts, p_o15, p_o25, p_o35) — raw, not normalised.
    """
    hp = poisson_vec(h_lam)
    ap = poisson_vec(a_lam)
    n  = len(hp)

    h_sum  = sum(hp)
    a_sum  = sum(ap)
    t_mass = h_sum * a_sum

    a_prefix = 0.0
    p_h = p_d = 0.0
    for i in range(n):
        p_h      += hp[i] * a_prefix
        p_d      += hp[i] * ap[i]
        a_prefix += ap[i]
    p_a = t_mass - p_h - p_d

    p_btts = t_mass - hp[0]*a_sum - h_sum*ap[0] + hp[0]*ap[0]

    h0,h1,h2,h3 = hp[0],hp[1],hp[2],hp[3]
    a0,a1,a2,a3 = ap[0],ap[1],ap[2],ap[3]
    le1 = h0*a0 + h0*a1 + h1*a0
    le2 = le1 + h0*a2 + h1*a1 + h2*a0
    le3 = le2 + h0*a3 + h1*a2 + h2*a1 + h3*a0

    return p_h, p_d, p_a, p_btts, t_mass-le1, t_mass-le2, t_mass-le3

# =========================================================
# 📈 STANDINGS ENGINE (home/away splits + live league avg)
# =========================================================
def _parse_table(table: list) -> dict:
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
    now    = time.time()
    cached = standings_cache.get(code)
    if cached and now - cached["t"] < STANDINGS_EXPIRY:
        return cached["d"]

    try:
        r = football_data_get(
            f"{BASE_URL}/competitions/{code}/standings",
            headers=HEADERS, timeout=10
        )
        if r.status_code == 429:
            print(f"[RATE LIMIT] standings {code} — using stale cache")
            return cached["d"] if cached else {"total": {}, "home": {}, "away": {}}
        if r.status_code != 200:
            return cached["d"] if cached else {"total": {}, "home": {}, "away": {}}

        standings = r.json()["standings"]

        def get_table(stype):
            try:
                return _parse_table(next(s for s in standings if s["type"] == stype)["table"])
            except StopIteration:
                return {}

        out = {
            "total": get_table("TOTAL"),
            "home":  get_table("HOME"),
            "away":  get_table("AWAY"),
        }

        # Live rolling league average from standings (replaces hardcoded constant)
        total_tbl = out.get("total", {})
        if total_tbl:
            raw_g  = sum(v["gf"] * v["played"] for v in total_tbl.values())
            raw_gp = sum(v["played"]            for v in total_tbl.values())
            out["league_avg"] = round(raw_g / raw_gp, 4) if raw_gp else LEAGUE_AVG_GOALS.get(code, DEFAULT_LEAGUE_AVG)
        else:
            out["league_avg"] = LEAGUE_AVG_GOALS.get(code, DEFAULT_LEAGUE_AVG)

        standings_cache[code] = {"t": now, "d": out}
        return out

    except Exception as e:
        print(f"[STANDINGS ERROR] {code}: {e}")
        return cached["d"] if cached else {"total": {}, "home": {}, "away": {}}

# =========================================================
# ⚽ FORM ENGINE — exponential decay, 8 games, venue-specific
# =========================================================
FORM_DECAY = 0.75
FORM_N     = 8
_calibration: dict = {"n": 0, "brier_sum": 0.0, "last_run": 0.0}

def get_detailed_form(team_id: int, league_avg: float = DEFAULT_LEAGUE_AVG, venue: str = ""):
    now       = time.time()
    cache_key = (team_id, venue)
    cached    = form_cache.get(cache_key)
    if cached and now - cached["t"] < FORM_EXPIRY:
        return cached["atk"], cached["def"], cached["s"]

    url = f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit={FORM_N}"
    if venue in ("HOME", "AWAY"):
        url += f"&venue={venue}"

    try:
        r = football_data_get(url, headers=HEADERS, timeout=10)

        if r.status_code == 429:
            if cached: return cached["atk"], cached["def"], cached["s"]
            return 1.0, 1.0, "???"
        if r.status_code != 200:
            return 1.0, 1.0, "???"

        matches = r.json().get("matches", [])
        history = []
        w_gf = w_ga = w_total = 0.0

        for idx, m in enumerate(matches):
            score = m["score"]["fullTime"]
            if score["home"] is None: continue
            hs, aw  = score["home"], score["away"]
            is_home = m["homeTeam"]["id"] == team_id
            gf, ga  = (hs, aw) if is_home else (aw, hs)
            w        = FORM_DECAY ** idx
            w_gf    += gf * w
            w_ga    += ga * w
            w_total += w
            history.append("W" if gf > ga else ("D" if gf == ga else "L"))

        if w_total == 0 or not history:
            return 1.0, 1.0, "???"

        avg_gf = w_gf / w_total
        avg_ga = max(w_ga / w_total, 0.1)

        atk  = avg_gf / league_avg if league_avg > 0 else 1.0
        def_ = league_avg / avg_ga
        atk  = max(min(atk,  1.20), 0.80)
        def_ = max(min(def_, 1.20), 0.80)

        form_str = "".join(history[:5])
        form_cache[cache_key] = {"t": now, "atk": atk, "def": def_, "s": form_str}
        return atk, def_, form_str

    except Exception as e:
        print(f"[FORM ERROR] team {team_id} {venue}: {e}")
        if cached: return cached["atk"], cached["def"], cached["s"]
        return 1.0, 1.0, "???"

# =========================================================
# ⚡ FIXTURE ENGINE (atomic swap + disk persistence)
# =========================================================
def fetch_all_fixtures() -> bool:
    global fixtures_store

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

        r = football_data_get(
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
            if comp_code not in COMPETITIONS: continue
            date  = m.get("utcDate", "")[:10]
            h_t   = m.get("homeTeam", {})
            a_t   = m.get("awayTeam", {})
            if not h_t.get("id") or not a_t.get("id"): continue
            temp.setdefault(date, []).append({
                "home":    h_t.get("name", "Unknown"),
                "home_id": h_t["id"],
                "away":    a_t.get("name", "Unknown"),
                "away_id": a_t["id"],
                "comp":    comp_code,
                "league":  comp.get("name", comp_code),
                "kickoff": m.get("utcDate", ""),
            })

        fixtures_store = temp
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
    if not date: return jsonify([])

    if not fixtures_store:
        loaded = load_cache_from_disk()
        if not loaded: fetch_all_fixtures()
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
        comp     = req["comp"]
        h_id     = req["home_id"]
        a_id     = req["away_id"]
        home_adv = LEAGUE_HOME_ADV.get(comp, DEFAULT_HOME_ADV)

        all_stats   = get_standings(comp)
        home_stats  = all_stats.get("home", {})
        away_stats  = all_stats.get("away", {})
        total_stats = all_stats.get("total", {})
        league_avg  = all_stats.get("league_avg", LEAGUE_AVG_GOALS.get(comp, DEFAULT_LEAGUE_AVG))

        fallback_h = {"gf": 1.2, "ga": 1.2, "rank": "N/A"}
        fallback_a = {"gf": 1.0, "ga": 1.3, "rank": "N/A"}

        h_venue = home_stats.get(str(h_id)) or total_stats.get(str(h_id), fallback_h)
        h_rank  = total_stats.get(str(h_id), fallback_h).get("rank", "N/A")
        a_venue = away_stats.get(str(a_id)) or total_stats.get(str(a_id), fallback_a)
        a_rank  = total_stats.get(str(a_id), fallback_a).get("rank", "N/A")

        h_atk, h_def, h_form = get_detailed_form(h_id, league_avg, venue="HOME")
        a_atk, a_def, a_form = get_detailed_form(a_id, league_avg, venue="AWAY")

        residual_adv = math.sqrt(home_adv)
        h_raw = h_venue["gf"] * (a_venue["ga"] / league_avg) * h_atk * (1.0 / a_def) * residual_adv
        a_raw = a_venue["gf"] * (h_venue["ga"] / league_avg) * a_atk * (1.0 / h_def)

        h_lam = max(min(h_raw, 3.2), 0.35)
        a_lam = max(min(a_raw, 3.2), 0.35)
        if h_raw != h_lam: print(f"[CLAMP] h_lam {h_raw:.3f}→{h_lam}")
        if a_raw != a_lam: print(f"[CLAMP] a_lam {a_raw:.3f}→{a_lam}")

        # ── Score Matrix (11×11 after PMF extension fix) ──────────────────
        p_h = p_d = p_a = p_btts = p_over15 = p_over25 = p_over35 = 0.0
        matrix = {}
        ah = {"hm15":0.0,"hm1":0.0,"hm05":0.0,"h0":0.0,"hp05":0.0,"hp1":0.0,"hp15":0.0}
        at = {"o05":0.0,"o15":0.0,"o25":0.0,"o35":0.0,"o45":0.0,
              "u05":0.0,"u15":0.0,"u25":0.0,"u35":0.0}

        _hp = poisson_vec(h_lam)
        _ap = poisson_vec(a_lam)
        for i in range(len(_hp)):
            for j in range(len(_ap)):
                p     = _hp[i] * _ap[j]
                matrix[(i, j)] = p
                diff  = i - j
                total = i + j

                if   i > j: p_h += p
                elif i == j: p_d += p
                else:        p_a += p

                if i > 0 and j > 0: p_btts   += p
                if total > 1:       p_over15  += p
                if total > 2:       p_over25  += p
                if total > 3:       p_over35  += p

                # AH accumulators
                if diff >= 2:  ah["hm15"] += p
                if diff >= 2:  ah["hm1"]  += p
                elif diff==1:  ah["hm1"]  += p * 0.5
                if diff >= 1:  ah["hm05"] += p
                if diff > 0:   ah["h0"]   += p
                elif diff==0:  ah["h0"]   += p * 0.5
                if diff >= 0:  ah["hp05"] += p
                if diff >= 0:  ah["hp1"]  += p
                elif diff==-1: ah["hp1"]  += p * 0.5
                if diff >= -1: ah["hp15"] += p

                # AT accumulators
                if total > 0: at["o05"] += p
                if total > 1: at["o15"] += p
                if total > 2: at["o25"] += p
                if total > 3: at["o35"] += p
                if total > 4: at["o45"] += p
                if total < 1: at["u05"] += p
                if total < 2: at["u15"] += p
                if total < 3: at["u25"] += p
                if total < 4: at["u35"] += p

        # Quarter AH lines
        ah["hm075"] = (ah["hm05"] + ah["hm1"])  / 2
        ah["hm025"] = (ah["hm05"] + ah["h0"])   / 2
        ah["hp025"] = (ah["h0"]   + ah["hp05"]) / 2
        ah["hp075"] = (ah["hp05"] + ah["hp1"])  / 2
        ah["hp125"] = (ah["hp1"]  + ah["hp15"]) / 2
        at["o175"]  = (at["o15"]  + at["o25"])  / 2
        at["o225"]  = (at["o15"]  + at["o25"])  / 2
        at["o275"]  = (at["o25"]  + at["o35"])  / 2
        at["o325"]  = (at["o25"]  + at["o35"])  / 2

        # ── Normalise 1X2 ─────────────────────────────────────────────────
        tot     = p_h + p_d + p_a
        p_h_raw = p_h / tot
        p_d_raw = p_d / tot
        p_a_raw = p_a / tot

        # ── Bayesian Blend ─────────────────────────────────────────────────
        market_events = get_market_odds(comp)
        match_odds    = find_match_odds(market_events, req.get("home", ""), req.get("away", ""))

        if match_odds:
            b_h = bayesian_blend(p_h_raw, match_odds["home_odds"])
            b_d = bayesian_blend(p_d_raw, match_odds["draw_odds"])
            b_a = bayesian_blend(p_a_raw, match_odds["away_odds"])
            b_tot     = b_h + b_d + b_a
            p_h_final = b_h / b_tot
            p_d_final = b_d / b_tot
            p_a_final = b_a / b_tot
            blended   = True
            print(f"[BAYES] {req.get('home')} vs {req.get('away')} — "
                  f"Poisson: H{p_h_raw:.2f}/D{p_d_raw:.2f}/A{p_a_raw:.2f} → "
                  f"Blended: H{p_h_final:.2f}/D{p_d_final:.2f}/A{p_a_final:.2f}")
        else:
            p_h_final = p_h_raw
            p_d_final = p_d_raw
            p_a_final = p_a_raw
            blended   = False

        # ── Scoreline (outcome-consistent, confidence threshold) ───────────
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

        best = "1-1"; max_p = -1.0
        for (i, j), p in matrix.items():
            if valid(i, j) and p > max_p:
                max_p, best = p, f"{i}-{j}"

        h_pct = round(p_h_final * 100)
        d_pct = round(p_d_final * 100)
        a_pct = 100 - h_pct - d_pct

        def fair_odds(p: float) -> float:
            return round(1 / p, 2) if p > 0.04 else 25.0

        # ── Double Chance ─────────────────────────────────────────────────
        p_1x = p_h_final + p_d_final
        p_x2 = p_d_final + p_a_final
        p_12 = p_h_final + p_a_final

        # ── AH lines ──────────────────────────────────────────────────────
        def ah_prob(handicap: float) -> tuple:
            frac = handicap % 0.5
            if abs(frac) == 0.25:
                lo = handicap - 0.25; hi = handicap + 0.25
                ph_l,pp_l,pa_l = ah_prob(lo)
                ph_h,pp_h,pa_h = ah_prob(hi)
                return (ph_l+ph_h)/2, (pp_l+pp_h)/2, (pa_l+pa_h)/2
            p_hc = p_push = p_ac = 0.0
            for (i, j), p in matrix.items():
                adj = (i - j) + handicap
                if handicap % 1 == 0:
                    if adj > 0:   p_hc   += p
                    elif adj==0:  p_push  += p
                    else:         p_ac   += p
                else:
                    if adj > 0:   p_hc   += p
                    else:         p_ac   += p
            return p_hc, p_push, p_ac

        def ah_fair_odds(p_cover: float, p_push: float) -> float:
            ep = p_cover / (1 - p_push) if p_push < 1 else 0
            return fair_odds(ep)

        ah_lines = [-1.5,-1.0,-0.75,-0.5,-0.25,0.0,0.25,0.5,0.75,1.0,1.25,1.5]
        ah_results = {}
        for line in ah_lines:
            ph_c, pp, pa_c = ah_prob(line)
            key = f"ah_{line:+.2f}".replace(".00","").replace("+","p").replace("-","m").replace(".","")
            ah_results[key] = {
                "line": line,
                "home_cover": round(ph_c*100,1), "push": round(pp*100,1),
                "away_cover": round(pa_c*100,1),
                "home_odds":  ah_fair_odds(ph_c, pp),
                "away_odds":  ah_fair_odds(pa_c, pp),
            }

        # ── Asian Totals ───────────────────────────────────────────────────
        def at_prob(line: float) -> tuple:
            frac = line % 0.5
            if abs(frac) == 0.25:
                lo = line - 0.25; hi = line + 0.25
                po_l,pp_l,pu_l = at_prob(lo)
                po_h,pp_h,pu_h = at_prob(hi)
                return (po_l+po_h)/2, (pp_l+pp_h)/2, (pu_l+pu_h)/2
            p_over = p_push = p_under = 0.0
            for (i, j), p in matrix.items():
                goals = i + j
                if line % 1 == 0:
                    if goals > line:    p_over  += p
                    elif goals == line: p_push  += p
                    else:               p_under += p
                else:
                    if goals > line:   p_over  += p
                    else:              p_under += p
            return p_over, p_push, p_under

        at_lines = [0.75,1.0,1.25,1.5,1.75,2.0,2.25,2.5,2.75,3.0,3.25,3.5]
        at_results = {}
        for line in at_lines:
            po, pp, pu = at_prob(line)
            key = f"at_{line:.2f}".replace(".","")
            at_results[key] = {
                "line": line,
                "over_pct":  round(po*100,1), "push_pct": round(pp*100,1),
                "under_pct": round(pu*100,1),
                "over_odds":  ah_fair_odds(po, pp),
                "under_odds": ah_fair_odds(pu, pp),
            }

        # ── Fair odds for all markets ──────────────────────────────────────
        fo_home   = fair_odds(p_h_final)
        fo_draw   = fair_odds(p_d_final)
        fo_away   = fair_odds(p_a_final)
        fo_dc_1x  = fair_odds(p_1x)
        fo_dc_x2  = fair_odds(p_x2)
        fo_dc_12  = fair_odds(p_12)
        fo_o15    = fair_odds(p_over15)
        fo_o25    = fair_odds(p_over25)
        fo_o35    = fair_odds(p_over35)
        fo_btts   = fair_odds(p_btts)

        # AH fair odds from ah_results (key format verified by Python script)
        fo_ah_hm05 = ah_results.get("ah_m050", {}).get("home_odds", fair_odds(ah["hm05"]))
        fo_ah_hp05 = ah_results.get("ah_p050", {}).get("home_odds", fair_odds(ah["hp05"]))
        fo_ah_hm15 = ah_results.get("ah_m150", {}).get("home_odds", fair_odds(ah["hm15"]))
        fo_ah_hp15 = ah_results.get("ah_p150", {}).get("home_odds", fair_odds(ah["hp15"]))

        # ── Real bookie odds + smart fallback ─────────────────────────────
        mo        = match_odds or {}
        totals    = mo.get("totals",    {})
        spreads_h = mo.get("spreads_h", {})

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
            """Real bookie price if available, else fair price — UI always shows something."""
            return round(api_val, 2) if (api_val and api_val > 1.0) else fair_val

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

        # Edge: only computed against real API prices (fallback produces 0)
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

        # ── Top Recommended Market ─────────────────────────────────────────
        # Priority 1: real edge > 5% (ranked by edge desc)
        # Priority 2: highest probability across ALL market types (no odds bias)
        # Minimum odds floor 1.40 for confidence fallback — excludes Over 0.5
        # and Over 1.5 which are near-certain and offer no betting value.
        def mkt_conf(fair):
            """Pure implied probability — no odds suitability bias."""
            return round(1 / fair, 4) if fair > 1.0 else 0.0

        candidates = [
            {"label": f"{req.get('home','Home')} Win", "code": "H",       "fair": fo_home,    "api": api_home,    "edge": edges["home"],    "type": "1X2"},
            {"label": "Draw",                          "code": "D",       "fair": fo_draw,    "api": api_draw,    "edge": edges["draw"],    "type": "1X2"},
            {"label": f"{req.get('away','Away')} Win", "code": "A",       "fair": fo_away,    "api": api_away,    "edge": edges["away"],    "type": "1X2"},
            {"label": "1X (Home or Draw)",             "code": "1X",      "fair": fo_dc_1x,   "api": api_dc_1x,   "edge": edges["dc_1x"],   "type": "DC"},
            {"label": "X2 (Draw or Away)",             "code": "X2",      "fair": fo_dc_x2,   "api": api_dc_x2,   "edge": edges["dc_x2"],   "type": "DC"},
            {"label": "12 (Home or Away)",             "code": "12",      "fair": fo_dc_12,   "api": api_dc_12,   "edge": edges["dc_12"],   "type": "DC"},
            {"label": "Over 1.5",                      "code": "O15",     "fair": fo_o15,     "api": api_o15,     "edge": edges["over15"],  "type": "Goals"},
            {"label": "Over 2.5",                      "code": "O25",     "fair": fo_o25,     "api": api_o25,     "edge": edges["over25"],  "type": "Goals"},
            {"label": "Over 3.5",                      "code": "O35",     "fair": fo_o35,     "api": api_o35,     "edge": edges["over35"],  "type": "Goals"},
            {"label": "BTTS",                          "code": "BTTS",    "fair": fo_btts,    "api": fo_btts,     "edge": 0.0,              "type": "Goals"},
            {"label": "AH Home -0.5",                  "code": "AH_HM05", "fair": fo_ah_hm05, "api": api_ah_hm05, "edge": edges["ah_hm05"], "type": "AH"},
            {"label": "AH Home +0.5",                  "code": "AH_HP05", "fair": fo_ah_hp05, "api": api_ah_hp05, "edge": edges["ah_hp05"], "type": "AH"},
            {"label": "AH Home -1.5",                  "code": "AH_HM15", "fair": fo_ah_hm15, "api": api_ah_hm15, "edge": edges["ah_hm15"], "type": "AH"},
            {"label": "AH Home +1.5",                  "code": "AH_HP15", "fair": fo_ah_hp15, "api": api_ah_hp15, "edge": edges["ah_hp15"], "type": "AH"},
        ]

        has_edge = sorted([c for c in candidates if c["edge"] > 0.05], key=lambda x: -x["edge"])

        # Confidence fallback: highest probability among markets with fair odds ≥ 1.40
        # (excludes near-certainties like Over 1.5 at 1.20 which have no betting value)
        eligible = [c for c in candidates if c["fair"] >= 1.40]
        conf_fallback = max(eligible, key=lambda x: mkt_conf(x["fair"]), default=candidates[0])

        top_pick = has_edge[0] if has_edge else conf_fallback

        return jsonify({
            "score":   best,
            "probs":   {"home": h_pct, "draw": d_pct, "away": a_pct},
            "market":  {
                "home":    fo_home,   "draw":   fo_draw,   "away":   fo_away,
                "dc_1x":  fo_dc_1x,  "dc_x2":  fo_dc_x2,  "dc_12":  fo_dc_12,
                "btts":   fo_btts,
                "over15": fo_o15,    "over25": fo_o25,    "over35": fo_o35,
                "ah_hm05": fo_ah_hm05, "ah_hp05": fo_ah_hp05,
                "ah_hm15": fo_ah_hm15, "ah_hp15": fo_ah_hp15,
            },
            "api_odds": {
                "home":    api_home,    "draw":   api_draw,   "away":   api_away,
                "dc_1x":  api_dc_1x,   "dc_x2":  api_dc_x2,  "dc_12":  api_dc_12,
                "over15": api_o15,     "over25": api_o25,    "over35": api_o35,
                "ah_hm05": api_ah_hm05,"ah_hp05": api_ah_hp05,
                "ah_hm15": api_ah_hm15,"ah_hp15": api_ah_hp15,
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
# 🔍 SCAN — rank fixtures by confidence for Acca Builder
# =========================================================
@app.route("/scan")
def scan():
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

        today   = _date.today()
        max_day = today + timedelta(days=5)
        d_to    = min(d_to, max_day)

        # Hoist standings + residual_adv outside per-fixture loop
        _comps: set = set()
        _tmp = d_from
        while _tmp <= d_to:
            for _m in fixtures_store.get(_tmp.isoformat(), []):
                if _m.get("comp"): _comps.add(_m["comp"])
            _tmp += timedelta(days=1)
        _standings = {c: get_standings(c) for c in _comps}
        _radv      = {c: math.sqrt(LEAGUE_HOME_ADV.get(c, DEFAULT_HOME_ADV)) for c in _comps}

        ranked = []
        current = d_from
        while current <= d_to:
            ds       = current.isoformat()
            days_out = (current - today).days
            for m in fixtures_store.get(ds, []):
                try:
                    comp = m.get("comp"); h_id = m.get("home_id"); a_id = m.get("away_id")
                    if not comp or not h_id or not a_id: continue

                    all_s  = _standings.get(comp, {"home":{}, "away":{}, "total":{}})
                    lg_avg = all_s.get("league_avg", LEAGUE_AVG_GOALS.get(comp, DEFAULT_LEAGUE_AVG))
                    fh = {"gf":1.2,"ga":1.2,"rank":"N/A"}; fa = {"gf":1.0,"ga":1.3,"rank":"N/A"}
                    h_v = all_s["home"].get(str(h_id)) or all_s["total"].get(str(h_id), fh)
                    a_v = all_s["away"].get(str(a_id)) or all_s["total"].get(str(a_id), fa)

                    hc = form_cache.get((h_id,"HOME")); ac = form_cache.get((a_id,"AWAY"))
                    h_atk = hc["atk"] if hc else 1.0; h_def = hc["def"] if hc else 1.0
                    a_atk = ac["atk"] if ac else 1.0; a_def = ac["def"] if ac else 1.0

                    radv  = _radv.get(comp, math.sqrt(DEFAULT_HOME_ADV))
                    h_lam = max(min(h_v["gf"]*(a_v["ga"]/lg_avg)*h_atk*(1.0/a_def)*radv, 3.2), 0.35)
                    a_lam = max(min(a_v["gf"]*(h_v["ga"]/lg_avg)*a_atk*(1.0/h_def),       3.2), 0.35)

                    p_h,p_d,p_a,p_btts,p_o15,p_o25,p_o35 = compute_probs(h_lam, a_lam)
                    t = p_h+p_d+p_a; p_h/=t; p_d/=t; p_a/=t

                    # Double chance
                    p_1x = p_h + p_d
                    p_x2 = p_d + p_a
                    p_12 = p_h + p_a

                    ps = sorted([p_h,p_d,p_a], reverse=True)
                    confidence = ps[0] - ps[1]

                    if p_h>=p_d and p_h>=p_a: pick,pp,pl,pt = "H",p_h,f"{m['home']} Win","1X2"
                    elif p_a>=p_h and p_a>=p_d: pick,pp,pl,pt = "A",p_a,f"{m['away']} Win","1X2"
                    else: pick,pp,pl,pt = "D",p_d,"Draw","1X2"

                    tier = "HIGH" if confidence>=0.30 else ("MED" if confidence>=0.15 else "LOW")

                    def fo(p): return round(1/p,2) if p>0.04 else 25.0

                    # Full market breakdown for acca leg card display
                    all_markets = [
                        {"code":"H",    "label":f"{m['home']} Win", "type":"1X2",   "prob":round(p_h*100,1),    "fair":fo(p_h)},
                        {"code":"D",    "label":"Draw",              "type":"1X2",   "prob":round(p_d*100,1),    "fair":fo(p_d)},
                        {"code":"A",    "label":f"{m['away']} Win",  "type":"1X2",   "prob":round(p_a*100,1),    "fair":fo(p_a)},
                        {"code":"1X",   "label":"1X Home/Draw",      "type":"DC",    "prob":round(p_1x*100,1),   "fair":fo(p_1x)},
                        {"code":"X2",   "label":"X2 Draw/Away",      "type":"DC",    "prob":round(p_x2*100,1),   "fair":fo(p_x2)},
                        {"code":"12",   "label":"12 Home/Away",      "type":"DC",    "prob":round(p_12*100,1),   "fair":fo(p_12)},
                        {"code":"BTTS", "label":"BTTS",              "type":"Goals", "prob":round(p_btts*100,1), "fair":fo(p_btts)},
                        {"code":"O15",  "label":"Over 1.5",          "type":"Goals", "prob":round(p_o15*100,1),  "fair":fo(p_o15)},
                        {"code":"O25",  "label":"Over 2.5",          "type":"Goals", "prob":round(p_o25*100,1),  "fair":fo(p_o25)},
                        {"code":"O35",  "label":"Over 3.5",          "type":"Goals", "prob":round(p_o35*100,1),  "fair":fo(p_o35)},
                    ]

                    ranked.append({
                        "date": ds, "days_out": days_out,
                        "kickoff": m.get("kickoff", ""),
                        "home": m["home"], "away": m["away"],
                        "home_id": h_id,   "away_id": a_id,
                        "comp": comp,      "league": m.get("league", comp),
                        "pick": pick,      "pick_label": pl, "mkt_type": pt,
                        "pick_prob": round(pp*100,1), "confidence": round(confidence,4),
                        "tier": tier,
                        "h_lam": round(h_lam,3), "a_lam": round(a_lam,3),
                        "probs": {"home":round(p_h*100,1),"draw":round(p_d*100,1),"away":round(p_a*100,1)},
                        "fair_odds": fo(pp),
                        "all_markets": all_markets,
                    })
                except Exception as e:
                    print(f"[SCAN] Skipped {m.get('home','?')} vs {m.get('away','?')}: {e}")
            current += timedelta(days=1)

        ranked.sort(key=lambda x: x["confidence"], reverse=True)
        return jsonify(ranked)

    except Exception as e:
        print(f"[SCAN ERROR] {e}")
        return jsonify({"error": str(e)}), 500


# =========================================================
# 🎲 ACCA — Monte Carlo simulation
# =========================================================
@app.route("/acca", methods=["POST"])
def acca():
    try:
        body = request.json
        if not body or "legs" not in body or len(body["legs"]) < 2:
            return jsonify({"error": "At least 2 legs required"}), 400

        legs = body["legs"]; n_sims = 10000; wins = 0

        def pois_draw(lam):
            lam = max(min(lam, 3.2), 0.35)
            u = random.random(); p_cum = 0.0; k = 0
            while k < 10:
                p_cum += (math.pow(lam,k)*math.exp(-lam))/math.factorial(k)
                if u < p_cum: return k
                k += 1
            return k

        for _ in range(n_sims):
            acca_won = True
            for leg in legs:
                h_lam = float(leg["h_lam"]); a_lam = float(leg["a_lam"]); pick = leg["pick"]
                hg = pois_draw(h_lam); ag = pois_draw(a_lam)
                diff = hg - ag; total = hg + ag

                won = False
                if   pick=="H":      won = diff>0
                elif pick=="D":      won = diff==0
                elif pick=="A":      won = diff<0
                elif pick=="1X":     won = diff>=0
                elif pick=="X2":     won = diff<=0
                elif pick=="12":     won = diff!=0
                elif pick=="BTTS":   won = hg>0 and ag>0
                elif pick=="O15":    won = total>1
                elif pick=="O25":    won = total>2
                elif pick=="O35":    won = total>3
                elif pick=="U25":    won = total<3
                elif pick=="U35":    won = total<4
                elif pick=="AH_HM15":  won = diff>=2
                elif pick=="AH_HM10":  won = diff>=2 or (diff==1 and random.random()<0.5)
                elif pick=="AH_HM075": won = diff>=2 or (diff==1 and random.random()<0.25)
                elif pick=="AH_HM05":  won = diff>=1
                elif pick=="AH_HM025": won = diff>=1 or (diff==0 and random.random()<0.5)
                elif pick=="AH_H0":    won = diff>0  or (diff==0 and random.random()<0.5)
                elif pick=="AH_HP025": won = diff>=0 or (diff==-1 and random.random()<0.5)
                elif pick=="AH_HP05":  won = diff>=0
                elif pick=="AH_HP075": won = diff>=0 or (diff==-1 and random.random()<0.25)
                elif pick=="AH_HP10":  won = diff>=0 or (diff==-1 and random.random()<0.5)
                elif pick=="AH_HP15":  won = diff>=-1
                elif pick=="AT_O125":  won = total>1 or (total==1 and random.random()<0.5)
                elif pick=="AT_O15":   won = total>1
                elif pick=="AT_O175":  won = total>2 or (total==2 and random.random()<0.5)
                elif pick=="AT_O20":   won = total>2 or (total==2 and random.random()<0.5)
                elif pick=="AT_O225":  won = total>2 or (total==2 and random.random()<0.5)
                elif pick=="AT_O25":   won = total>2
                elif pick=="AT_O275":  won = total>3 or (total==3 and random.random()<0.5)
                elif pick=="AT_O30":   won = total>3 or (total==3 and random.random()<0.5)
                elif pick=="AT_O325":  won = total>3 or (total==3 and random.random()<0.5)
                elif pick=="AT_O35":   won = total>3
                elif pick=="AT_U25":   won = total<3 or (total==3 and random.random()<0.5)
                elif pick=="AT_U35":   won = total<4 or (total==4 and random.random()<0.5)
                else:                  won = diff>0

                if not won: acca_won = False; break
            if acca_won: wins += 1

        prob      = wins / n_sims
        fair_odds = round(1/prob, 2) if prob>0.005 else 200.0
        bookie_odds = body.get("bookie_odds")
        ev = None
        if bookie_odds and float(bookie_odds)>1:
            bo = float(bookie_odds)
            ev = round((prob*(bo-1))-(1-prob), 4)

        return jsonify({"probability": round(prob*100,2), "fair_odds": fair_odds,
                        "wins": wins, "simulations": n_sims, "ev": ev})

    except Exception as e:
        print(f"[ACCA ERROR] {e}")
        return jsonify({"error": str(e)}), 500


# =========================================================
# 🎯 SESSION — sequential Masaniello picks
# =========================================================
@app.route("/session")
def session():
    try:
        if not fixtures_store:
            load_cache_from_disk()
        if not fixtures_store:
            fetch_all_fixtures()
        if not fixtures_store:
            return jsonify({"status": "loading", "message": "Data syncing, try again shortly."})

        today   = _date.today()
        max_day = today + timedelta(days=5)
        raw_from = request.args.get("date_from","").split("T")[0] or today.isoformat()
        raw_to   = request.args.get("date_to",  "").split("T")[0] or max_day.isoformat()

        try:
            d_from = _date.fromisoformat(raw_from)
            d_to   = _date.fromisoformat(raw_to)
        except ValueError:
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

        d_from = max(d_from, today)
        d_to   = min(d_to, max_day)
        if d_from > d_to:
            return jsonify({"error": "date_from must be before date_to"}), 400

        def mas_score(prob, fo, conf):
            if fo < 1.2 or fo > 5.0:    odds_suit = 0.0
            elif fo <= 2.0:              odds_suit = (fo-1.2)/0.8
            elif fo <= 3.5:              odds_suit = 1.0-((fo-2.0)/2.5)
            else:                        odds_suit = max(0, 1.0-((fo-3.5)/3.0))
            return round(prob * conf * odds_suit, 6)

        # Hoist standings + radv
        _comps: set = set()
        _tmp = d_from
        while _tmp <= d_to:
            for _m in fixtures_store.get(_tmp.isoformat(),[]):
                if _m.get("comp"): _comps.add(_m["comp"])
            _tmp += timedelta(days=1)
        _standings = {c: get_standings(c) for c in _comps}
        _radv      = {c: math.sqrt(LEAGUE_HOME_ADV.get(c, DEFAULT_HOME_ADV)) for c in _comps}

        fixture_best = {}
        current = d_from
        while current <= d_to:
            ds = current.isoformat(); days_out = (current - today).days
            for m in fixtures_store.get(ds, []):
                try:
                    comp = m.get("comp"); h_id = m.get("home_id"); a_id = m.get("away_id")
                    if not comp or not h_id or not a_id: continue

                    all_s  = _standings.get(comp, {"home":{}, "away":{}, "total":{}})
                    lg_avg = all_s.get("league_avg", LEAGUE_AVG_GOALS.get(comp, DEFAULT_LEAGUE_AVG))
                    fh = {"gf":1.2,"ga":1.2}; fa = {"gf":1.0,"ga":1.3}
                    h_v = all_s["home"].get(str(h_id)) or all_s["total"].get(str(h_id), fh)
                    a_v = all_s["away"].get(str(a_id)) or all_s["total"].get(str(a_id), fa)

                    hc = form_cache.get((h_id,"HOME")); ac = form_cache.get((a_id,"AWAY"))
                    h_atk = hc["atk"] if hc else 1.0; h_def = hc["def"] if hc else 1.0
                    a_atk = ac["atk"] if ac else 1.0; a_def = ac["def"] if ac else 1.0

                    radv  = _radv.get(comp, math.sqrt(DEFAULT_HOME_ADV))
                    h_lam = max(min(h_v["gf"]*(a_v["ga"]/lg_avg)*h_atk*(1.0/a_def)*radv, 3.2), 0.35)
                    a_lam = max(min(a_v["gf"]*(h_v["ga"]/lg_avg)*a_atk*(1.0/h_def),       3.2), 0.35)

                    p_h,p_d,p_a,p_btts,p_o15,p_o25,p_o35 = compute_probs(h_lam, a_lam)
                    t = p_h+p_d+p_a; p_h/=t; p_d/=t; p_a/=t

                    ps = sorted([p_h,p_d,p_a], reverse=True)
                    confidence = ps[0] - ps[1]

                    def fair(p): return round(1/p,2) if p>0.04 else 25.0

                    markets = [
                        ("Home Win","1X2",  p_h,    fair(p_h),    confidence),
                        ("Draw",    "1X2",  p_d,    fair(p_d),    confidence*0.7),
                        ("Away Win","1X2",  p_a,    fair(p_a),    confidence),
                        ("BTTS",   "Goals", p_btts, fair(p_btts), 0.25),
                        ("Over 1.5","Goals",p_o15,  fair(p_o15),  0.30),
                        ("Over 2.5","Goals",p_o25,  fair(p_o25),  0.28),
                        ("Over 3.5","Goals",p_o35,  fair(p_o35),  0.20),
                    ]

                    best_fix = None
                    for label, mkt_type, prob, fo, conf in markets:
                        sc = mas_score(prob, fo, conf)
                        if sc <= 0 or prob < 0.40: continue
                        if best_fix is None or sc > best_fix["mas_score"]:
                            best_fix = {
                                "date": ds, "kickoff": m.get("kickoff",""),
                                "home": m["home"], "away": m["away"],
                                "home_id": h_id,   "away_id": a_id,
                                "comp": comp,      "league": m.get("league", comp),
                                "market": label,   "mkt_type": mkt_type,
                                "prob": round(prob*100,1), "fair_odds": fo,
                                "confidence": round(conf,4), "mas_score": sc,
                                "h_lam": round(h_lam,3), "a_lam": round(a_lam,3),
                                "days_out": days_out,
                            }

                    if best_fix:
                        fixture_best[(ds, m["home"], m["away"])] = best_fix

                except Exception as e:
                    print(f"[SESSION] Skipped {m.get('home','?')} vs {m.get('away','?')}: {e}")
            current += timedelta(days=1)

        # ── Step 2: sort all picks by score descending (best first) ──
        # The greedy selector below will pick from this ordered pool.
        all_picks_by_score = sorted(
            fixture_best.values(),
            key=lambda x: -x["mas_score"]
        )

        # ── Step 3: greedy sequential selection with 105-minute gap ──
        # At each step, pick the HIGHEST-SCORING fixture whose kickoff is
        # at least 105 minutes after the previous pick's kickoff.
        # This guarantees every match result is known before the next bet.
        # Fixtures without a kickoff time fall back to date-based ordering.

        GAP_MINUTES = 90

        def kickoff_dt(pick):
            """Parse kickoff to datetime, fallback to date at 00:00 UTC."""
            ko = pick.get("kickoff", "")
            if ko and len(ko) > 10:
                try:
                    # ISO format: "2026-05-10T14:00:00Z"
                    return time.strptime(ko[:19], "%Y-%m-%dT%H:%M:%S")
                except Exception:
                    pass
            # Fallback: use date only — treated as midnight so same-day
            # fixtures without exact times are grouped together
            ds = pick.get("date", "2000-01-01")
            try:
                return time.strptime(ds, "%Y-%m-%d")
            except Exception:
                return time.gmtime(0)

        def to_epoch(t_struct):
            return int(time.mktime(t_struct))

        selected = []
        reserves = []
        last_kickoff_epoch = 0  # epoch seconds of last selected pick's kickoff

        for pick in all_picks_by_score:
            ko_epoch = to_epoch(kickoff_dt(pick))
            gap_ok   = (ko_epoch - last_kickoff_epoch) >= (GAP_MINUTES * 60)

            if len(selected) == 0 or gap_ok:
                selected.append(pick)
                last_kickoff_epoch = ko_epoch
                if len(selected) >= 10:
                    break
            else:
                reserves.append(pick)

        # Sort selected in strict kickoff order for display
        selected.sort(key=lambda x: kickoff_dt(x))

        # Sort reserves by score so best replacements appear first
        reserves.sort(key=lambda x: -x["mas_score"])

        return jsonify({"session": selected, "reserves": reserves[:15]})

    except Exception as e:
        print(f"[SESSION ERROR] {e}")
        return jsonify({"error": str(e)}), 500


# =========================================================
# 📐 CALIBRATION ENGINE (Brier score — runs hourly in background)
# =========================================================
def run_calibration_check():
    global _calibration
    now_t = time.time()
    yest  = time.strftime("%Y-%m-%d", time.gmtime(now_t - 86400))
    try:
        r = football_data_get(
            f"{BASE_URL}/matches", headers=HEADERS,
            params={"dateFrom": yest, "dateTo": yest, "status": "FINISHED"}, timeout=15
        )
        if r.status_code != 200:
            print(f"[CALIB] Could not fetch results ({r.status_code})"); return

        matches = r.json().get("matches", [])
        brier_sum = 0.0; n = 0
        for m in matches:
            try:
                comp = m.get("competition",{}).get("code")
                if comp not in COMPETITIONS: continue
                h_id = m["homeTeam"]["id"]; a_id = m["awayTeam"]["id"]
                sc   = m["score"]["fullTime"]
                if sc["home"] is None: continue
                hs, as_ = sc["home"], sc["away"]
                r_h = 1.0 if hs>as_ else 0.0
                r_d = 1.0 if hs==as_ else 0.0
                r_a = 1.0 if hs<as_ else 0.0

                all_s  = get_standings(comp)
                lg_avg = all_s.get("league_avg", LEAGUE_AVG_GOALS.get(comp, DEFAULT_LEAGUE_AVG))
                home_adv = LEAGUE_HOME_ADV.get(comp, DEFAULT_HOME_ADV)
                tot_s  = all_s.get("total",{})
                fh = {"gf":1.2,"ga":1.2}; fa = {"gf":1.0,"ga":1.3}
                h_s = tot_s.get(str(h_id), fh); a_s = tot_s.get(str(a_id), fa)

                h_lam = max(min(h_s["gf"]*(a_s["ga"]/lg_avg)*math.sqrt(home_adv), 3.2), 0.35)
                a_lam = max(min(a_s["gf"]*(h_s["ga"]/lg_avg),                      3.2), 0.35)

                p_h,p_d,p_a,_,_,_,_ = compute_probs(h_lam, a_lam)
                t = p_h+p_d+p_a; p_h/=t; p_d/=t; p_a/=t

                brier_sum += (p_h-r_h)**2 + (p_d-r_d)**2 + (p_a-r_a)**2
                n += 1
            except Exception:
                continue

        if n:
            _calibration["n"]         += n
            _calibration["brier_sum"] += brier_sum
            _calibration["last_run"]   = now_t
            cb = _calibration["brier_sum"] / _calibration["n"]
            print(f"[CALIB] {yest}: n={n}, Brier={brier_sum/n:.4f} "
                  f"(cumul: {cb:.4f} over {_calibration['n']} matches. target<0.50)")
        else:
            print(f"[CALIB] No finished matches for {yest}")
    except Exception as e:
        print(f"[CALIB ERROR] {e}")


# =========================================================
# 🚀 BACKGROUND SCHEDULER
# =========================================================
def preload_standings():
    print("[BOOT] Preloading standings cache...")
    for comp in COMPETITIONS:
        get_standings(comp)
        time.sleep(7)
    print("[BOOT] Standings preload complete")

def run_scheduler():
    fetch_all_fixtures()
    threading.Thread(target=preload_standings, daemon=True).start()
    while True:
        time.sleep(3600)
        print("[SCHEDULER] Hourly refresh...")
        fetch_all_fixtures()
        preload_standings()
        threading.Thread(target=run_calibration_check, daemon=True).start()

_started = False
def start_once():
    global _started
    if not _started:
        _started = True
        print("[INIT] Starting background scheduler...")
        threading.Thread(target=run_scheduler, daemon=True).start()

load_cache_from_disk()
start_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
