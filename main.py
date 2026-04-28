import math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

# TIER 1 COMPETITIONS
COMPETITIONS = ["CL", "PL", "BL1", "SA", "PD", "FL1", "DED", "PPL", "BSA", "ELC", "WC", "EC"]

cache = {}

def poisson(k, lam):
    lam = max(min(lam, 4.0), 0.1) 
    return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)

def get_standings(code):
    now = time.time()
    if code in cache and now - cache[code]["t"] < 86400:
        return cache[code]["d"]
    try:
        r = requests.get(f"{BASE_URL}/competitions/{code}/standings", headers=HEADERS, timeout=5)
        if r.status_code != 200: return {}
        data = r.json()
        table_data = next(s for s in data["standings"] if s["type"] == "TOTAL")["table"]
        out = {}
        for t in table_data:
            tid = str(t["team"]["id"])
            played = max(t["playedGames"], 1)
            out[tid] = {
                "name": t["team"]["name"],
                "rank": t["position"],
                "gf": t["goalsFor"] / played,
                "ga": t["goalsAgainst"] / played
            }
        cache[code] = {"t": now, "d": out}
        return out
    except:
        return {}

def get_form(team_id):
    try:
        r = requests.get(f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5", headers=HEADERS, timeout=5)
        if r.status_code != 200: return 1.0
        matches = r.json().get("matches", [])
        pts = 0
        for m in matches:
            score = m["score"]["fullTime"]
            if score["home"] is None: continue
            hs, aw = score["home"], score["away"]
            if m["homeTeam"]["id"] == team_id:
                pts += 3 if hs > aw else 1 if hs == aw else 0
            else:
                pts += 3 if aw > hs else 1 if hs == aw else 0
        return 0.9 + (pts / 15) * 0.2
    except:
        return 1.0

@app.route("/fixtures")
def fixtures():
    date = request.args.get("date")
    if not date: return jsonify([])
    all_matches = []
    for comp in COMPETITIONS:
        try:
            r = requests.get(f"{BASE_URL}/competitions/{comp}/matches", headers=HEADERS, params={"dateFrom": date, "dateTo": date}, timeout=5)
            if r.status_code == 200:
                for m in r.json().get("matches", []):
                    all_matches.append({
                        "home": m["homeTeam"]["name"],
                        "home_id": m["homeTeam"]["id"],
                        "away": m["awayTeam"]["name"],
                        "away_id": m["awayTeam"]["id"],
                        "comp": comp,
                        "league": m["competition"]["name"]
                    })
            time.sleep(0.25) # Doc-compliant throttling
        except:
            continue
    return jsonify(all_matches)

@app.route("/predict", methods=["POST"])
def predict():
    data = request.json
    stats = get_standings(data["comp"])
    h_team = stats.get(str(data["home_id"]), {"gf": 1.2, "ga": 1.2, "rank": "N/A"})
    a_team = stats.get(str(data["away_id"]), {"gf": 1.0, "ga": 1.3, "rank": "N/A"})
    
    hf = get_form(data["home_id"])
    af = get_form(data["away_id"])

    h_lam = (h_team["gf"] * a_team["ga"]) * hf * 1.15
    a_lam = (a_team["gf"] * h_team["ga"]) * af

    prob_home = prob_draw = prob_away = 0
    max_p = -1
    predicted_score = "1-1"

    for h_goals in range(6):
        for a_goals in range(6):
            p = poisson(h_goals, h_lam) * poisson(a_goals, a_lam)
            
            if p > max_p:
                max_p = p
                predicted_score = f"{h_goals}-{a_goals}"
            
            if h_goals > a_goals: prob_home += p
            elif h_goals == a_goals: prob_draw += p
            else: prob_away += p

    total = max(prob_home + prob_draw + prob_away, 0.001)
    return jsonify({
        "score": predicted_score,
        "probs": {
            "home": round((prob_home/total)*100),
            "draw": round((prob_draw/total)*100),
            "away": round((prob_away/total)*100)
        },
        "h_rank": h_team["rank"],
        "a_rank": a_team["rank"],
        "insight": f"Analysis complete. Logic gates stable. Predicted: {predicted_score}."
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
