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
    res = requests.get(f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5", headers=HEADERS)
    if res.status_code != 200: return 1.0, 0
    matches = res.json().get("matches", [])
    pts = sum(3 if (m["homeTeam"]["id"] == team_id and m["score"]["fullTime"]["home"] > m["score"]["fullTime"]["away"]) or (m["awayTeam"]["id"] == team_id and m["score"]["fullTime"]["away"] > m["score"]["fullTime"]["home"]) else (1 if m["score"]["fullTime"]["home"] == m["score"]["fullTime"]["away"] else 0) for m in matches)
    return 0.85 + (pts / 15 * 0.3), pts

def gaffer_logic(h_name, a_name, h_s, a_s, h_f, a_f, score):
    """The AI Voice: Translating numbers into 'The Gaffer's' speech."""
    h_goals, a_goals = map(int, score.split('-'))
    
    # Analyze the clash
    if h_s['h_atk'] > 1.3 and a_s['a_def'] > 1.2:
        clash = f"Look, {h_name} are a different beast at home. They'll throw the kitchen sink at 'em."
    elif a_s['a_atk'] > 1.2 and h_s['h_def'] > 1.2:
        clash = f"The visitors have some real quality on the break. {a_name} won't just sit back and take it."
    else:
        clash = "This one's going to be won in the trenches. It's a proper tactical chess match."

    # Analyze form
    if h_f > a_f + 0.15:
        momentum = f"The home side is flying right now. Momentum is everything in this league."
    elif a_f > h_f + 0.15:
        momentum = f"Don't be fooled by the table; the visitors are in a rich vein of form."
    else:
        momentum = "Both sides have been a bit 'patchy' lately, to be honest."

    # Final verdict
    if h_goals > a_goals:
        verdict = f"If you're asking me, I'm backing {h_name} to get the job done. {score} feels right."
    elif a_goals > h_goals:
        verdict = f"I've got a sneaky feeling about an away win here. I'm calling it {score}."
    else:
        verdict = f"Neither side has enough to kill it off. Put me down for a {score} draw."

    return f"{clash} {momentum} {verdict}"

@app.route("/fixtures", methods=["GET"])
def fixtures():
    d = request.args.get("date")
    d_to = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": d, "dateTo": d_to, "competitions": "PL,BL1,SA,PD,FL1"})
    return jsonify([{"home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"], "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"], "competition": m["competition"]["code"]} for m in res.json().get("matches", [])])

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    stats = get_venue_stats(data["competition"])
    h_s, a_s = stats.get(data["home_id"]), stats.get(data["away_id"])
    if not h_s or not a_s: return jsonify({"error": "No data"})

    h_f, h_pts = get_form_multiplier(data["home_id"])
    a_f, a_pts = get_form_multiplier(data["away_id"])

    h_xg = h_s["h_atk"] * a_s["a_def"] * 1.40 * h_f
    a_xg = a_s["a_atk"] * h_s["h_def"] * 1.25 * a_f

    max_p, score, h_win, draw, a_win = 0, "1-1", 0, 0, 0
    for h in range(6):
        for a in range(6):
            p = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if p > max_p: max_p, score = p, f"{h}-{a}"
            if h > a: h_win += p
            elif h == a: draw += p
            else: a_win += p

    return jsonify({
        "score": score,
        "probs": {"home": round(h_win*100), "draw": round(draw*100), "away": round(a_win*100)},
        "insight": gaffer_logic(h_s['name'], a_s['name'], h_s, a_s, h_f, a_f, score),
        "metrics": {"h_atk": round(h_s['h_atk'],2), "a_def": round(a_s['a_def'],2), "h_form": h_pts, "a_form": a_pts}
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
