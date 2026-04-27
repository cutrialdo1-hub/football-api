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
    if lam <= 0:
        lam = 0.01
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

    if total == 0:
        return {"home_win": 0.33, "draw": 0.33, "away_win": 0.33}

    return {
        "home_win": home / total,
        "draw": draw / total,
        "away_win": away / total
    }


# ----------------------------
# SAFE TEAM MATCHING (IMPORTANT FIX)
# ----------------------------
def find_team(name, teams):
    if not name:
        return None

    name_lower = name.lower()

    for k in teams.keys():
        k_lower = k.lower()

        if name_lower == k_lower:
            return k

        if name_lower in k_lower or k_lower in name_lower:
            return k

    return None


# ----------------------------
# LEAGUE STATS
# ----------------------------
def get_team_strengths(competition):
    url = f"{BASE_URL}/competitions/{competition}/standings"
    res = requests.get(url, headers=HEADERS)

    if res.status_code != 200:
        return {}

    data = res.json()

    try:
        table = data["standings"][0]["table"]
    except:
        return {}

    teams = {}

    for t in table:
        name = t["team"]["name"]
        games = max(t["playedGames"], 1)

        attack = t["goalsFor"] / games
        defence = t["goalsAgainst"] / games

        teams[name] = {
            "attack": attack,
            "defence": defence
        }

    return teams


# ----------------------------
# FIXTURES
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
# PREDICT (UPGRADED + FIXED)
# ----------------------------
@app.post("/predict")
def predict():
    try:
        data = request.get_json()

        home = data.get("home_team")
        away = data.get("away_team")
        comp = data.get("competition", "PL")

        teams = get_team_strengths(comp)

        home_key = find_team(home, teams)
        away_key = find_team(away, teams)

        # fallback safe values
        if not home_key or not away_key:
            home_xg = 1.3
            away_xg = 1.1
        else:
            home_team = teams[home_key]
            away_team = teams[away_key]

            HOME_ADV = 1.15

            home_xg = (
                home_team["attack"] * away_team["defence"] * 1.25 * HOME_ADV
            )

            away_xg = (
                away_team["attack"] * home_team["defence"] * 1.10
            )

            # safety clamps (stability improvement)
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
            "model": "Upgraded Poisson v2 (Fixed Matching)"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ----------------------------
@app.get("/")
def root():
    return jsonify({"status": "API running"})


if __name__ == "__main__":
    app.run()
