import math
import os
import requests
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_KEY = os.environ.get("FOOTBALL_API_KEY")

BASE_URL = "https://api.football-data.org/v4"

HEADERS = {
    "X-Auth-Token": API_KEY
}

# ----------------------------
# POISSON MODEL
# ----------------------------
MAX_GOALS = 5

def poisson(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)

def build_matrix(home_xg, away_xg):
    matrix = []
    for h in range(MAX_GOALS + 1):
        row = []
        for a in range(MAX_GOALS + 1):
            p = poisson(h, home_xg) * poisson(a, away_xg)
            row.append(p)
        matrix.append(row)
    return matrix

def calculate_outcomes(matrix):
    home = draw = away = 0

    for h in range(len(matrix)):
        for a in range(len(matrix)):
            p = matrix[h][a]
            if h > a:
                home += p
            elif h == a:
                draw += p
            else:
                away += p

    total = home + draw + away

    return {
        "home_win": home / total,
        "draw": draw / total,
        "away_win": away / total
    }

def top_scores(matrix):
    scores = []

    for h in range(len(matrix)):
        for a in range(len(matrix)):
            scores.append({
                "score": f"{h}-{a}",
                "probability": matrix[h][a]
            })

    scores.sort(key=lambda x: x["probability"], reverse=True)
    return scores[:5]

# ----------------------------
# FETCH STANDINGS (REAL DATA)
# ----------------------------
def get_team_stats(competition_code):
    url = f"{BASE_URL}/competitions/{competition_code}/standings"

    res = requests.get(url, headers=HEADERS)

    if res.status_code != 200:
        return {}

    data = res.json()

    table = data["standings"][0]["table"]

    stats = {}

    for team in table:
        name = team["team"]["name"]

        games = team["playedGames"] or 1

        stats[name] = {
            "attack": team["goalsFor"] / games,
            "defense": team["goalsAgainst"] / games
        }

    return stats

# ----------------------------
# FIXTURES (FILTERED BY COMP)
# ----------------------------
@app.get("/fixtures")
def fixtures():
    try:
        date_from = request.args.get("from")

        if not date_from:
            return jsonify({"error": "Missing date"}), 400

        date_to = (
            datetime.strptime(date_from, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")

        # 🔥 LIMIT TO COMPETITIONS (IMPORTANT)
        competitions = ["PL", "BL1", "SA", "PD", "FL1"]

        all_matches = []

        for comp in competitions:
            url = f"{BASE_URL}/competitions/{comp}/matches"

            params = {
                "dateFrom": date_from,
                "dateTo": date_to
            }

            res = requests.get(url, headers=HEADERS, params=params)

            if res.status_code != 200:
                continue

            data = res.json()

            for m in data.get("matches", []):
                all_matches.append({
                    "home": m["homeTeam"]["name"],
                    "away": m["awayTeam"]["name"],
                    "competition": comp,
                    "date": m["utcDate"][:10]
                })

        if not all_matches:
            return jsonify({"data": {}})

        grouped = {}

        for m in all_matches:
            grouped.setdefault(m["date"], []).append(m)

        return jsonify({"data": grouped})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ----------------------------
# PREDICT (REAL MODEL)
# ----------------------------
@app.post("/predict")
def predict():
    try:
        data = request.get_json()

        home = data.get("home_team")
        away = data.get("away_team")
        competition = data.get("competition", "PL")

        team_stats = get_team_stats(competition)

        if home not in team_stats or away not in team_stats:
            # fallback
            home_xg = 1.2
            away_xg = 1.2
        else:
            home_attack = team_stats[home]["attack"]
            home_defense = team_stats[home]["defense"]

            away_attack = team_stats[away]["attack"]
            away_defense = team_stats[away]["defense"]

            # 🔥 SIMPLE BUT REAL MODEL
            home_xg = home_attack * (away_defense / 1.3)
            away_xg = away_attack * (home_defense / 1.3)

        matrix = build_matrix(home_xg, away_xg)

        return jsonify({
            "home_team": home,
            "away_team": away,
            "expected_goals": {
                "home": home_xg,
                "away": away_xg
            },
            "outcomes": calculate_outcomes(matrix),
            "top_scorelines": top_scores(matrix),
            "model": "Real Data Poisson"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ----------------------------
@app.get("/")
def root():
    return jsonify({"status": "API running"})

if __name__ == "__main__":
    app.run()
