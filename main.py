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
    "User-Agent": "Mozilla/5.0 (GafferTacticalBot/1.0)"
}
COMPETITIONS = ["CL","PL","PD","BL1","SA","FL1","ELC","DED","PPL","BSA"]

CACHE_FILE = "cache.json"
CACHE_MAX_AGE = 3600  # 1 hour shielding

# =========================================================
# 🔒 LOCKS & CACHE
# =========================================================
fetch_lock = threading.Lock()
standings_cache = {}
form_cache = {}
fixtures_store = {}

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
        print(f"[BOOT] Cache loaded from disk. Dates: {list(fixtures_store.keys())}")
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
# 🎲 POISSON & DATA
# =========================================================
def poisson(k, lam):
    lam = max(min(float(lam or 0.1), 4.0), 0.1)
    try:
        return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)
    except: return 0.0

def get_standings(code):
    now = time.time()
    if code in standings_cache and now - standings_cache[code]["t"] < 86400:
        return standings_cache[code]["d"]
    try:
        r = requests.get(f"{BASE_URL}/competitions/{code}/standings", headers=HEADERS, timeout=10)
        if r.status_code != 200: return standings_cache.get(code, {}).get("d", {})
        data = r.json()
        table = next(s for s in data["standings"] if s["type"] == "TOTAL")["table"]
        out = {str(t["team"]["id"]): {
            "rank": t["position"],
            "gf": t["goalsFor"] / max(t["playedGames"], 1),
            "ga": t["goalsAgainst"] / max(t["playedGames"], 1)
        } for t in table}
        standings_cache[code] = {"t": now, "d": out}
        return out
    except: return {}

def get_detailed_form(team_id):
    now = time.time()
    if team_id in form_cache and now - form_cache[team_id]["t"] < 600:
        return form_cache[team_id]["d"], form_cache[team_id]["s"]
    try:
        r = requests.get(f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5", headers=HEADERS, timeout=10)
        if r.status_code != 200: return 1.0, "???"
        matches = r.json().get("matches", [])
        history, pts = [], 0
        for m in matches:
            score = m["score"]["fullTime"]
            if score["home"] is None: continue
            hs, aw, is_home = score["home"], score["away"], m["homeTeam"]["id"] == team_id
            if (is_home and hs > aw) or (not is_home and aw > hs):
                history.append("W"); pts += 3
            elif hs == aw: history.append("D"); pts += 1
            else: history.append("L")
        m_val = 0.85 + (pts / 15) * 0.3
        form_cache[team_id] = {"t": now, "d": m_val, "s": "".join(history)}
        return m_val, "".join(history)
    except: return 1.0, "???"

# =========================================================
# ⚡ CORE LOGIC (FIXED: Null vs Null Prevention)
# =========================================================
def fetch_all_fixtures():
    global fixtures_store
    if get_cache_age() < CACHE_MAX_AGE and fixtures_store: return True
    if not fetch_lock.acquire(blocking=False): return False
    try:
        print("[CACHE] Fetching fresh data from API...")
        now = time.time()
        start = time.strftime("%Y-%m-%d", time.gmtime(now - 86400))
        end = time.strftime("%Y-%m-%d", time.gmtime(now + (5 * 86400)))
        r = requests.get(f"{BASE_URL}/matches", headers=HEADERS, 
                         params={"dateFrom": start, "dateTo": end}, timeout=25)
        
        if r.status_code != 200: return False
        
        matches = r.json().get("matches", [])
        temp = {}
        for m in matches:
            c = m.get("competition", {}).get("code")
            if c in COMPETITIONS:
                date = m.get("utcDate", "")[:10]
                
                # 🛡️ HARDENED EXTRACTION: Fixes "null v null" issue
                h_team_obj = m.get("homeTeam") or {}
                a_team_obj = m.get("awayTeam") or {}
                
                h_name = h_team_obj.get("name") or h_team_obj.get("shortName") or "Unknown Team"
                a_name = a_team_obj.get("name") or a_team_obj.get("shortName") or "Unknown Team"
                h_id = h_team_obj.get("id")
                a_id = a_team_obj.get("id")
                
                # We cannot predict without IDs, skip matches that are TBD or missing info
                if not h_id or not a_id: continue

                temp.setdefault(date, []).append({
                    "home": h_name, 
                    "home_id": h_id,
                    "away": a_name, 
                    "away_id": a_id,
                    "comp": c, 
                    "league": m.get("competition", {}).get("name", "Unknown League")
                })
        
        fixtures_store = temp
        save_cache_to_disk(temp)
        print(f"[CACHE] Saved to disk. Dates available: {list(temp.keys())}")
        return True
    except Exception as e:
        print(f"[FETCH ERROR] {e}")
        return False
    finally:
        fetch_lock.release()

@app.route("/fixtures")
def fixtures():
    d = request.args.get("date", "").split("T")[0]
    if not d: return jsonify([])
    if not fixtures_store: load_cache_from_disk()
    if not fixtures_store: 
        fetch_all_fixtures()
        if not fixtures_store: return jsonify({"status": "loading", "message": "Syncing match data..."})
    res = fixtures_store.get(d)
    if res is not None:
        return jsonify(res)
    else:
        return jsonify({"status": "no_games", "message": "No tactical data today."})

@app.route("/predict", methods=["POST"])
def predict():
    try:
        req = request.json
        stats = get_standings(req["comp"])
        if not stats: return jsonify({"error": "Stats stream 429 limited"}), 429
        
        h_t = stats.get(str(req["home_id"]), {"gf":1.2, "ga":1.2, "rank":"N/A"})
        a_t = stats.get(str(req["away_id"]), {"gf":1.0, "ga":1.3, "rank":"N/A"})
        
        h_m, h_f = get_detailed_form(req["home_id"])
        a_m, a_f = get_detailed_form(req["away_id"])
        
        h_l = h_t["gf"] * a_t["ga"] * h_m * 1.15
        a_l = a_t["gf"] * h_t["ga"] * a_m
        
        p_h = p_d = p_a = 0
        max_p, best_s = -1, "1-1"
        for i in range(6):
            for j in range(6):
                p = poisson(i, h_l) * poisson(j, a_l)
                if p > max_p: max_p, best_s = p, f"{i}-{j}"
                if i > j: p_h += p
                elif i == j: p_d += p
                else: p_a += p
        
        tot = p_h + p_d + p_a
        return jsonify({
            "score": best_s, 
            "probs": {"home": round((p_h/tot)*100), "draw": round((p_d/tot)*100), "away": round((p_a/tot)*100)},
            "h_rank": h_t["rank"], 
            "a_rank": a_t["rank"], 
            "h_form": h_f, 
            "a_form": a_f
        })
    except Exception as e:
        print(f"[PREDICTION ERROR] {e}")
        return jsonify({"error": "Prediction failed"}), 500

def scheduler():
    fetch_all_fixtures()
    while True:
        time.sleep(3600)
        fetch_all_fixtures()

_started = False
def start_once():
    global _started
    if not _started:
        _started = True
        load_cache_from_disk()
        threading.Thread(target=scheduler, daemon=True).start()

start_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
