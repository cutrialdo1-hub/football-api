import math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

# --- CONFIG ---
API_KEY = os.environ.get("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

# Caches
standings_cache = {}
form_cache = {}
CACHE_TTL = 86400  # 24 hours

# ----------------------------
# CORE MATH & LOGIC
# ----------------------------
def poisson_probability(actual, expected):
    if expected <= 0: expected = 0.01
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)

def get_team_strengths(comp_code):
    now = time.time()
    if comp_code in standings_cache:
        tstamp, data = standings_cache[comp_code]
        if now - tstamp < CACHE_TTL: return data

    url = f"{BASE_URL}/competitions/{comp_code}/standings"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return {}

    data = res.json()
    table = data["standings"][0]["table"]
    avg_g = sum(t["goalsFor"] for t in table) / max(sum(t["playedGames"] for t in table), 1)

    strengths = {}
    for t in table:
        played = max(t["playedGames"], 1)
        strengths[t["team"]["id"]] = {
            "name": t["team"]["name"],
            "attack": (t["goalsFor"] / played) / avg_g,
            "defence": (t["goalsAgainst"] / played) / avg_g
        }
    standings_cache[comp_code] = (now, strengths)
    return strengths

def get_team_form(team_id):
    """Calculates a multiplier (0.8 to 1.2) based on last 5 games."""
    now = time.time()
    if team_id in form_cache:
        tstamp, val = form_cache[team_id]
        if now - tstamp < 3600: return val # Cache form for 1 hour

    url = f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return 1.0

    matches = res.json().get("matches", [])
    points = 0
    for m in matches:
        is_home = m["homeTeam"]["id"] == team_id
        hs, ascore = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
        if hs == ascore: points += 1
        elif (is_home and hs > ascore) or (not is_home and ascore > hs): points += 3

    # 15 points max -> normalized to 0.8 - 1.2 range
    multiplier = 0.8 + (points / 15 * 0.4)
    form_cache[team_id] = (now, multiplier)
    return multiplier

# ----------------------------
# ROUTES
# ----------------------------
@app.route("/fixtures", methods=["GET"])
def fixtures():
    date_str = request.args.get("date")
    if not date_str: return jsonify({"error": "No date"}), 400
    
    date_dt = datetime.strptime(date_str, "%Y-%m-%d")
    date_to = (date_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    params = {"dateFrom": date_str, "dateTo": date_to, "competitions": "PL,BL1,SA,PD,FL1"}
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params=params)
    
    output = []
    if res.status_code == 200:
        for m in res.json().get("matches", []):
            output.append({
                "home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"],
                "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"],
                "competition": m["competition"]["code"]
            })
    return jsonify(output)

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    comp, h_id, a_id = data.get("competition"), data.get("home_id"), data.get("away_id")
    
    strengths = get_team_strengths(comp)
    h_s = strengths.get(h_id, {"attack": 1.0, "defence": 1.0, "name": "Home"})
    a_s = strengths.get(a_id, {"attack": 1.0, "defence": 1.0, "name": "Away"})

    # Apply Form Momentum
    h_form = get_team_form(h_id)
    a_form = get_team_form(a_id)

    # Calculate xG with Form Factor
    h_xg = round(h_s["attack"] * a_s["defence"] * 1.35 * 1.10 * h_form, 2)
    a_xg = round(a_s["attack"] * h_s["defence"] * 1.35 * a_form, 2)

    # Matrix for probabilities & best score
    home_p = draw_p = away_p = max_p = 0
    best_score = "1-1"
    for h in range(6):
        for a in range(6):
            prob = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if prob > max_p: max_p, best_score = prob, f"{h}-{a}"
            if h > a: home_p += prob
            elif h == a: draw_p += prob
            else: away_p += prob

    total = home_p + draw_p + away_p
    
    # Simple AI Analysis
    insight = "Expect a balanced tactical battle."
    if h_form > 1.1 and h_xg > a_xg + 0.5: insight = f"{h_s['name']} is in peak form and statistically dominant."
    elif a_form > 1.1 and a_xg > h_xg: insight = f"Upset Alert: {a_s['name']} momentum could override home advantage."

    return jsonify({
        "prediction": best_score,
        "xg": {"home": h_xg, "away": a_xg},
        "form": {"home": round(h_form, 2), "away": round(a_form, 2)},
        "probs": {"home": round(home_p/total*100, 1), "draw": round(draw_p/total*100, 1), "away": round(away_p/total*100, 1)},
        "analysis": insight
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
