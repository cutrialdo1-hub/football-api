import math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

# THE OFFICIAL 12 FREE TIER COMPETITIONS
COMPETITIONS = [
    "PL", "PD", "BL1", "SA", "FL1", "CL", 
    "DED", "PPL", "BSA", "ELC", "WC", "EC"
]

cache_standings = {}

def poisson(k, lam):
    lam = max(min(lam, 4.0), 0.1) 
    return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)

def get_standings(code):
    now = time.time()
    # Cache standings for 24 hours to save API calls
    if code in cache_standings and now - cache_standings[code]["t"] < 86400:
        return cache_standings[code]["d"]
    
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
        cache_standings[code] = {"t": now, "d": out}
        return out
    except Exception as e:
        print(f"Standings Error for {code}: {e}")
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
            # We add a longer delay (0.6s) to ensure we don't exceed 10 req/min
            r = requests.get(
                f"{BASE_URL}/competitions/{comp}/matches", 
                headers=HEADERS, 
                params={"dateFrom": date, "dateTo": date}, 
                timeout=5
            )
            
            if r.status_code == 200:
                data = r.json()
                for m in data.get("matches", []):
                    all_matches.append({
                        "home": m["homeTeam"]["name"],
                        "home_id": m["homeTeam"]["id"],
                        "away": m["awayTeam"]["name"],
                        "away_id": m["awayTeam"]["id"],
                        "comp": comp,
                        "league": m["competition"]["name"]
                    })
            elif r.status_code == 429:
                print(f"Rate limit hit on {comp}. Slowing down...")
                time.sleep(2)
                
            time.sleep(0.6) # The "Golden Buffer" for the Free Tier
        except Exception as e:
            print(f"Error scanning {comp}: {e}")
            continue
            
    return jsonify(all_matches)

@app.route("/predict", methods=["POST"])
def predict():
    data = request.json
    # Fetch standings (often from cache)
    stats = get_standings(data["comp"])
    
    # Fallback if team data isn't in standings yet
    h_team = stats.get(str(data["home_id"]), {"gf": 1.2, "ga": 1.2, "rank": "N/A"})
    a_team = stats.get(str(data["away_id"]), {"gf": 1.0, "ga": 1.3, "rank": "N/A"})
    
    # Live form check
    hf = get_form(data["home_id"])
    af = get_form(data["away_id"])

    # Poisson Math
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
        "insight": f"Gaffer's Verdict: {data['home']} vs {data['away']}. Logic suggests a {predicted_score} result."
    })

if __name__ == "__main__":
    # Render uses the PORT environment variable
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
