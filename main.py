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
# POISSON
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
# REAL TEAM STRENGTH MODEL
# ----------------------------
def get_league_stats(competition):
    url = f"{BASE_URL}/competitions/{competition}/standings"
    res = requests.get(url, headers=HEADERS)

    if res.status_code != 200:
        return {}

    table = res.json()["standings"][0]["table"]

    total_goals_for = sum(t["goalsFor"] for t in table)
    total_goals_against = sum(t["goalsAgainst"] for t in table)
    total_games = sum(t["playedGames"] for t in table)

    return {
        "avg_goals_for": total_goals_for / total_games,
        "avg_goals_against": total_goals_against / total_games
    }

def get_team_strengths(competition):
    url = f"{BASE_URL}/competitions/{competition}/standings"
    res = requests.get(url, headers=HEADERS)

    if res.status_code != 200:
        return {}

    table = res.json()["standings"][0]["table"]

    league_stats = get_league_stats(competition)

    avg_goals = league_stats["avg_goals_for"]

    teams = {}

    for t in table:
        name = t["team"]["name"]
        games = max(t["playedGames"], 1)

        attack = (t["goalsFor"] / games) / avg_goals
        defence = (t["goalsAgainst"] / games) / avg_goals

        teams[name] = {
            "attack": attack,
            "defence": defence
        }

    return teams

# ----------------------------
# PREDICT (UPGRADED MODEL)
# ----------------------------
@app.post("/predict")
def predict():
    try:
        data = request.get_json()

        home = data.get("home_team")
        away = data.get("away_team")
        comp = data.get("competition", "PL")

        teams = get_team_strengths(comp)

        # fallback safety
        if home not in teams or away not in teams:
            home_xg = 1.3
            away_xg = 1.1
        else:
            home_team = teams[home]
            away_team = teams[away]

            # 🏠 real home advantage (important)
            HOME_ADV = 1.18

            home_xg = 1.35 * home_team["attack"] * away_team["defence"] * HOME_ADV
            away_xg = 1.20 * away_team["attack"] * home_team["defence"]

            # 🔧 smoothing (prevents extremes)
            home_xg = max(0.2, min(home_xg, 3.5))
            away_xg = max(0.2, min(away_xg, 3.5))

        matrix = build_matrix(home_xg, away_xg)

        return jsonify({
            "home_team": home,
            "away_team": away,
            "expected_goals": {
                "home": home_xg,
                "away": away_xg
            },
            "outcomes": calculate_outcomes(matrix),
            "model": "Upgraded Strength Poisson"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/")
def root():
    return jsonify({"status": "API running"})
