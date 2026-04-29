import math
import os
import time
import requests
import threading
import netrc  # 🔥 FIX 1: Force early import to prevent deadlock
from flask import Flask, jsonify, request
from flask_cors import CORS

# =========================================================
# 🚀 APP INIT
# =========================================================
app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

COMPETITIONS = [
    "CL", "PL", "PD", "BL1", "SA", "FL1", "ELC", "DED", "PPL", "BSA"
]

print("[INIT] Background workers initiated at top-level import")

# =========================================================
# 🔒 THREAD LOCK (FIX 2)
# =========================================================
fetch_lock = threading.Lock()

# =========================================================
# 🧠 CACHE
# =========================================================
standings_cache = {}
form_cache = {}
fixtures_store = {}
last_refresh = 0


# =========================================================
# 📊 POISSON
# =========================================================
def poisson(k, lam):
    lam = max(min(lam, 4.0), 0.1)
    return (math.pow(lam, k) * math.exp(-lam)) / math.factorial(k)


# =========================================================
# 📊 STANDINGS PRELOAD
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
            time.sleep(6)
        except Exception as e:
            print("[STANDINGS ERROR]", comp, e)

    print("[BOOT] Standings preload complete")


# =========================================================
# ⚽ FORM ENGINE
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

        form_cache[team_id] = {"t": now, "d": multiplier, "s": form_string}

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
# ⚡ FIXTURE ENGINE (THREAD SAFE)
# =========================================================
def fetch_all_fixtures():
    global fixtures_store, last_refresh

    # 🔒 NON-BLOCKING LOCK
    if not fetch_lock.acquire(blocking=False):
        print("[CACHE] Fetch already in progress by another thread. Skipping.")
        return False

    try:
        print("[CACHE] Fetching fixtures (TURBO MODE)...")

        now = time.time()
        start = now - 86400
        end = now + (5 * 86400)

        start_date = time.strftime("%Y-%m-%d", time.gmtime(start))
        end_date = time.strftime("%Y-%m-%d", time.gmtime(end))

        print(f"[DEBUG] Requesting range: {start_date} → {end_date}")

        new_store = {}

        r = requests.get(
            f"{BASE_URL}/matches",
            headers=HEADERS,
            params={"dateFrom": start_date, "dateTo": end_date},
            timeout=20
        )

        print(f"[DEBUG] API Status Code: {r.status_code}")

        if r.status_code != 200:
            print(f"[DEBUG] API Response Snippet: {r.text[:200]}")
            return False

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

            date = m.get("utcDate", "")[:10]

            if date not in new_store:
                new_store[date] = []

            new_store[date].append({
                "home": home["name"],
                "home_id": home["id"],
                "away": away["name"],
                "away_id": away["id"],
                "comp": comp_code,
                "league": comp.get("name")
            })

        for date, games in new_store.items():
            print(f"DEBUG: Successfully fetched {len(games)} matches for {date}")

        fixtures_store.update(new_store)
        last_refresh = time.time()

        print(f"[CACHE] Fixtures loaded: {len(fixtures_store)} days")

        return True

    except Exception as e:
        print("[FIXTURE ENGINE ERROR]", e)
        return False

    finally:
        fetch_lock.release()  # 🔥 ALWAYS RELEASE LOCK


# =========================================================
# ⚽ FIXTURES ROUTE (ON-DEMAND SAFE)
# =========================================================
@app.route("/fixtures")
def fixtures():
    date = request.args.get("date")

    if not date:
        return jsonify([])

    date = date.split("T")[0]

    if not fixtures_store:
        print(f"[ON-DEMAND] Store empty. Triggering API fetch for {date}...")

        success = fetch_all_fixtures()

        if not success:
            return jsonify({
                "status": "loading",
                "data": [],
                "message": "Data is still syncing, please refresh in 30 seconds"
            })

    if date not in fixtures_store:
        return jsonify({
            "status": "loading",
            "data": [],
            "message": "Data is still syncing, please refresh in 30 seconds"
        })

    return jsonify(fixtures_store.get(date, []))


# =========================================================
# 🎯 PREDICTION
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

    return jsonify({
        "score": predicted_score,
        "probs": {
            "home": round(prob_home * 100),
            "draw": round(prob_draw * 100),
            "away": round(prob_away * 100)
        }
    })


# =========================================================
# 🚀 BACKGROUND TASKS
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


def start_background_tasks():
    threading.Thread(target=boot_sequence, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()


# =========================================================
# 🚀 AUTO START
# =========================================================
start_background_tasks()


# =========================================================
# 🚀 RUN SERVER
# =========================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
