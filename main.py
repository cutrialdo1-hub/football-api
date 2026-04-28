import math
import os
import time
import requests
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_KEY = os.environ.get("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

standings_cache = {} 
CACHE_DURATION = 86400

def get_team_strengths(comp_code):
    now = time.time()
    if comp_code in standings_cache:
        timestamp, data = standings_cache[comp_code]
        if now - timestamp < CACHE_DURATION:
            return data

    url = f"{BASE_URL}/competitions/{comp_code}/standings"
    try:
        res = requests.get(url, headers=HEADERS)
        if res.status_code != 200: return {}
        data = res.json()
        table = data["standings"][0]["table"]
        total_goals = sum(t["goalsFor"] for t in table)
        total_games = sum(t["playedGames"] for t in table)
        avg_goals = total_goals / max(total_games, 1)

        strengths = {}
        for t in table:
            p = max(t["playedGames"], 1)
            strengths[t["team"]["id"]] = {
                "name": t["team"]["name"],
                "attack": (t["goalsFor"] / p) / avg_goals,
                "defence": (t["goalsAgainst"] / p) / avg_goals
            }
        standings_cache[comp_code] = (now, strengths)
        return strengths
    except: return {}

def poisson_probability(actual, expected):
    if expected <= 0: expected = 0.01
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)

@app.route("/fixtures", methods=["GET"])
def fixtures():
    date_str = request.args.get("date")
    if not date_str: return jsonify({"error": "No date"}), 400
    date_dt = datetime.strptime(date_str, "%Y-%m-%d")
    date_to = (date_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    params = {"dateFrom": date_str, "dateTo": date_to, "competitions": "PL,BL1,SA,PD,FL1"}
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params=params)
    if res.status_code != 200: return jsonify([]), 200

    matches = []
    for m in res.json().get("matches", []):
        matches.append({
            "home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"],
            "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"],
            "competition": m["competition"]["code"]
        })
    return jsonify(matches)

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    comp, h_id, a_id = data.get("competition"), data.get("home_id"), data.get("away_id")
    strengths = get_team_strengths(comp)
    
    h_s = strengths.get(h_id, {"attack": 1.0, "defence": 1.0, "name": "Home Team"})
    a_s = strengths.get(a_id, {"attack": 1.0, "defence": 1.0, "name": "Away Team"})

    h_xg = round(h_s["attack"] * a_s["defence"] * 1.35 * 1.10, 2)
    a_xg = round(a_s["attack"] * h_s["defence"] * 1.35, 2)

    # Find probabilities and the HIGHEST scoreline
    home_p = draw_p = away_p = 0
    max_score_prob = -1
    predicted_score = "1-1"

    for h in range(6):
        for a in range(6):
            prob = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if prob > max_score_prob:
                max_score_prob = prob
                predicted_score = f"{h}-{a}"
            
            if h > a: home_p += prob
            elif h == a: draw_p += prob
            else: away_p += prob

    # AI Analysis Logic
    diff = h_xg - a_xg
    if abs(diff) < 0.2:
        analysis = f"Expect a tactical deadlock. Both sides are evenly matched on paper, making the '{predicted_score}' scoreline a high-probability outcome."
    elif diff > 0.6:
        analysis = f"{h_s['name']} shows dominant attacking metrics. Their offensive pressure likely overwhelms the visitors, favoring a comfortable home win."
    elif diff < -0.6:
        analysis = f"The visitors ({a_s['name']}) hold a clear statistical edge. Expect them to exploit defensive gaps, making an away victory the logical choice."
    else:
        analysis = "A competitive fixture where home advantage might be the deciding factor. A narrow margin is expected."

    return jsonify({
        "xg": {"home": h_xg, "away": a_xg},
        "probs": {"home": round(home_p*100, 1), "draw": round(draw_p*100, 1), "away": round(away_p*100, 1)},
        "prediction": predicted_score,
        "analysis": analysis
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
