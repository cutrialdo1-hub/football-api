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

standings_cache = {}
CACHE_TTL = 86400 

def poisson_probability(actual, expected):
    if expected <= 0: expected = 0.01
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)

def get_venue_stats(comp_code):
    now = time.time()
    if comp_code in standings_cache and (now - standings_cache[comp_code][0] < CACHE_TTL):
        return standings_cache[comp_code][1]

    url = f"{BASE_URL}/competitions/{comp_code}/standings"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return {}

    data = res.json()
    h_table = next((s for s in data["standings"] if s["type"] == "HOME"), data["standings"][0])["table"]
    a_table = next((s for s in data["standings"] if s["type"] == "AWAY"), data["standings"][0])["table"]

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
    url = f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return 1.0, 0
    matches = res.json().get("matches", [])
    pts = sum(3 if (m["homeTeam"]["id"] == team_id and m["score"]["fullTime"]["home"] > m["score"]["fullTime"]["away"]) or (m["awayTeam"]["id"] == team_id and m["score"]["fullTime"]["away"] > m["score"]["fullTime"]["home"]) else (1 if m["score"]["fullTime"]["home"] == m["score"]["fullTime"]["away"] else 0) for m in matches)
    return 0.85 + (pts / 15 * 0.3), pts

@app.route("/fixtures", methods=["GET"])
def fixtures():
    date_str = request.args.get("date")
    if not date_str: return jsonify([])
    d_to = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": date_str, "dateTo": d_to, "competitions": "PL,BL1,SA,PD,FL1"})
    return jsonify([{"home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"], "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"], "competition": m["competition"]["code"]} for m in res.json().get("matches", [])])

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    comp, h_id, a_id = data["competition"], data["home_id"], data["away_id"]
    stats = get_venue_stats(comp)
    h_s = stats.get(h_id, {"h_atk": 1.0, "h_def": 1.0, "a_atk": 1.0, "a_def": 1.0, "name": "Home"})
    a_s = stats.get(a_id, {"h_atk": 1.0, "h_def": 1.0, "a_atk": 1.0, "a_def": 1.0, "name": "Away"})
    
    h_f, h_pts = get_form_multiplier(h_id)
    a_f, a_pts = get_form_multiplier(a_id)

    h_xg = h_s["h_atk"] * a_s["a_def"] * 1.40 * h_f
    a_xg = a_s["a_atk"] * h_s["h_def"] * 1.25 * a_f

    max_p, best_score = 0, "1-1"
    h_win, draw, a_win = 0, 0, 0
    for h in range(6):
        for a in range(6):
            p = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if p > max_p: max_p, best_score = p, f"{h}-{a}"
            if h > a: h_win += p
            elif h == a: draw += p
            else: a_win += p

    # Expert Insight Generation
    adv_reason = f"{h_s['name']} dominates at home with an attack rating of {h_s['h_atk']:.2f}, "
    adv_reason += f"while {a_s['name']} has an away defensive coefficient of {a_s['a_def']:.2f}. "
    form_gap = abs(h_f - a_f)
    if form_gap > 0.1:
        adv_reason += f"The primary driver here is the form disparity: {h_s['name'] if h_f > a_f else a_s['name']} is significantly outperforming their baseline season stats."
    else:
        adv_reason += "Both teams show stable historical patterns, making this a high-probability statistical match."

    return jsonify({
        "score": best_score,
        "probs": {"home": round(h_win*100), "draw": round(draw*100), "away": round(a_win*100)},
        "insight": adv_reason,
        "metrics": {"h_atk": round(h_s['h_atk'],2), "a_def": round(a_s['a_def'],2), "h_form": h_pts, "a_form": a_pts}
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
