import math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from google import genai

app = Flask(__name__)
CORS(app)

# --- CONFIG ---
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"AI Client Init Failed: {e}")

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": FOOTBALL_API_KEY}

# --- LOGIC ---
def poisson_probability(actual, expected):
    if expected <= 0:
        expected = 0.01
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)


def get_stats(team_id):
    default = {"rank": "N/A", "atk": 1.2, "df": 1.0}

    try:
        # NOTE: using Premier League as default fallback for stats
        res = requests.get(
            f"{BASE_URL}/competitions/PL/standings",
            headers=HEADERS
        )

        data = res.json()

        if "standings" not in data:
            return default

        table = data["standings"][0]["table"]

        avg_g = sum(t["goalsFor"] for t in table) / max(sum(t["playedGames"] for t in table), 1)

        for t in table:
            if t["team"]["id"] == team_id:
                return {
                    "rank": t["position"],
                    "atk": (t["goalsFor"] / max(t["playedGames"], 1)) / avg_g,
                    "df": (t["goalsAgainst"] / max(t["playedGames"], 1)) / avg_g
                }

    except Exception as e:
        print("Stats error:", e)

    return default


def gaffer_verdict(h_name, a_name, score):
    if not client:
        return "The Gaffer's busy in the dressing room."

    prompt = f"Blunt manager prediction for {h_name} vs {a_name}. Score: {score}. Use football manager tone."

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        return response.text.strip()
    except:
        return "Play for the badge. Keep it simple."


# --- FIXED FIXTURES ENDPOINT ---
@app.route("/fixtures", methods=["GET"])
def fixtures():
    date = request.args.get("date")

    if not date:
        return jsonify([])

    try:
        res = requests.get(
            f"{BASE_URL}/matches",
            headers=HEADERS,
            params={
                "dateFrom": date,
                "dateTo": date,
                "status": "SCHEDULED"
            }
        )

        data = res.json()

        matches = data.get("matches", [])

        return jsonify([
            {
                "home": m["homeTeam"]["name"],
                "home_id": m["homeTeam"]["id"],
                "away": m["awayTeam"]["name"],
                "away_id": m["awayTeam"]["id"],
                "comp": m["competition"]["code"],
                "league": m["competition"]["name"]
            }
            for m in matches
        ])

    except Exception as e:
        print("Fixtures error:", e)
        return jsonify([])


# --- PREDICT ---
@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()

    h_s = get_stats(data["home_id"])
    a_s = get_stats(data["away_id"])

    h_xg = h_s["atk"] * a_s["df"] * 1.3
    a_xg = a_s["atk"] * h_s["df"] * 1.1

    max_p = 0
    score = "1-1"
    h_w = d = a_w = 0

    for h in range(5):
        for a in range(5):
            p = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)

            if p > max_p:
                max_p = p
                score = f"{h}-{a}"

            if h > a:
                h_w += p
            elif h == a:
                d += p
            else:
                a_w += p

    return jsonify({
        "score": score,
        "probs": {
            "home": round(h_w * 100),
            "draw": round(d * 100),
            "away": round(a_w * 100)
        },
        "insight": gaffer_verdict(data["home"], data["away"], score),
        "h_rank": h_s["rank"],
        "a_rank": a_s["rank"],
        "metrics": {
            "h_atk": round(h_s["atk"], 2),
            "a_def": round(a_s["df"], 2)
        }
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
