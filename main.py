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
# TEAM NAME NORMALISATION
# ----------------------------
def normalize(name):
    if not name:
        return ""
    return (
        name.replace(" FC", "")
            .replace(" AFC", "")
            .replace(" CF", "")
            .strip()
    )

# ----------------------------
# FETCH STATS FROM STANDINGS
# ----------------------------
def get_team_stats(competition_code):
    url = f"{BASE_URL}/competitions/{competition_code}/standings"
    res = requests.get(url, headers=HEADERS)

    if res.status_code != 200:
        return {}

    data = res.json()

    table = data["standings"][0]["table"]

    stats = {}

    league_goals = 0
    league_games = 0

    # first pass: league averages
    for t in table:
        league_goals += t["goalsFor"]
        league_games += t["playedGames"]

    league_avg = league_goals / league_games if league_games else 1.4

    # second pass: team stats normalized
    for t in table:
        name = normalize(t["team"]["name"])
        games = t["playedGames"] or 1

        attack = (t["goalsFor"] / games) / league_avg
        defense = (t["goalsAgainst"] / games) / league_avg

        stats[name] = {
            "attack": attack,
            "defense": defense
        }

    return stats

# ----------------------------
# FIXTURES (ALL COMPETITIONS)
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
                    "home": normalize(m["homeTeam"]["name"]),
                    "away": normalize(m["awayTeam"]["name"]),
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
# PREDICT (FIXED MODEL)
# ----------------------------
@app.post("/predict")
def predict():
    try:
        data = request.get_json()

        home = normalize(data.get("home_team"))
        away = normalize(data.get("away_team"))
        competition = data.get("competition", "PL")

        stats = get_team_stats(competition)

        # fallback safe values
        if home not in stats or away not in stats:
            home_xg = 1.3
            away_xg = 1.3
        else:
            h = stats[home]
            a = stats[away]

            # 🔥 stronger model (balanced + normalized)
            home_xg = 1.35 * h["attack"] * (1 / (a["defense"] + 0.75))
            away_xg = 1.15 * a["attack"] * (1 / (h["defense"] + 0.75))

            # clamp unrealistic values
            home_xg = max(0.2, min(home_xg, 4.0))
            away_xg = max(0.2, min(away_xg, 4.0))

        matrix = build_matrix(home_xg, away_xg)

        return jsonify({
            "home_team": home,
            "away_team": away,
            "expected_goals": {
                "home": round(home_xg, 2),
                "away": round(away_xg, 2)
            },
            "outcomes": calculate_outcomes(matrix),
            "top_scorelines": top_scores(matrix),
            "model": "Improved Poisson v2"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ----------------------------
@app.get("/")
def root():
    return jsonify({"status": "API running"})

if __name__ == "__main__":
    app.run()
