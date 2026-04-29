import math
import os
import time
import requests
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

COMPETITIONS = [
    "CL", "PL", "PD", "BL1", "SA", "FL1", "ELC", "DED", "PPL", "BSA"
]

# ---------------- CACHE ----------------
standings_cache = {}
form_cache = {}
fixtures_store = {}
last_refresh = 0


# ---------------- POISSON ----------------
def poisson(k, lam):
    lam = max(min(lam, 4.0), 0.1)
    return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)


# =========================================================
# 📊 RATE SAFE PRELOAD (STANDINGS)
# =========================================================
def preload_standings():
    print("[BOOT] Preloading standings...")

    for comp in COMPETITIONS:
        try:
            requests.get(
                f"{BASE_URL}/competitions/{comp}/standings",
                headers=HEADERS,
                timeout=5
            )

            time.sleep(6)  # RATE LIMIT SAFETY

        except Exception as e:
            print("[STANDINGS ERROR]", comp, e)


# =========================================================
# ⚽ FORM
# =========================================================
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


# =========================================================
# 📈 STANDINGS
# =========================================================
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


# =========================================================
# ⚽ FIXTURE ENGINE (SAFE + NON-EMPTY CACHE)
# =========================================================
def fetch_all_fixtures():
    global fixtures_store, last_refresh

    print("[CACHE] Refreshing fixtures...")

    now = time.time()
    new_store = {}

    for i in range(7):
        date = time.strftime("%Y-%m-%d", time.gmtime(now + i * 86400))

        time.sleep(6)  # RATE LIMIT SAFETY

        day_matches = []

        try:
            r = requests.get(
                f"{BASE_URL}/matches",
                headers=HEADERS,
                params={"dateFrom": date, "dateTo": date},
                timeout=10
            )

            if r.status_code != 200:
                continue

            matches = r.json().get("matches", [])

            for m in matches:
                home = m.get("homeTeam")
                away = m.get("awayTeam")
                comp = m.get("competition")

                if not home or not away or not comp:
                    continue

                comp_code = comp.get("code")

                if comp_code not in COMPETITIONS:
                    continue

                day_matches.append({
                    "home": home["name"],
                    "home_id": home["id"],
                    "away": away["name"],
                    "away_id": away["id"],
                    "comp": comp_code,
                    "league": comp.get("name")
                })

            print(f"DEBUG: Successfully fetched {len(day_matches)} matches for {date}")

        except Exception as e:
            print("[FIXTURE ERROR]", date, e)

        # ONLY STORE IF NOT EMPTY
        if day_matches:
            new_store[date] = day_matches

    fixtures_store = new_store
    last_refresh = time.time()

    print(f"[CACHE] Fixtures loaded: {len(fixtures_store)} days")


# =========================================================
# ⚽ FIXTURES ENDPOINT (FRONTEND SAFE)
# =========================================================
@app.route("/fixtures")
def fixtures():
    date = request.args.get("date")

    if not date:
        return jsonify([])

    date = date.split("T")[0]

    # LOADING STATE (FRONTEND SAFE OBJECT WRAPPER)
    if date not in fixtures_store:
        return jsonify({
            "status": "loading",
            "data": [],
            "message": "Data is still syncing, please refresh in 30 seconds"
        })

    return jsonify(fixtures_store.get(date, []))


# =========================================================
# 🎯 PREDICTION (CRASH SAFE)
# =========================================================
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

    # SAFE RANK LOGIC
    try:
        h_rank = int(h_team["rank"]) if str(h_team["rank"]).isdigit() else None
        a_rank = int(a_team["rank"]) if str(a_team["rank"]).isdigit() else None

        diff = abs(h_rank - a_rank) if h_rank is not None and a_rank is not None else 999
    except:
        diff = 999

    vibe = "A high-intensity clash for position." if diff <= 3 else "A David vs Goliath scenario."

    h_wins = h_form.count("W")

    if h_wins >= 4:
        form_note = f"{data['home']} is currently untouchable; they're playing on another level."
    elif "LLL" in h_form:
        form_note = f"There's unrest in the {data['home']} camp; the losses are mounting up."
    else:
        form_note = "Expect a measured approach from both dugouts."

    tactics = (
        "We're likely to see a high press today."
        if h_lam + a_lam > 2.8
        else "It'll be won in the mud and the shadows of the midfield."
    )

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


# =========================================================
# 🚀 STARTUP (THREAD SAFE + RENDER READY)
# =========================================================
def boot_sequence():
    print("[BOOT] Boot sequence started...")
    fetch_all_fixtures()
    preload_standings()
    print("[BOOT] Complete")


def scheduler():
    while True:
        time.sleep(3600)
        print("[SCHEDULER] Refreshing data...")
        fetch_all_fixtures()
        preload_standings()


if __name__ == "__main__":

    threading.Thread(target=boot_sequence, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()

    # Render-compatible port fix
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
