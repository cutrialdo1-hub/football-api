import math
import os
import time
import json
import random
import calendar
import bisect
import requests
import threading
import functools
from datetime import date as _date, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS

# =========================================================
# 🚀 APP INIT
# =========================================================
app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("FOOTBALL_API_KEY")
if not API_KEY:
    raise RuntimeError("CRITICAL: FOOTBALL_API_KEY environment variable not set!")

BASE_URL = "https://api.football-data.org/v4"
HEADERS  = {"X-Auth-Token": API_KEY}

COMPETITIONS = ["CL", "PL", "PD", "BL1", "SA", "FL1", "ELC", "DED", "PPL", "BSA"]

LEAGUE_AVG_GOALS = {
    "BL1": 1.55, "PL": 1.35, "PD": 1.25, "SA": 1.25,
    "FL1": 1.20, "CL": 1.30, "ELC": 1.40, "DED": 1.30,
    "PPL": 1.25, "BSA": 1.35,
}
DEFAULT_LEAGUE_AVG = 1.30

LEAGUE_HOME_ADV = {
    "BL1": 1.08, "PL": 1.07, "PD": 1.10, "SA": 1.10,
    "FL1": 1.09, "CL": 1.08, "ELC": 1.12, "DED": 1.10,
    "PPL": 1.10, "BSA": 1.13,
}
DEFAULT_HOME_ADV = 1.10

# Bayesian blend weights — 40% Poisson model, 60% market
# Applied only when user manually enters bookie odds in the Kelly staker
WEIGHT_POISSON = 0.40
WEIGHT_MARKET  = 0.60

fetch_lock             = threading.Lock()
football_api_lock      = threading.Lock()
football_api_last_call = 0.0
standings_cache: dict  = {}
form_cache: dict       = {}
fixtures_store: dict   = {}

FOOTBALL_API_MIN_INTERVAL = 6.5
CACHE_FILE       = "cache.json"
CACHE_MAX_AGE    = 3600
STANDINGS_EXPIRY = 86400
FORM_EXPIRY      = 3600

def football_data_get(url: str, **kwargs):
    global football_api_last_call
    with football_api_lock:
        elapsed = time.monotonic() - football_api_last_call
        if elapsed < FOOTBALL_API_MIN_INTERVAL:
            time.sleep(FOOTBALL_API_MIN_INTERVAL - elapsed)
        r = requests.get(url, **kwargs)
        football_api_last_call = time.monotonic()
        return r

# =========================================================
# 💾 DISK CACHE
# =========================================================
def load_cache_from_disk() -> bool:
    global fixtures_store
    if not os.path.exists(CACHE_FILE):
        return False
    try:
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
        age = time.time() - data.get("timestamp", 0)
        if age > CACHE_MAX_AGE * 6:
            print(f"[DISK] Cache too old ({age/3600:.1f}h), ignoring")
            return False
        fixtures_store = data.get("fixtures", {})
        print(f"[DISK] Loaded cache ({age/60:.0f}m old). Dates: {list(fixtures_store.keys())}")
        return bool(fixtures_store)
    except Exception as e:
        print(f"[DISK] Load error: {e}")
        return False

def save_cache_to_disk(fixtures: dict):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"timestamp": time.time(), "fixtures": fixtures}, f)
        print("[DISK] Cache saved.")
    except Exception as e:
        print(f"[DISK] Save error: {e}")

def get_cache_age() -> float:
    if not os.path.exists(CACHE_FILE):
        return float("inf")
    try:
        with open(CACHE_FILE, "r") as f:
            return time.time() - json.load(f).get("timestamp", 0)
    except:
        return float("inf")

# =========================================================
# 📊 POISSON MATH
# =========================================================
@functools.lru_cache(maxsize=1024)
def _pmf_vec(lam_q: int) -> tuple:
    """
    Cached Poisson PMF for k=0..10.
    Extended from k=6 to eliminate truncation bias at high lambdas.
    lam_q = round(lam * 1000) for cache key efficiency.
    """
    lam = max(min(lam_q / 1000.0, 3.5), 0.3)
    p   = math.exp(-lam)
    out = [p]
    for k in range(1, 11):
        p = p * lam / k
        out.append(p)
    return tuple(out)

def poisson_vec(lam: float) -> tuple:
    return _pmf_vec(round(lam * 1000))

def poisson(k: int, lam: float) -> float:
    vec = poisson_vec(float(lam or DEFAULT_LEAGUE_AVG))
    return vec[k] if 0 <= k < 7 else 0.0

def compute_probs(h_lam: float, a_lam: float):
    """
    O(n) probability computation using prefix-sum identities.
    Returns (p_h, p_d, p_a, p_btts, p_o15, p_o25, p_o35) — raw, not normalised.
    """
    hp = poisson_vec(h_lam)
    ap = poisson_vec(a_lam)
    n  = len(hp)
    h_sum  = sum(hp); a_sum = sum(ap)
    t_mass = h_sum * a_sum

    a_prefix = 0.0; p_h = p_d = 0.0
    for i in range(n):
        p_h      += hp[i] * a_prefix
        p_d      += hp[i] * ap[i]
        a_prefix += ap[i]
    p_a = t_mass - p_h - p_d

    p_btts = t_mass - hp[0]*a_sum - h_sum*ap[0] + hp[0]*ap[0]

    h0,h1,h2,h3 = hp[0],hp[1],hp[2],hp[3]
    a0,a1,a2,a3 = ap[0],ap[1],ap[2],ap[3]
    le1 = h0*a0 + h0*a1 + h1*a0
    le2 = le1 + h0*a2 + h1*a1 + h2*a0
    le3 = le2 + h0*a3 + h1*a2 + h2*a1 + h3*a0

    return p_h, p_d, p_a, p_btts, t_mass-le1, t_mass-le2, t_mass-le3

# =========================================================
# 📈 STANDINGS ENGINE
# =========================================================
def _parse_table(table: list) -> dict:
    return {
        str(t["team"]["id"]): {
            "rank":   t["position"],
            "played": max(t["playedGames"], 1),
            "gf":     t["goalsFor"]     / max(t["playedGames"], 1),
            "ga":     t["goalsAgainst"] / max(t["playedGames"], 1),
            "pts":    t["points"],
        }
        for t in table
    }

def get_standings(code: str) -> dict:
    now    = time.time()
    cached = standings_cache.get(code)
    if cached and now - cached["t"] < STANDINGS_EXPIRY:
        return cached["d"]
    try:
        r = football_data_get(
            f"{BASE_URL}/competitions/{code}/standings",
            headers=HEADERS, timeout=10
        )
        if r.status_code == 429:
            return cached["d"] if cached else {"total": {}, "home": {}, "away": {}}
        if r.status_code != 200:
            return cached["d"] if cached else {"total": {}, "home": {}, "away": {}}

        standings = r.json()["standings"]
        def get_table(stype):
            try:
                return _parse_table(next(s for s in standings if s["type"] == stype)["table"])
            except StopIteration:
                return {}

        out = {
            "total": get_table("TOTAL"),
            "home":  get_table("HOME"),
            "away":  get_table("AWAY"),
        }
        total_tbl = out.get("total", {})
        if total_tbl:
            raw_g  = sum(v["gf"] * v["played"] for v in total_tbl.values())
            raw_gp = sum(v["played"]            for v in total_tbl.values())
            out["league_avg"] = round(raw_g / raw_gp, 4) if raw_gp else LEAGUE_AVG_GOALS.get(code, DEFAULT_LEAGUE_AVG)
        else:
            out["league_avg"] = LEAGUE_AVG_GOALS.get(code, DEFAULT_LEAGUE_AVG)

        standings_cache[code] = {"t": now, "d": out}
        return out
    except Exception as e:
        print(f"[STANDINGS ERROR] {code}: {e}")
        return cached["d"] if cached else {"total": {}, "home": {}, "away": {}}

