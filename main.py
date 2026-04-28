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

# --- THE MATH ---
def poisson_probability(lmbda, k):
    return (math.exp(-lmbda) * (lmbda**k)) / math.factorial(k)

def calculate_match_probs(h_xg, a_xg):
    h_win, draw, a_win = 0, 0, 0
    for i in range(7): # Checking up to 6 goals
        for j in range(7):
            prob = poisson_probability(h_xg, i) * poisson_probability(a_xg, j)
            if i > j: h_win += prob
            elif j > i: a_win += prob
            else: draw += prob
    return h_win, draw, a_win

# --- DATA FETCHING (Based on your provided docs) ---
def get_stats(comp_code, team_id, side):
    url = f"https://api.football-data.org/v4/competitions/{comp_code}/standings"
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    try:
        response = requests.get(url, headers=headers).json()
        standings = response.get('standings', [])
        # Your documentation showed HOME/AWAY types in the standings array
        table_data = next((s for s in standings if s['type'] == side), standings[0])['table']
        
        for entry in table_data:
            if entry['team']['id'] == int(team_id):
                return {
                    "avg_goals": entry['goalsFor'] / entry['playedGames'],
                    "name": entry['team']['shortName'],
                    "form": entry.get('form', '??')
                }
    except Exception as e:
        print(f"Error: {e}")
        return None

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/predict', methods=['POST'])
def predict():
    data = request.json
    # Pull Home stats for Home team, Away stats for Away team
    h_data = get_stats(data['league'], data['home_id'], "HOME")
    a_data = get_stats(data['league'], data['away_id'], "AWAY")
    
    if not h_data or not a_data:
        return jsonify({"error": "Could not fetch team data"}), 400

    h_win, draw, a_win = calculate_match_probs(h_data['avg_goals'], a_data['avg_goals'])
    
    # The Gaffer's AI Personality
    prompt = f"You are a grumpy, old-school football manager. Give a 2-sentence tactical verdict on {h_data['name']} vs {a_data['name']}. Home win prob: {h_win:.0%}. Home form: {h_data['form']}, Away form: {a_data['form']}."
    response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
    
    return jsonify({
        "home_name": h_data['name'],
        "away_name": a_data['name'],
        "verdict": response.text,
        "probs": {
            "home": f"{h_win:.0%}",
            "draw": f"{draw:.0%}",
            "away": f"{a_win:.0%}"
        }
    })

if __name__ == '__main__':
    app.run(debug=True)
