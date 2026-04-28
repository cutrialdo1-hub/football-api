import math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORSimport math
import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime, timedelta
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
    except:
        print("AI Client Init Failed")

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": FOOTBALL_API_KEY}
FREE_COMPS = "CL,PL,ELC,BL1,SA,PD,FL1,DED,PPL,BSA,EC,WC"
standings_cache = {}

def poisson_probability(actual, expected):
    if expected <= 0: expected = 0.01
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)

def get_venue_stats(comp_code):
    now = time.time()
    if comp_code in standings_cache and (now - standings_cache[comp_code][0] < 86400):
        return standings_cache[comp_code][1]

    url = f"{BASE_URL}/competitions/{comp_code}/standings"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return {}

    data = res.json()
    try:
        h_table = next((s for s in data["standings"] if s["type"] == "HOME"), data["standings"][0])["table"]
        a_table = next((s for s in data["standings"] if s["type"] == "AWAY"), data["standings"][0])["table"]
        t_table = next((s for s in data["standings"] if s["type"] == "TOTAL"), data["standings"][0])["table"]

        avg_h_goals = sum(t["goalsFor"] for t in h_table) / max(sum(t["playedGames"] for t in h_table), 1)
        avg_a_goals = sum(t["goalsFor"] for t in a_table) / max(sum(t["playedGames"] for t in a_table), 1)

        venue_data = {t["team"]["id"]: {"name": t["team"]["name"], "rank": t["position"]} for t in t_table}
        for t in h_table:
            tid = t["team"]["id"]
            if tid in venue_data:
                venue_data[tid].update({"h_atk": (t["goalsFor"]/max(t["playedGames"], 1))/avg_h_goals, "h_def": (t["goalsAgainst"]/max(t["playedGames"],1))/avg_h_goals})
        for t in a_table:
            tid = t["team"]["id"]
            if tid in venue_data:
                venue_data[tid].update({"a_atk": (t["goalsFor"]/max(t["playedGames"], 1))/avg_a_goals, "a_def": (t["goalsAgainst"]/max(t["playedGames"],1))/avg_a_goals})
        
        standings_cache[comp_code] = (now, venue_data)
        return venue_data
    except: return {}