# =========================================================
# ⚽ FORM ENGINE
# =========================================================
FORM_DECAY = 0.75
FORM_N     = 8
_calibration: dict = {"n": 0, "brier_sum": 0.0, "last_run": 0.0}

def get_detailed_form(team_id: int, league_avg: float = DEFAULT_LEAGUE_AVG, venue: str = ""):
    now       = time.time()
    cache_key = (team_id, venue)
    cached    = form_cache.get(cache_key)
    if cached and now - cached["t"] < FORM_EXPIRY:
        return cached["atk"], cached["def"], cached["s"]

    url = f"{BASE_URL}/teams/{team_id}/matches?status=FINISHED&limit={FORM_N}"
    if venue in ("HOME", "AWAY"):
        url += f"&venue={venue}"

    try:
        r = football_data_get(url, headers=HEADERS, timeout=10)
        if r.status_code == 429:
            if cached: return cached["atk"], cached["def"], cached["s"]
            return 1.0, 1.0, "???"
        if r.status_code != 200:
            return 1.0, 1.0, "???"

        matches = r.json().get("matches", [])
        history = []; w_gf = w_ga = w_total = 0.0

        for idx, m in enumerate(matches):
            score = m["score"]["fullTime"]
            if score["home"] is None: continue
            hs, aw  = score["home"], score["away"]
            is_home = m["homeTeam"]["id"] == team_id
            gf, ga  = (hs, aw) if is_home else (aw, hs)
            w        = FORM_DECAY ** idx
            w_gf    += gf * w; w_ga += ga * w; w_total += w
            history.append("W" if gf > ga else ("D" if gf == ga else "L"))

        if w_total == 0 or not history:
            return 1.0, 1.0, "???"

        avg_gf = w_gf / w_total
        avg_ga = max(w_ga / w_total, 0.1)
        atk    = avg_gf / league_avg if league_avg > 0 else 1.0
        def_   = league_avg / avg_ga
        atk    = max(min(atk,  1.20), 0.80)
        def_   = max(min(def_, 1.20), 0.80)

        form_str = "".join(history[:5])
        form_cache[cache_key] = {"t": now, "atk": atk, "def": def_, "s": form_str}
        return atk, def_, form_str
    except Exception as e:
        print(f"[FORM ERROR] team {team_id} {venue}: {e}")
        if cached: return cached["atk"], cached["def"], cached["s"]
        return 1.0, 1.0, "???"

# =========================================================
# ⚡ FIXTURE ENGINE
# =========================================================
def fetch_all_fixtures() -> bool:
    global fixtures_store
    if fixtures_store and get_cache_age() < CACHE_MAX_AGE:
        print("[CACHE] Fixtures fresh, skipping fetch")
        return True
    if not fetch_lock.acquire(blocking=False):
        print("[CACHE] Fetch already in progress, skipping")
        return bool(fixtures_store)
    try:
        print("[CACHE] Fetching fixtures from API...")
        now        = time.time()
        start_date = time.strftime("%Y-%m-%d", time.gmtime(now - 86400))
        end_date   = time.strftime("%Y-%m-%d", time.gmtime(now + 7 * 86400))
        r = football_data_get(
            f"{BASE_URL}/matches", headers=HEADERS,
            params={"dateFrom": start_date, "dateTo": end_date}, timeout=25
        )
        if r.status_code == 429:
            print("[RATE LIMIT] fixtures — keeping existing store")
            return bool(fixtures_store)
        if r.status_code != 200:
            print(f"[FIXTURE] API returned {r.status_code}: {r.text[:120]}")
            return False

        temp: dict = {}
        for m in r.json().get("matches", []):
            comp      = m.get("competition", {})
            comp_code = comp.get("code")
            if comp_code not in COMPETITIONS: continue
            date = m.get("utcDate", "")[:10]
            h_t  = m.get("homeTeam", {}); a_t = m.get("awayTeam", {})
            if not h_t.get("id") or not a_t.get("id"): continue
            temp.setdefault(date, []).append({
                "home":    h_t.get("name", "Unknown"),
                "home_id": h_t["id"],
                "away":    a_t.get("name", "Unknown"),
                "away_id": a_t["id"],
                "comp":    comp_code,
                "league":  comp.get("name", comp_code),
                "kickoff": m.get("utcDate", ""),
            })

        fixtures_store = temp
        save_cache_to_disk(temp)
        print(f"[CACHE] Loaded {sum(len(v) for v in temp.values())} matches across {len(temp)} days")
        return True
    except Exception as e:
        print(f"[FIXTURE ERROR] {e}")
        return False
    finally:
        fetch_lock.release()

# =========================================================
# ⚽ ROUTES
# =========================================================
@app.route("/ping")
def ping():
    """
    Lightweight wake-up endpoint.
    Called silently by all frontend pages on load to warm the server
    before the user interacts. Returns instantly — no DB or API calls.
    Also triggers fixture fetch if store is empty (non-blocking).
    """
    if not fixtures_store:
        threading.Thread(target=fetch_all_fixtures, daemon=True).start()
    return jsonify({"status": "ok", "warm": bool(fixtures_store)})


# =========================================================
# 📒 PREDICTION LOG SYSTEM
# =========================================================
LOG_FILE  = "predictions.json"
_log_lock = threading.Lock()

def load_log() -> list:
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"[LOG] Load error: {e}")
    return []

def save_log(entries: list):
    try:
        with open(LOG_FILE, "w") as f:
            json.dump(entries, f)
    except Exception as e:
        print(f"[LOG] Save error: {e}")

def log_prediction(entry: dict):
    with _log_lock:
        entries = load_log()
        key = entry.get("match_key", "")
        if not any(e.get("match_key") == key for e in entries):
            entries.append(entry)
            save_log(entries)
            print(f"[LOG] Logged: {key}")

def match_results_to_log():
    now_t = time.time()
    yest  = time.strftime("%Y-%m-%d", time.gmtime(now_t - 86400))
    try:
        r = football_data_get(
            f"{BASE_URL}/matches", headers=HEADERS,
            params={"dateFrom": yest, "dateTo": yest, "status": "FINISHED"}, timeout=15
        )
        if r.status_code != 200:
            print(f"[LOG] Results fetch failed ({r.status_code})"); return
        matches = r.json().get("matches", [])

        def jac(s1, s2):
            t1 = set(s1.lower().split()); t2 = set(s2.lower().split())
            return len(t1&t2)/len(t1|t2) if t1|t2 else 0

        with _log_lock:
            entries = load_log(); updated = 0
            for entry in entries:
                if entry.get("resolved") or entry.get("date") != yest: continue
                for m in matches:
                    h  = m.get("homeTeam",{}).get("name","")
                    a  = m.get("awayTeam",{}).get("name","")
                    sc = m["score"]["fullTime"]
                    if sc["home"] is None: continue
                    if jac(entry.get("home",""), h) > 0.3 and jac(entry.get("away",""), a) > 0.3:
                        hs, as_ = sc["home"], sc["away"]
                        outcome = "H" if hs>as_ else ("D" if hs==as_ else "A")
                        entry.update({"actual_home":hs,"actual_away":as_,
                                      "actual_score":f"{hs}-{as_}","actual_outcome":outcome,"resolved":True})
                        total = hs+as_; diff = hs-as_
                        for mkt in entry.get("suggested_markets",[]):
                            code = mkt.get("code","")
                            won  = False
                            if code=="H":       won = diff>0
                            elif code=="D":     won = diff==0
                            elif code=="A":     won = diff<0
                            elif code=="BTTS":  won = hs>0 and as_>0
                            elif code=="O25":   won = total>2
                            elif code=="O15":   won = total>1
                            elif code=="O35":   won = total>3
                            elif "HM05" in code: won = diff>=1
                            elif "HP05" in code: won = diff>=0
                            elif "A05" in code:  won = diff<=0
                            mkt["won"] = won
                        ph = entry.get("prob_home",33)/100; pd = entry.get("prob_draw",33)/100; pa = entry.get("prob_away",34)/100
                        r_h,r_d,r_a = (1.0 if outcome=="H" else 0.0),(1.0 if outcome=="D" else 0.0),(1.0 if outcome=="A" else 0.0)
                        entry["brier"] = round((ph-r_h)**2+(pd-r_d)**2+(pa-r_a)**2,4)
                        updated += 1; break
            if updated:
                save_log(entries)
                print(f"[LOG] Resolved {updated} predictions for {yest}")
    except Exception as e:
        print(f"[LOG] match_results_to_log error: {e}")


