import os
import math
import requests
from flask import Flask, render_template, request, jsonify
from google import genai
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# --- CONFIG ---
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

# The 12 Free Tier Competitions
FREE_LEAGUES = ['PL', 'PD', 'SA', 'BL1', 'FL1', 'CL', 'DED', 'PPL', 'ELC', 'BSA', 'WC', 'EC']

# --- THE MATH ---
def poisson_probability(lmbda, k):
    return (math.exp(-lmbda) * (lmbda**k)) / math.factorial(k)

def calculate_match_probs(h_xg, a_xg):
    h_win, draw, a_win = 0, 0, 0
    for i in range(7): 
        for j in range(7):
            prob = poisson_probability(h_xg, i) * poisson_probability(a_xg, j)
            if i > j: h_win += prob
            elif j > i: a_win += prob
            else: draw += prob
    return h_win, draw, a_win

def get_stats(comp_code, team_id, side):
    url = f"https://api.football-data.org/v4/competitions/{comp_code}/standings"
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    try:
        response = requests.get(url, headers=headers).json()
        standings = response.get('standings', [])
        table_data = next((s for s in standings if s['type'] == side), standings[0])['table']
        for entry in table_data:
            if entry['team']['id'] == int(team_id):
                return {
                    "avg_goals": entry['goalsFor'] / entry['playedGames'],
                    "name": entry['team']['shortName'],
                    "form": entry.get('form', '??')
                }
    except: return None

# --- ROUTES ---
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/get_matches', methods=['GET'])
def get_matches():
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    all_fixtures = []
    # Loop through the free leagues to find scheduled games
    for league in FREE_LEAGUES:
        url = f"https://api.football-data.org/v4/competitions/{league}/matches?status=SCHEDULED"
        try:
            res = requests.get(url, headers=headers).json()
            for m in res.get('matches', [])[:10]: # Top 10 per league
                all_fixtures.append({
                    "home": m['homeTeam']['shortName'],
                    "home_id": m['homeTeam']['id'],
                    "away": m['awayTeam']['shortName'],
                    "away_id": m['awayTeam']['id'],
                    "league": league,
                    "date": m['utcDate'][:10]
                })
        except: continue
    return jsonify(sorted(all_fixtures, key=lambda x: x['date']))

@app.route('/predict', methods=['POST'])
def predict():
    data = request.json
    h_data = get_stats(data['league'], data['home_id'], "HOME")
    a_data = get_stats(data['league'], data['away_id'], "AWAY")
    
    if not h_data or not a_data:
        return jsonify({"error": "Data unavailable"}), 400

    h_win, draw, a_win = calculate_match_probs(h_data['avg_goals'], a_data['avg_goals'])
    
    prompt = f"You are a grumpy football manager. Verdict on {h_data['name']} vs {a_data['name']}. Home Win: {h_win:.0%}. Form: {h_data['form']} vs {a_data['form']}."
    response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
    
    return jsonify({
        "home_name": h_data['name'], "away_name": a_data['name'],
        "verdict": response.text,
        "probs": {"home": f"{h_win:.0%}", "draw": f"{draw:.0%}", "away": f"{a_win:.0%}"}
    })

if __name__ == '__main__':
    app.run(debug=True)