def get_form(team_id):
    res = requests.get(f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit=5", headers=HEADERS)
    if res.status_code != 200: return 1.0, 0
    m = res.json().get("matches", [])
    pts = sum(3 if (x["score"]["fullTime"]["home"] > x["score"]["fullTime"]["away"] and x["homeTeam"]["id"] == team_id) or (x["score"]["fullTime"]["away"] > x["score"]["fullTime"]["home"] and x["awayTeam"]["id"] == team_id) else 1 if x["score"]["fullTime"]["home"] == x["score"]["fullTime"]["away"] else 0 for x in m)
    return 0.85 + (pts/15 * 0.3), pts

def gaffer_verdict(h_name, a_name, score):
    if not client: return "Play for the badge. Stick to the basics."
    prompt = f"Blunt manager prediction for {h_name} vs {a_name}. Score: {score}. Use manager slang and future tense."
    try:
        response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return response.text.strip()
    except: return "The Gaffer's lost his voice. Tighten up at the back."

@app.route("/fixtures", methods=["GET"])
def fixtures():
    d = request.args.get("date")
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": d, "dateTo": d, "competitions": FREE_COMPS})
    return jsonify([{"home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"], "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"], "comp": m["competition"]["code"], "league": m["competition"]["name"]} for m in res.json().get("matches", [])])

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    stats = get_venue_stats(data["comp"])
    h_s, a_s = stats.get(data["home_id"], {}), stats.get(data["away_id"], {})
    
    h_f, h_pts = get_form(data["home_id"])
    a_f, a_pts = get_form(data["away_id"])
    
    h_xg = h_s.get("h_atk", 1.2) * a_s.get("a_def", 1.0) * 1.35 * h_f
    a_xg = a_s.get("a_atk", 1.1) * h_s.get("h_def", 1.0) * 1.25 * a_f
    
    max_p, score, h_w, draw, a_w = 0, "1-1", 0, 0, 0
    for h in range(5):
        for a in range(5):
            p = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if p > max_p: max_p, score = p, f"{h}-{a}"
            if h > a: h_w += p
            elif h == a: draw += p
            else: a_w += p
            
    total = max(h_w + draw + a_w, 0.01)
    return jsonify({
        "score": score, "probs": {"home": round(h_w/total*100), "draw": round(draw/total*100), "away": round(a_w/total*100)},
        "insight": gaffer_verdict(data["home"], data["away"], score),
        "h_rank": h_s.get('rank', 'N/A'), "a_rank": a_s.get('rank', 'N/A'),
        "metrics": {
            "h_atk": round(h_s.get('h_atk', 1),2), "h_def": round(h_s.get('h_def', 1),2),
            "a_atk": round(a_s.get('a_atk', 1),2), "a_def": round(a_s.get('a_def', 1),2),
            "h_form": h_pts, "a_form": a_pts
        }
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
from datetime import datetime, timedelta
from google import genai

app = Flask(__name__)
CORS(app)

# --- CONFIG ---
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 2026 Google GenAI SDK Setup
client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"AI Client Init Failed: {e}")

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": FOOTBALL_API_KEY}
FREE_COMPS = "CL,PL,ELC,BL1,SA,PD,FL1,DED,PPL,BSA,EC,WC"

# --- LOGIC ---
def poisson_probability(actual, expected):
    if expected <= 0: expected = 0.01
    return (math.pow(expected, actual) * math.exp(-expected)) / math.factorial(actual)

def get_stats(comp_code, team_id):
    # Fallback stats for knockouts/missing data
    default = {"rank": "N/A", "atk": 1.2, "df": 1.0}
    try:
        res = requests.get(f"{BASE_URL}/competitions/{comp_code}/standings", headers=HEADERS)
        data = res.json()
        if "standings" not in data: return default
        
        table = data["standings"][0]["table"]
        avg_g = sum(t["goalsFor"] for t in table) / max(sum(t["playedGames"] for t in table), 1)
        
        for t in table:
            if t["team"]["id"] == team_id:
                return {
                    "rank": t["position"],
                    "atk": (t["goalsFor"]/max(t["playedGames"], 1)) / avg_g,
                    "df": (t["goalsAgainst"]/max(t["playedGames"], 1)) / avg_g
                }
    except: pass
    return default

def gaffer_verdict(h_name, a_name, score):
    if not client: return "The Gaffer's busy in the dressing room."
    
    prompt = f"Blunt manager prediction for {h_name} vs {a_name}. Score: {score}. Use future tense and manager slang."
    
    for _ in range(2): # Retry once on 429
        try:
            response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
            return response.text.strip()
        except Exception as e:
            if "429" in str(e): time.sleep(2); continue
            break
    return "Play for the badge. Stick to the basics. No mistakes."

@app.route("/fixtures", methods=["GET"])
def fixtures():
    d = request.args.get("date")
    res = requests.get(f"{BASE_URL}/matches", headers=HEADERS, params={"dateFrom": d, "dateTo": d, "competitions": FREE_COMPS})
    return jsonify([{"home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"], "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"], "comp": m["competition"]["code"], "league": m["competition"]["name"]} for m in res.json().get("matches", [])])

@app.route("/predict", methods=["POST"])
def predict():
    data = request.get_json()
    h_s = get_stats(data["comp"], data["home_id"])
    a_s = get_stats(data["comp"], data["away_id"])
    
    h_xg, a_xg = h_s["atk"] * a_s["df"] * 1.3, a_s["atk"] * h_s["df"] * 1.1
    
    max_p, score, h_w, d, a_w = 0, "1-1", 0, 0, 0
    for h in range(5):
        for a in range(5):
            p = poisson_probability(h, h_xg) * poisson_probability(a, a_xg)
            if p > max_p: max_p, score = p, f"{h}-{a}"
            if h > a: h_w += p
            elif h == a: d += p
            else: a_w += p
            
    return jsonify({
        "score": score, "probs": {"home": round(h_w*100), "draw": round(d*100), "away": round(a_w*100)},
        "insight": gaffer_verdict(data["home"], data["away"], score),
        "h_rank": h_s["rank"], "a_rank": a_s["rank"],
        "metrics": {"h_atk": round(h_s["atk"],2), "a_def": round(a_s["df"],2)}
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
