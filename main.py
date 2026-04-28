import math
import os
import time
import requests
import random
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

# --- CONFIG ---
API_KEY = os.environ.get("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

# All 12 Free Tier Competitions
FREE_COMPS = "CL,PL,ELC,BL1,SA,PD,FL1,DED,PPL,BSA,EC,WC"

standings_cache = {}

def poisson_probability(actual, expected):
    if expected <= 0: expected = 0.01
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)

def get_venue_stats(comp_code):
    now = time.time()
    if comp_code in standings_cache and (now - standings_cache[comp_code][0] < 86400):
        return standings_cache[comp_code][1]

    url = f"{BASE_URL}/competitions/{comp_code}/standings"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return {}

    data = res.json()
    # Get Home, Away, and Total tables
    h_table = next((s for s in data["standings"] if s["type"] == "HOME"), data["standings"][0])["table"]
    a_table = next((s for s in data["standings"] if s["type"] == "AWAY"), data["standings"][0])["table"]
    t_table = next((s for s in data["standings"] if s["type"] == "TOTAL"), data["standings"][0])["table"]

    avg_h_goals = sum(t["goalsFor"] for t in h_table) / max(sum(t["playedGames"] for t in h_table), 1)
    avg_a_goals = sum(t["goalsFor"] for t in a_table) / max(sum(t["playedGames"] for t in a_table), 1)

    venue_data = {}
    # Build core data from Total table first
    for t in t_table:
        tid = t["team"]["id"]
        venue_data[tid] = {
            "name": t["team"]["name"],
            "rank": t["position"],
            "played": t["playedGames"]
        }

    for t in h_table:
        tid = t["team"]["id"]
        if tid in venue_data:
            venue_data[tid].update({
                "h_atk": (t["goalsFor"] / max(t["playedGames"], 1)) / avg_h_goals,
                "h_def": (t["goalsAgainst"] / max(t["playedGames"], 1)) / avg_h_goals
            })
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
    res = requests.get(f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5", headers=HEADERS)
    if res.status_code != 200: return 1.0, 0
    matches = res.json().get("matches", [])
    pts = sum(3 if (m["score"]["fullTime"]["home"] > m["score"]["fullTime"]["away"] and m["homeTeam"]["id"] == team_id) or 
              (m["score"]["fullTime"]["away"] > m["score"]["fullTime"]["home"] and m["awayTeam"]["id"] == team_id) 
              else 1 if m["score"]["fullTime"]["home"] == m["score"]["fullTime"]["away"] else 0 for m in matches)
    return 0.85 + (pts / 15 * 0.3), pts

def gaffer_logic(h_s, a_s, h_f_pts, a_f_pts, score):
    h_name, a_name = h_s['name'], a_s['name']
    h_rank, a_rank = h_s.get('rank', 10), a_s.get('rank', 10)
    
    # Situational Openers
    if h_rank <= 4 and a_rank <= 4:
        sit = f"This is a proper heavyweight clash at the top of the table."
    elif h_rank >= 17 or a_rank >= 17:
        sit = f"It's a scrap at the bottom. Every point is like gold for these two."
    elif abs(h_rank - a_rank) <= 3:
        sit = f"These two are neck-and-neck in the standings. Expect a cagey affair."
    else:
        sit = f"{h_name} are sitting {abs(h_rank - a_rank)} places apart from {a_name}."

    # Tactics Pool
    tactics = [
        f"I suspect {h_name} will try to boss the possession early on.",
        f"If {a_name} keep their shape, they'll find joy on the counter-attack.",
        f"It's going to be won or lost in the transitions today.",
        f"Expect a lot of tactical fouling to break up the rhythm."
    ]

    # Form Pool
    if h_f_pts >= 12: form = f"{h_name} are flying right now with {h_f_pts} points from 15."
    elif a_f_pts >= 12: form = f"The visitors are in a rich vein of form lately."
    else: form = "Both sides have been a bit hit-and-miss recently."

    return f"{sit} {random.choice(tactics)} {form} My gut says {score}."

@app.route("/fixtures", methods=["GET"])
def fixtures():
    d = request.args.get("date")
    # Fetch specifically for that day
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": d, "dateTo": d, "competitions": FREE_COMPS})
    
    grouped = {}
    matches = res.json().get("matches", [])
    for m in matches:
        league = m["competition"]["name"]
        if league not in grouped: grouped[league] = []
        grouped[league].append({
            "home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"],
            "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"],
            "competition": m["competition"]["code"]
        })
    return jsonify(grouped)

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    stats = get_venue_stats(data["competition"])
    h_s, a_s = stats.get(data["home_id"]), stats.get(data["away_id"])
    if not h_s or not a_s: return jsonify({"error": "Data Missing"})

    h_f, h_pts = get_form_multiplier(data["home_id"])
    a_f, a_pts = get_form_multiplier(data["away_id"])

    h_xg = h_s["h_atk"] * a_s["a_def"] * 1.35 * h_f
    a_xg = a_s["a_atk"] * h_s["h_def"] * 1.25 * a_f

    max_p, score, h_win, draw, a_win = 0, "1-1", 0, 0, 0
    for h in range(6):
        for a in range(6):
            p = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if p > max_p: max_p, score = p, f"{h}-{a}"
            if h > a: h_win += p
            elif h == a: draw += p
            else: a_win += p

    total = h_win + draw + a_win
    confidence = random.randint(78, 96) if max(h_win, draw, a_win)/total > 0.45 else random.randint(50, 77)

    return jsonify({
        "score": score,
        "probs": {"home": round(h_win/total*100), "draw": round(draw/total*100), "away": round(a_win/total*100)},
        "insight": gaffer_logic(h_s, a_s, h_pts, a_pts, score),
        "confidence": confidence,
        "h_rank": h_s['rank'], "a_rank": a_s['rank'],
        "metrics": {"h_atk": round(h_s['h_atk'],2), "a_def": round(a_s['a_def'],2), "h_form": h_pts, "a_form": a_pts}
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
