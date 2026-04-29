import math
import os
import time
import json
import requests
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS

# =========================================================
# 🚀 APP INIT
# =========================================================
app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
if not API_KEY:
    raise RuntimeError("CRITICAL: FOOTBALL_API_KEY not set!")

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {
    "X-Auth-Token": API_KEY,
    "User-Agent": "Mozilla/5.0 (GafferTacticalBot/2.0)"
}
COMPETITIONS = ["CL","PL","PD","BL1","SA","FL1","ELC","DED","PPL","BSA"]

CACHE_FILE = "cache.json"
CACHE_MAX_AGE   = 3600
STANDINGS_EXPIRY = 86400
FORM_EXPIRY      = 86400

# =========================================================
# 🔒 LOCKS & CACHE
# =========================================================
fetch_lock     = threading.Lock()
standings_cache = {}
form_cache      = {}
fixtures_store  = {}

# =========================================================
# 📦 PERSISTENCE ENGINE
# =========================================================
def load_cache_from_disk():
    global fixtures_store
    if not os.path.exists(CACHE_FILE): return False
    try:
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
        fixtures_store = data.get("fixtures", {})
        return True
    except: return False

def save_cache_to_disk(fixtures):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"timestamp": time.time(), "fixtures": fixtures}, f)
    except: pass

def get_cache_age():
    if not os.path.exists(CACHE_FILE): return float("inf")
    try:
        with open(CACHE_FILE, "r") as f:
            return time.time() - json.load(f).get("timestamp", 0)
    except: return float("inf")

# =========================================================
# 🎲 POISSON MATH
# =========================================================
def poisson(k, lam):
    lam = max(min(float(lam or 0.1), 4.0), 0.1)
    try:
        return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)
    except: return 0.0

# =========================================================
# 📈 DATA ENGINES
# =========================================================
def get_standings(code):
    now = time.time()
    if code in standings_cache and now - standings_cache[code]["t"] < STANDINGS_EXPIRY:
        return standings_cache[code]["d"]
    try:
        r = requests.get(f"{BASE_URL}/competitions/{code}/standings", headers=HEADERS, timeout=10)
        if r.status_code == 429: return standings_cache.get(code, {}).get("d", {})
        if r.status_code != 200: return {}
        data  = r.json()
        table = next(s for s in data["standings"] if s["type"] == "TOTAL")["table"]
        out   = {str(t["team"]["id"]): {
            "rank": t["position"],
            "gf":   t["goalsFor"]      / max(t["playedGames"], 1),
            "ga":   t["goalsAgainst"]  / max(t["playedGames"], 1)
        } for t in table}
        standings_cache[code] = {"t": now, "d": out}
        return out
    except: return standings_cache.get(code, {}).get("d", {})

