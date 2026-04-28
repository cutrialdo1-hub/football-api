import math, os, time, requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ================= CONFIG =================
API_KEY = os.environ.get("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}
cache = {}

# ================= MATH =================
def poisson_probability(actual, expected):
    expected = max(expected, 0.01)
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)

# ================= STATS ENGINE =================
def get_venue_stats(comp_code):
    now = time.time()
    if comp_code in cache and (now - cache[comp_code][0] < 86400):
        return cache[comp_code][1]
    
    try:
        res = requests.get(f"{BASE_URL}/competitions/{comp_code}/standings", headers=HEADERS, timeout=5)
        data = res.json()
        standings = data.get("standings", [])
        if not standings: return {}

        # FALLBACK LOGIC: Try to find HOME/AWAY tables, if not, use the first one (TOTAL)
        h_table = next((s for s in standings if s.get("type") == "HOME"), standings[0])["table"]
        a_table = next((s for s in standings if s.get("type") == "AWAY"), standings[0])["table"]
        t_table = next((s for s in standings if s.get("type") == "TOTAL"), standings[0])["table"]

        venue = {}
        for t in t_table:
            venue[t["team"]["id"]] = {"rank": t["position"], "h_atk": 1.0, "h_def": 1.0, "a_atk": 1.0, "a_def": 1.0}

        for t in h_table:
            tid = t["team"]["id"]
            if tid in venue:
                venue[tid]["h_atk"] = (t["goalsFor"] / max(t["playedGames"], 1)) / 1.3
                venue[tid]["h_def"] = (t["goalsAgainst"] / max(t["playedGames"], 1)) / 1.3

        for t in a_table:
            tid = t["team"]["id"]
            if tid in venue:
                venue[tid]["a_atk"] = (t["goalsFor"] / max(t["playedGames"], 1)) / 1.1
                venue[tid]["a_def"] = (t["goalsAgainst"] / max(t["playedGames"], 1)) / 1.1

        cache[comp_code] = (now, venue)
        return venue
    except:
        return {}

# ================= ROUTES =================
@app.route("/")
def home():
    return "Gaffer API Active"

@app.route("/fixtures")
def fixtures():
    d = request.args.get("date")
    try:
        # Removed strict competition filter so you can see more games
        res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": d, "dateTo": d}, timeout=5)
        matches = res.json().get("matches", [])
        return jsonify([{
            "home": m["homeTeam"]["name"], 
            "home_id": m["homeTeam"]["id"], 
            "away": m["awayTeam"]["name"], 
            "away_id": m["awayTeam"]["id"], 
            "comp": m["competition"]["code"]
        } for m in matches])
    except:
        return jsonify([])

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    stats = get_venue_stats(data["comp"])
    
    # NEUTRAL FALLBACK: If stats are missing, we use 1.0 baseline instead of erroring
    h_s = stats.get(data["home_id"], {"rank": "N/A", "h_atk": 1.0, "h_def": 1.0, "a_atk": 1.0, "a_def": 1.0})
    a_s = stats.get(data["away_id"], {"rank": "N/A", "h_atk": 1.0, "h_def": 1.0, "a_atk": 1.0, "a_def": 1.0})

    h_xg = h_s["h_atk"] * a_s["a_def"] * 1.2
    a_xg = a_s["a_atk"] * h_s["h_def"] * 1.1

    best_p, score, h_w, d_w, a_w = 0, "1-1", 0, 0, 0
    for h in range(5):
        for a in range(5):
            p = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if p > best_p: best_p, score = p, f"{h}-{a}"
            if h > a: h_w += p
            elif h == a: d_w += p
            else: a_w += p
    
    total = max(h_w + d_w + a_w, 0.01)
    return jsonify({
        "score": score,
        "probs": {"home": round(h_w/total*100), "draw": round(d_w/total*100), "away": round(a_w/total*100)},
        "insight": f"Tactical standoff between {data['home']} and {data['away']}. Expected game flow suggests {score}.",
        "h_rank": h_s["rank"], "a_rank": a_s["rank"]
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
