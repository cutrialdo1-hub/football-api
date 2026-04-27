import math
import os
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)

# ✅ CORS FIX (important for Netlify)
CORS(app, resources={r"/*": {"origins": "*"}})

# 🔐 API KEY (set in Render environment)
API_KEY = os.environ.get("FOOTBALL_API_KEY")

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
def team_strength(team):
    t = TEAMS.get(team)
    if not t:
        return None

    return {
        "attack_home": t["home_scored"] / LEAGUE_AVERAGES["home_scored"],
        "def_home": t["home_conceded"] / LEAGUE_AVERAGES["home_conceded"],
        "attack_away": t["away_scored"] / LEAGUE_AVERAGES["away_scored"],
        "def_away": t["away_conceded"] / LEAGUE_AVERAGES["away_conceded"],
    }

def expected_goals(home, away):
    h = team_strength(home)
    a = team_strength(away)

    if not h or not a:
        return None, None

    home_xg = h["attack_home"] * a["def_away"] * LEAGUE_AVERAGES["home_scored"]
    away_xg = a["attack_away"] * h["def_home"] * LEAGUE_AVERAGES["away_scored"]

    return home_xg, away_xg

def poisson(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)

def dc_adjust(h, a, hxg, axg):
    rho = -0.08
    if h == 0 and a == 0:
        return 1 - (hxg * axg * rho)
    if h == 0 and a == 1:
        return 1 + (hxg * rho)
    if h == 1 and a == 0:
        return 1 + (axg * rho)
    if h == 1 and a == 1:
        return 1 - rho
    return 1.0

def score_matrix(hxg, axg):
    matrix = []
    for h in range(MAX_GOALS + 1):
        row = []
        for a in range(MAX_GOALS + 1):
            p = poisson(h, hxg) * poisson(a, axg)
            p *= dc_adjust(h, a, hxg, axg)
            row.append(p)
        matrix.append(row)
    return matrix

def outcomes(matrix):
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
        "away_win": away / total,
    }

def top_scorelines(matrix):
    results = []
    for h in range(len(matrix)):
        for a in range(len(matrix)):
            results.append({
                "score": f"{h}-{a}",
                "probability": matrix[h][a]
            })

    results.sort(key=lambda x: x["probability"], reverse=True)
    return results[:6]

# ----------------------------
# 📅 FIXTURES (API)
# ----------------------------
@app.get("/fixtures")
def fixtures():
    date_from = request.args.get("from")
    date_to = request.args.get("to")

    if not date_from or not date_to:
        return jsonify({"error": "Provide from and to dates"}), 400

    url = f"https://api.football-data.org/v4/matches?dateFrom={date_from}&dateTo={date_to}"
    headers = {"X-Auth-Token": API_KEY}

    res = requests.get(url, headers=headers)

    if res.status_code != 200:
        return jsonify({"error": "API failed"}), 500

    data = res.json()

    fixtures = []
    for m in data.get("matches", []):
        fixtures.append({
            "date": m["utcDate"][:10],
            "home": m["homeTeam"]["name"],
            "away": m["awayTeam"]["name"]
        })

    return jsonify(fixtures)

# ----------------------------
# API ROUTES
# ----------------------------
@app.get("/")
def root():
    return jsonify({"status": "Football API running with fixtures"})

@app.get("/teams")
def teams():
    return jsonify(list(TEAMS.keys()))

@app.post("/predict")
def predict():
    data = request.get_json()

    home = data.get("home_team")
    away = data.get("away_team")

    hxg, axg = expected_goals(home, away)

    if hxg is None:
        return jsonify({"error": "Team not supported in model"}), 400

    matrix = score_matrix(hxg, axg)

    return jsonify({
        "home_team": home,
        "away_team": away,
        "expected_goals": {
            "home": hxg,
            "away": axg
        },
        "outcomes": outcomes(matrix),
        "top_scorelines": top_scorelines(matrix),
        "model": "Dixon-Coles"
    })

# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