def get_detailed_form(team_id):
    now = time.time()
    if team_id in form_cache and now - form_cache[team_id]["t"] < FORM_EXPIRY:
        c = form_cache[team_id]
        return c["atk"], c["def"], c["s"]
    try:
        r = requests.get(f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5", headers=HEADERS, timeout=10)
        if r.status_code == 429:
            cached = form_cache.get(team_id)
            return (cached["atk"], cached["def"], cached["s"]) if cached else (1.0, 1.0, "???")

        matches = r.json().get("matches", [])
        history = []
        for m in matches:
            score = m["score"]["fullTime"]
            if score["home"] is None: continue
            hs, aw  = score["home"], score["away"]
            is_home = m["homeTeam"]["id"] == team_id
            gf, ga  = (hs, aw) if is_home else (aw, hs)
            if gf > ga:   history.append("W")
            elif gf == ga: history.append("D")
            else:          history.append("L")

        n = len(history)
        if n == 0: return 1.0, 1.0, "???"
        weights   = list(range(n, 0, -1))
        total_w   = sum(weights)
        win_score = sum(w for res, w in zip(history, weights) if res == "W")
        win_ratio = win_score / total_w
        atk_mult = max(min(0.80 + (win_ratio * 0.40), 1.30), 0.70)
        def_mult = max(min(0.85 + (win_ratio * 0.30), 1.20), 0.80)
        form_cache[team_id] = {"t": now, "atk": atk_mult, "def": def_mult, "s": "".join(history)}
        return atk_mult, def_mult, "".join(history)
    except:
        cached = form_cache.get(team_id)
        return (cached["atk"], cached["def"], cached["s"]) if cached else (1.0, 1.0, "???")

# =========================================================
# ⚡ FIXTURE ENGINE
# =========================================================
def fetch_all_fixtures():
    global fixtures_store
    if get_cache_age() < CACHE_MAX_AGE and fixtures_store: return True
    if not fetch_lock.acquire(blocking=False): return False
    try:
        now   = time.time()
        start = time.strftime("%Y-%m-%d", time.gmtime(now - 86400))
        end   = time.strftime("%Y-%m-%d", time.gmtime(now + (5 * 86400)))
        r = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": start, "dateTo": end}, timeout=25)
        if r.status_code != 200: return False
        temp = {}
        for m in r.json().get("matches", []):
            c = m.get("competition", {}).get("code")
            if c in COMPETITIONS:
                date   = m.get("utcDate", "")[:10]
                h_team, a_team = m.get("homeTeam", {}), m.get("awayTeam", {})
                h_id, a_id = h_team.get("id"), a_team.get("id")
                if not h_id or not a_id: continue
                temp.setdefault(date, []).append({
                    "home":    h_team.get("name") or h_team.get("shortName") or "Unknown",
                    "home_id": h_id,
                    "away":    a_team.get("name") or a_team.get("shortName") or "Unknown",
                    "away_id": a_id,
                    "comp":    c
                })
        fixtures_store = temp
        save_cache_to_disk(temp)
        return True
    except: return False
    finally: fetch_lock.release()

@app.route("/fixtures")
def fixtures():
    d = request.args.get("date", "").split("T")[0]
    if not d: return jsonify([])
    if not fixtures_store: load_cache_from_disk()
    if not fixtures_store: fetch_all_fixtures()
    res = fixtures_store.get(d)
    return jsonify(res) if res is not None else jsonify({"status": "no_games", "message": "No tactical data today."})

@app.route("/predict", methods=["POST"])
def predict():
    try:
        req = request.json
        if not req: return jsonify({"error": "No data"}), 400
        comp, home_id, away_id = req.get("comp"), req.get("home_id"), req.get("away_id")
        bookie = req.get("bookie_odds", {})

        stats = get_standings(comp)
        h_atk, h_def, h_f = get_detailed_form(home_id)
        a_atk, a_def, a_f = get_detailed_form(away_id)

        h_t = stats.get(str(home_id), {"gf": 1.2, "ga": 1.2, "rank": "N/A"})
        a_t = stats.get(str(away_id), {"gf": 1.0, "ga": 1.3, "rank": "N/A"})

        h_l = max(float(h_t.get("gf") or 1.2), 0.1) * max(float(a_t.get("ga") or 1.2), 0.1) * h_atk * (1.0 / a_def) * 1.15
        a_l = max(float(a_t.get("gf") or 1.0), 0.1) * max(float(h_t.get("ga") or 1.2), 0.1) * a_atk * (1.0 / h_def)

        matrix = [[poisson(i, h_l) * poisson(j, a_l) for j in range(6)] for i in range(6)]
        p_h = sum(matrix[i][j] for i in range(6) for j in range(6) if i > j)
        p_d = sum(matrix[i][j] for i in range(6) for j in range(6) if i == j)
        p_a = sum(matrix[i][j] for i in range(6) for j in range(6) if i < j)
        
        max_p, best_s = -1.0, "1-1"
        for i in range(6):
            for j in range(6):
                if matrix[i][j] > max_p: max_p, best_s = matrix[i][j], f"{i}-{j}"

        p_home_scores = 1 - sum(matrix[0][j] for j in range(6))
        p_away_scores = 1 - sum(matrix[i][0] for i in range(6))
        p_btts = p_home_scores * p_away_scores
        p_over25 = sum(matrix[i][j] for i in range(6) for j in range(6) if i + j >= 3)

        def to_odds(p): return round(1 / p, 2) if p > 0.01 else 99.99

        gaffer_odds = {"home": to_odds(p_h), "draw": to_odds(p_d), "away": to_odds(p_a), "btts": to_odds(p_btts), "over25": to_odds(p_over25)}
        market = {}
        for key in ["home", "draw", "away", "btts", "over25"]:
            g = gaffer_odds[key]
            b_val = bookie.get(key)
            market[key] = {"gaffer_odds": g, "bookie_odds": b_val, "value_edge": (float(b_val or 0) > g) if b_val else False}

        return jsonify({"score": best_s, "probs": {"home": round(p_h*100), "draw": round(p_d*100), "away": round(p_a*100), "btts": round(p_btts*100), "over25": round(p_over25*100)}, "market": market, "h_rank": h_t.get("rank"), "a_rank": a_t.get("rank"), "h_form": h_f, "a_form": a_f})
    except: return jsonify({"error": "Prediction failed"}), 500

if __name__ == "__main__":
    load_cache_from_disk()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
