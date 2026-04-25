import math
import os
from flask import Flask, jsonify, request
from flask_cors import CORS

# -----------------------------
# DATA
# -----------------------------
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
    "Liverpool": {"home_scored": 1.81, "home_conceded": 1.06, "away_scored": 1.47, "away_conceded": 1.53},
    "Manchester City": {"home_scored": 2.38, "home_conceded": 0.75, "away_scored": 1.65, "away_conceded": 1.00},
    "Manchester Utd": {"home_scored": 1.94, "home_conceded": 1.19, "away_scored": 1.59, "away_conceded": 1.53},
    "Tottenham": {"home_scored": 1.18, "home_conceded": 1.76, "away_scored": 1.38, "away_conceded": 1.44},
}

MAX_GOALS = 5


# -----------------------------
# MODEL
# -----------------------------
def poisson(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def xg(home, away):
    h = TEAMS[home]
    a = TEAMS[away]

    home_xg = (h["home_scored"] / LEAGUE_AVERAGES["home_scored"]) * (a["away_conceded"] / LEAGUE_AVERAGES["away_conceded"]) * LEAGUE_AVERAGES["home_scored"]

    away_xg = (a["away_scored"] / LEAGUE_AVERAGES["away_scored"]) * (h["home_conceded"] / LEAGUE_AVERAGES["home_conceded"]) * LEAGUE_AVERAGES["away_scored"]

    return home_xg, away_xg


def build_matrix(hxg, axg):
    h_dist = [poisson(i, hxg) for i in range(MAX_GOALS + 1)]
    a_dist = [poisson(j, axg) for j in range(MAX_GOALS + 1)]

    matrix = []
    for i in range(MAX_GOALS + 1):
        row = []
        for j in range(MAX_GOALS + 1):
            row.append(h_dist[i] * a_dist[j])
        matrix.append(row)

    return matrix


def outcomes(matrix):
    home = draw = away = 0

    for i in range(len(matrix)):
        for j in range(len(matrix)):
            if i > j:
                home += matrix[i][j]
            elif i == j:
                draw += matrix[i][j]
            else:
                away += matrix[i][j]

    return {
        "home_win": home,
        "draw": draw,
        "away_win": away
    }


def top_scores(matrix, limit=10):
    scores = []

    for i in range(len(matrix)):
        for j in range(len(matrix)):
            scores.append({
                "score": f"{i}-{j}",
                "probability": matrix[i][j]
            })

    scores.sort(key=lambda x: x["probability"], reverse=True)

    return scores[:limit]


# -----------------------------
# API
# -----------------------------
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


@app.get("/")
def home():
    return jsonify({"status": "Football Predictor Running"})


@app.get("/teams")
def teams():
    return jsonify(list(TEAMS.keys()))


@app.post("/predict")
def predict():
    data = request.get_json()

    home = data.get("home_team")
    away = data.get("away_team")

    hxg, axg = xg(home, away)
    matrix = build_matrix(hxg, axg)

    return jsonify({
        "home_team": home,
        "away_team": away,
        "expected_goals": {
            "home": hxg,
            "away": axg
        },
        "outcomes": outcomes(matrix),
        "top_scorelines": top_scores(matrix)
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
