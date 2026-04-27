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

HEADERS = {"X-Auth-Token": API_KEY}

# ----------------------------
# POISSON MODEL
# ----------------------------
MAX_GOALS = 5

def poisson(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)

def build_matrix(home_xg, away_xg):
    return [
        [poisson(h, home_xg) * poisson(a, away_xg)
         for a in range(MAX_GOALS + 1)]
        for h in range(MAX_GOALS + 1)
    ]

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

    return sorted(scores, key=lambda x: x["probability"], reverse=True)[:5]

# ----------------------------
# REAL TEAM MODEL (FIXED)
# ----------------------------
def get_team_model(competition):
    url = f"{BASE_URL}/competitions/{competition}/standings"
    res = requests.get(url, headers=HEADERS)

    if res.status_code != 200:
        return {}

    data = res.json()
    table = data["standings"][0]["table"]

    stats = {}

    total_goals_for = 0
    total_goals_against = 0
    total_games = 0

    # build raw stats
    for t in table:
        team_id = t["team"]["id"]
        games = max(t["playedGames"], 1)

        gf = t["goalsFor"]
        ga = t["goalsAgainst"]

        total_goals_for += gf
        total_goals_against += ga
        total_games += games

        stats[team_id] = {
            "attack": gf / games,
            "defense": ga / games
        }

    # league baseline (IMPORTANT FIX)
    league_attack_avg = total_goals_for / total_games
    league_def_avg = total_goals_against / total_games

    return {
        "teams": stats,
        "league_attack_avg": league_attack_avg,
        "league_def_avg": league_def_avg
    }

# ----------------------------
# FIXTURES
# ----------------------------
@app.get("/fixtures")
def fixtures():
    date_from = request.args.get("from")

    if not date_from:
        return jsonify({"error": "Missing date"}), 400

    date_to = (
        datetime.strptime(date_from, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    competitions = ["PL", "BL1", "SA", "PD", "FL1"]

    all_matches = []

    for comp in competitions:
        url = f"{BASE_URL}/competitions/{comp}/matches"

        res = requests.get(url, headers=HEADERS, params={
            "dateFrom": date_from,
            "dateTo": date_to
        })

        if res.status_code != 200:
            continue

        for m in res.json().get("matches", []):
            all_matches.append({
                "home": m["homeTeam"]["name"],
                "away": m["awayTeam"]["name"],
                "home_id": m["homeTeam"]["id"],
                "away_id": m["awayTeam"]["id"],
                "competition": comp,
                "date": m["utcDate"][:10]
            })

    grouped = {}

    for m in all_matches:
        grouped.setdefault(m["date"], []).append(m)

    return jsonify({"data": grouped})

# ----------------------------
# PREDICT (FIXED ACCURACY MODEL)
# ----------------------------
@app.post("/predict")
def predict():
    data = request.get_json()

    home_id = data.get("home_id")
    away_id = data.get("away_id")
    competition = data.get("competition", "PL")

    model = get_team_model(competition)

    teams = model.get("teams", {})

    # fallback
    if home_id not in teams or away_id not in teams:
        home_xg = 1.3
        away_xg = 1.1
    else:
        h = teams[home_id]
        a = teams[away_id]

        league_attack = model["league_attack_avg"]
        league_def = model["league_def_avg"]

        # 🔥 NORMALISED MODEL (KEY FIX)
        home_xg = (h["attack"] / league_attack) * (a["defense"])
        away_xg = (a["attack"] / league_attack) * (h["defense"])

        # stabilise values
        home_xg = max(0.2, min(home_xg, 3.5))
        away_xg = max(0.2, min(away_xg, 3.5))

    matrix = build_matrix(home_xg, away_xg)

    return jsonify({
        "home_team": home_id,
        "away_team": away_id,
        "expected_goals": {
            "home": home_xg,
            "away": away_xg
        },
        "outcomes": calculate_outcomes(matrix),
        "top_scorelines": top_scores(matrix),
        "model": "Improved Normalised Poisson"
    })

# ----------------------------
@app.get("/")
def root():
    return jsonify({"status": "API running"})
