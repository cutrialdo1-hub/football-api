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

# List of all 12 Free Tier Competitions
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
    h_table = next((s for s in data["standings"] if s["type"] == "HOME"), data["standings"][0])["table"]
    a_table = next((s for s in data["standings"] if s["type"] == "AWAY"), data["standings"][0])["table"]
    full_table = next((s for s in data["standings"] if s["type"] == "TOTAL"), data["standings"][0])["table"]

    avg_h_goals = sum(t["goalsFor"] for t in h_table) / max(sum(t["playedGames"] for t in h_table), 1)
    avg_a_goals = sum(t["goalsFor"] for t in a_table) / max(sum(t["playedGames"] for t in a_table), 1)

    venue_data = {}
    for t in full_table:
        tid = t["team"]["id"]
        venue_data[tid] = {
            "name": t["team"]["name"],
            "rank": t["position"],
            "points": t["points"],
            "played": t["playedGames"]
        }
    
    for t in h_table:
        venue_data[t["team"]["id"]].update({
            "h_atk": (t["goalsFor"] / max(t["playedGames"], 1)) / avg_h_goals,
            "h_def": (t["goalsAgainst"] / max(t["playedGames"], 1)) / avg_h_goals
        })
    for t in a_table:
        venue_data[t["team"]["id"]].update({
            "a_atk": (t["goalsFor"] / max(t["playedGames"], 1)) / avg_a_goals,
            "a_def": (t["goalsAgainst"] / max(t["playedGames"], 1)) / avg_a_goals
        })
        
    standings_cache[comp_code] = (now, venue_data)
    return venue_data

def gaffer_logic(h_s, a_s, score, h_pts, a_pts):
    h_name, a_name = h_s['name'], a_s['name']
    h_rank, a_rank = h_s['rank'], a_s['rank']
    
    # 1. Situation Analysis
    if h_rank <= 4 and a_rank <= 4:
        sit = f"This is a massive six-pointer at the top of the table. A proper heavyweight clash."
    elif h_rank >= 17 or a_rank >= 17:
        sit = f"It's a scrap at the bottom. Neither side can afford to drop points here if they want to stay up."
    elif abs(h_rank - a_rank) < 3:
        sit = f"These two are neck-and-neck in the standings. It’s going to be a cagey affair."
    else:
        sit = f"{h_name} are sitting {abs(h_rank - a_rank)} places apart from {a_name}, but don't let the table fool you."

    # 2. Tactical Phrase Pools
    tactics = [
        f"I've watched {h_name} recently; they love to overload the flanks.",
        f"If {a_name} can weather the early storm, they'll find gaps in behind.",
        f"It'll come down to a bit of individual brilliance or a set-piece.",
        f"Both managers will be telling their lads to keep it tight for the first twenty."
    ]

    # 3. Form Analysis (Last 5 games)
    if h_pts >= 12:
        form = f"The home side is on an absolute tear lately, collecting {h_pts} points from their last five."
    elif a_pts >= 12:
        form = f"The visitors are the form team here, they're playing with real swagger."
    else:
        form = "Both teams have been a bit patchy, struggling for consistency in recent weeks."

    verdict = f"I’m calling it {score}. Take it or leave it."
    
    return f"{sit} {random.choice(tactics)} {form} {verdict}"

@app.route("/fixtures", methods=["GET"])
def fixtures():
    d = request.args.get("date")
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": d, "dateTo": d, "competitions": FREE_COMPS})
    
    # Grouping Logic
    grouped = {}
    for m in res.json().get("matches", []):
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
    stats = get_venue_stats(data["competition"])
    h_s, a_s = stats.get(data["home_id"]), stats.get(data["away_id"])
    
    res = requests.get(f"{BASE_URL}/teams/{data['home_id']}/matches?status=FINISHED&limit=5", headers=HEADERS)
    h_f_pts = sum(3 if (m["score"]["fullTime"]["home"] > m["score"]["fullTime"]["away"] and m["homeTeam"]["id"] == data["home_id"]) else 1 if m["score"]["fullTime"]["home"] == m["score"]["fullTime"]["away"] else 0 for m in res.json().get("matches", []))
    
    res = requests.get(f"{BASE_URL}/teams/{data['away_id']}/matches?status=FINISHED&limit=5", headers=HEADERS)
    a_f_pts = sum(3 if (m["score"]["fullTime"]["away"] > m["score"]["fullTime"]["home"] and m["awayTeam"]["id"] == data["away_id"]) else 1 if m["score"]["fullTime"]["home"] == m["score"]["fullTime"]["away"] else 0 for m in res.json().get("matches", []))

    h_xg = h_s["h_atk"] * a_s["a_def"] * 1.35 * (0.85 + (h_f_pts/15 * 0.3))
    a_xg = a_s["a_atk"] * h_s["h_def"] * 1.25 * (0.85 + (a_f_pts/15 * 0.3))

    max_p, score = 0, "1-1"
    for h in range(5):
        for a in range(5):
            p = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if p > max_p: max_p, score = p, f"{h}-{a}"

    return jsonify({
        "score": score,
        "insight": gaffer_logic(h_s, a_s, score, h_f_pts, a_f_pts),
        "h_rank": h_s["rank"], "a_rank": a_s["rank"],
        "h_form": h_f_pts, "a_form": a_f_pts
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
