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

# FULL 12 FREE COMPETITIONS
COMPETITIONS = [
    "CL", "PL", "BL1", "SA", "PD", "FL1",
    "DED", "PPL", "BSA", "ELC", "WC", "EC"
]

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
            r = requests.get(
                f"{BASE_URL}/competitions/{comp}/matches",
                headers=HEADERS,
                params={"dateFrom": date, "dateTo": date},
                timeout=5
            )
            if r.status_code == 200:
                matches = r.json().get("matches", [])
                for m in matches:
                    all_matches.append({
                        "home": m["homeTeam"]["name"],
                        "home_id": m["homeTeam"]["id"],
                        "away": m["awayTeam"]["name"],
                        "away_id": m["awayTeam"]["id"],
                        "comp": comp,
                        "league": m["competition"]["name"]
                    })
            # IMPORTANT: Sleep 0.2s to prevent API Rate Limit (max 10 requests per minute)
            time.sleep(0.2) 
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

    hxg = (h_team["gf"] * a_team["ga"]) * hf * 1.15
    axg = (a_team["gf"] * h_team["ga"]) * af

    best_p, score_val = 0, "1-1"
    hw = dw = aw = 0

    # TYPO FIXED HERE: changed {h}-{a} to {i}-{j}
    for i in range(6):
        for j in range(6):
            p = poisson(i, hxg) * poisson(j, axg)
            if p > best_p:
                best_p = p
                score_val = f"{i}-{j}"
            if i > j: hw += p
            elif i == j: dw += p
            else: aw += p

    total = max(hw + dw + aw, 0.001)
    return jsonify({
        "score": score_val,
        "probs": {
            "home": round(hw/total*100),
            "draw": round(dw/total*100),
            "away": round(aw/total*100)
        },
        "h_rank": h_team["rank"],
        "a_rank": a_team["rank"],
        "insight": f"Gaffer Verdict: Expected {score_val} based on Poisson distribution."
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
