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
    try:
        h_table = next((s for s in data["standings"] if s["type"] == "HOME"), data["standings"][0])["table"]
        a_table = next((s for s in data["standings"] if s["type"] == "AWAY"), data["standings"][0])["table"]
        t_table = next((s for s in data["standings"] if s["type"] == "TOTAL"), data["standings"][0])["table"]

        avg_h_goals = sum(t["goalsFor"] for t in h_table) / max(sum(t["playedGames"] for t in h_table), 1)
        avg_a_goals = sum(t["goalsFor"] for t in a_table) / max(sum(t["playedGames"] for t in a_table), 1)

        venue_data = {}
        for t in t_table:
            tid = t["team"]["id"]
            venue_data[tid] = {"name": t["team"]["name"], "rank": t["position"]}

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
    except: return {}

def get_form(team_id):
    res = requests.get(f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5", headers=HEADERS)
    if res.status_code != 200: return 1.0, 0
    m = res.json().get("matches", [])
    pts = sum(3 if (x["score"]["fullTime"]["home"] > x["score"]["fullTime"]["away"] and x["homeTeam"]["id"] == team_id) or (x["score"]["fullTime"]["away"] > x["score"]["fullTime"]["home"] and x["awayTeam"]["id"] == team_id) else 1 if x["score"]["fullTime"]["home"] == x["score"]["fullTime"]["away"] else 0 for x in m)
    return 0.85 + (pts/15 * 0.3), pts

def gaffer_logic(h_name, a_name, h_s, a_s, h_pts, a_pts, score):
    h_rank, a_rank = h_s.get('rank', 10), a_s.get('rank', 10)
    h_atk, h_def = h_s.get('h_atk', 1), h_s.get('h_def', 1)
    a_atk, a_def = a_s.get('a_atk', 1), a_s.get('a_def', 1)
    
    # 1. Match Context
    if h_rank <= 4 and a_rank <= 4:
        context = f"This is a massive heavyweight clash at the top. Games like this decide seasons."
    elif h_rank >= 16 and a_rank >= 16:
        context = f"It's a proper relegation dogfight. Pure desperation from both sides."
    elif (a_rank - h_rank) >= 8:
        context = f"On paper, {h_name} should dictate this game, but complacency is a killer."
    elif (h_rank - a_rank) >= 8:
        context = f"Tough day at the office ahead for {h_name} hosting a high-flying {a_name} side."
    else:
        context = f"Not much separating these two in the table. It's going to be a tight affair."

    # 2. Stat-Driven Tactics
    if h_atk > 1.2 and a_def > 1.2:
        tactic = f"I've looked at the numbers. {h_name} are lethal here, and frankly, {a_name} are leaking goals like a sieve on the road."
    elif h_def < 0.8 and a_atk > 1.2:
        tactic = f"The visitors pose a massive threat on the counter, but {h_name} have made this stadium a fortress defensively."
    elif h_atk < 0.9 and a_def < 0.9:
        tactic = f"Don't expect a thriller. Both sides struggle to create and love to park the bus."
    else:
        tactic = "The midfield battle will dictate everything. Whoever wins the second balls takes the points."

    # 3. Form & Verdict
    h_goals, a_goals = int(score.split('-')[0]), int(score.split('-')[1])
    if h_goals > a_goals:
        verdict = f"With {h_name} taking {h_pts} points from their last 5, I back the home crowd to pull them over the line. I'm calling it {score}."
    elif a_goals > h_goals:
        verdict = f"{a_name} have real swagger right now. I think they'll go there and do a professional job. {score} to the visitors."
    else:
        if score == "0-0":
            verdict = "I can't separate them, and neither will the pitch. A bore draw, 0-0."
        else:
            verdict = f"They'll cancel each other out. Both managers might happily take a point right now. I'm going {score}."

    return f"{context} {tactic} {verdict}"

@app.route("/fixtures", methods=["GET"])
def fixtures():
    d = request.args.get("date")
    d_to = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": d, "dateTo": d_to, "competitions": FREE_COMPS})
    
    matches = res.json().get("matches", [])
    return jsonify([{
        "home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"],
        "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"],
        "comp": m["competition"]["code"], "league": m["competition"]["name"]
    } for m in matches])

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    stats = get_venue_stats(data["comp"])
    h_s, a_s = stats.get(data["home_id"]), stats.get(data["away_id"])
    if not h_s or not a_s: return jsonify({"error": "Stats unavailable"})

    h_f, h_pts = get_form(data["home_id"])
    a_f, a_pts = get_form(data["away_id"])

    h_xg = h_s.get("h_atk", 1) * a_s.get("a_def", 1) * 1.35 * h_f
    a_xg = a_s.get("a_atk", 1) * h_s.get("h_def", 1) * 1.25 * a_f

    max_p, score, h_win, draw, a_win = 0, "1-1", 0, 0, 0
    for h in range(5):
        for a in range(5):
            p = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if p > max_p: max_p, score = p, f"{h}-{a}"
            if h > a: h_win += p
            elif h == a: draw += p
            else: a_win += p

    total = h_win + draw + a_win
    conf = random.randint(75, 95) if max(h_win, draw, a_win)/total > 0.4 else random.randint(50, 74)

    return jsonify({
        "score": score,
        "probs": {"home": round(h_win/total*100), "draw": round(draw/total*100), "away": round(a_win/total*100)},
        "insight": gaffer_logic(data["home"], data["away"], h_s, a_s, h_pts, a_pts, score),
        "confidence": conf, "h_rank": h_s['rank'], "a_rank": a_s['rank'],
        "metrics": {"h_atk": round(h_s.get('h_atk', 1),2), "h_def": round(h_s.get('h_def', 1),2), "a_atk": round(a_s.get('a_atk', 1),2), "a_def": round(a_s.get('a_def', 1),2), "h_form": h_pts, "a_form": a_pts}
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
