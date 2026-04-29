import math
import os
import time
import json
import requests
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY, "User-Agent": "GafferTactical/2.0"}
COMPETITIONS = ["CL","PL","PD","BL1","SA","FL1","ELC","DED","PPL","BSA"]

standings_cache = {}
form_cache = {}

def poisson(k, lam):
    if lam <= 0: lam = 0.1
    return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)

def get_standings(code):
    if code in standings_cache: return standings_cache[code]
    try:
        r = requests.get(f"{BASE_URL}/competitions/{code}/standings", headers=HEADERS, timeout=10)
        data = r.json()
        table = next(s for s in data["standings"] if s["type"] == "TOTAL")["table"]
        out = {str(t["team"]["id"]): {
            "rank": t["position"],
            "gf": t["goalsFor"] / max(t["playedGames"], 1),
            "ga": t["goalsAgainst"] / max(t["playedGames"], 1)
        } for t in table}
        standings_cache[code] = out
        return out
    except: return {}

def get_detailed_form(team_id):
    if team_id in form_cache: return form_cache[team_id]
    try:
        r = requests.get(f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5", headers=HEADERS, timeout=10)
        matches = r.json().get("matches", [])
        history = []
        for m in matches:
            hs, aw = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
            is_home = m["homeTeam"]["id"] == team_id
            gf, ga = (hs, aw) if is_home else (aw, hs)
            history.append("W" if gf > ga else ("D" if gf == ga else "L"))
        
        win_ratio = history.count("W") / 5
        atk_mult = max(min(0.90 + (win_ratio * 0.20), 1.10), 0.90)
        def_mult = max(min(0.90 + (win_ratio * 0.20), 1.10), 0.90)
        
        res = (atk_mult, def_mult, "".join(history))
        form_cache[team_id] = res
        return res
    except: return (1.0, 1.0, "???")

@app.route("/fixtures")
def fixtures():
    d = request.args.get("date", "").split("T")[0]
    r = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": d, "dateTo": d}, timeout=10)
    temp = []
    for m in r.json().get("matches", []):
        if m.get("competition", {}).get("code") in COMPETITIONS:
            temp.append({
                "home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"],
                "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"],
                "comp": m["competition"]["code"]
            })
    return jsonify(temp)

@app.route("/predict", methods=["POST"])
def predict():
    req = request.json
    stats = get_standings(req["comp"])
    h_atk, h_def, h_f = get_detailed_form(req["home_id"])
    a_atk, a_def, a_f = get_detailed_form(req["away_id"])

    h_t = stats.get(str(req["home_id"]), {"gf": 1.3, "ga": 1.3})
    a_t = stats.get(str(req["away_id"]), {"gf": 1.1, "ga": 1.4})

    # --- THE REALITY TUNNEL FIX ---
    h_l = (h_t["gf"] * a_t["ga"] * h_atk * (1/a_def)) * 1.10 
    a_l = (a_t["gf"] * h_t["ga"] * a_atk * (1/h_def))
    h_l = max(min(h_l, 2.8), 0.4)
    a_l = max(min(a_l, 2.8), 0.4)

    matrix = [[poisson(i, h_l) * poisson(j, a_l) for j in range(6)] for i in range(6)]
    p_h = sum(matrix[i][j] for i in range(6) for j in range(6) if i > j)
    p_d = sum(matrix[i][j] for i in range(6) for j in range(6) if i == j)
    p_a = sum(matrix[i][j] for i in range(6) for j in range(6) if i < j)
    p_btts = (1 - sum(matrix[0][j] for j in range(6))) * (1 - sum(matrix[i][0] for i in range(6)))
    p_o25 = sum(matrix[i][j] for i in range(6) for j in range(6) if i+j >= 3)

    def to_odds(p): return round(1/p, 2) if p > 0.05 else 19.00

    return jsonify({
        "score": next((f"{i}-{j}" for i in range(6) for j in range(6) if matrix[i][j] == max(max(row) for row in matrix)), "1-1"),
        "market": {
            "home": {"gaffer_odds": to_odds(p_h)}, "draw": {"gaffer_odds": to_odds(p_d)}, "away": {"gaffer_odds": to_odds(p_a)},
            "btts": {"gaffer_odds": to_odds(p_btts)}, "over25": {"gaffer_odds": to_odds(p_o25)}
        },
        "h_form": h_f, "a_form": a_f
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
