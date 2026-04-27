import math
import os
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_KEY = os.environ.get("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"

HEADERS = {"X-Auth-Token": API_KEY}

MAX_GOALS = 5

# ----------------------------
# POISSON MODEL
# ----------------------------
def poisson(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)

def build_matrix(home_xg, away_xg):
    matrix = []
    for h in range(MAX_GOALS + 1):
        row = []
        for a in range(MAX_GOALS + 1):
            row.append(poisson(h, home_xg) * poisson(a, away_xg))
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

# ----------------------------
# TEAM STRENGTHS (BY TEAM ID)
# ----------------------------
def get_team_strengths(competition):
    url = f"{BASE_URL}/competitions/{competition}/standings"
    res = requests.get(url, headers=HEADERS)

    if res.status_code != 200:
        return {}

    table = res.json()["standings"][0]["table"]

    goals_for = sum(t["goalsFor"] for t in table)
    goals_against = sum(t["goalsAgainst"] for t in table)
    games = sum(t["playedGames"] for t in table)

    avg_gf = goals_for / max(games, 1)
    avg_ga = goals_against / max(games, 1)

    teams = {}

    for t in table:
        team_id = t["team"]["id"]

        played = max(t["playedGames"], 1)

        attack = (t["goalsFor"] / played) / avg_gf
        defence = (t["goalsAgainst"] / played) / avg_ga

        teams[team_id] = {
            "attack": attack,
            "defence": defence
        }

    return teams

# ----------------------------
# FIXTURES (USE TEAM IDS)
# ----------------------------
@app.get("/fixtures")
def fixtures():
    try:
        date_from = request.args.get("from")

        if not date_from:
            return jsonify({"error": "Missing date"}), 400

        competitions = ["PL", "BL1", "SA", "PD", "FL1"]

        all_matches = []

        for comp in competitions:
            url = f"{BASE_URL}/competitions/{comp}/matches"

            params = {
                "dateFrom": date_from,
                "dateTo": date_from
            }

            res = requests.get(url, headers=HEADERS, params=params)

            if res.status_code != 200:
                continue

            data = res.json()

            for m in data.get("matches", []):

                all_matches.append({
                    "home": m["homeTeam"]["name"],
                    "away": m["awayTeam"]["name"],

                    # 🔥 CRITICAL FIX
                    "home_id": m["homeTeam"]["id"],
                    "away_id": m["awayTeam"]["id"],

                    "competition": comp,
                    "date": m["utcDate"][:10]
                })

        grouped = {}

        for m in all_matches:
            grouped.setdefault(m["date"], []).append(m)

        return jsonify({"data": grouped})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ----------------------------
# PREDICT (FIXED TO USE IDS)
# ----------------------------
@app.post("/predict")
def predict():
    try:
        data = request.get_json()

        home_id = data.get("home_id")
        away_id = data.get("away_id")
        comp = data.get("competition", "PL")

        teams = get_team_strengths(comp)

        # fallback
        if home_id not in teams or away_id not in teams:
            home_xg = 1.3
            away_xg = 1.1
        else:
            home = teams[home_id]
            away = teams[away_id]

            HOME_ADV = 1.15

            home_xg = 1.30 * home["attack"] * away["defence"] * HOME_ADV
            away_xg = 1.15 * away["attack"] * home["defence"]

            home_xg = max(0.2, min(home_xg, 4))
            away_xg = max(0.2, min(away_xg, 4))

        matrix = build_matrix(home_xg, away_xg)

        return jsonify({
            "expected_goals": {
                "home": home_xg,
                "away": away_xg
            },
            "outcomes": calculate_outcomes(matrix),
            "model": "ID-based Poisson v2"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/")
def root():
    return jsonify({"status": "API running"})
