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
CACHE_TTL = 86400 

def poisson_probability(actual, expected):
    if expected <= 0: expected = 0.01
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)

def get_venue_strengths(comp_code):
    """Fetches separate Home and Away tables for deep analysis."""
    now = time.time()
    if comp_code in standings_cache:
        tstamp, data = standings_cache[comp_code]
        if now - tstamp < CACHE_TTL: return data

    url = f"{BASE_URL}/competitions/{comp_code}/standings"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return {}

    data = res.json()
    # v4 provides 'TOTAL', 'HOME', and 'AWAY' tables
    home_table = next(s for s in data["standings"] if s["type"] == "HOME")["table"]
    away_table = next(s for s in data["standings"] if s["type"] == "AWAY")["table"]

    # Calculate League Averages
    avg_h_goals = sum(t["goalsFor"] for t in home_table) / max(sum(t["playedGames"] for t in home_table), 1)
    avg_a_goals = sum(t["goalsFor"] for t in away_table) / max(sum(t["playedGames"] for t in away_table), 1)

    venue_data = {}
    # Process Home Strengths
    for t in home_table:
        tid = t["team"]["id"]
        p = max(t["playedGames"], 1)
        venue_data[tid] = {
            "name": t["team"]["name"],
            "h_atk": (t["goalsFor"] / p) / avg_h_goals,
            "h_def": (t["goalsAgainst"] / p) / avg_h_goals
        }
    
    # Merge Away Strengths
    for t in away_table:
        tid = t["team"]["id"]
        p = max(t["playedGames"], 1)
        if tid in venue_data:
            venue_data[tid].update({
                "a_atk": (t["goalsFor"] / p) / avg_a_goals,
                "a_def": (t["goalsAgainst"] / p) / avg_a_goals
            })

    standings_cache[comp_code] = (now, venue_data)
    return venue_data

def get_team_form(team_id):
    url = f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return 1.0
    matches = res.json().get("matches", [])
    pts = 0
    for m in matches:
        is_h = m["homeTeam"]["id"] == team_id
        h_s, a_s = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
        if h_s == a_s: pts += 1
        elif (is_h and h_s > a_s) or (not is_h and a_s > h_s): pts += 3
    return 0.85 + (pts / 15 * 0.3) # Multiplier 0.85 to 1.15

@app.route("/fixtures", methods=["GET"])
def fixtures():
    date_str = request.args.get("date")
    if not date_str: return jsonify([]), 400
    d_dt = datetime.strptime(date_str, "%Y-%m-%d")
    d_to = (d_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    params = {"dateFrom": date_str, "dateTo": d_to, "competitions": "PL,BL1,SA,PD,FL1"}
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
    comp, h_id, a_id = data["competition"], data["home_id"], data["away_id"]
    
    venue_stats = get_venue_strengths(comp)
    h_s = venue_stats.get(h_id, {"h_atk": 1.0, "h_def": 1.0, "name": "Home"})
    a_s = venue_stats.get(a_id, {"a_atk": 1.0, "a_def": 1.0, "name": "Away"})
    
    h_form = get_team_form(h_id)
    a_form = get_team_form(a_id)

    # NEW LOGIC: Home Attack vs Away Defense & Away Attack vs Home Defense
    h_xg = round(h_s["h_atk"] * a_s["a_def"] * 1.40 * h_form, 2)
    a_xg = round(a_s["a_atk"] * h_s["h_def"] * 1.25 * a_form, 2)

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
    
    # Detailed AI Insight
    insight = f"Analysis detects a { 'strong' if h_s['h_atk'] > 1.2 else 'stable' } home offensive profile clashing with {a_s['name']}'s away defensive structure. "
    if h_form > a_form + 0.1: insight += "Home momentum is currently a decisive factor in this projection."
    elif a_form > h_form + 0.1: insight += "The visitors are over-performing their away baseline, suggesting a potential upset."

    return jsonify({
        "prediction": best_score,
        "xg": {"home": h_xg, "away": a_xg},
        "probs": {"home": round(home_p/total*100, 1), "draw": round(draw_p/total*100, 1), "away": round(away_p/total*100, 1)},
        "analysis": insight,
        "venue_metrics": {"h_atk": round(h_s['h_atk'], 2), "a_def": round(a_s['a_def'], 2)}
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
