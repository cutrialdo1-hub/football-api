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

# Caches to avoid rate limits
standings_cache = {}
form_cache = {}
CACHE_TTL = 86400  # 24 Hours

# ----------------------------
# LOGIC FUNCTIONS
# ----------------------------

def poisson_probability(actual, expected):
    if expected <= 0: expected = 0.01
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)

def get_venue_stats(comp_code):
    """Fetches Home and Away tables separately for granular accuracy."""
    now = time.time()
    if comp_code in standings_cache and (now - standings_cache[comp_code][0] < CACHE_TTL):
        return standings_cache[comp_code][1]

    url = f"{BASE_URL}/competitions/{comp_code}/standings"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return {}

    data = res.json()
    # v4 structure: standings list usually contains TOTAL, HOME, and AWAY types
    h_table = next((s for s in data["standings"] if s["type"] == "HOME"), data["standings"][0])["table"]
    a_table = next((s for s in data["standings"] if s["type"] == "AWAY"), data["standings"][0])["table"]

    # Calculate League Goal Averages
    avg_h_goals = sum(t["goalsFor"] for t in h_table) / max(sum(t["playedGames"] for t in h_table), 1)
    avg_a_goals = sum(t["goalsFor"] for t in a_table) / max(sum(t["playedGames"] for t in a_table), 1)

    venue_data = {}
    for t in h_table:
        tid = t["team"]["id"]
        venue_data[tid] = {
            "name": t["team"]["name"],
            "h_atk": (t["goalsFor"] / max(t["playedGames"], 1)) / avg_h_goals,
            "h_def": (t["goalsAgainst"] / max(t["playedGames"], 1)) / avg_h_goals
        }
    for t in a_table:
        tid = t["team"]["id"]
        if tid in venue_data:
            venue_data[tid].update({
                "a_atk": (t["goalsFor"] / max(t["playedGames"], 1)) / avg_a_goals,
                "a_def": (t["goalsAgainst"] / max(t["playedGames"], 1)) / avg_a_goals
            })

    standings_cache[comp_code] = (now, venue_data)
    return venue_data

def get_form_multiplier(team_id):
    """Calculates momentum (0.85 to 1.15) based on last 5 games."""
    url = f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return 1.0
    
    matches = res.json().get("matches", [])
    pts = 0
    for m in matches:
        is_h = m["homeTeam"]["id"] == team_id
        h_score, a_score = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
        if h_score == a_score: pts += 1
        elif (is_h and h_score > a_score) or (not is_h and a_score > h_score): pts += 3
    
    return 0.85 + (pts / 15 * 0.3)

def generate_insight(h_name, a_name, h_metrics, a_metrics, h_form, a_form, score):
    """Builds the AI scouting report."""
    diff = (h_metrics['h_atk'] * a_metrics['a_def']) - (a_metrics['a_atk'] * h_metrics['h_def'])
    
    matchup = f"The core clash pits {h_name}'s home attacking force ({h_metrics['h_atk']:.2f}) against {a_name}'s away defensive shape."
    momentum = "Momentum is neutral." if abs(h_form - a_form) < 0.1 else f"{h_name if h_form > a_form else a_name} enters with a significant form advantage."
    
    return f"{matchup} {momentum} Based on these vectors, {score} is the highest-probability convergence point."

# ----------------------------
# ENDPOINTS
# ----------------------------

@app.route("/fixtures", methods=["GET"])
def fixtures():
    date_str = request.args.get("date")
    if not date_str: return jsonify([])
    d_to = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, 
                       params={"dateFrom": date_str, "dateTo": d_to, "competitions": "PL,BL1,SA,PD,FL1"})
    
    return jsonify([{
        "home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"],
        "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"],
        "competition": m["competition"]["code"]
    } for m in res.json().get("matches", [])])

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    comp, h_id, a_id = data["competition"], data["home_id"], data["away_id"]
    
    stats = get_venue_stats(comp)
    h_s = stats.get(h_id, {"h_atk": 1.0, "h_def": 1.0, "a_atk": 1.0, "a_def": 1.0, "name": "Home"})
    a_s = stats.get(a_id, {"h_atk": 1.0, "h_def": 1.0, "a_atk": 1.0, "a_def": 1.0, "name": "Away"})
    
    h_f, a_f = get_form_multiplier(h_id), get_form_multiplier(a_id)

    # Calculate xG: (Home Atk * Away Def) and (Away Atk * Home Def)
    h_xg = round(h_s["h_atk"] * a_s["a_def"] * 1.40 * h_f, 2)
    a_xg = round(a_s["a_atk"] * h_s["h_def"] * 1.25 * a_f, 2)

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
    return jsonify({
        "prediction": best_score,
        "probs": {"home": round(home_p/total*100, 1), "draw": round(draw_p/total*100, 1), "away": round(away_p/total*100, 1)},
        "analysis": generate_insight(h_s['name'], a_s['name'], h_s, a_s, h_f, a_f, best_score),
        "venue_metrics": {"h_atk": round(h_s['h_atk'], 2), "a_def": round(a_s['a_def'], 2)},
        "xg": {"home": h_xg, "away": a_xg}
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
