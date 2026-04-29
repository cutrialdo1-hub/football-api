import math
import os
import time
import requests
import threading
import netrc  # 🔥 FIX: Prevent import deadlock in some environments
from flask import Flask, jsonify, request
from flask_cors import CORS

# =========================================================
# 🚀 APP INIT & ENV CHECK
# =========================================================
app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
if not API_KEY:
    # App crashes immediately if key is missing (Better than silent failure)
    raise RuntimeError("CRITICAL: FOOTBALL_API_KEY environment variable not set!")

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

COMPETITIONS = [
    "CL", "PL", "PD", "BL1", "SA", "FL1", "ELC", "DED", "PPL", "BSA"
]

# =========================================================
# 🔒 LOCKS & CACHE
# =========================================================
fetch_lock = threading.Lock()
standings_cache = {}
form_cache = {}
fixtures_store = {}  # Atomic storage

# =========================================================
# 📊 POISSON MATH
# =========================================================
def poisson(k, lam):
    lam = max(min(lam, 4.0), 0.1)
    return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)

# =========================================================
# 📈 STANDINGS & FORM ENGINE
# =========================================================
def get_standings(code):
    now = time.time()
    if code in standings_cache and now - standings_cache[code]["t"] < 86400:
        return standings_cache[code]["d"]

    try:
        r = requests.get(f"{BASE_URL}/competitions/{code}/standings", headers=HEADERS, timeout=10)
        if r.status_code != 200: 
            return {}

        data = r.json()
        table = next(s for s in data["standings"] if s["type"] == "TOTAL")["table"]
        out = {
            str(t["team"]["id"]): {
                "rank": t["position"],
                "gf": t["goalsFor"] / max(t["playedGames"], 1),
                "ga": t["goalsAgainst"] / max(t["playedGames"], 1)
            } for t in table
        }
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
        if r.status_code != 200: 
            return 1.0, "???"

        matches = r.json().get("matches", [])
        history, pts = [], 0
        for m in matches:
            score = m["score"]["fullTime"]
            if score["home"] is None: continue
            hs, aw = score["home"], score["away"]
            is_home = m["homeTeam"]["id"] == team_id
            
            if (is_home and hs > aw) or (not is_home and aw > hs):
                history.append("W"); pts += 3
            elif hs == aw:
                history.append("D"); pts += 1
            else:
                history.append("L")
        
        form_string = "".join(history)
        multiplier = 0.85 + (pts / 15) * 0.3
        form_cache[team_id] = {"t": now, "d": multiplier, "s": form_string}
        return multiplier, form_string
    except Exception as e:
        print(f"[FORM ERROR] {team_id}: {e}")
        return 1.0, "???"

# =========================================================
# ⚡ FIXTURE ENGINE (ATOMIC SWAP)
# =========================================================
def fetch_all_fixtures():
    global fixtures_store
    if not fetch_lock.acquire(blocking=False):
        return False

    try:
        print("[CACHE] Background fetch started...")
        now = time.time()
        start_date = time.strftime("%Y-%m-%d", time.gmtime(now - 86400))
        end_date = time.strftime("%Y-%m-%d", time.gmtime(now + (5 * 86400)))

        r = requests.get(f"{BASE_URL}/matches", headers=HEADERS, 
                         params={"dateFrom": start_date, "dateTo": end_date}, timeout=25)
        
        if r.status_code != 200: 
            return False

        matches = r.json().get("matches", [])
        temp_store = {} # 🔥 Build locally to prevent race conditions

        for m in matches:
            comp_code = m.get("competition", {}).get("code")
            if comp_code not in COMPETITIONS: 
                continue

            date = m.get("utcDate", "")[:10]
            temp_store.setdefault(date, []).append({
                "home": m["homeTeam"]["name"],
                "home_id": m["homeTeam"]["id"],
                "away": m["awayTeam"]["name"],
                "away_id": m["awayTeam"]["id"],
                "comp": comp_code,
                "league": m["competition"]["name"]
            })

        fixtures_store = temp_store # 🔥 Atomic Swap
        print(f"[CACHE] Success. Loaded dates: {list(fixtures_store.keys())}")
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
    date_param = request.args.get("date", "").split("T")[0]
    if not date_param: 
        return jsonify([])

    # If the cache is empty, the server might still be booting
    if not fixtures_store:
        fetch_all_fixtures()
        if not fixtures_store:
            return jsonify({"status": "loading", "message": "Server is syncing..."})

    # Atomic read from the store
    data = fixtures_store.get(date_param)
    
    if data is None:
        return jsonify({
            "status": "no_games", 
            "data": [], 
            "message": "No matches scheduled for this date."
        })

    return jsonify(data)

@app.route("/predict", methods=["POST"])
def predict():
    try:
        data = request.json
        comp = data["comp"]
        h_id, a_id = data["home_id"], data["away_id"]

        stats = get_standings(comp)
        h_team = stats.get(str(h_id), {"gf": 1.2, "ga": 1.2, "rank": "N/A"})
        a_team = stats.get(str(a_id), {"gf": 1.0, "ga": 1.3, "rank": "N/A"})

        h_mult, h_form = get_detailed_form(h_id)
        a_mult, a_form = get_detailed_form(a_id)

        # Baseline + home advantage + form
        h_lam = (h_team["gf"] * a_team["ga"]) * h_mult * 1.15
        a_lam = (a_team["gf"] * h_team["ga"]) * a_mult

        p_h, p_d, p_a = 0, 0, 0
        max_p, best_s = -1, "1-1"

        for i in range(6):
            for j in range(6):
                p = poisson(i, h_lam) * poisson(j, a_lam)
                if p > max_p: 
                    max_p, best_s = p, f"{i}-{j}"
                if i > j: p_h += p
                elif i == j: p_d += p
                else: p_a += p

        # Normalize probabilities to sum to 100%
        total = p_h + p_d + p_a
        h_pct = round((p_h/total) * 100)
        d_pct = round((p_d/total) * 100)
        a_pct = 100 - h_pct - d_pct

        return jsonify({
            "score": best_s,
            "probs": {"home": h_pct, "draw": d_pct, "away": a_pct},
            "h_rank": h_team["rank"], 
            "a_rank": a_team["rank"],
            "h_form": h_form, 
            "a_form": a_form,
            "h_id": h_id, 
            "a_id": a_id
        })
    except Exception as e:
        print(f"[PREDICT ERROR] {e}")
        return jsonify({"error": "Prediction unavailable"}), 500

# =========================================================
# 🚀 BACKGROUND TASKS & GUNICORN GUARD
# =========================================================
def preload_standings():
    print("[BOOT] Preloading standings cache...")
    for comp in COMPETITIONS:
        get_standings(comp)
        time.sleep(6) # Respect rate limits during boot

def run_scheduler():
    # Initial boot fetch
    fetch_all_fixtures()
    preload_standings()
    # Hourly refresh
    while True:
        time.sleep(3600)
        fetch_all_fixtures()
        preload_standings()

_started = False
def start_once():
    global _started
    if not _started:
        _started = True
        print("[INIT] Launching background threads...")
        threading.Thread(target=run_scheduler, daemon=True).start()

start_once()

if __name__ == "__main__":
    # Note: On Render, Gunicorn typically ignores this block.
    # The start_once() call above ensures threads start when the module loads.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
