import math
import os
import time
import requests
import threading
import netrc
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
HEADERS = {"X-Auth-Token": API_KEY}
COMPETITIONS = ["CL","PL","PD","BL1","SA","FL1","ELC","DED","PPL","BSA"]

# =========================================================
# 🔒 LOCKS & CACHE
# =========================================================
fetch_lock = threading.Lock()
standings_cache = {}
form_cache = {}
fixtures_store = {}

def poisson(k, lam):
    lam = max(min(lam, 4.0), 0.1)
    return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)

# =========================================================
# 📈 DATA ENGINES
# =========================================================
def get_standings(code):
    now = time.time()
    if code in standings_cache and now - standings_cache[code]["t"] < 86400:
        return standings_cache[code]["d"]
    try:
        r = requests.get(f"{BASE_URL}/competitions/{code}/standings", headers=HEADERS, timeout=10)
        if r.status_code != 200: return {}
        data = r.json()
        table = next(s for s in data["standings"] if s["type"] == "TOTAL")["table"]
        out = {str(t["team"]["id"]): {
            "rank": t["position"],
            "gf": t["goalsFor"] / max(t["playedGames"], 1),
            "ga": t["goalsAgainst"] / max(t["playedGames"], 1)
        } for t in table}
        standings_cache[code] = {"t": now, "d": out}
        return out
    except Exception as e:
        print(f"[STANDINGS ERROR] {code}: {e}")
        return {}

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
            elif hs == aw:
                history.append("D"); pts += 1
            else:
                history.append("L")
        form_str = "".join(history)
        mult = 0.85 + (pts / 15) * 0.3
        form_cache[team_id] = {"t": now, "d": mult, "s": form_str}
        return mult, form_str
    except Exception: return 1.0, "???"

# =========================================================
# ⚡ FIXTURE ENGINE (ATOMIC)
# =========================================================
def fetch_all_fixtures():
    global fixtures_store
    if not fetch_lock.acquire(blocking=False): return False
    try:
        print("[CACHE] Fetching matches...")
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
                temp.setdefault(date, []).append({
                    "home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"],
                    "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"],
                    "comp": c, "league": m["competition"]["name"]
                })
        fixtures_store = temp
        print("[CACHE] Loaded dates:", list(fixtures_store.keys()))
        return True
    except Exception: return False
    finally: fetch_lock.release()

# =========================================================
# ⚽ ROUTES
# =========================================================
@app.route("/fixtures")
def fixtures():
    date = request.args.get("date", "").split("T")[0]
    if not date: return jsonify([])
    if not fixtures_store:
        success = fetch_all_fixtures()
        if not success or not fixtures_store:
            return jsonify({"status": "loading", "message": "Syncing API data..."})
    data = fixtures_store.get(date)
    if data is None:
        return jsonify({"status": "no_games", "data": [], "message": "No games today."})
    return jsonify(data)

@app.route("/predict", methods=["POST"])
def predict():
    try:
        d = request.json
        stats = get_standings(d["comp"])
        if not stats: return jsonify({"error": "Stats unavailable"}), 429
        h_t = stats.get(str(d["home_id"]), {"gf":1.2, "ga":1.2, "rank":"N/A"})
        a_t = stats.get(str(d["away_id"]), {"gf":1.0, "ga":1.3, "rank":"N/A"})
        h_m, h_f = get_detailed_form(d["home_id"])
        a_m, a_f = get_detailed_form(d["away_id"])
        h_lam = (h_t["gf"] * a_t["ga"]) * h_m * 1.15
        a_lam = (a_t["gf"] * h_t["ga"]) * a_m
        p_h = p_d = p_a = 0
        max_p, best_s = -1, "1-1"
        for i in range(6):
            for j in range(6):
                p = poisson(i, h_lam) * poisson(j, a_lam)
                if p > max_p: max_p, best_s = p, f"{i}-{j}"
                if i > j: p_h += p
                elif i == j: p_d += p
                else: p_a += p
        total = p_h + p_d + p_a
        if total <= 0: return jsonify({"error": "Math error"}), 500
        h_pct = round((p_h/total)*100)
        d_pct = round((p_d/total)*100)
        return jsonify({
            "score": best_s, "probs": {"home": h_pct, "draw": d_pct, "away": 100-h_pct-d_pct},
            "h_rank": h_t["rank"], "a_rank": a_t["rank"], "h_form": h_f, "a_form": a_f
        })
    except Exception: return jsonify({"error": "Prediction failed"}), 500

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
        threading.Thread(target=scheduler, daemon=True).start()

start_once()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
