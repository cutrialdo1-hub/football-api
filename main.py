import math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

COMPETITIONS = ["PL", "PD", "BL1", "SA", "FL1", "CL", "DED", "PPL", "BSA", "ELC", "WC", "EC"]

cache_standings = {}

def poisson(k, lam):
    lam = max(min(lam, 4.0), 0.1) 
    return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)

def get_detailed_form(team_id):
    try:
        r = requests.get(f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5", headers=HEADERS, timeout=5)
        if r.status_code != 200: return 1.0, "???"
        
        matches = r.json().get("matches", [])
        history = []
        pts = 0
        for m in matches:
            score = m["score"]["fullTime"]
            if score["home"] is None: continue
            hs, aw = score["home"], score["away"]
            is_home = m["homeTeam"]["id"] == team_id
            if (is_home and hs > aw) or (not is_home and aw > hs):
                history.append("W"); pts += 3
            elif hs == aw:
                history.append("D"); pts += 1
            else:
                history.append("L")
        
        form_string = "".join(history)
        multiplier = 0.85 + (pts / 15) * 0.3 
        return multiplier, form_string
    except:
        return 1.0, "???"

def get_standings(code):
    now = time.time()
    if code in cache_standings and now - cache_standings[code]["t"] < 86400:
        return cache_standings[code]["d"]
    try:
        r = requests.get(f"{BASE_URL}/competitions/{code}/standings", headers=HEADERS, timeout=5)
        if r.status_code != 200: return {}
        data = r.json()
        table_data = next(s for s in data["standings"] if s["type"] == "TOTAL")["table"]
        out = {str(t["team"]["id"]): {
            "rank": t["position"],
            "gf": t["goalsFor"] / max(t["playedGames"], 1),
            "ga": t["goalsAgainst"] / max(t["playedGames"], 1)
        } for t in table_data}
        cache_standings[code] = {"t": now, "d": out}
        return out
    except: return {}

@app.route("/fixtures")
def fixtures():
    date = request.args.get("date")
    if not date: return jsonify([])
    all_matches = []
    for comp in COMPETITIONS:
        try:
            r = requests.get(f"{BASE_URL}/competitions/{comp}/matches", headers=HEADERS, params={"dateFrom": date, "dateTo": date}, timeout=5)
            if r.status_code == 200:
                for m in r.json().get("matches", []):
                    all_matches.append({
                        "home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"],
                        "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"],
                        "comp": comp, "league": m["competition"]["name"]
                    })
            time.sleep(0.6) # Anti-Rate-Limit Buffer
        except: continue
    return jsonify(all_matches)

@app.route("/predict", methods=["POST"])
def predict():
    data = request.json
    stats = get_standings(data["comp"])
    h_team = stats.get(str(data["home_id"]), {"gf": 1.2, "ga": 1.2, "rank": "N/A"})
    a_team = stats.get(str(data["away_id"]), {"gf": 1.0, "ga": 1.3, "rank": "N/A"})
    
    h_mult, h_form = get_detailed_form(data["home_id"])
    a_mult, a_form = get_detailed_form(data["away_id"])

    h_lam = (h_team["gf"] * a_team["ga"]) * h_mult * 1.15
    a_lam = (a_team["gf"] * h_team["ga"]) * a_mult

    prob_home = prob_draw = prob_away = 0
    max_p, predicted_score = -1, "1-1"

    for i in range(6):
        for j in range(6):
            p = poisson(i, h_lam) * poisson(j, a_lam)
            if p > max_p:
                max_p, predicted_score = p, f"{i}-{j}"
            if i > j: prob_home += p
            elif i == j: prob_draw += p
            else: prob_away += p

    # THE GAFFER'S PERSONALITY LOGIC
    h_name, a_name = data['home'], data['away']
    if h_team['rank'] != "N/A" and a_team['rank'] != "N/A":
        vibe = "A high-stakes clash at the top." if h_team['rank'] < 5 and a_team['rank'] < 5 else "A gritty mid-table battle."
    else: vibe = "Form is the only thing that matters here."

    if "L" * 3 in h_form: streak = f"The wheels have fallen off for {h_name} lately."
    elif "W" * 3 in h_form: streak = f"{h_name} is playing with massive confidence."
    else: streak = "Neither side is showing total dominance."

    insight = f"{vibe} {streak} {h_name} [{h_form}] meets {a_name} [{a_form}]. I'm putting my neck out for a {predicted_score}."

    return jsonify({
        "score": predicted_score,
        "probs": {"home": round(prob_home*100), "draw": round(prob_draw*100), "away": round(prob_away*100)},
        "h_rank": h_team["rank"], "a_rank": a_team["rank"],
        "h_form": h_form, "a_form": a_form, "insight": insight
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
