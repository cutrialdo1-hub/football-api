"""
Football match predictor (Improved Poisson + Dixon-Coles model)
Full single-file API for Render deployment
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
# 2. MODEL (IMPROVED)
# ---------------------------------------------------------------------------

def calculate_team_strength(team_name):
    """
    Form-weighted attack/defence strength
    """
    team = TEAMS[team_name]

    form_weight = 0.06  # tuning knob

    home_attack = team["home_scored"] / LEAGUE_AVERAGES["home_scored"]
    home_defence = team["home_conceded"] / LEAGUE_AVERAGES["home_conceded"]
    away_attack = team["away_scored"] / LEAGUE_AVERAGES["away_scored"]
    away_defence = team["away_conceded"] / LEAGUE_AVERAGES["away_conceded"]

    # simple form adjustment
    home_attack *= (1 + form_weight)
    away_attack *= (1 - form_weight)

    return {
        "home_attack": home_attack,
        "home_defence": home_defence,
        "away_attack": away_attack,
        "away_defence": away_defence,
    }


def expected_goals(home_team, away_team):
    home = calculate_team_strength(home_team)
    away = calculate_team_strength(away_team)

    home_xg = home["home_attack"] * away["away_defence"] * LEAGUE_AVERAGES["home_scored"]
    away_xg = away["away_attack"] * home["home_defence"] * LEAGUE_AVERAGES["away_scored"]

    return home_xg, away_xg


def poisson_probability(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


# ---------------------------------------------------------------------------
# 3. DIXON-COLES ADJUSTMENT
# ---------------------------------------------------------------------------

def dixon_coles(i, j, lam, mu, rho=-0.08):
    """
    Adjusts low-score probabilities (key upgrade)
    """

    if i == 0 and j == 0:
        return 1 - (lam * mu * rho)
    if i == 0 and j == 1:
        return 1 + (lam * rho)
    if i == 1 and j == 0:
        return 1 + (mu * rho)
    if i == 1 and j == 1:
        return 1 - rho

    return 1.0


# ---------------------------------------------------------------------------
# 4. SCORE MATRIX (IMPROVED + NORMALISED)
# ---------------------------------------------------------------------------

def score_matrix(home_xg, away_xg):
    home_dist = [poisson_probability(k, home_xg) for k in range(MAX_GOALS + 1)]
    away_dist = [poisson_probability(k, away_xg) for k in range(MAX_GOALS + 1)]

    matrix = []

    for i, h in enumerate(home_dist):
        row = []
        for j, a in enumerate(away_dist):

            base = h * a
            adj = dixon_coles(i, j, home_xg, away_xg)

            row.append(base * adj)

        matrix.append(row)

    return normalize_matrix(matrix)


def normalize_matrix(matrix):
    total = sum(sum(row) for row in matrix)
    return [[cell / total for cell in row] for row in matrix]


# ---------------------------------------------------------------------------
# 5. OUTCOMES
# ---------------------------------------------------------------------------

def match_outcomes(matrix):
    home_win = draw = away_win = 0.0

    for i, row in enumerate(matrix):
        for j, p in enumerate(row):
            if i > j:
                home_win += p
            elif i == j:
                draw += p
            else:
                away_win += p

    return {
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win
    }


# ---------------------------------------------------------------------------
# 6. FLASK APP
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


@app.get("/")
def home():
    return jsonify({"status": "Football Predictor API running"})


@app.get("/healthz")
def health():
    return jsonify({"status": "ok"})


@app.get("/teams")
def teams():
    return jsonify(sorted(TEAMS.keys()))


@app.route("/predict", methods=["POST", "OPTIONS"])
def predict():
    if request.method == "OPTIONS":
        return jsonify({}), 200

    body = request.get_json(silent=True) or {}
    home_team = body.get("home_team")
    away_team = body.get("away_team")

    if not home_team or not away_team:
        return jsonify({"error": "missing teams"}), 400

    if home_team not in TEAMS or away_team not in TEAMS:
        return jsonify({"error": "unknown team"}), 400

    home_xg, away_xg = expected_goals(home_team, away_team)
    matrix = score_matrix(home_xg, away_xg)
    outcomes = match_outcomes(matrix)

    return jsonify({
        "home_team": home_team,
        "away_team": away_team,
        "expected_goals": {"home": home_xg, "away": away_xg},
        "outcomes": outcomes
    })


# ---------------------------------------------------------------------------
# 7. RUN
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
