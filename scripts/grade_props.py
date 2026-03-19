#!/usr/bin/env python3
"""
Court Edge — Nightly Prop Grader
Runs via GitHub Actions every night at 1 AM CT.
Reads picks from data/picks.json, fetches box scores from the NBA stats API,
grades each prop, and writes results to data/results.json + data/performance.json.
"""

import json
import time
import os
import requests
from datetime import datetime, timedelta, timezone
from dateutil import parser as dateparser

# ─────────────────────────────────────────
#  PATHS
# ─────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICKS_FILE = os.path.join(BASE_DIR, "data", "picks.json")
RESULTS_FILE = os.path.join(BASE_DIR, "data", "results.json")
PERF_FILE  = os.path.join(BASE_DIR, "data", "performance.json")

API_DELAY  = float(os.environ.get("NBA_API_DELAY", "1"))

# NBA Stats API headers (required — it blocks requests without a Referer)
NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.nba.com/",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com",
}

# ─────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────
def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Saved {path}")

def nba_get(url, params=None):
    """GET from NBA stats API with retry logic."""
    for attempt in range(3):
        try:
            r = requests.get(url, headers=NBA_HEADERS, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  Attempt {attempt+1} failed: {e}")
            time.sleep(3 * (attempt + 1))
    return None

# ─────────────────────────────────────────
#  FETCH TODAY'S SCOREBOARD
# ─────────────────────────────────────────
def get_game_ids_for_date(date_str):
    """
    date_str: 'YYYY-MM-DD'
    Returns list of gameId strings for that date.
    """
    url = "https://stats.nba.com/stats/scoreboardv2"
    params = {
        "GameDate": date_str,
        "LeagueID": "00",
        "DayOffset": "0",
    }
    data = nba_get(url, params)
    if not data:
        return []

    headers = data["resultSets"][0]["headers"]
    rows    = data["resultSets"][0]["rowSet"]
    id_idx  = headers.index("GAME_ID")
    status_idx = headers.index("GAME_STATUS_ID")  # 3 = final

    game_ids = []
    for row in rows:
        if row[status_idx] == 3:   # only grade finished games
            game_ids.append(row[id_idx])
    print(f"  Found {len(game_ids)} finished games on {date_str}")
    return game_ids

# ─────────────────────────────────────────
#  FETCH BOX SCORE FOR ONE GAME
# ─────────────────────────────────────────
def get_player_stats(game_id):
    """
    Returns dict: { normalized_player_name: { pts, reb, ast, blk, stl, tpm, ... } }
    """
    url = "https://stats.nba.com/stats/boxscoretraditionalv2"
    params = {"GameID": game_id, "StartPeriod": 0, "EndPeriod": 10,
              "StartRange": 0, "EndRange": 28800, "RangeType": 2}
    data = nba_get(url, params)
    if not data:
        return {}

    players = {}
    for result_set in data["resultSets"]:
        if result_set["name"] == "PlayerStats":
            headers = result_set["headers"]
            for row in result_set["rowSet"]:
                r = dict(zip(headers, row))
                name = normalize(r.get("PLAYER_NAME", ""))
                pts  = r.get("PTS", 0) or 0
                reb  = r.get("REB", 0) or 0
                ast  = r.get("AST", 0) or 0
                blk  = r.get("BLK", 0) or 0
                stl  = r.get("STL", 0) or 0
                tpm  = r.get("FG3M", 0) or 0
                min_ = r.get("MIN", "0:00") or "0:00"
                players[name] = {
                    "pts":  pts,
                    "reb":  reb,
                    "ast":  ast,
                    "blk":  blk,
                    "stl":  stl,
                    "tpm":  tpm,
                    "pra":  pts + reb + ast,
                    "pr":   pts + reb,
                    "pa":   pts + ast,
                    "min":  min_,
                    "raw":  r,
                }
    return players

# ─────────────────────────────────────────
#  STAT RESOLVER
# ─────────────────────────────────────────
STAT_MAP = {
    "pts":     "pts",
    "reb":     "reb",
    "ast":     "ast",
    "blk":     "blk",
    "stl":     "stl",
    "3pm":     "tpm",
    "tpm":     "tpm",
    "pra":     "pra",
    "pr":      "pr",
    "pa":      "pa",
    "pts+reb+ast": "pra",
    "pts+reb": "pr",
    "pts+ast": "pa",
}

def get_actual(player_stats, stat_key):
    """Look up the relevant combined stat for a player."""
    key = STAT_MAP.get(stat_key.lower().replace(" ", ""))
    if key and key in player_stats:
        return player_stats[key]
    return None

def normalize(name):
    """Lowercase + strip accents for fuzzy matching."""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_ = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_.lower().strip()

def match_player(player_name, stats_dict):
    """Find player in stats dict, tolerant of accent differences."""
    norm_target = normalize(player_name)
    if norm_target in stats_dict:
        return stats_dict[norm_target]
    # Partial match fallback (last name)
    last = norm_target.split()[-1]
    for k, v in stats_dict.items():
        if k.endswith(last):
            return v
    return None

# ─────────────────────────────────────────
#  GRADE ONE PROP
# ─────────────────────────────────────────
def grade_prop(prop, player_stats):
    """
    prop: { player, line, threshold, dir, stat, conf, ... }
    player_stats: output of get_player_stats()
    Returns: { ...prop, actual, result, graded: True/False }
    """
    ps = match_player(prop["player"], player_stats)
    if ps is None:
        print(f"    ⚠  Player not found: {prop['player']}")
        return {**prop, "actual": None, "result": "ungraded", "graded": False}

    actual = get_actual(ps, prop.get("stat", "pts"))
    if actual is None:
        print(f"    ⚠  Stat not found: {prop['stat']} for {prop['player']}")
        return {**prop, "actual": None, "result": "ungraded", "graded": False}

    threshold = prop["threshold"]
    direction = prop.get("dir", "over")
    hit = actual > threshold if direction == "over" else actual < threshold
    diff = actual - threshold if direction == "over" else threshold - actual
    diff_str = (f"+{diff:.1f}" if diff >= 0 else f"{diff:.1f}")

    result = "hit" if hit else "miss"
    print(f"    {'✓' if hit else '✗'}  {prop['player']} | {prop['line']} | actual: {actual} {prop['stat']} ({diff_str}) → {result.upper()}")

    return {
        **prop,
        "actual":  actual,
        "result":  result,
        "diff":    round(actual - threshold, 1),
        "graded":  True,
    }

# ─────────────────────────────────────────
#  MAIN GRADING LOOP
# ─────────────────────────────────────────
def grade_day(date_str, picks_for_date, existing_results):
    """Grade all picks for a given date. Returns updated results list."""
    print(f"\n{'─'*50}")
    print(f"Grading picks for {date_str}")
    print(f"{'─'*50}")

    game_ids = get_game_ids_for_date(date_str)
    if not game_ids:
        print(f"  No finished games found — skipping")
        return existing_results

    # Fetch all box scores for the day and merge into one lookup
    all_player_stats = {}
    for gid in game_ids:
        print(f"  Fetching box score: {gid}")
        stats = get_player_stats(gid)
        all_player_stats.update(stats)
        time.sleep(API_DELAY)

    print(f"  Loaded stats for {len(all_player_stats)} players")

    # Build set of already-graded pick IDs to avoid double-grading
    graded_ids = {r["pick_id"] for r in existing_results if r.get("graded")}

    new_results = list(existing_results)
    for pick in picks_for_date:
        pick_id = pick.get("pick_id")
        if pick_id in graded_ids:
            print(f"  Already graded: {pick['player']} — skipping")
            continue
        graded = grade_prop(pick, all_player_stats)
        graded["date"]    = date_str
        graded["pick_id"] = pick_id
        new_results.append(graded)

    return new_results

# ─────────────────────────────────────────
#  PERFORMANCE SUMMARY
# ─────────────────────────────────────────
def build_performance(results):
    """Compute overall + per-user + per-player performance stats."""
    graded = [r for r in results if r.get("graded")]

    overall_hits  = sum(1 for r in graded if r["result"] == "hit")
    overall_total = len(graded)
    overall_rate  = round(overall_hits / overall_total * 100, 1) if overall_total else 0

    # Per player
    by_player = {}
    for r in graded:
        p = r["player"]
        if p not in by_player:
            by_player[p] = {"hits": 0, "total": 0, "diffs": []}
        by_player[p]["total"] += 1
        if r["result"] == "hit":
            by_player[p]["hits"] += 1
        if r.get("diff") is not None:
            by_player[p]["diffs"].append(r["diff"])

    player_stats = {}
    for p, s in by_player.items():
        avg_diff = round(sum(s["diffs"]) / len(s["diffs"]), 1) if s["diffs"] else 0
        player_stats[p] = {
            "hits":    s["hits"],
            "total":   s["total"],
            "rate":    round(s["hits"] / s["total"] * 100, 1) if s["total"] else 0,
            "avg_diff": avg_diff,
        }

    # Per user (if picks have user_id)
    by_user = {}
    for r in graded:
        uid = r.get("user_id", "shared")
        if uid not in by_user:
            by_user[uid] = {"hits": 0, "total": 0}
        by_user[uid]["total"] += 1
        if r["result"] == "hit":
            by_user[uid]["hits"] += 1

    user_stats = {
        uid: {
            "hits":  s["hits"],
            "total": s["total"],
            "rate":  round(s["hits"] / s["total"] * 100, 1) if s["total"] else 0,
        }
        for uid, s in by_user.items()
    }

    # Daily breakdown
    by_date = {}
    for r in graded:
        d = r.get("date", "unknown")
        if d not in by_date:
            by_date[d] = {"hits": 0, "total": 0}
        by_date[d]["total"] += 1
        if r["result"] == "hit":
            by_date[d]["hits"] += 1

    daily = {
        d: {
            "hits":  s["hits"],
            "total": s["total"],
            "rate":  round(s["hits"] / s["total"] * 100, 1) if s["total"] else 0,
        }
        for d, s in sorted(by_date.items())
    }

    # Current streak
    streak = 0
    for r in reversed(graded):
        if r["result"] == "hit":
            streak += 1
        else:
            break

    return {
        "last_updated":  datetime.now(timezone.utc).isoformat(),
        "overall": {
            "hits":   overall_hits,
            "total":  overall_total,
            "rate":   overall_rate,
            "streak": streak,
        },
        "by_player": player_stats,
        "by_user":   user_stats,
        "daily":     daily,
    }

# ─────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────
def main():
    print("Court Edge — Nightly Prop Grader")
    print(f"Run time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    # Load picks and existing results
    picks_data      = load_json(PICKS_FILE, {"picks": []})
    existing_results = load_json(RESULTS_FILE, [])

    all_picks = picks_data.get("picks", [])
    if not all_picks:
        print("No picks found in data/picks.json — nothing to grade.")
        return

    # Figure out which dates need grading
    # The workflow runs at 6 AM UTC = 1 AM CT, so "yesterday" CT = previous NBA slate
    ct_now = datetime.now(timezone.utc) - timedelta(hours=6)
    yesterday = (ct_now - timedelta(days=1)).strftime("%Y-%m-%d")
    today     = ct_now.strftime("%Y-%m-%d")

    # Group picks by date
    dates_to_grade = {yesterday, today}
    graded_dates   = {r.get("date") for r in existing_results if r.get("graded")}

    results = list(existing_results)
    for date_str in sorted(dates_to_grade):
        picks_for_date = [
            p for p in all_picks
            if p.get("date") == date_str and date_str not in graded_dates
        ]
        if not picks_for_date:
            print(f"\nNo ungraded picks for {date_str} — skipping")
            continue
        results = grade_day(date_str, picks_for_date, results)

    # Save results
    save_json(RESULTS_FILE, results)

    # Build and save performance summary
    perf = build_performance(results)
    save_json(PERF_FILE, perf)

    # Print summary
    o = perf["overall"]
    print(f"\n{'═'*50}")
    print(f"OVERALL: {o['hits']}/{o['total']} props hit — {o['rate']}% hit rate")
    print(f"Current streak: {o['streak']} consecutive hits")
    print(f"{'═'*50}")

if __name__ == "__main__":
    main()
