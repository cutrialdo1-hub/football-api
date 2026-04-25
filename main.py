"""
Football match predictor (Poisson model) + Flask API
Fixed for Render + Netlify (CORS + preflight enabled)
"""

import math
import os
from flask import Flask, jsonify, request
from flask_cors import CORS


# ---------------------------------------------------------------------------
# 1. DATA
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 2. MODEL
# ---------------------------------------------------------------------------
def calculate_team_strength(team_name):
    team = TEAMS[team_name]
    return {
        "home_attack": team["home_scored"] / LEAGUE_AVERAGES["home_scored"],
        "home_defense": team["home_conceded"] / LEAGUE_AVERAGES["home_conceded"],
        "away_attack": team["away_scored"] / LEAGUE_AVERAGES["away_scored"],
        "away_defense": team["away_conceded"] / LEAGUE_AVERAGES["away_conceded"],
    }


def expected_goals(home_team, away_team):
    home = calculate_team_strength(home_team)
    away = calculate_team_strength(away_team)

    home_xg = home["home_attack"] * away["away_defense"] * LEAGUE_AVERAGES["home_scored"]
    away_xg = away["away_attack"] * home["home_defense"] * LEAGUE_AVERAGES["away_scored"]

    return home_xg, away_xg


def poisson_probability(k, expected):
    return (expected ** k) * math.exp(-expected) / math.factorial(k)


def score_matrix(home_xg, away_xg):
    home_dist = [poisson_probability(k, home_xg) for k in range(MAX_GOALS + 1)]
    away_dist = [poisson_probability(k, away_xg) for k in range(MAX_GOALS + 1)]
    return [[h * a for a in away_dist] for h in home_dist]


def match_outcomes(matrix):
    home_win = draw = away_win = 0.0

    for h, row in enumerate(matrix):
        for a, p in enumerate(row):
            if h > a:
                home_win += p
            elif h == a:
                draw += p
            else:
                away_win += p

    return {
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win
    }


# ---------------------------------------------------------------------------
# 3. FLASK APP (CORS FIXED)
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ✅ FIX CORS PROPERLY FOR NETLIFY
CORS(app, resources={r"/*": {"origins": "*"}})


@app.get("/")
def root():
    return jsonify({
        "status": "Football API running",
        "endpoints": ["/teams", "/predict", "/healthz"]
    })


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/teams")
def teams():
    return jsonify(sorted(TEAMS.keys()))


# ✅ FIX: handle OPTIONS preflight + POST
@app.route("/predict", methods=["POST", "OPTIONS"])
def predict():
    if request.method == "OPTIONS":
        return jsonify({}), 200

    body = request.get_json(silent=True) or {}
    home_team = body.get("home_team")
    away_team = body.get("away_team")

    if not home_team or not away_team:
        return jsonify({"error": "home_team and away_team required"}), 400

    if home_team not in TEAMS or away_team not in TEAMS:
        return jsonify({"error": "unknown team"}), 400

    home_xg, away_xg = expected_goals(home_team, away_team)
    matrix = score_matrix(home_xg, away_xg)
    outcomes = match_outcomes(matrix)

    return jsonify({
        "home_team": home_team,
        "away_team": away_team,
        "expected_goals": {
            "home": home_xg,
            "away": away_xg
        },
        "outcomes": outcomes
    })


# ---------------------------------------------------------------------------
# 4. RUN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
