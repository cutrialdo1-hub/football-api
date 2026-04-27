import math
import os
import time
import requests
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
# This allows your GitHub Pages site to communicate with your Render server
CORS(app)

# Environment Variables
API_KEY = os.environ.get("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

# --- CACHE SYSTEM ---
# We store standings for 24 hours to avoid hitting the 10 req/min API limit
standings_cache = {} 
CACHE_DURATION = 86400  # 24 hours in seconds

def get_team_strengths(comp_code):
    now = time.time()
    
    # Check if we have a fresh cache for this competition
    if comp_code in standings_cache:
        timestamp, data = standings_cache[comp_code]
        if now - timestamp < CACHE_DURATION:
            return data

    # Fetch fresh standings from API
    url = f"{BASE_URL}/competitions/{comp_code}/standings"
    try:
        res = requests.get(url, headers=HEADERS)
        if res.status_code != 200:
            print(f"API Error fetching standings: {res.status_code}")
            return {}

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
    except Exception as e:
        print(f"Standing Fetch Exception: {e}")
        return {}

def poisson_probability(actual, expected):
    """Calculates probability of scoring exactly 'actual' goals given 'expected' goals."""
    if expected <= 0: expected = 0.01 # Prevent math errors
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "Online", "api_key_configured": bool(API_KEY)})

@app.route("/fixtures", methods=["GET"])
def fixtures():
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "Missing date parameter"}), 400

    # API v4 dateTo is exclusive. To get matches for one day, we need date + 1 day.
    try:
        date_dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_to = (date_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    # Major leagues: PL (Eng), BL1 (Ger), SA (Ita), PD (Spa), FL1 (Fra)
    params = {
        "dateFrom": date_str,
        "dateTo": date_to,
        "competitions": "PL,BL1,SA,PD,FL1"
    }
    
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params=params)
    
    if res.status_code == 429:
        return jsonify({"error": "API Rate Limit reached. Try again in a minute."}), 429
    
    if res.status_code != 200:
        return jsonify({"error": "External API Error", "status": res.status_code}), res.status_code

    matches = res.json().get("matches", [])
    output = []
    for m in matches:
        output.append({
            "home": m["homeTeam"]["name"],
            "home_id": m["homeTeam"]["id"],
            "away": m["awayTeam"]["name"],
            "away_id": m["awayTeam"]["id"],
            "competition": m["competition"]["code"],
            "date": m["utcDate"][:10]
        })
    return jsonify(output)

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    comp = data.get("competition")
    h_id = data.get("home_id")
    a_id = data.get("away_id")

    if not all([comp, h_id, a_id]):
        return jsonify({"error": "Missing match identification data"}), 400

    strengths = get_team_strengths(comp)
    
    # Fallbacks: if IDs aren't in the table, assume they are "Average" (1.0)
    h_atk = strengths.get(h_id, {}).get("attack", 1.0)
    a_def = strengths.get(a_id, {}).get("defence", 1.0)
    a_atk = strengths.get(a_id, {}).get("attack", 1.0)
    h_def = strengths.get(h_id, {}).get("defence", 1.0)

    # Statistical Constants
    LEAGUE_AVG_GOALS = 1.35 
    HOME_ADVANTAGE = 1.10 # 10% boost for playing at home

    # Calculate Expected Goals (xG)
    h_xg = h_atk * a_def * LEAGUE_AVG_GOALS * HOME_ADVANTAGE
    a_xg = a_atk * h_def * LEAGUE_AVG_GOALS

    # Calculate Win/Draw/Loss via 6x6 Poisson Matrix (Scores 0-0 up to 5-5)
    home_p = draw_p = away_p = 0
    for h in range(6):
        for a in range(6):
            prob = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if h > a: 
                home_p += prob
            elif h == a: 
                draw_p += prob
            else: 
                away_p += prob

    # Normalize probabilities to ensure they sum to 100%
    total = home_p + draw_p + away_p
    
    return jsonify({
        "home_xg": round(h_xg, 2),
        "away_xg": round(a_xg, 2),
        "probabilities": {
            "home": round((home_p / total) * 100, 1),
            "draw": round((draw_p / total) * 100, 1),
            "away": round((away_p / total) * 100, 1)
        },
        "model": "Poisson v4 (Standings-Cached)"
    })

if __name__ == "__main__":
    # Render provides a PORT environment variable. If it's not there, default to 5000.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
