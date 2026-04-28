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
    """Fetches Home, Away, and Total standings to calculate strengths and ranks."""
    now = time.time()
    if comp_code in standings_cache and (now - standings_cache[comp_code][0] < 86400):
        return standings_cache[comp_code][1]

    url = f"{BASE_URL}/competitions/{comp_code}/standings"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return {}

    data = res.json()
    # Safely find tables
    h_table = next((s for s in data["standings"] if s["type"] == "HOME"), data["standings"][0])["table"]
    a_table = next((s for s in data["standings"] if s["type"] == "AWAY"), data["standings"][0])["table"]
    full_table = next((s for s in data["standings"] if s["type"] == "TOTAL"), data["standings"][0])["table"]

    # Calculate League Goal Averages
    avg_h_goals = sum(t["goalsFor"] for t in h_table) / max(sum(t["playedGames"] for t in h_table), 1)
    avg_a_goals = sum(t["goalsFor"] for t in a_table) / max(sum(t["playedGames"] for t in a_table), 1)

    venue_data = {}
    # Build base from full table
    for t in full_table:
        tid = t["team"]["id"]
        venue_data[tid] = {
            "name": t["team"]["name"],
            "rank": t["position"],
            "points": t["points"]
        }
    
    # Add Home metrics
    for t in h_table:
        tid = t["team"]["id"]
        if tid in venue_data:
            venue_data[tid].update({
                "h_atk": (t["goalsFor"] / max(t["playedGames"], 1)) / avg_h_goals,
                "h_def": (t["goalsAgainst"] / max(t["playedGames"], 1)) / avg_h_goals
            })
    
    # Add Away metrics
    for t in a_table:
        tid = t["team"]["id"]
        if tid in venue_data:
            venue_data[tid].update({
                "a_atk": (t["goalsFor"] / max(t["playedGames"], 1)) / avg_a_goals,
                "a_def": (t["goalsAgainst"] / max(t["playedGames"], 1)) / avg_a_goals
            })
        
    standings_cache[comp_code] = (now, venue_data)
    return venue_data

def gaffer_logic(h_s, a_s, score, h_pts, a_pts):
    """Generates the randomized, context-aware speech."""
    h_name, a_name = h_s['name'], a_s['name']
    h_rank, a_rank = h_s.get('rank', 10), a_s.get('rank', 10)
    
    # 1. League Position Context
    if h_rank <= 4 and a_rank <= 4:
        sit = f"This is a massive six-pointer at the top of the table. A proper heavyweight clash."
    elif h_rank >= 17 or a_rank >= 17:
        sit = f"It's a scrap at the bottom. Neither side can afford to drop points here if they want to stay up."
    elif abs(h_rank - a_rank) <= 3:
        sit = f"These two are neck-and-neck in the standings. It’s going to be a cagey affair."
    else:
        sit = f"There's a {abs(h_rank - a_rank)} place gap in the table, but on match day, that often goes out the window."

    # 2. Tactical Randomization
    tactics = [
        f"I've watched {h_name} recently; they're well-drilled and love to overload the flanks.",
        f"If {a_name} can weather the early storm, they'll find gaps to exploit on the break.",
        f"Expect this one to be won or lost in the transitions. High intensity stuff.",
        f"Both managers will be telling their lads to keep it tight for the first twenty and see who blinks first."
    ]

    # 3. Form Context (Last 5)
    if h_pts >= 12:
        form = f"The home side is on an absolute tear lately, taking {h_pts} points from the last 15 available."
    elif a_pts >= 12:
        form = f"The visitors are the form team here, playing with real swagger at the moment."
    else:
        form = f"Neither side has been particularly consistent lately, picking up {h_pts} and {a_pts} points respectively in their last five."

    verdict = f"If you're asking me for a prediction, I’m calling it {score}."
    
    return f"{sit} {random.choice(tactics)} {form} {verdict}"

@app.route("/fixtures", methods=["GET"])
def fixtures():
    d = request.args.get("date")
    if not d: return jsonify({})
    
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": d, "dateTo": d, "competitions": FREE_COMPS})
    
    grouped = {}
    matches = res.json().get("matches", [])
    for m in matches:
        c_name = m["competition"]["name"]
        if c_name not in grouped: grouped[c_name] = []
        grouped[c_name].append({
            "home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"],
            "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"],
            "competition": m["competition"]["code"]
        })
    return jsonify(grouped)

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    comp, h_id, a_id = data["competition"], data["home_id"], data["away_id"]
    
    stats = get_venue_stats(comp)
    h_s = stats.get(h_id, {"name": "Home Team", "rank": "-", "h_atk": 1, "h_def": 1, "a_atk": 1, "a_def": 1})
    a_s = stats.get(a_id, {"name": "Away Team", "rank": "-", "h_atk": 1, "h_def": 1, "a_atk": 1, "a_def": 1})
    
    # Get Last 5 Form
    h_res = requests.get(f"{BASE_URL}/teams/{h_id}/matches?status=FINISHED&limit=5", headers=HEADERS).json()
    a_res = requests.get(f"{BASE_URL}/teams/{a_id}/matches?status=FINISHED&limit=5", headers=HEADERS).json()
    
    h_f_pts = sum(3 if (m["score"]["fullTime"]["home"] > m["score"]["fullTime"]["away"] and m["homeTeam"]["id"] == h_id) or (m["score"]["fullTime"]["away"] > m["score"]["fullTime"]["home"] and m["awayTeam"]["id"] == h_id) else 1 if m["score"]["fullTime"]["home"] == m["score"]["fullTime"]["away"] else 0 for m in h_res.get("matches", []))
    a_f_pts = sum(3 if (m["score"]["fullTime"]["home"] > m["score"]["fullTime"]["away"] and m["homeTeam"]["id"] == a_id) or (m["score"]["fullTime"]["away"] > m["score"]["fullTime"]["home"] and m["awayTeam"]["id"] == a_id) else 1 if m["score"]["fullTime"]["home"] == m["score"]["fullTime"]["away"] else 0 for m in a_res.get("matches", []))

    # Math
    h_xg = h_s.get("h_atk", 1) * a_s.get("a_def", 1) * 1.35 * (0.85 + (h_f_pts/15 * 0.3))
    a_xg = a_s.get("a_atk", 1) * h_s.get("h_def", 1) * 1.25 * (0.85 + (a_f_pts/15 * 0.3))

    max_p, score, h_win, draw, a_win = 0, "1-1", 0, 0, 0
    for h in range(5):
        for a in range(5):
            p = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if p > max_p: max_p, score = p, f"{h}-{a}"
            if h > a: h_win += p
            elif h == a: draw += p
            else: a_win += p

    total = h_win + draw + a_win
    return jsonify({
        "score": score,
        "probs": {"home": round(h_win/total*100), "draw": round(draw/total*100), "away": round(a_win/total*100)},
        "insight": gaffer_logic(h_s, a_s, score, h_f_pts, a_f_pts),
        "h_rank": h_s.get("rank", "-"), "a_rank": a_s.get("rank", "-"),
        "h_form": h_f_pts, "a_form": a_f_pts
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
