import math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

# FULL 12 FREE COMPETITIONS
COMPETITIONS = [
    "CL", "PL", "BL1", "SA", "PD", "FL1",
    "DED", "PPL", "BSA", "ELC", "WC", "EC"
]

cache = {}

# ---------------------------
# POISSON
# ---------------------------
def poisson(k, lam):
    lam = max(min(lam, 3.5), 0.2)  # IMPORTANT FIX (prevents 4-4 chaos)
    return (lam**k * math.exp(-lam)) / math.factorial(k)

# ---------------------------
# STANDINGS
# ---------------------------
def get_standings(code):
    now = time.time()

    if code in cache and now - cache[code]["t"] < 86400:
        return cache[code]["d"]

    r = requests.get(f"{BASE_URL}/competitions/{code}/standings", headers=HEADERS)
    if r.status_code != 200:
        return {}

    data = r.json()

    try:
        total = next(s for s in data["standings"] if s["type"] == "TOTAL")["table"]

        out = {}

        for t in total:
            tid = t["team"]["id"]

            played = max(t["playedGames"], 1)

            gf = t["goalsFor"] / played
            ga = t["goalsAgainst"] / played

            out[tid] = {
                "name": t["team"]["name"],
                "rank": t["position"],
                "gf": gf,
                "ga": ga
            }

        cache[code] = {"t": now, "d": out}
        return out

    except:
        return {}

# ---------------------------
# FORM (FIXED WEIGHTING)
# ---------------------------
def form(team_id):
    r = requests.get(
        f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5",
        headers=HEADERS
    )

    if r.status_code != 200:
        return 1.0, 0

    matches = r.json().get("matches", [])

    pts = 0

    for m in matches:
        hs = m["score"]["fullTime"]["home"]
        aw = m["score"]["fullTime"]["away"]

        if m["homeTeam"]["id"] == team_id and hs > aw:
            pts += 3
        elif m["awayTeam"]["id"] == team_id and aw > hs:
            pts += 3
        elif hs == aw:
            pts += 1

    strength = 0.85 + (pts / 15) * 0.25  # tighter scaling
    return strength, pts

# ---------------------------
# FIXTURES (ALL 12 COMPETITIONS)
# ---------------------------
@app.route("/fixtures")
def fixtures():
    date = request.args.get("date")
    date_to = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    all_matches = []

    for comp in COMPETITIONS:
        try:
            r = requests.get(
                f"{BASE_URL}/competitions/{comp}/matches",
                headers=HEADERS,
                params={"dateFrom": date, "dateTo": date_to}
            )

            if r.status_code != 200:
                continue

            matches = r.json().get("matches", [])

            for m in matches:
                all_matches.append({
                    "home": m["homeTeam"]["name"],
                    "home_id": m["homeTeam"]["id"],
                    "away": m["awayTeam"]["name"],
                    "away_id": m["awayTeam"]["id"],
                    "comp": comp,
                    "league": m["competition"]["name"]
                })

        except:
            continue

    return jsonify(all_matches)

# ---------------------------
# PREDICTION (FIXED MODEL)
# ---------------------------
@app.route("/predict", methods=["POST"])
def predict():
    data = request.json

    stats = get_standings(data["comp"])

    h = stats.get(data["home_id"])
    a = stats.get(data["away_id"])

    if not h or not a:
        return jsonify({"error": "No stats available"}), 400

    hf, hp = form(data["home_id"])
    af, ap = form(data["away_id"])

    # FIXED XG MODEL (more stable football logic)
    home_adv = 1.10

    hxg = (h["gf"] * a["ga"]) * hf * home_adv
    axg = (a["gf"] * h["ga"]) * af

    hxg = max(min(hxg, 3.0), 0.3)
    axg = max(min(axg, 3.0), 0.3)

    best, score = 0, "1-1"
    hw = dw = aw = 0

    for i in range(6):
        for j in range(6):
            p = poisson(i, hxg) * poisson(j, axg)

            if p > best:
                best = p
                score = f"{i}-{j}"

            if i > j:
                hw += p
            elif i == j:
                dw += p
            else:
                aw += p

    total = hw + dw + aw

    return jsonify({
        "score": score,
        "probs": {
            "home": round(hw/total*100),
            "draw": round(dw/total*100),
            "away": round(aw/total*100)
        },
        "h_rank": h["rank"],
        "a_rank": a["rank"],
        "insight": f"{data['home']} vs {data['away']} tactical model stabilised."
    })

# ---------------------------
# RUN
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
