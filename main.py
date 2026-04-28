import math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime, timedelta
# New official Google library
from google import genai
from google.genai import types

app = Flask(__name__)
CORS(app)

# --- CONFIG ---
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY") or os.environ.get("API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Initialize Gemini Client - Force Stable v1 Path
client = None
if GEMINI_API_KEY:
    try:
        # We define the client with strict HTTP options to avoid the v1beta 404
        client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options={'api_version': 'v1'}
        )
    except Exception as e:
        print(f"INIT ERROR: {e}")

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": FOOTBALL_API_KEY}
FREE_COMPS = "CL,PL,ELC,BL1,SA,PD,FL1,DED,PPL,BSA,EC,WC"
standings_cache = {}

# --- POISSON MATH ---
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

# --- THE GAFFER'S VERDICT ---
def gaffer_ai_verdict(h_name, a_name, h_s, a_s, h_pts, a_pts, score):
    if not client:
        return "The Gaffer's in the dressing room giving them the hairdryer treatment. He's letting the numbers speak for themselves today."

    context = (f"Match: {h_name} vs {a_name}. Prediction: {score}.")

    try:
        # Forcing model version 1.5-flash on the stable v1 API
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=f"You are 'The Gaffer', a blunt football manager. Analyze this in 3 sentences: {context}"
        )
        return response.text.strip()
    except Exception as e:
        print(f"GAFFER ERROR: {e}")
        # If this still fails, it's likely a region/billing issue on the Google account
        return "The Gaffer's lost his temper with the fourth official. He's letting the numbers speak for themselves today."

@app.route("/fixtures", methods=["GET"])
def fixtures():
    d = request.args.get("date")
    if not d: return jsonify([])
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
    insight = gaffer_ai_verdict(data["home"], data["away"], h_s, a_s, h_pts, a_pts, score)
    return jsonify({
        "score": score,
        "probs": {"home": round(h_win/total*100), "draw": round(draw/total*100), "away": round(a_win/total*100)},
        "insight": insight,
        "h_rank": h_s['rank'], "a_rank": a_s['rank'],
        "metrics": {"h_atk": round(h_s.get('h_atk', 1),2), "h_def": round(h_s.get('h_def', 1),2), 
                    "a_atk": round(a_s.get('a_atk', 1),2), "a_def": round(a_s.get('a_def', 1),2), 
                    "h_form": h_pts, "a_form": a_pts}
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
