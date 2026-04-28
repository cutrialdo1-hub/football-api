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

FREE_COMPS = "PL,CL,BL1,SA,PD,FL1,DED"

cache = {}

def poisson(k, lam):
    lam = max(lam, 0.01)
    return (lam**k * math.exp(-lam)) / math.factorial(k)

def get_standings(code):
    now = time.time()

    if code in cache and now - cache[code]["t"] < 86400:
        return cache[code]["d"]

    r = requests.get(f"{BASE_URL}/competitions/{code}/standings", headers=HEADERS)
    if r.status_code != 200:
        return {}

    data = r.json()

    try:
        standings = data["standings"]

        total = next(s for s in standings if s["type"] == "TOTAL")["table"]

        out = {}

        for t in total:
            tid = t["team"]["id"]
            out[tid] = {
                "name": t["team"]["name"],
                "rank": t["position"],
                "gf": t["goalsFor"] / max(t["playedGames"], 1),
                "ga": t["goalsAgainst"] / max(t["playedGames"], 1)
            }

        cache[code] = {"t": now, "d": out}
        return out

    except:
        return {}

def form(team_id):
    r = requests.get(
        f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5",
        headers=HEADERS
    )

    if r.status_code != 200:
        return 1.0, 0

    m = r.json().get("matches", [])
    pts = 0

    for x in m:
        hs = x["score"]["fullTime"]["home"]
        aw = x["score"]["fullTime"]["away"]

        if x["homeTeam"]["id"] == team_id and hs > aw:
            pts += 3
        elif x["awayTeam"]["id"] == team_id and aw > hs:
            pts += 3
        elif hs == aw:
            pts += 1

    return 0.9 + (pts / 15), pts

COMPETITIONS = [
    "PL","CL","BL1","SA","PD","FL1","DED","PPL","BSA","ELC"
]

@app.route("/fixtures")
def fixtures():
    date = request.args.get("date")
    date_to = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    all_matches = []

    for comp in COMPETITIONS:
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

    return jsonify(all_matches)
        "home": m["homeTeam"]["name"],
        "home_id": m["homeTeam"]["id"],
        "away": m["awayTeam"]["name"],
        "away_id": m["awayTeam"]["id"],
        "comp": m["competition"]["code"],
        "league": m["competition"]["name"]
    } for m in matches])

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

    hxg = h["gf"] * a["ga"] * hf
    axg = a["gf"] * h["ga"] * af

    best, score = 0, "1-1"
    hw = dw = aw = 0

    for i in range(5):
        for j in range(5):
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
        "insight": f"{data['home']} vs {data['away']} looks balanced tactically."
    })

if __name__ == "__main__":
    app.run()
