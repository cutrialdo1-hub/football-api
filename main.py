import math
import os
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

API_KEY = os.environ.get("FOOTBALL_API_KEY")

# ----------------------------
# TEAMS (unchanged)
# ----------------------------
TEAMS = {
    "Arsenal": {"home_scored": 2.25, "home_conceded": 0.69, "away_scored": 1.59, "away_conceded": 0.88},
    "Aston Villa": {"home_scored": 1.59, "home_conceded": 1.06, "away_scored": 1.25, "away_conceded": 1.44},
    "Bournemouth": {"home_scored": 1.47, "home_conceded": 1.12, "away_scored": 1.59, "away_conceded": 1.94},
    "Brentford": {"home_scored": 1.65, "home_conceded": 1.12, "away_scored": 1.25, "away_conceded": 1.56},
    "Brighton": {"home_scored": 1.59, "home_conceded": 1.00, "away_scored": 1.24, "away_conceded": 1.29},
    "Burnley": {"home_scored": 0.88, "home_conceded": 1.53, "away_scored": 1.12, "away_conceded": 2.47},
    "Chelsea": {"home_scored": 1.35, "home_conceded": 1.24, "away_scored": 1.76, "away_conceded": 1.41},
    "Crystal Palace": {"home_scored": 0.94, "home_conceded": 1.12, "away_scored": 1.27, "away_conceded": 1.13},
    "Everton": {"home_scored": 1.29, "home_conceded": 1.24, "away_scored": 1.13, "away_conceded": 1.13},
    "Fulham": {"home_scored": 1.69, "home_conceded": 1.19, "away_scored": 0.94, "away_conceded": 1.59},
    "Leeds Utd": {"home_scored": 1.47, "home_conceded": 1.18, "away_scored": 1.12, "away_conceded": 1.82},
    "Liverpool": {"home_scored": 1.81, "home_conceded": 1.06, "away_scored": 1.47, "away_conceded": 1.53},
    "Manchester City": {"home_scored": 2.38, "home_conceded": 0.75, "away_scored": 1.65, "away_conceded": 1.00},
    "Manchester Utd": {"home_scored": 1.94, "home_conceded": 1.19, "away_scored": 1.59, "away_conceded": 1.53},
    "Newcastle Utd": {"home_scored": 1.76, "home_conceded": 1.65, "away_scored": 1.00, "away_conceded": 1.31},
    "Tottenham": {"home_scored": 1.18, "home_conceded": 1.76, "away_scored": 1.38, "away_conceded": 1.44},
}

# ----------------------------
# FIXTURES (MATCHDAY SYSTEM)
# ----------------------------
@app.get("/fixtures")
def fixtures():
    url = "https://api.football-data.org/v4/matches"

    headers = {"X-Auth-Token": API_KEY}

    params = {
        "dateFrom": request.args.get("from"),
        "dateTo": request.args.get("to"),
        "competitions": "PL"
    }

    res = requests.get(url, headers=headers, params=params)

    print("STATUS:", res.status_code)
    print("RESPONSE:", res.text[:500])  # 👈 IMPORTANT DEBUG

    if res.status_code != 200:
        return jsonify({"error": "API failed", "status": res.status_code}), 500

    data = res.json()

    matches = data.get("matches", [])

    # if empty, return clearly
    if not matches:
        return jsonify([])

    grouped = {}

    for m in matches:
        date = m["utcDate"][:10]

        grouped.setdefault(date, []).append({
            "home": m["homeTeam"]["name"],
            "away": m["awayTeam"]["name"],
            "date": date
        })

    return jsonify(grouped)
# ----------------------------
@app.get("/")
def root():
    return jsonify({"status": "Matchday API running"})

@app.get("/teams")
def teams():
    return jsonify(list(TEAMS.keys()))

# ----------------------------
@app.post("/predict")
def predict():
    data = request.get_json()

    home = data.get("home_team")
    away = data.get("away_team")

    if home not in TEAMS or away not in TEAMS:
        return jsonify({"error": "Team not found"}), 400

    h = TEAMS[home]
    a = TEAMS[away]

    home_xg = h["home_scored"] * 0.8 + a["away_conceded"] * 0.6
    away_xg = a["away_scored"] * 0.8 + h["home_conceded"] * 0.6

    return jsonify({
        "home_team": home,
        "away_team": away,
        "expected_goals": {
            "home": home_xg,
            "away": away_xg
        },
        "outcomes": {
            "home_win": 0.45,
            "draw": 0.25,
            "away_win": 0.30
        },
        "top_scorelines": [
            {"score": "1-0", "probability": 0.12},
            {"score": "2-1", "probability": 0.10},
            {"score": "1-1", "probability": 0.09}
        ],
        "model": "Matchday Simplified"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
