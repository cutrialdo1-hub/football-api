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

# In-Memory Caches
standings_cache = {}
form_cache = {}
CACHE_TTL = 86400  # 24 Hours

# ----------------------------
# MATH & AI LOGIC
# ----------------------------
def poisson_probability(actual, expected):
    if expected <= 0: expected = 0.01
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)

def generate_detailed_insight(h_name, a_name, h_s, a_s, h_form, a_form, prediction):
    """AI logic that breaks down the 'Why' behind the numbers."""
    
    # 1. Structural Clash
    atk_v_def = h_s['attack'] - a_s['defence']
    if atk_v_def > 0.4:
        matchup = f"The primary driver is {h_name}'s high-octane attack (rated {h_s['attack']:.2f}) exploiting {a_name}'s defensive gaps."
    elif atk_v_def < -0.4:
        matchup = f"Expect a low-scoring affair as {a_name}'s robust defense is statistically primed to neutralize {h_name}."
    else:
        matchup = "This is a balanced tactical matchup where neither side holds a significant structural edge."

    # 2. Momentum Analysis
    form_diff = h_form - a_form
    if abs(form_diff) > 0.15:
        momentum = f"Current momentum is a major factor: {h_name if h_form > a_form else a_name} is performing {abs(form_diff)*100:.0f}% above their seasonal baseline."
    else:
        momentum = "Both squads are operating at their standard seasonal efficiency with no major form deviations."

    # 3. Probability Rationale
    h_goals, a_goals = map(int, prediction.split('-'))
    if h_goals > a_goals:
        logic = "The model weights home advantage and clinical finishing as the deciding factors."
    elif h_goals == a_goals:
        logic = "A draw is the high-probability outcome due to matching defensive resilience."
    else:
        logic = "The data suggests an away victory, driven by superior counter-attacking metrics."

    return f"{matchup} {momentum} {logic} The Poisson matrix identifies {prediction} as the point of highest statistical convergence."

def get_team_strengths(comp_code):
    now = time.time()
    if comp_code in standings_cache:
        tstamp, data = standings_cache[comp_code]
        if now - tstamp < CACHE_TTL: return data

    url = f"{BASE_URL}/competitions/{comp_code}/standings"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return {}

    data = res.json()
    table = data["standings"][0]["table"]
    avg_g = sum(t["goalsFor"] for t in table) / max(sum(t["playedGames"] for t in table), 1)

    strengths = {}
    for t in table:
        p = max(t["playedGames"], 1)
        strengths[t["team"]["id"]] = {
            "name": t["team"]["name"],
            "attack": (t["goalsFor"] / p) / avg_g,
            "defence": (t["goalsAgainst"] / p) / avg_g
        }
    standings_cache[comp_code] = (now, strengths)
    return strengths

def get_team_form(team_id):
    now = time.time()
    if team_id in form_cache:
        tstamp, val = form_cache[team_id]
        if now - tstamp < 3600: return val

    url = f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return 1.0

    matches = res.json().get("matches", [])
    points = sum(3 if (m["homeTeam"]["id"] == team_id and m["score"]["fullTime"]["home"] > m["score"]["fullTime"]["away"]) or 
                 (m["awayTeam"]["id"] == team_id and m["score"]["fullTime"]["away"] > m["score"]["fullTime"]["home"]) else 
                 1 if m["score"]["fullTime"]["home"] == m["score"]["fullTime"]["away"] else 0 for m in matches)
    
    multiplier = 0.8 + (points / 15 * 0.4)
    form_cache[team_id] = (now, multiplier)
    return multiplier

# ----------------------------
# API ROUTES
# ----------------------------
@app.route("/fixtures", methods=["GET"])
def fixtures():
    date_str = request.args.get("date")
    if not date_str: return jsonify({"error": "No date"}), 400
    date_dt = datetime.strptime(date_str, "%Y-%m-%d")
    date_to = (date_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    params = {"dateFrom": date_str, "dateTo": date_to, "competitions": "PL,BL1,SA,PD,FL1"}
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params=params)
    
    output = []
    if res.status_code == 200:
        for m in res.json().get("matches", []):
            output.append({
                "home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"],
                "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"],
                "competition": m["competition"]["code"]
            })
    return jsonify(output)

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    comp, h_id, a_id = data.get("competition"), data.get("home_id"), data.get("away_id")
    
    strengths = get_team_strengths(comp)
    h_s = strengths.get(h_id, {"attack": 1.0, "defence": 1.0, "name": "Home"})
    a_s = strengths.get(a_id, {"attack": 1.0, "defence": 1.0, "name": "Away"})

    h_form = get_team_form(h_id)
    a_form = get_team_form(a_id)

    h_xg = round(h_s["attack"] * a_s["defence"] * 1.35 * 1.10 * h_form, 2)
    a_xg = round(a_s["attack"] * h_s["defence"] * 1.35 * a_form, 2)

    home_p = draw_p = away_p = max_p = 0
    best_score = "1-1"
    for h in range(6):
        for a in range(6):
            prob = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if prob > max_p: max_p, best_score = prob, f"{h}-{a}"
            if h > a: home_p += prob
            elif h == a: draw_p += prob
            else: away_p += prob

    total = home_p + draw_p + away_p
    detailed_analysis = generate_detailed_insight(h_s['name'], a_s['name'], h_s, a_s, h_form, a_form, best_score)

    return jsonify({
        "prediction": best_score,
        "xg": {"home": h_xg, "away": a_xg},
        "form": {"home": round(h_form, 2), "away": round(a_form, 2)},
        "probs": {"home": round(home_p/total*100, 1), "draw": round(draw_p/total*100, 1), "away": round(away_p/total*100, 1)},
        "analysis": detailed_analysis,
        "raw_metrics": {"h_atk": round(h_s['attack'], 2), "a_def": round(a_s['defence'], 2)}
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
