import math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

API_KEY = os.environ.get("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}
FREE_COMPS = "PL,PD,SA,BL1,FL1,DED,CL,EL"

cache = {}

def poisson_probability(actual, expected):
    expected = max(expected, 0.01)
    return (expected ** actual) * math.exp(-expected) / math.factorial(actual)

def get_venue_stats(comp_code):
    now = time.time()
    if comp_code in cache and (now - cache[comp_code][0] < 86400):
        return cache[comp_code][1]

    url = f"{BASE_URL}/competitions/{comp_code}/standings"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return {}

    data = res.json()
    try:
        standings = data.get("standings", [])
        # Fallback: If HOME/AWAY not found, just use the TOTAL table
        h_table = next((s for s in standings if s["type"] == "HOME"), standings[0])["table"]
        a_table = next((s for s in standings if s["type"] == "AWAY"), standings[0])["table"]
        t_table = next((s for s in standings if s["type"] == "TOTAL"), standings[0])["table"]

        avg_h_goals = sum(t["goalsFor"] for t in h_table) / max(sum(t["playedGames"] for t in h_table), 1)
        avg_a_goals = sum(t["goalsFor"] for t in a_table) / max(sum(t["playedGames"] for t in a_table), 1)

        venue = {}
        for t in t_table:
            venue[t["team"]["id"]] = {"name": t["team"]["name"], "rank": t["position"]}

        for t in h_table:
            tid = t["team"]["id"]
            if tid in venue:
                venue[tid]["h_atk"] = (t["goalsFor"] / max(t["playedGames"], 1)) / max(avg_h_goals, 0.1)
                venue[tid]["h_def"] = (t["goalsAgainst"] / max(t["playedGames"], 1)) / max(avg_h_goals, 0.1)

        for t in a_table:
            tid = t["team"]["id"]
            if tid in venue:
                venue[tid]["a_atk"] = (t["goalsFor"] / max(t["playedGames"], 1)) / max(avg_a_goals, 0.1)
                venue[tid]["a_def"] = (t["goalsAgainst"] / max(t["playedGames"], 1)) / max(avg_a_goals, 0.1)

        cache[comp_code] = (now, venue)
        return venue
    except Exception: return {}

def get_form(team_id):
    url = f"{BASE_URL}/teams/{team_id}/matches"
    res = requests.get(url, headers=HEADERS, params={"status": "FINISHED", "limit": 5})
    if res.status_code != 200: return 1.0, 0
    matches = res.json().get("matches", [])
    pts = 0
    for m in matches:
        hg, ag = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
        if m["homeTeam"]["id"] == team_id:
            pts += 3 if hg > ag else 1 if hg == ag else 0
        else:
            pts += 3 if ag > hg else 1 if hg == ag else 0
    return 0.85 + (pts / 15 * 0.3), pts

@app.route("/fixtures")
def fixtures():
    d = request.args.get("date")
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": d, "dateTo": d, "competitions": FREE_COMPS})
    matches = res.json().get("matches", [])
    return jsonify([{"home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"], "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"], "comp": m["competition"]["code"], "league": m["competition"]["name"]} for m in matches])

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    stats = get_venue_stats(data["comp"])
    
    # FALLBACK: If team data is missing, use neutral 1.0 baseline instead of erroring
    h_s = stats.get(data["home_id"], {"rank": "??", "h_atk": 1.0, "h_def": 1.0})
    a_s = stats.get(data["away_id"], {"rank": "??", "a_atk": 1.0, "a_def": 1.0})

    h_f, h_pts = get_form(data["home_id"])
    a_f, a_pts = get_form(data["away_id"])

    h_xg = h_s.get("h_atk", 1.0) * a_s.get("a_def", 1.0) * 1.3 * h_f
    a_xg = a_s.get("a_atk", 1.0) * h_s.get("h_def", 1.0) * 1.2 * a_f

    best, score, h_win, d_win, a_win = 0, "1-1", 0, 0, 0
    for h in range(5):
        for a in range(5):
            p = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if p > best: best, score = p, f"{h}-{a}"
            if h > a: h_win += p
            elif h == a: d_win += p
            else: a_win += p

    total = h_win + d_win + a_win
    
    # Simple Insight Logic
    insight = f"Tactical standoff. {data['home']} (Rank {h_s['rank']}) vs {data['away']} (Rank {a_s['rank']})."
    if h_win/total > 0.5: insight = f"Strong home advantage for {data['home']}. Expected to dominate."
    elif a_win/total > 0.5: insight = f"Tough road trip for {data['home']}. {data['away']} are clear favorites."

    return jsonify({
        "score": score,
        "probs": {"home": round(h_win/total*100), "draw": round(d_win/total*100), "away": round(a_win/total*100)},
        "insight": insight,
        "h_rank": h_s["rank"], "a_rank": a_s["rank"]
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
