import math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_KEY = os.environ.get("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

# --- CACHE SETTINGS ---
# We store standings for 24 hours to avoid hitting the 10 req/min limit
standings_cache = {} 
CACHE_DURATION = 86400  # 24 hours in seconds

def get_team_strengths(comp_code):
    now = time.time()
    
    # Check if we have a fresh cache for this competition
    if comp_code in standings_cache:
        timestamp, data = standings_cache[comp_code]
        if now - timestamp < CACHE_DURATION:
            return data

    # If no cache, fetch from API
    url = f"{BASE_URL}/competitions/{comp_code}/standings"
    res = requests.get(url, headers=HEADERS)
    
    if res.status_code != 200:
        return {} # Fallback to empty if API is down or rate-limited

    data = res.json()
    # v4 docs: standings[0] is usually the 'TOTAL' table for LEAGUE types
    table = data["standings"][0]["table"]

    # Calculate League Averages for Scaling
    total_goals = sum(t["goalsFor"] for t in table)
    total_games = sum(t["playedGames"] for t in table)
    avg_goals_per_game = total_goals / max(total_games, 1)

    strengths = {}
    for t in table:
        t_id = t["team"]["id"]
        played = max(t["playedGames"], 1)
        
        # Normalize: 1.0 is "Average", >1.0 is "Stronger than average"
        strengths[t_id] = {
            "attack": (t["goalsFor"] / played) / avg_goals_per_game,
            "defence": (t["goalsAgainst"] / played) / avg_goals_per_game
        }
    
    # Save to cache
    standings_cache[comp_code] = (now, strengths)
    return strengths

def poisson_probability(actual, expected):
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)

@app.get("/fixtures")
def fixtures():
    date_str = request.args.get("date") # Format YYYY-MM-DD
    if not date_str:
        return jsonify({"error": "Date required"}), 400

    # IMPROVEMENT: Date ranges in v4 are [inclusive, exclusive)
    # To get matches for JUST today, we go from Today to Tomorrow
    from datetime import datetime, timedelta
    date_dt = datetime.strptime(date_str, "%Y-%m-%d")
    date_to = (date_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    params = {
        "dateFrom": date_str,
        "dateTo": date_to,
        "competitions": "PL,BL1,SA,PD,FL1"
    }
    
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params=params)
    if res.status_code != 200:
        return jsonify({"error": "API Limit reached or invalid request"}), res.status_code

    matches = res.json().get("matches", [])
    output = []
    for m in matches:
        output.append({
            "id": m["id"],
            "home": m["homeTeam"]["name"],
            "home_id": m["homeTeam"]["id"],
            "away": m["awayTeam"]["name"],
            "away_id": m["awayTeam"]["id"],
            "competition": m["competition"]["code"],
            "status": m["status"]
        })
    return jsonify(output)

@app.post("/predict")
def predict():
    req = request.get_json()
    comp = req.get("competition")
    h_id = req.get("home_id")
    a_id = req.get("away_id")

    strengths = get_team_strengths(comp)
    
    # Default values if team is new/not in standings
    h_attack = strengths.get(h_id, {}).get("attack", 1.0)
    a_defence = strengths.get(a_id, {}).get("defence", 1.0)
    a_attack = strengths.get(a_id, {}).get("attack", 1.0)
    h_defence = strengths.get(h_id, {}).get("defence", 1.0)

    # League Constant (Avg goals in a PL match is ~1.3-1.5 per team)
    LEAGUE_AVG = 1.35
    h_xg = h_attack * a_defence * LEAGUE_AVG * 1.10 # 10% Home Advantage
    a_xg = a_attack * h_defence * LEAGUE_AVG

    # Calculate Win/Draw/Loss via 6x6 Poisson Matrix
    home_p = draw_p = away_p = 0
    for h in range(6):
        for a in range(6):
            prob = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if h > a: home_p += prob
            elif h == a: draw_p += prob
            else: away_p += prob

    return jsonify({
        "home_xg": round(h_xg, 2),
        "away_xg": round(a_xg, 2),
        "probabilities": {
            "home": round(home_p * 100, 1),
            "draw": round(draw_p * 100, 1),
            "away": round(away_p * 100, 1)
        }
    })
