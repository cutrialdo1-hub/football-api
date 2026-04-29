import math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

COMPETITIONS = [
    "CL","PL","PD","BL1","SA","FL1","ELC","DED","PPL","BSA"
]

# ---------------- CACHE LAYERS ----------------
standings_cache = {}
fixtures_cache = {}      # 🚀 NEW
form_cache = {}          # 🚀 NEW

# ---------------- POISSON (UNCHANGED) ----------------
def poisson(k, lam):
    lam = max(min(lam, 4.0), 0.1)
    return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)

# ---------------- FAST FORM (CACHED) ----------------
def get_detailed_form(team_id):
    now = time.time()

    if team_id in form_cache and now - form_cache[team_id]["t"] < 600:
        return form_cache[team_id]["d"], form_cache[team_id]["s"]

    try:
        r = requests.get(
            f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5",
            headers=HEADERS,
            timeout=5
        )

        if r.status_code != 200:
            return 1.0, "???"

        matches = r.json().get("matches", [])
        history = []
        pts = 0

        for m in matches:
            score = m["score"]["fullTime"]
            if score["home"] is None:
                continue

            hs, aw = score["home"], score["away"]
            is_home = m["homeTeam"]["id"] == team_id

            if (is_home and hs > aw) or (not is_home and aw > hs):
                history.append("W"); pts += 3
            elif hs == aw:
                history.append("D"); pts += 1
            else:
                history.append("L")

        form_string = "".join(history)
        multiplier = 0.85 + (pts / 15) * 0.3

        form_cache[team_id] = {
            "t": now,
            "d": multiplier,
            "s": form_string
        }

        return multiplier, form_string

    except:
        return 1.0, "???"

# ---------------- STANDINGS (UNCHANGED BUT SAFE) ----------------
def get_standings(code):
    now = time.time()

    if code in standings_cache and now - standings_cache[code]["t"] < 86400:
        return standings_cache[code]["d"]

    try:
        r = requests.get(
            f"{BASE_URL}/competitions/{code}/standings",
            headers=HEADERS,
            timeout=5
        )

        if r.status_code != 200:
            return {}

        data = r.json()

        table = next(s for s in data["standings"] if s["type"] == "TOTAL")["table"]

        out = {
            str(t["team"]["id"]): {
                "rank": t["position"],
                "gf": t["goalsFor"] / max(t["playedGames"], 1),
                "ga": t["goalsAgainst"] / max(t["playedGames"], 1)
            }
            for t in table
        }

        standings_cache[code] = {"t": now, "d": out}
        return out

    except:
        return {}

# ---------------- 🚀 FIXTURES (ULTRA FAST CACHE) ----------------
@app.route("/fixtures")
def fixtures():
    date = request.args.get("date")
    if not date:
        return jsonify([])

    now = time.time()

    # 🚀 return cached result instantly
    if date in fixtures_cache and now - fixtures_cache[date]["t"] < 300:
        return jsonify(fixtures_cache[date]["d"])

    try:
        r = requests.get(
            f"{BASE_URL}/matches",
            headers=HEADERS,
            params={
                "dateFrom": date,
                "dateTo": date,
                "competitions": ",".join(COMPETITIONS)
            },
            timeout=10
        )

        if r.status_code != 200:
            return jsonify([])

        matches = r.json().get("matches", [])

        all_matches = []

        for m in matches:
            if not m.get("homeTeam") or not m.get("awayTeam"):
                continue

            all_matches.append({
                "home": m["homeTeam"]["name"],
                "home_id": m["homeTeam"]["id"],
                "away": m["awayTeam"]["name"],
                "away_id": m["awayTeam"]["id"],
                "comp": m["competition"]["code"],
                "league": m["competition"]["name"]
            })

        # cache result
        fixtures_cache[date] = {
            "t": now,
            "d": all_matches
        }

        return jsonify(all_matches)

    except:
        return jsonify([])

# ---------------- PREDICTION (UNCHANGED MODEL) ----------------
@app.route("/predict", methods=["POST"])
def predict():
    data = request.json

    stats = get_standings(data["comp"])

    h_team = stats.get(str(data["home_id"]), {"gf": 1.2, "ga": 1.2, "rank": "N/A"})
    a_team = stats.get(str(data["away_id"]), {"gf": 1.0, "ga": 1.3, "rank": "N/A"})

    h_mult, h_form = get_detailed_form(data["home_id"])
    a_mult, a_form = get_detailed_form(data["away_id"])

    h_lam = (h_team["gf"] * a_team["ga"]) * h_mult * 1.15
    a_lam = (a_team["gf"] * h_team["ga"]) * a_mult

    prob_home = prob_draw = prob_away = 0
    max_p, predicted_score = -1, "1-1"

    for i in range(6):
        for j in range(6):
            p = poisson(i, h_lam) * poisson(j, a_lam)

            if p > max_p:
                max_p, predicted_score = p, f"{i}-{j}"

            if i > j:
                prob_home += p
            elif i == j:
                prob_draw += p
            else:
                prob_away += p

    h_name, a_name = data["home"], data["away"]

    diff = abs(int(h_team["rank"]) - int(a_team["rank"])) if h_team["rank"] != "N/A" else 999
    vibe = "A high-intensity clash for position." if diff <= 3 else "A David vs Goliath scenario."

    h_wins = h_form.count("W")

    if h_wins >= 4:
        form_note = f"{h_name} is currently untouchable; they're playing on another level."
    elif "LLL" in h_form:
        form_note = f"There's unrest in the {h_name} camp; the losses are mounting up."
    else:
        form_note = "Expect a measured approach from both dugouts."

    tactics = "We're likely to see a high press today." if h_lam + a_lam > 2.8 else "It'll be won in the mud and the shadows of the midfield."

    insight = f"{vibe} {form_note} {tactics} I’m going with {predicted_score} on my sheet."

    return jsonify({
        "score": predicted_score,
        "probs": {
            "home": round(prob_home * 100),
            "draw": round(prob_draw * 100),
            "away": round(prob_away * 100)
        },
        "h_rank": h_team["rank"],
        "a_rank": a_team["rank"],
        "h_form": h_form,
        "a_form": a_form,
        "insight": insight
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
