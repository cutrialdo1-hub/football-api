import math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": FOOTBALL_API_KEY}

FREE_COMPS = "CL,PL,ELC,BL1,SA,PD,FL1"

# ----------------------------
# SAFE REQUEST
# ----------------------------
def safe_get(url, params=None):
    try:
        res = requests.get(url, headers=HEADERS, params=params)
        if res.status_code != 200:
            print("API ERROR:", res.text)
            return None
        return res.json()
    except Exception as e:
        print("REQUEST FAILED:", e)
        return None

# ----------------------------
# FIXTURES
# ----------------------------
@app.route("/fixtures", methods=["GET"])
def fixtures():
    d = request.args.get("date")

    if not d:
        return jsonify({"error": "Missing date"}), 400

    data = safe_get(
        f"{BASE_URL}/matches",
        {"dateFrom": d, "dateTo": d, "competitions": FREE_COMPS}
    )

    if not data:
        return jsonify([])

    matches = []

    for m in data.get("matches", []):
        matches.append({
            "home": m["homeTeam"]["name"],
            "away": m["awayTeam"]["name"],
            "home_id": m["homeTeam"]["id"],
            "away_id": m["awayTeam"]["id"],
            "comp": m["competition"]["code"],
            "league": m["competition"]["name"]
        })

    return jsonify(matches)

# ----------------------------
# STATS
# ----------------------------
def get_stats(comp, team_id):
    data = safe_get(f"{BASE_URL}/competitions/{comp}/standings")

    if not data or "standings" not in data:
        return {"rank": "N/A", "atk": 1.2, "df": 1.0}

    table = data["standings"][0]["table"]

    avg_goals = sum(t["goalsFor"] for t in table) / max(sum(t["playedGames"] for t in table), 1)

    for t in table:
        if t["team"]["id"] == team_id:
            return {
                "rank": t["position"],
                "atk": (t["goalsFor"] / max(t["playedGames"], 1)) / avg_goals,
                "df": (t["goalsAgainst"] / max(t["playedGames"], 1)) / avg_goals
            }

    return {"rank": "N/A", "atk": 1.2, "df": 1.0}

# ----------------------------
# POISSON
# ----------------------------
def poisson(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)

# ----------------------------
# PREDICT
# ----------------------------
@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()

    h_stats = get_stats(data["comp"], data["home_id"])
    a_stats = get_stats(data["comp"], data["away_id"])

    home_xg = h_stats["atk"] * a_stats["df"] * 1.3
    away_xg = a_stats["atk"] * h_stats["df"] * 1.1

    home_win = draw = away_win = 0
    best_score = "1-1"
    max_p = 0

    for h in range(5):
        for a in range(5):
            p = poisson(h, home_xg) * poisson(a, away_xg)

            if p > max_p:
                max_p = p
                best_score = f"{h}-{a}"

            if h > a:
                home_win += p
            elif h == a:
                draw += p
            else:
                away_win += p

    return jsonify({
        "score": best_score,
        "probs": {
            "home": round(home_win * 100),
            "draw": round(draw * 100),
            "away": round(away_win * 100)
        },
        "h_rank": h_stats["rank"],
        "a_rank": a_stats["rank"],
        "metrics": {
            "h_atk": round(h_stats["atk"], 2),
            "a_def": round(a_stats["df"], 2)
        }
    })

@app.route("/")
def home():
    return jsonify({"status": "running"})

if __name__ == "__main__":
    app.run()
