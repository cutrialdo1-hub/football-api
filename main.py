import math
import os
import time
import json
import requests
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
if not API_KEY:
    raise RuntimeError("CRITICAL: FOOTBALL_API_KEY not set!")

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY, "User-Agent": "Mozilla/5.0 (GafferTacticalBot/1.0)"}
COMPETITIONS = ["CL","PL","PD","BL1","SA","FL1","ELC","DED","PPL","BSA"]

CACHE_FILE = "cache.json"
CACHE_MAX_AGE = 3600  
STANDINGS_EXPIRY = 86400 
FORM_EXPIRY = 86400 

fetch_lock = threading.Lock()
standings_cache = {}
form_cache = {}
fixtures_store = {}

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

def poisson(k, lam):
    lam = max(min(float(lam or 0.1), 4.0), 0.1)
    try:
        return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)
    except: return 0.0

def get_standings(code):
    now = time.time()
    if code in standings_cache and now - standings_cache[code]["t"] < STANDINGS_EXPIRY:
        return standings_cache[code]["d"]
    try:
        r = requests.get(f"{BASE_URL}/competitions/{code}/standings", headers=HEADERS, timeout=10)
        if r.status_code == 429: return standings_cache.get(code, {}).get("d", {})
        data = r.json()
        table = next(s for s in data["standings"] if s["type"] == "TOTAL")["table"]
        out = {str(t["team"]["id"]): {"rank": t["position"], "gf": t["goalsFor"] / max(t["playedGames"], 1), "ga": t["goalsAgainst"] / max(t["playedGames"], 1)} for t in table}
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
        history, gf_list, ga_list = [], [], []
        for m in matches:
            score = m["score"]["fullTime"]
            if score["home"] is None: continue
            hs, aw, is_home = score["home"], score["away"], m["homeTeam"]["id"] == team_id
            gf, ga = (hs, aw) if is_home else (aw, hs)
            history.append("W" if gf > ga else ("D" if gf == ga else "L"))
        n = len(history)
        if n == 0: return 1.0, 1.0, "???"
        win_ratio = history.count("W") / n
        # TIGHT CLAMPING: Prevents form from creating outliers
        atk_mult = max(min(0.85 + (win_ratio * 0.30), 1.15), 0.85)
        def_mult = max(min(0.85 + (win_ratio * 0.30), 1.15), 0.85)
        form_cache[team_id] = {"t": now, "atk": atk_mult, "def": def_mult, "s": "".join(history)}
        return atk_mult, def_mult, "".join(history)
    except:
        cached = form_cache.get(team_id)
        return (cached["atk"], cached["def"], cached["s"]) if cached else (1.0, 1.0, "???")

def fetch_all_fixtures():
    global fixtures_store
    if get_cache_age() < CACHE_MAX_AGE and fixtures_store: return True
    if not fetch_lock.acquire(blocking=False): return False
    try:
        now = time.time()
        start = time.strftime("%Y-%m-%d", time.gmtime(now - 86400))
        end = time.strftime("%Y-%m-%d", time.gmtime(now + (5 * 86400)))
        r = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": start, "dateTo": end}, timeout=25)
        if r.status_code != 200: return False
        temp = {}
        for m in r.json().get("matches", []):
            c = m.get("competition", {}).get("code")
            if c in COMPETITIONS:
                date = m.get("utcDate", "")[:10]
                h_t, a_t = m.get("homeTeam", {}), m.get("awayTeam", {})
                temp.setdefault(date, []).append({"home": h_t.get("name"), "home_id": h_t.get("id"), "away": a_t.get("name"), "away_id": a_t.get("id"), "comp": c})
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
        stats = get_standings(req["comp"])
        h_atk, h_def, h_f = get_detailed_form(req["home_id"])
        a_atk, a_def, a_f = get_detailed_form(req["away_id"])
        h_t = stats.get(str(req["home_id"]), {"gf":1.2, "ga":1.2, "rank":"N/A"})
        a_t = stats.get(str(req["away_id"]), {"gf":1.0, "ga":1.3, "rank":"N/A"})

        # --- REALITY TUNNEL FIX ---
        h_l = max(min(h_t["gf"] * a_t["ga"] * h_atk * (1.0 / a_def) * 1.15, 2.8), 0.4)
        a_l = max(min(a_t["gf"] * h_t["ga"] * a_atk * (1.0 / h_def), 2.8), 0.4)

        p_h = p_d = p_a = 0
        p_btts_y = 0
        max_p, best_s = -1, "1-1"
        for i in range(6):
            for j in range(6):
                p = poisson(i, h_l) * poisson(j, a_l)
                if p > max_p: max_p, best_s = p, f"{i}-{j}"
                if i > j: p_h += p
                elif i == j: p_d += p
                else: p_a += p
                if i > 0 and j > 0: p_btts_y += p
        
        tot = p_h + p_d + p_a
        def to_o(p): return round(1/p, 2) if p > 0.05 else 18.0

        return jsonify({
            "score": best_s, 
            "probs": {"home": round((p_h/tot)*100), "draw": round((p_d/tot)*100), "away": round((p_a/tot)*100)},
            "market": {"home": to_o(p_h/tot), "draw": to_o(p_d/tot), "away": to_o(p_a/tot), "btts": to_o(p_btts_y)},
            "h_rank": h_t["rank"], "a_rank": a_t["rank"], "h_form": h_f, "a_form": a_f
        })
    except Exception as e: return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    load_cache_from_disk()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
