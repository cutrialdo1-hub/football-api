from flask_cors import CORS
"""
Football match predictor (Poisson model) + Flask API
Ready for deployment on Render
"""

import math
import os
from flask import Flask, jsonify, request

# ----------------------------
# DATA
# ----------------------------
LEAGUE_AVERAGES = {
    "home_scored": 1.50,
    "home_conceded": 1.23,
    "away_scored": 1.23,
    "away_conceded": 1.50,
}

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
    "Nottm Forest": {"home_scored": 1.06, "home_conceded": 1.24, "away_scored": 1.13, "away_conceded": 1.50},
    "Sunderland": {"home_scored": 1.44, "home_conceded": 0.88, "away_scored": 0.76, "away_conceded": 1.53},
    "Tottenham": {"home_scored": 1.18, "home_conceded": 1.76, "away_scored": 1.38, "away_conceded": 1.44},
    "West Ham Utd": {"home_scored": 1.38, "home_conceded": 1.75, "away_scored": 1.06, "away_conceded": 1.71},
    "Wolverhampton": {"home_scored": 1.06, "home_conceded": 1.94, "away_scored": 0.41, "away_conceded": 1.76},
}

MAX_GOALS = 5

# ----------------------------
# MODEL
# ----------------------------
def poisson(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def team_strength(team):
    t = TEAMS[team]
    return {
        "ha": t["home_scored"] / LEAGUE_AVERAGES["home_scored"],
        "hd": t["home_conceded"] / LEAGUE_AVERAGES["home_conceded"],
        "aa": t["away_scored"] / LEAGUE_AVERAGES["away_scored"],
        "ad": t["away_conceded"] / LEAGUE_AVERAGES["away_conceded"],
    }


def expected_goals(home, away):
    h = team_strength(home)
    a = team_strength(away)

    home_xg = h["ha"] * a["ad"] * LEAGUE_AVERAGES["home_scored"]
    away_xg = a["aa"] * h["hd"] * LEAGUE_AVERAGES["away_scored"]

    return home_xg, away_xg


def matrix(hxg, axg):
    h = [poisson(i, hxg) for i in range(MAX_GOALS + 1)]
    a = [poisson(i, axg) for i in range(MAX_GOALS + 1)]
    return [[hi * aj for aj in a] for hi in h]


def outcomes(mat):
    home = draw = away = 0

    for i in range(len(mat)):
        for j in range(len(mat[i])):
            if i > j:
                home += mat[i][j]
            elif i == j:
                draw += mat[i][j]
            else:
                away += mat[i][j]

    return {"home_win": home, "draw": draw, "away_win": away}


# ----------------------------
# API
# ----------------------------
app = Flask(__name__)


@app.get("/")
def home():
    return jsonify({"status": "Football API running"})


@app.get("/teams")
def teams():
    return jsonify(list(TEAMS.keys()))


@app.get("/healthz")
def health():
    return jsonify({"ok": True})


@app.post("/predict")
def predict():
    data = request.get_json()

    home = data.get("home_team")
    away = data.get("away_team")

    if home not in TEAMS or away not in TEAMS:
        return jsonify({"error": "Invalid team"}), 400

    hxg, axg = expected_goals(home, away)
    m = matrix(hxg, axg)
    o = outcomes(m)

    return jsonify({
        "home_team": home,
        "away_team": away,
        "expected_goals": {"home": hxg, "away": axg},
        "outcomes": o
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