@app.route("/log/predictions")
def get_predictions_log():
    try:
        entries   = load_log()
        resolved  = [e for e in entries if e.get("resolved")]
        brier_avg = round(sum(e.get("brier",0) for e in resolved)/len(resolved),4) if resolved else None
        mkt_stats = {}
        for e in resolved:
            for mkt in e.get("suggested_markets",[]):
                t = mkt.get("type","Unknown")
                if t not in mkt_stats: mkt_stats[t] = {"won":0,"total":0}
                mkt_stats[t]["total"] += 1
                if mkt.get("won"): mkt_stats[t]["won"] += 1
        return jsonify({
            "entries":    sorted(entries, key=lambda x: x.get("timestamp",""), reverse=True),
            "total":      len(entries),
            "resolved":   len(resolved),
            "unresolved": len(entries)-len(resolved),
            "brier_avg":  brier_avg,
            "mkt_stats":  mkt_stats,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/log/bet", methods=["POST"])
def log_bet():
    try:
        body = request.json
        if not body: return jsonify({"error": "No data"}), 400
        with _log_lock:
            entries = load_log()
            key = body.get("match_key","")
            for entry in entries:
                if entry.get("match_key") == key:
                    entry.update({
                        "bet_market":    body.get("market"),
                        "bet_bookie":    body.get("bookie_odds"),
                        "bet_stake":     body.get("stake"),
                        "bet_edge":      body.get("edge"),
                        "bet_logged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
                    })
                    save_log(entries)
                    return jsonify({"status":"ok"})
            body["bet_logged_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
            entries.append(body); save_log(entries)
        return jsonify({"status":"ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    date = request.args.get("date", "").split("T")[0]
    if not date: return jsonify([])
    if not fixtures_store:
        loaded = load_cache_from_disk()
        if not loaded: fetch_all_fixtures()
        if not fixtures_store:
            return jsonify({"status": "loading", "message": "Server is syncing match data..."})
    result = fixtures_store.get(date)
    if result is None:
        return jsonify({"status": "no_games", "data": [], "message": "No matches scheduled for this date."})
    return jsonify(result)


@app.route("/predict", methods=["POST"])
def predict():
    req = request.json
    if not req or "comp" not in req or "home_id" not in req or "away_id" not in req:
        return jsonify({"error": "Invalid request body"}), 400

    try:
        comp = req["comp"]; h_id = req["home_id"]; a_id = req["away_id"]

        all_stats   = get_standings(comp)
        home_stats  = all_stats.get("home", {})
        away_stats  = all_stats.get("away", {})
        total_stats = all_stats.get("total", {})
        league_avg  = all_stats.get("league_avg", LEAGUE_AVG_GOALS.get(comp, DEFAULT_LEAGUE_AVG))

        fallback_h = {"gf": 1.2, "ga": 1.2, "rank": "N/A"}
        fallback_a = {"gf": 1.0, "ga": 1.3, "rank": "N/A"}
        h_venue = home_stats.get(str(h_id)) or total_stats.get(str(h_id), fallback_h)
        h_rank  = total_stats.get(str(h_id), fallback_h).get("rank", "N/A")
        a_venue = away_stats.get(str(a_id)) or total_stats.get(str(a_id), fallback_a)
        a_rank  = total_stats.get(str(a_id), fallback_a).get("rank", "N/A")

        h_atk, h_def, h_form = get_detailed_form(h_id, league_avg, venue="HOME")
        a_atk, a_def, a_form = get_detailed_form(a_id, league_avg, venue="AWAY")

        # Venue-specific standings already encode home advantage — no multiplier needed
        h_lam = max(min(h_venue["gf"] * (a_venue["ga"] / league_avg) * h_atk * (1.0 / a_def), 3.2), 0.35)
        a_lam = max(min(a_venue["gf"] * (h_venue["ga"] / league_avg) * a_atk * (1.0 / h_def), 3.2), 0.35)

        # ── Score Matrix (11×11) ──
        p_h = p_d = p_a = p_btts = p_over15 = p_over25 = p_over35 = 0.0
        matrix = {}
        ah = {"hm15":0.0,"hm1":0.0,"hm05":0.0,"h0":0.0,"hp05":0.0,"hp1":0.0,"hp15":0.0}
        at = {"o05":0.0,"o15":0.0,"o20_over":0.0,"o20_push":0.0,"o20_under":0.0,
              "o25":0.0,"o35":0.0,"o45":0.0,"u05":0.0,"u15":0.0,"u25":0.0,"u35":0.0}

        _hp = poisson_vec(h_lam); _ap = poisson_vec(a_lam)
        for i in range(len(_hp)):
            for j in range(len(_ap)):
                p = _hp[i] * _ap[j]; matrix[(i,j)] = p
                diff = i - j; total = i + j

                if   i > j:  p_h += p
                elif i == j: p_d += p
                else:        p_a += p

                if i > 0 and j > 0: p_btts   += p
                if total > 1:       p_over15  += p
                if total > 2:       p_over25  += p
                if total > 3:       p_over35  += p

                if diff >= 2:  ah["hm15"] += p
                if diff >= 2:  ah["hm1"]  += p
                elif diff==1:  ah["hm1"]  += p * 0.5
                if diff >= 1:  ah["hm05"] += p
                if diff > 0:   ah["h0"]   += p
                elif diff==0:  ah["h0"]   += p * 0.5
                if diff >= 0:  ah["hp05"] += p
                if diff >= 0:  ah["hp1"]  += p
                elif diff==-1: ah["hp1"]  += p * 0.5
                if diff >= -1: ah["hp15"] += p

                if total > 0: at["o05"] += p
                if total > 1: at["o15"] += p
                if total > 2:    at["o20_over"]  += p
                elif total == 2: at["o20_push"]  += p
                else:            at["o20_under"] += p
                if total > 2: at["o25"] += p
                if total > 3: at["o35"] += p
                if total > 4: at["o45"] += p
                if total < 1: at["u05"] += p
                if total < 2: at["u15"] += p
                if total < 3: at["u25"] += p
                if total < 4: at["u35"] += p

        ah["hm075"] = (ah["hm05"] + ah["hm1"])  / 2
        ah["hm025"] = (ah["hm05"] + ah["h0"])   / 2
        ah["hp025"] = (ah["h0"]   + ah["hp05"]) / 2
        ah["hp075"] = (ah["hp05"] + ah["hp1"])  / 2
        ah["hp125"] = (ah["hp1"]  + ah["hp15"]) / 2

        o20_eff     = at["o20_over"] + 0.5 * at["o20_push"]
        at["o175"]  = (at["o15"]  + o20_eff)   / 2
        at["o225"]  = (o20_eff    + at["o25"]) / 2
        at["o275"]  = (at["o25"]  + at["o35"]) / 2
        at["o325"]  = (at["o25"]  + at["o35"]) / 2

        # Normalise 1X2
        tot = p_h + p_d + p_a
        p_h /= tot; p_d /= tot; p_a /= tot

        # Double Chance
        p_1x = p_h + p_d; p_x2 = p_d + p_a; p_12 = p_h + p_a

        # Scoreline — filter to winning bracket if model has conviction
        sorted_probs = sorted([p_h, p_d, p_a], reverse=True)
        lead = sorted_probs[0] - sorted_probs[1]
        if lead >= 0.05:
            if p_h >= p_d and p_h >= p_a: valid = lambda i,j: i > j
            elif p_a >= p_h and p_a >= p_d: valid = lambda i,j: j > i
            else: valid = lambda i,j: i == j
        else:
            valid = lambda i,j: True

        best = "1-1"; max_p = -1.0
        for (i,j), p in matrix.items():
            if valid(i,j) and p > max_p: max_p, best = p, f"{i}-{j}"

        h_pct = round(p_h * 100); d_pct = round(p_d * 100); a_pct = 100 - h_pct - d_pct

        def fair_odds(p: float) -> float:
            return round(1/p, 2) if p > 0.04 else 25.0

        # AH lines
        def ah_prob(handicap: float) -> tuple:
            frac = handicap % 0.5
            if abs(frac) == 0.25:
                lo = handicap - 0.25; hi = handicap + 0.25
                ph_l,pp_l,pa_l = ah_prob(lo); ph_h,pp_h,pa_h = ah_prob(hi)
                return (ph_l+ph_h)/2, (pp_l+pp_h)/2, (pa_l+pa_h)/2
            p_hc = p_push = p_ac = 0.0
            for (i,j), p in matrix.items():
                adj = (i-j) + handicap
                if handicap % 1 == 0:
                    if adj > 0:   p_hc   += p
                    elif adj==0:  p_push += p
                    else:         p_ac   += p
                else:
                    if adj > 0:   p_hc   += p
                    else:         p_ac   += p
            return p_hc, p_push, p_ac

        def ah_fair_odds(p_cover: float, p_push: float) -> float:
            ep = p_cover / (1 - p_push) if p_push < 1 else 0
            return fair_odds(ep)

        ah_lines = [-1.5,-1.0,-0.75,-0.5,-0.25,0.0,0.25,0.5,0.75,1.0,1.25,1.5]
        ah_results = {}
        for line in ah_lines:
            ph_c, pp, pa_c = ah_prob(line)
            key = f"ah_{line:+.2f}".replace(".00","").replace("+","p").replace("-","m").replace(".","")
            ah_results[key] = {
                "line": line,
                "home_cover": round(ph_c*100,1), "push": round(pp*100,1),
                "away_cover": round(pa_c*100,1),
                "home_odds":  ah_fair_odds(ph_c, pp),
                "away_odds":  ah_fair_odds(pa_c, pp),
            }

        def at_prob(line: float) -> tuple:
            frac = line % 0.5
            if abs(frac) == 0.25:
                lo = line-0.25; hi = line+0.25
                po_l,pp_l,pu_l = at_prob(lo); po_h,pp_h,pu_h = at_prob(hi)
                return (po_l+po_h)/2, (pp_l+pp_h)/2, (pu_l+pu_h)/2
            p_over = p_push = p_under = 0.0
            for (i,j), p in matrix.items():
                goals = i + j
                if line % 1 == 0:
                    if goals > line:    p_over  += p
                    elif goals == line: p_push  += p
                    else:               p_under += p
                else:
                    if goals > line: p_over  += p
                    else:            p_under += p
            return p_over, p_push, p_under

        at_lines = [0.75,1.0,1.25,1.5,1.75,2.0,2.25,2.5,2.75,3.0,3.25,3.5]
        at_results = {}
        for line in at_lines:
            po, pp, pu = at_prob(line)
            key = f"at_{line:.2f}".replace(".","")
            at_results[key] = {
                "line": line,
                "over_pct":  round(po*100,1), "push_pct": round(pp*100,1),
                "under_pct": round(pu*100,1),
                "over_odds":  ah_fair_odds(po, pp),
                "under_odds": ah_fair_odds(pu, pp),
            }

        # ── Suggested Markets ──────────────────────────────────────────────
        # Directs the user to the 2-3 most worthwhile markets to check at
        # their bookie. Scoring combines three factors:
        #
        # 1. MARKET INEFFICIENCY WEIGHT — where bookmakers make the most
        #    systematic pricing errors (AH > Draw > Goals > DC > Home Win)
        # 2. VALUE ZONE — fair odds between 1.40 and 3.50. Below 1.40 the
        #    market is near-certain and bookmakers price it very accurately.
        #    Above 3.50 it's a longshot where variance is too high for Kelly.
        # 3. PROBABILITY STRENGTH — how cleanly the model separates this
        #    outcome. Combined with the value zone, this filters out markets
        #    where the model has weak signal.

        def mkt_inefficiency(mkt_type: str) -> float:
            """
            Structural inefficiency weight per market type.
            AH highest because: less liquid, complex pricing, Poisson matrix
            directly models goal margins which is exactly what AH requires.
            Draw high because: public systematically underbets draws, bookmakers
            can overprice them. Goals/BTTS medium. Home Win lowest — most
            efficient market on earth, sharp money corrects it fastest.
            """
            return {
                "AH":     1.00,   # Most inefficient — model's structural advantage
                "Draw":   0.85,   # Public bias creates systematic overpricing
                "Goals":  0.70,   # Popular but priceable with good goals model
                "DC":     0.45,   # Safety net markets — low edge potential
                "Home":   0.30,   # Most liquid, most efficient — hardest to beat
                "Away":   0.55,   # Public bias inflates away prices on big fixtures
            }.get(mkt_type, 0.50)

        def value_zone_score(fo: float) -> float:
            """
            Score based on how well the fair odds sit in the value zone.
            Peak at 1.80-2.20 (sweet spot for finding bookie errors).
            Falls off sharply below 1.40 (too short) and above 3.50 (longshot).
            Returns 0 outside viable range — these markets are not suggested.
            """
            if fo < 1.40 or fo > 4.00: return 0.0
            if fo <= 2.00: return (fo - 1.40) / 0.60        # ramp up 1.40→2.00
            if fo <= 2.50: return 1.0                        # peak zone
            if fo <= 3.50: return 1.0 - ((fo - 2.50) / 2.0) # ramp down 2.50→3.50
            return max(0, 1.0 - ((fo - 3.50) / 1.0))        # sharp drop 3.50→4.00

        # Build candidate list — every market the model has priced
        home_name = req.get("home", "Home")
        away_name = req.get("away", "Away")

        candidates = []

        # 1X2
        candidates.append({"label": f"{home_name} Win", "type": "Home",  "code": "home",   "fair": fair_odds(p_h),   "prob": p_h})
        candidates.append({"label": "Draw",              "type": "Draw",  "code": "draw",   "fair": fair_odds(p_d),   "prob": p_d})
        candidates.append({"label": f"{away_name} Win",  "type": "Away",  "code": "away",   "fair": fair_odds(p_a),   "prob": p_a})

        # Double Chance — only suggest if it fills a genuine gap
        candidates.append({"label": "1X (Home or Draw)", "type": "DC", "code": "dc_1x", "fair": fair_odds(p_1x), "prob": p_1x})
        candidates.append({"label": "X2 (Draw or Away)", "type": "DC", "code": "dc_x2", "fair": fair_odds(p_x2), "prob": p_x2})

        # Goals
        candidates.append({"label": "BTTS",      "type": "Goals", "code": "btts",   "fair": fair_odds(p_btts),   "prob": p_btts})
        candidates.append({"label": "Over 1.5",  "type": "Goals", "code": "over15", "fair": fair_odds(p_over15), "prob": p_over15})
        candidates.append({"label": "Over 2.5",  "type": "Goals", "code": "over25", "fair": fair_odds(p_over25), "prob": p_over25})
        candidates.append({"label": "Over 3.5",  "type": "Goals", "code": "over35", "fair": fair_odds(p_over35), "prob": p_over35})

        # Asian Handicap — both sides, key lines only (most liquid AH markets)
        for line, label_h, label_a in [
            (-0.5, f"{home_name} -0.5", f"{away_name} +0.5"),
            (-1.0, f"{home_name} -1.0", f"{away_name} +1.0"),
            (+0.5, f"{home_name} +0.5", f"{away_name} -0.5"),
            (+1.0, f"{home_name} +1.0", f"{away_name} -1.0"),
            (-1.5, f"{home_name} -1.5", f"{away_name} +1.5"),
            (+1.5, f"{home_name} +1.5", f"{away_name} -1.5"),
        ]:
            ph_c, pp, pa_c = ah_prob(line)
            fo_h = ah_fair_odds(ph_c, pp)
            fo_a = ah_fair_odds(pa_c, pp)
            prob_h = ph_c / (1 - pp) if pp < 1 else 0
            prob_a = pa_c / (1 - pp) if pp < 1 else 0
            candidates.append({"label": label_h, "type": "AH", "code": f"ah_h_{line}", "fair": fo_h, "prob": prob_h})
            candidates.append({"label": label_a, "type": "AH", "code": f"ah_a_{line}", "fair": fo_a, "prob": prob_a})

        # Score each candidate
        def suggestion_score(c: dict) -> float:
            ineff = mkt_inefficiency(c["type"])
            vzone = value_zone_score(c["fair"])
            # Probability strength: peaks at 50-65% (genuine contest)
            prob  = c["prob"]
            if prob < 0.25 or prob > 0.85: prob_score = 0.0
            elif prob <= 0.50: prob_score = (prob - 0.25) / 0.25
            else:              prob_score = 1.0 - ((prob - 0.50) / 0.50)
            prob_score = max(0, prob_score)
            return round(ineff * vzone * (0.5 + 0.5 * prob_score), 4)

        for c in candidates:
            c["score"] = suggestion_score(c)

        # Filter to scoreable candidates and sort by score desc
        viable = sorted([c for c in candidates if c["score"] > 0], key=lambda x: -x["score"])

        # Take top 3, but ensure diversity — no more than 1 AH, 1 Goals, 1 1X2/DC
        # This prevents the suggestions being 3 AH lines which confuses users
        suggestions = []
        type_counts = {}
        for c in viable:
            broad = "AH" if c["type"] == "AH" else \
                    "Goals" if c["type"] == "Goals" else \
                    "Result"  # covers Home, Away, Draw, DC
            if type_counts.get(broad, 0) < 1:
                suggestions.append(c)
                type_counts[broad] = type_counts.get(broad, 0) + 1
            if len(suggestions) >= 3:
                break

        # Format for frontend
        suggested_markets = [
            {
                "label":    s["label"],
                "type":     s["type"],
                "code":     s["code"],
                "fair":     s["fair"],
                "prob":     round(s["prob"] * 100, 1),
                "score":    s["score"],
            }
            for s in suggestions
        ]

        # ── Auto-log this prediction ──
        match_key = f"{comp}_{h_id}_{a_id}_{time.strftime('%Y-%m-%d',time.gmtime())}"
        log_prediction({
            "match_key":         match_key,
            "date":              time.strftime("%Y-%m-%d", time.gmtime()),
            "timestamp":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "home":              req.get("home",""),
            "away":              req.get("away",""),
            "comp":              comp,
            "league":            req.get("league",""),
            "kickoff":           req.get("kickoff",""),
            "prob_home":         h_pct,
            "prob_draw":         d_pct,
            "prob_away":         a_pct,
            "projected_score":   best,
            "h_lam":             round(h_lam,3),
            "a_lam":             round(a_lam,3),
            "suggested_markets": suggested_markets,
            "resolved":          False,
        })

        return jsonify({
            "score":   best,
            "probs":   {"home": h_pct, "draw": d_pct, "away": a_pct},
            "market":  {
                "home":   fair_odds(p_h),   "draw":  fair_odds(p_d),   "away":  fair_odds(p_a),
                "dc_1x":  fair_odds(p_1x),  "dc_x2": fair_odds(p_x2),  "dc_12": fair_odds(p_12),
                "btts":   fair_odds(p_btts),
                "over15": fair_odds(p_over15), "over25": fair_odds(p_over25), "over35": fair_odds(p_over35),
                "under25": fair_odds(at["u25"]) if at["u25"] > 0 else None,
                "under35": fair_odds(at["u35"]) if at["u35"] > 0 else None,
                "ah_hm05": ah_fair_odds(ah["hm05"], 0), "ah_hp05": ah_fair_odds(ah["hp05"], 0),
                "ah_hm15": ah_fair_odds(ah["hm15"], 0), "ah_hp15": ah_fair_odds(ah["hp15"], 0),
            },
            "suggested_markets": suggested_markets,
            "ah":      ah_results,
            "at":      at_results,
            "h_rank":  h_rank,  "a_rank": a_rank,
            "h_form":  h_form,  "a_form": a_form,
            "h_lam":   round(h_lam, 3), "a_lam": round(a_lam, 3),
        })

    except Exception as e:
        print(f"[PREDICT ERROR] {e}")
        return jsonify({"error": "Prediction engine failed", "detail": str(e)}), 500


@app.route("/scan")
def scan():
    try:
        date_from = request.args.get("date_from","").split("T")[0]
        date_to   = request.args.get("date_to",  "").split("T")[0]
        if not date_from or not date_to:
            return jsonify({"error": "date_from and date_to required"}), 400

        if not fixtures_store: load_cache_from_disk()
        if not fixtures_store: fetch_all_fixtures()
        if not fixtures_store:
            return jsonify({"status": "loading", "message": "Data syncing, try again shortly."})

        try:
            d_from = _date.fromisoformat(date_from); d_to = _date.fromisoformat(date_to)
        except ValueError:
            return jsonify({"error": "Invalid date format"}), 400

        today   = _date.today()
        max_day = today + timedelta(days=7)
        d_to    = min(d_to, max_day)

        _comps: set = set()
        _tmp = d_from
        while _tmp <= d_to:
            for _m in fixtures_store.get(_tmp.isoformat(),[]):
                if _m.get("comp"): _comps.add(_m["comp"])
            _tmp += timedelta(days=1)
        _standings = {c: get_standings(c) for c in _comps}

        ranked = []
        current = d_from
        while current <= d_to:
            ds       = current.isoformat()
            days_out = (current - today).days
            for m in fixtures_store.get(ds, []):
                try:
                    comp = m.get("comp"); h_id = m.get("home_id"); a_id = m.get("away_id")
                    if not comp or not h_id or not a_id: continue

                    all_s  = _standings.get(comp, {"home":{}, "away":{}, "total":{}})
                    lg_avg = all_s.get("league_avg", LEAGUE_AVG_GOALS.get(comp, DEFAULT_LEAGUE_AVG))
                    fh = {"gf":1.2,"ga":1.2,"rank":"N/A"}; fa = {"gf":1.0,"ga":1.3,"rank":"N/A"}
                    h_v = all_s["home"].get(str(h_id)) or all_s["total"].get(str(h_id), fh)
                    a_v = all_s["away"].get(str(a_id)) or all_s["total"].get(str(a_id), fa)

                    hc = form_cache.get((h_id,"HOME")); ac = form_cache.get((a_id,"AWAY"))
                    h_atk = hc["atk"] if hc else 1.0; h_def = hc["def"] if hc else 1.0
                    a_atk = ac["atk"] if ac else 1.0; a_def = ac["def"] if ac else 1.0

                    h_lam = max(min(h_v["gf"]*(a_v["ga"]/lg_avg)*h_atk*(1.0/a_def), 3.2), 0.35)
                    a_lam = max(min(a_v["gf"]*(h_v["ga"]/lg_avg)*a_atk*(1.0/h_def), 3.2), 0.35)

                    p_h,p_d,p_a,p_btts,p_o15,p_o25,p_o35 = compute_probs(h_lam, a_lam)
                    t = p_h+p_d+p_a; p_h/=t; p_d/=t; p_a/=t

                    p_1x = p_h+p_d; p_x2 = p_d+p_a; p_12 = p_h+p_a
                    ps = sorted([p_h,p_d,p_a], reverse=True)
                    confidence = ps[0] - ps[1]

                    if p_h>=p_d and p_h>=p_a: pick,pp,pl,pt = "H",p_h,f"{m['home']} Win","1X2"
                    elif p_a>=p_h and p_a>=p_d: pick,pp,pl,pt = "A",p_a,f"{m['away']} Win","1X2"
                    else: pick,pp,pl,pt = "D",p_d,"Draw","1X2"

                    tier = "HIGH" if confidence>=0.30 else ("MED" if confidence>=0.15 else "LOW")

                    def fo(p): return round(1/p,2) if p>0.04 else 25.0

                    kickoff_str = m.get("kickoff","")
                    # Filter placeholder times (22:00+ UTC for European fixtures)
                    if kickoff_str and len(kickoff_str) > 16 and comp != "BSA":
                        try:
                            if int(kickoff_str[11:13]) >= 22: continue
                        except: pass

                    # Quick AH probability for scan (half-line only — no push)
                    _hp = poisson_vec(h_lam); _ap = poisson_vec(a_lam)
                    def scan_ah(handicap):
                        p = 0.0
                        for i in range(len(_hp)):
                            for j in range(len(_ap)):
                                if (i - j) + handicap > 0: p += _hp[i]*_ap[j]
                        return max(min(p, 0.99), 0.01)

                    ah_hm05 = scan_ah(-0.5)          # home must win
                    ah_hp05 = scan_ah(+0.5)          # home wins or draws
                    ah_a05  = 1.0 - ah_hm05          # away +0.5 (away wins or draws)
                    ah_am05 = 1.0 - ah_hp05          # away -0.5 (away must win)
                    ah_hm10 = scan_ah(-1.0)          # home wins by 2+
                    ah_hp10 = scan_ah(+1.0)          # home wins, draws, or loses by 1

                    all_markets = [
                        # 1X2 — type must match mktInefficiency keys exactly
                        {"code":"H",      "label":f"{m['home']} Win",  "type":"Home",  "prob":round(p_h*100,1),    "fair":fo(p_h)},
                        {"code":"D",      "label":"Draw",               "type":"Draw",  "prob":round(p_d*100,1),    "fair":fo(p_d)},
                        {"code":"A",      "label":f"{m['away']} Win",   "type":"Away",  "prob":round(p_a*100,1),    "fair":fo(p_a)},
                        # Double Chance
                        {"code":"1X",     "label":"1X Home/Draw",       "type":"DC",    "prob":round(p_1x*100,1),   "fair":fo(p_1x)},
                        {"code":"X2",     "label":"X2 Draw/Away",       "type":"DC",    "prob":round(p_x2*100,1),   "fair":fo(p_x2)},
                        {"code":"12",     "label":"12 Home/Away",       "type":"DC",    "prob":round(p_12*100,1),   "fair":fo(p_12)},
                        # Goals
                        {"code":"BTTS",   "label":"BTTS",               "type":"Goals", "prob":round(p_btts*100,1), "fair":fo(p_btts)},
                        {"code":"O15",    "label":"Over 1.5",           "type":"Goals", "prob":round(p_o15*100,1),  "fair":fo(p_o15)},
                        {"code":"O25",    "label":"Over 2.5",           "type":"Goals", "prob":round(p_o25*100,1),  "fair":fo(p_o25)},
                        {"code":"O35",    "label":"Over 3.5",           "type":"Goals", "prob":round(p_o35*100,1),  "fair":fo(p_o35)},
                        # Asian Handicap — key lines both sides
                        {"code":"AH_HM05","label":f"{m['home']} -0.5",  "type":"AH",   "prob":round(ah_hm05*100,1),"fair":fo(ah_hm05)},
                        {"code":"AH_A05", "label":f"{m['away']} +0.5",  "type":"AH",   "prob":round(ah_a05*100,1), "fair":fo(ah_a05)},
                        {"code":"AH_HP05","label":f"{m['home']} +0.5",  "type":"AH",   "prob":round(ah_hp05*100,1),"fair":fo(ah_hp05)},
                        {"code":"AH_AM05","label":f"{m['away']} -0.5",  "type":"AH",   "prob":round(ah_am05*100,1),"fair":fo(ah_am05)},
                        {"code":"AH_HM10","label":f"{m['home']} -1.0",  "type":"AH",   "prob":round(ah_hm10*100,1),"fair":fo(ah_hm10)},
                        {"code":"AH_HP10","label":f"{m['home']} +1.0",  "type":"AH",   "prob":round(ah_hp10*100,1),"fair":fo(ah_hp10)},
                    ]

                    ranked.append({
                        "date": ds, "days_out": days_out, "kickoff": kickoff_str,
                        "home": m["home"], "away": m["away"],
                        "home_id": h_id, "away_id": a_id,
                        "comp": comp, "league": m.get("league", comp),
                        "pick": pick, "pick_label": pl, "mkt_type": pt,
                        "pick_prob": round(pp*100,1), "confidence": round(confidence,4),
                        "tier": tier,
                        "h_lam": round(h_lam,3), "a_lam": round(a_lam,3),
                        "probs": {"home":round(p_h*100,1),"draw":round(p_d*100,1),"away":round(p_a*100,1)},
                        "fair_odds": fo(pp),
                        "all_markets": all_markets,
                    })
                except Exception as e:
                    print(f"[SCAN] Skipped {m.get('home','?')} vs {m.get('away','?')}: {e}")
            current += timedelta(days=1)

        ranked.sort(key=lambda x: x["confidence"], reverse=True)
        return jsonify(ranked)

    except Exception as e:
        print(f"[SCAN ERROR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/acca", methods=["POST"])
def acca():
    try:
        body = request.json
        if not body or "legs" not in body or len(body["legs"]) < 2:
            return jsonify({"error": "At least 2 legs required"}), 400

        legs = body["legs"]; n_sims = 10000; wins = 0

        def build_cdf(lam: float) -> list:
            pmf = poisson_vec(max(min(lam, 3.2), 0.35))
            cdf = []; cumsum = 0.0
            for p in pmf: cumsum += p; cdf.append(cumsum)
            return cdf

        def pois_draw_cdf(cdf: list) -> int:
            k = bisect.bisect_left(cdf, random.random())
            return min(k, len(cdf)-1)

        leg_cdfs = [(build_cdf(float(l["h_lam"])), build_cdf(float(l["a_lam"])), l["pick"]) for l in legs]

        for _ in range(n_sims):
            acca_won = True
            for h_cdf, a_cdf, pick in leg_cdfs:
                hg = pois_draw_cdf(h_cdf); ag = pois_draw_cdf(a_cdf)
                diff = hg - ag; total = hg + ag
                won = False
                if   pick=="H":       won = diff>0
                elif pick=="D":       won = diff==0
                elif pick=="A":       won = diff<0
                elif pick=="1X":      won = diff>=0
                elif pick=="X2":      won = diff<=0
                elif pick=="12":      won = diff!=0
                elif pick=="BTTS":    won = hg>0 and ag>0
                elif pick=="O15":     won = total>1
                elif pick=="O25":     won = total>2
                elif pick=="O35":     won = total>3
                elif pick=="U25":     won = total<3
                elif pick=="U35":     won = total<4
                elif pick=="AH_HM15": won = diff>=2
                elif pick=="AH_HM10": won = diff>=2 or (diff==1 and random.random()<0.5)
                elif pick=="AH_HM05": won = diff>=1
                elif pick=="AH_HM025":won = diff>=1 or (diff==0 and random.random()<0.5)
                elif pick=="AH_H0":   won = diff>0  or (diff==0 and random.random()<0.5)
                elif pick=="AH_HP025":won = diff>=0 or (diff==-1 and random.random()<0.5)
                elif pick=="AH_HP05": won = diff>=0
                elif pick=="AH_HP10": won = diff>=0 or (diff==-1 and random.random()<0.5)
                elif pick=="AH_HP15": won = diff>=-1
                elif pick=="AT_O15":  won = total>1
                elif pick=="AT_O175": won = total>2 or (total==2 and random.random()<0.5)
                elif pick=="AT_O25":  won = total>2
                elif pick=="AT_O275": won = total>3 or (total==3 and random.random()<0.5)
                elif pick=="AT_O35":  won = total>3
                elif pick=="AT_U25":  won = total<3 or (total==3 and random.random()<0.5)
                else:                 won = diff>0
                if not won: acca_won = False; break
            if acca_won: wins += 1

        prob      = wins / n_sims
        fair_odds = round(1/prob, 2) if prob > 0.005 else 200.0
        bookie_odds = body.get("bookie_odds")
        ev = None
        if bookie_odds and float(bookie_odds) > 1:
            bo = float(bookie_odds)
            ev = round((prob*(bo-1))-(1-prob), 4)

        return jsonify({"probability": round(prob*100,2), "fair_odds": fair_odds,
                        "wins": wins, "simulations": n_sims, "ev": ev})
    except Exception as e:
        print(f"[ACCA ERROR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/session")
def session():
    try:
        if not fixtures_store: load_cache_from_disk()
        if not fixtures_store: fetch_all_fixtures()
        if not fixtures_store:
            return jsonify({"status": "loading", "message": "Data syncing, try again shortly."})

        today   = _date.today()
        max_day = today + timedelta(days=7)
        raw_from = request.args.get("date_from","").split("T")[0] or today.isoformat()
        raw_to   = request.args.get("date_to",  "").split("T")[0] or max_day.isoformat()

        try:
            d_from = _date.fromisoformat(raw_from); d_to = _date.fromisoformat(raw_to)
        except ValueError:
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

        d_from = max(d_from, today)
        if d_from > d_to:
            return jsonify({"error": "date_from must be before date_to"}), 400

        print(f"[SESSION] ═══ REQUEST {d_from} → {d_to} ═══")
        print(f"[SESSION] fixtures_store keys: {sorted(fixtures_store.keys())}")

        def mas_score(prob, fo):
            if fo < 1.10 or fo > 6.0: return 0.0
            if fo <= 2.0:   odds_suit = (fo-1.10)/0.9
            elif fo <= 3.5: odds_suit = 1.0-((fo-2.0)/2.5)
            else:           odds_suit = max(0, 1.0-((fo-3.5)/3.0))
            return round(prob * odds_suit, 6)

        stage1_total = 0; stage1_filtered = 0
        raw_pool = []
        _comps: set = set()

        current = d_from
        while current <= d_to:
            ds = current.isoformat()
            day_matches = fixtures_store.get(ds, [])
            print(f"[SESSION] Stage1 {ds}: {len(day_matches)} fixtures in store")
            for m in day_matches:
                stage1_total += 1
                comp = m.get("comp"); h_id = m.get("home_id"); a_id = m.get("away_id")
                if not comp or not h_id or not a_id:
                    stage1_filtered += 1; continue
                kickoff_str = m.get("kickoff","")
                if kickoff_str and len(kickoff_str) >= 10:
                    ko_date = _date.fromisoformat(kickoff_str[:10])
                    if ko_date < d_from or ko_date > d_to:
                        stage1_filtered += 1; continue
                    if len(kickoff_str) > 16 and comp != "BSA":
                        try:
                            if int(kickoff_str[11:13]) >= 22:
                                stage1_filtered += 1
                                print(f"[SESSION]   REJECT placeholder-time: {m.get('home')} vs {m.get('away')}")
                                continue
                        except: pass
                _comps.add(comp)
                raw_pool.append({**m, "_ds": ds})
            current += timedelta(days=1)

        print(f"[SESSION] Stage1: total={stage1_total} | filtered={stage1_filtered} | raw_pool={len(raw_pool)}")

        _standings = {c: get_standings(c) for c in _comps}
        print(f"[SESSION] Stage2: standings for {list(_comps)}")

        scored_pool = []
        for m in raw_pool:
            try:
                comp = m.get("comp"); h_id = m.get("home_id"); a_id = m.get("away_id")
                ds   = m["_ds"]; days_out = (_date.fromisoformat(ds) - today).days
                all_s  = _standings.get(comp, {"home":{}, "away":{}, "total":{}})
                lg_avg = all_s.get("league_avg", LEAGUE_AVG_GOALS.get(comp, DEFAULT_LEAGUE_AVG))
                fh = {"gf":1.2,"ga":1.2}; fa = {"gf":1.0,"ga":1.3}
                h_v = all_s["home"].get(str(h_id)) or all_s["total"].get(str(h_id), fh)
                a_v = all_s["away"].get(str(a_id)) or all_s["total"].get(str(a_id), fa)
                hc = form_cache.get((h_id,"HOME")); ac = form_cache.get((a_id,"AWAY"))
                h_atk = hc["atk"] if hc else 1.0; h_def = hc["def"] if hc else 1.0
                a_atk = ac["atk"] if ac else 1.0; a_def = ac["def"] if ac else 1.0
                h_lam = max(min(h_v["gf"]*(a_v["ga"]/lg_avg)*h_atk*(1.0/a_def), 3.2), 0.35)
                a_lam = max(min(a_v["gf"]*(h_v["ga"]/lg_avg)*a_atk*(1.0/h_def), 3.2), 0.35)
                p_h,p_d,p_a,p_btts,p_o15,p_o25,p_o35 = compute_probs(h_lam, a_lam)
                t = p_h+p_d+p_a; p_h/=t; p_d/=t; p_a/=t
                ps = sorted([p_h,p_d,p_a], reverse=True); confidence = ps[0]-ps[1]
                def fair(p): return round(1/p,2) if p>0.04 else 25.0
                markets = [
                    ("Home Win","1X2",  p_h,    fair(p_h)),
                    ("Draw",    "1X2",  p_d,    fair(p_d)),
                    ("Away Win","1X2",  p_a,    fair(p_a)),
                    ("BTTS",   "Goals", p_btts, fair(p_btts)),
                    ("Over 1.5","Goals",p_o15,  fair(p_o15)),
                    ("Over 2.5","Goals",p_o25,  fair(p_o25)),
                    ("Over 3.5","Goals",p_o35,  fair(p_o35)),
                ]
                best_mkt = None; best_sc = 0.0
                for label, mkt_type, prob, fo in markets:
                    sc = mas_score(prob, fo)
                    if sc > best_sc: best_sc = sc; best_mkt = (label, mkt_type, prob, fo)
                if not best_mkt or best_sc <= 0: continue
                label, mkt_type, prob, fo = best_mkt
                kickoff_str = m.get("kickoff","")
                print(f"[SESSION]   SCORED: {m.get('home')} vs {m.get('away')} | {label} | prob={prob:.2f} fo={fo} sc={best_sc:.4f}")
                scored_pool.append({
                    "date": ds, "kickoff": kickoff_str,
                    "home": m["home"], "away": m["away"],
                    "home_id": h_id, "away_id": a_id,
                    "comp": comp, "league": m.get("league", comp),
                    "market": label, "mkt_type": mkt_type,
                    "prob": round(prob*100,1), "fair_odds": fo,
                    "confidence": round(confidence,4), "mas_score": best_sc,
                    "h_lam": round(h_lam,3), "a_lam": round(a_lam,3),
                    "days_out": days_out,
                })
            except Exception as e:
                print(f"[SESSION] Skipped {m.get('home','?')} vs {m.get('away','?')}: {e}")

        print(f"[SESSION] Stage3: raw_pool={len(raw_pool)} | scored={len(scored_pool)}")

        def ko_epoch(pick):
            ko = pick.get("kickoff","")
            if ko and len(ko) > 10:
                try: return calendar.timegm(time.strptime(ko[:19], "%Y-%m-%dT%H:%M:%S"))
                except: pass
            try: return calendar.timegm(time.strptime(pick["date"], "%Y-%m-%d"))
            except: return 0

        scored_pool.sort(key=ko_epoch)
        WAVE_WINDOW_SEC  = 30 * 60
        GAP_REQUIRED_SEC = 105 * 60

        waves = []
        for pick in scored_pool:
            ep = ko_epoch(pick)
            if not waves or ep - waves[-1]["epoch"] > WAVE_WINDOW_SEC:
                waves.append({"epoch": ep, "picks": []})
            waves[-1]["picks"].append(pick)

        print(f"[SESSION] Stage4: {len(scored_pool)} scored → {len(waves)} waves")

        wave_bests = [max(w["picks"], key=lambda x: x["mas_score"]) for w in waves]
        selected = []; reserves = []; last_ep = 0

        for i, pick in enumerate(wave_bests):
            ep = ko_epoch(pick)
            gap_ok = (not selected) or (ep - last_ep >= GAP_REQUIRED_SEC)
            if gap_ok and len(selected) < 10:
                selected.append(pick); last_ep = ep
            else:
                reserves.append(pick)
                for p in waves[i]["picks"]:
                    if p is not pick and p not in reserves: reserves.append(p)

        if len(selected) < 10:
            last_ep = ko_epoch(selected[-1]) if selected else 0
            for pick in list(reserves):
                ep = ko_epoch(pick)
                if ep - last_ep >= 75*60 and len(selected) < 10:
                    selected.append(pick); reserves.remove(pick); last_ep = ep
            selected.sort(key=ko_epoch)

        reserves.sort(key=lambda x: -x["mas_score"])
        print(f"[SESSION] ═══ FINAL: {len(selected)} picks | {len(reserves)} reserves ═══")

        return jsonify({"session": selected, "reserves": reserves[:15]})

    except Exception as e:
        print(f"[SESSION ERROR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/calibration")
def calibration():
    n = _calibration.get("n", 0); bsum = _calibration.get("brier_sum", 0.0)
    last = _calibration.get("last_run", 0.0)
    if n == 0:
        return jsonify({"status":"pending","message":"Calibration data not yet available.",
                        "brier":None,"matches":0,"last_run":None,"rating":None})
    brier = round(bsum/n, 4)
    rating = "Excellent" if brier<0.45 else "Good" if brier<0.50 else "Fair" if brier<0.55 else "Needs improvement"
    return jsonify({"status":"ok","brier":brier,"matches":n,
                    "last_run":time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last)),
                    "rating":rating,"baseline":0.667,"target":0.50})


# =========================================================
# 📐 CALIBRATION ENGINE
# =========================================================
def run_calibration_check():
    global _calibration
    now_t = time.time()
    yest  = time.strftime("%Y-%m-%d", time.gmtime(now_t - 86400))
    try:
        r = football_data_get(
            f"{BASE_URL}/matches", headers=HEADERS,
            params={"dateFrom":yest,"dateTo":yest,"status":"FINISHED"}, timeout=15
        )
        if r.status_code != 200:
            print(f"[CALIB] Could not fetch results ({r.status_code})"); return
        matches = r.json().get("matches",[]); brier_sum=0.0; n=0
        for m in matches:
            try:
                comp = m.get("competition",{}).get("code")
                if comp not in COMPETITIONS: continue
                h_id = m["homeTeam"]["id"]; a_id = m["awayTeam"]["id"]
                sc   = m["score"]["fullTime"]
                if sc["home"] is None: continue
                hs, as_ = sc["home"], sc["away"]
                r_h=1.0 if hs>as_ else 0.0; r_d=1.0 if hs==as_ else 0.0; r_a=1.0 if hs<as_ else 0.0
                all_s = get_standings(comp)
                lg_avg= all_s.get("league_avg", LEAGUE_AVG_GOALS.get(comp, DEFAULT_LEAGUE_AVG))
                tot_s = all_s.get("total",{})
                fh={"gf":1.2,"ga":1.2}; fa={"gf":1.0,"ga":1.3}
                h_s=tot_s.get(str(h_id),fh); a_s=tot_s.get(str(a_id),fa)
                h_lam=max(min(h_s["gf"]*(a_s["ga"]/lg_avg),3.2),0.35)
                a_lam=max(min(a_s["gf"]*(h_s["ga"]/lg_avg),3.2),0.35)
                p_h,p_d,p_a,_,_,_,_=compute_probs(h_lam,a_lam)
                t=p_h+p_d+p_a; p_h/=t; p_d/=t; p_a/=t
                brier_sum+=(p_h-r_h)**2+(p_d-r_d)**2+(p_a-r_a)**2; n+=1
            except: continue
        if n:
            _calibration["n"]+=n; _calibration["brier_sum"]+=brier_sum; _calibration["last_run"]=now_t
            print(f"[CALIB] {yest}: n={n}, Brier={brier_sum/n:.4f} (cumul: {brier_sum/n:.4f} over {n} matches)")
        else:
            print(f"[CALIB] No finished matches for {yest}")
    except Exception as e:
        print(f"[CALIB ERROR] {e}")


# =========================================================
# 🚀 SCHEDULER
# =========================================================
def preload_standings():
    print("[BOOT] Preloading standings cache...")
    for comp in COMPETITIONS:
        get_standings(comp); time.sleep(7)
    print("[BOOT] Standings preload complete")

def run_scheduler():
    fetch_all_fixtures()
    threading.Thread(target=preload_standings, daemon=True).start()
    while True:
        time.sleep(3600)
        print("[SCHEDULER] Hourly refresh...")
        fetch_all_fixtures()
        preload_standings()
        threading.Thread(target=run_calibration_check, daemon=True).start()
        threading.Thread(target=match_results_to_log,  daemon=True).start()

_workers = int(os.getenv("WEB_CONCURRENCY","1"))
if _workers > 1:
    print(f"⚠️  WARNING: WEB_CONCURRENCY={_workers} — set to 1 to avoid duplicate schedulers.")

_started = False
def start_once():
    global _started
    if not _started:
        _started = True
        print("[INIT] Starting background scheduler...")
        threading.Thread(target=run_scheduler, daemon=True).start()

load_cache_from_disk()
start_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
