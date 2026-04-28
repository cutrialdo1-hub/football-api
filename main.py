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
    
    # --- PART 1: THE STAKES (Table Context) ---
    if h_rank <= 6 and a_rank <= 6:
        stakes = "This is a massive six-pointer at the sharp end of the table. Promotion and European spots are defined by fixtures like this."
    elif h_rank >= 16 and a_rank >= 16:
        stakes = "Pure desperation today. It's a six-pointer at the bottom of the table where fear of losing often overrides the desire to win."
    elif h_rank <= 4 and a_rank >= 14:
        stakes = f"{h_name} will expect nothing less than three points here, but {a_name} are fighting for their lives and could be dangerous."
    elif a_rank <= 4 and h_rank >= 14:
        stakes = f"The visitors are flying high, but trips to struggling sides like {h_name} are classic potential banana skins."
    elif abs(h_rank - a_rank) <= 3:
        stakes = "These two are neck-and-neck in the standings. Expect a fiercely contested, cagey affair."
    else:
        stakes = f"{h_name} are looking to bridge the gap in the table against a stubborn {a_name} outfit."

    # --- PART 2: TACTICAL INSIGHT (Data-to-English Synthesis) ---
    insight = ""
    # Analyze Home Momentum
    if h_pts >= 10 and h_atk < 1.0:
        insight += f"{h_name} aren't exactly blowing teams away, but they've mastered the art of grinding out ugly results lately. "
    elif h_pts >= 10 and h_atk >= 1.2:
        insight += f"{h_name} are absolutely purring. Their attacking fluidity is perfectly matching their points haul. "
    elif h_pts <= 5 and h_atk >= 1.1:
        insight += f"Don't let the recent form fool you; {h_name} are creating enough chances, they just need to find a clinical edge. "
        
    # Analyze the Clash
    if h_atk > 1.1 and a_def > 1.1:
        insight += f"I suspect the home side will find plenty of joy in the final third against a leaky visiting defense."
    elif a_atk > 1.1 and h_def > 1.1:
        insight += f"{a_name} pack a real punch on the road, and the home defense looks highly vulnerable to being caught in transition."
    elif h_atk < 0.9 and a_atk < 0.9:
        insight += "With both attacks misfiring recently, the first goal today is going to be absolutely monumental."
    elif h_def < 0.8 and a_def < 0.8:
        insight += "We're looking at two highly disciplined defensive units here. Space will be at a premium."

    # --- PART 3: THE VERDICT (Tying predicted score to the narrative) ---
    h_goals, a_goals = int(score.split('-')[0]), int(score.split('-')[1])
    
    if h_goals > a_goals:
        v = f"I'm backing a comfortable {score} home win." if (h_goals - a_goals >= 2) else f"It'll be tight, but I'm calling a {score} home victory."
    elif a_goals > h_goals:
        v = f"A dominant {score} away performance is on the cards." if (a_goals - h_goals >= 2) else f"I've got a sneaky feeling for the visitors to nick a {score} win."
    else:
        v = "It's got a 0-0 bore draw written all over it." if score == "0-0" else f"I'll sit on the fence with a {score} draw—both sides have enough to hurt each other."

    return f"{stakes} {insight} {v}"

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
    return jsonify({
        "score": score,
        "probs": {"home": round(h_win/total*100), "draw": round(draw/total*100), "away": round(a_win/total*100)},
        "insight": gaffer_logic(data["home"], data["away"], h_s, a_s, h_pts, a_pts, score),
        "h_rank": h_s['rank'], "a_rank": a_s['rank'],
        "metrics": {"h_atk": round(h_s.get('h_atk', 1),2), "h_def": round(h_s.get('h_def', 1),2), 
                    "a_atk": round(a_s.get('a_atk', 1),2), "a_def": round(a_s.get('a_def', 1),2), 
                    "h_form": h_pts, "a_form": a_pts}
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
