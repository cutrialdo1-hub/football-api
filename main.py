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
HEADERS = {"X-Auth-Token": API_KEY, "User-Agent": "GafferTacticalBot/1.0"}
COMPETITIONS = ["CL","PL","PD","BL1","SA","FL1","ELC","DED","PPL","BSA"]

CACHE_FILE = "cache.json"
CACHE_MAX_AGE = 3600  
STANDINGS_EXPIRY = 86400 
FORM_EXPIRY = 86400 

fetch_lock = threading.Lock()
standings_cache = {}
form_cache = {}
fixtures_store = {}

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
        if r.status_code != 200: return standings_cache.get(code, {}).get("d", {})
        data = r.json()
        table = next(s for s in data["standings"] if s["type"] == "TOTAL")["table"]
        out = {str(t["team"]["id"]): {"rank": t["position"], "gf": t["goalsFor"]/max(t["playedGames"], 1), "ga": t["goalsAgainst"]/max(t["playedGames"], 1)} for t in table}
        standings_cache[code] = {"t": now, "d": out}
        return out
    except: return {}

def get_detailed_form(team_id):
    now = time.time()
    if team_id in form_cache and now - form_cache[team_id]["t"] < FORM_EXPIRY:
        c = form_cache[team_id]
        return c["atk"], c["def"], c["s"]
    try:
        r = requests.get(f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5", headers=HEADERS, timeout=10)
        if r.status_code != 200: return 1.0, 1.0, "???"
        matches = r.json().get("matches", [])
        history = []
        for m in matches:
            score = m["score"]["fullTime"]
            if score["home"] is None: continue
            is_home = m["homeTeam"]["id"] == team_id
            gf, ga = (score["home"], score["away"]) if is_home else (score["away"], score["home"])
            if gf > ga: history.append("W")
            elif gf == ga: history.append("D")
            else: history.append("L")
        n = len(history)
        if n == 0: return 1.0, 1.0, "???"
        win_ratio = sum((n-i) for i, r in enumerate(history) if r == "W") / sum(range(1, n+1))
        atk_mult = max(min(0.80 + (win_ratio * 0.40), 1.30), 0.70)
        def_mult = max(min(0.85 + (win_ratio * 0.30), 1.20), 0.80)
        form_cache[team_id] = {"t": now, "atk": atk_mult, "def": def_mult, "s": "".join(history)}
        return atk_mult, def_mult, "".join(history)
    except: return 1.0, 1.0, "???"

def fetch_all_fixtures():
    global fixtures_store
    if not fetch_lock.acquire(blocking=False): return False
    try:
        now = time.time()
        start = time.strftime("%Y-%m-%d", time.gmtime(now - 86400))
        end = time.strftime("%Y-%m-%d", time.gmtime(now + (7 * 86400)))
        r = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": start, "dateTo": end}, timeout=25)
        if r.status_code == 200:
            temp = {}
            for m in r.json().get("matches", []):
                c = m.get("competition", {}).get("code")
                if c in COMPETITIONS:
                    date = m.get("utcDate", "")[:10]
                    temp.setdefault(date, []).append({
                        "home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"],
                        "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"], "comp": c
                    })
            fixtures_store = temp
            return True
    finally: fetch_lock.release()
    return False

@app.route("/fixtures")
def fixtures():
    d = request.args.get("date", "").split("T")[0]
    if not fixtures_store: fetch_all_fixtures()
    res = fixtures_store.get(d)
    return jsonify(res) if res is not None else jsonify({"status": "no_games", "message": "No tactical data today."})

@app.route("/predict", methods=["POST"])
def predict():
    try:
        req = request.json
        bookie_odds = req.get("bookie_odds", {})
        stats = get_standings(req["comp"])
        h_atk, h_def, h_f = get_detailed_form(req["home_id"])
        a_atk, a_def, a_f = get_detailed_form(req["away_id"])
        
        h_t = stats.get(str(req["home_id"]), {"gf":1.2, "ga":1.2, "rank":"N/A"})
        a_t = stats.get(str(req["away_id"]), {"gf":1.0, "ga":1.3, "rank":"N/A"})
        
        h_l = h_t["gf"] * a_t["ga"] * h_atk * (1.0 / a_def) * 1.15
        a_l = a_t["gf"] * h_t["ga"] * a_atk * (1.0 / h_def)
        
        matrix = [[poisson(i, h_l) * poisson(j, a_l) for j in range(6)] for i in range(6)]
        p_h = sum(matrix[i][j] for i in range(6) for j in range(6) if i > j)
        p_d = sum(matrix[i][j] for i in range(6) for j in range(6) if i == j)
        p_a = sum(matrix[i][j] for i in range(6) for j in range(6) if i < j)
        tot = p_h + p_d + p_a
        h_pct, d_pct, a_pct = round((p_h/tot)*100), round((p_d/tot)*100), round((p_a/tot)*100)
        
        btts_pct = round((1 - sum(matrix[0][j] for j in range(6))) * (1 - sum(matrix[i][0] for i in range(6))) * 100)
        over25_pct = round(sum(matrix[i][j] for i in range(6) for j in range(6) if i+j >= 3) * 100)

        value_alerts = {}
        for outcome, g_pct in {"home": h_pct, "draw": d_pct, "away": a_pct}.items():
            odds = bookie_odds.get(outcome)
            if odds and odds > 1.0:
                edge = round(g_pct - (100/odds), 1)
                value_alerts[outcome] = {"edge": edge, "value_edge": edge > 5.0}

        max_p, best_s = -1, "1-1"
        for i in range(6):
            for j in range(6):
                if matrix[i][j] > max_p: max_p, best_s = matrix[i][j], f"{i}-{j}"

        return jsonify({
            "score": best_s, "probs": {"home": h_pct, "draw": d_pct, "away": a_pct},
            "h_rank": h_t["rank"], "a_rank": a_t["rank"], "h_form": h_f, "a_form": a_f,
            "market": {"btts": btts_pct, "over25": over25_pct}, "value_alerts": value_alerts
        })
    except: return jsonify({"error": "Failed"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
