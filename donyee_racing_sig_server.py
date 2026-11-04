#!/usr/bin/env python3
"""
Donyee [4498891] — Torn City Live Racing & Bazaar Signature Server (v3.0)
=========================================================================
Serves TWO live, auto-updating PNG signature banners from a single Render instance:
  1. Racing Telemetry HUD:  GET /donyee_sig.png     (Diagnostics: GET /debug)
  2. Live Bazaar HUD:       GET /donyee_bazaar.png  (Diagnostics: GET /debug-bazaar)

What's New in v3.0 (Live Bazaar Inventory, Market Price Delta & Top Buyers):
  - Real-Time Bazaar Inventory (`GET /user/4498891?selections=bazaar`):
    Reads every item currently listed in Donyee's Bazaar (`name`, `quantity`,
    `price`, and Torn's official `market_price`), calculates exact % above/below
    Market Value (e.g., `-1.8% MV`, `AT MV`, `+1.2% MV`), plus total stock value
    vs. total market value.
  - Real-Time Lifetime Merchant Stats (`GET /user/?selections=personalstats`):
    Reads real `bazaarprofit` (lifetime sales revenue), `bazaarsales` (total
    items sold), and `bazaarcustomers` (total customers).
  - Real-Time Top Buyers Leaderboard (`GET /user/?selections=events` & `log`):
    Parses real Bazaar sale events ("X bought Y x Item from your bazaar for $Z")
    and Bazaar sale logs (log type 1225 / category Bazaars), deduplicates by
    event/log ID into a persistent ledger (`bazaar_buyers_ledger.json`), and
    ranks Top Buyers by cumulative $ spent and items purchased.
  - Browser Live Push Support (`POST /bazaar-telemetry` & `POST /telemetry`):
    Accepts real-time JSON pushes from the companion Tampermonkey script for
    both Racing waiting lobbies and Bazaar inventory/buyer updates.
"""

import argparse
import html
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from PIL import Image, ImageDraw, ImageFont

USER_ID = 4498891
USER_NAME = "DONYEE"
CACHE_TTL_SECONDS = 15
LIVE_PUSH_TTL_SECONDS = 900  # Keep live waiting/racing push active for up to 15 mins
BAZAAR_LEDGER_PATH = os.environ.get("BAZAAR_LEDGER_PATH", "bazaar_buyers_ledger.json")

# Official Torn Wiki Track Lengths (by lowercase track name)
TRACK_LENGTH_BY_NAME = {
    "uptown": 2.25,
    "withdrawal": 3.40,
    "underdog": 1.73,
    "parkland": 3.43,
    "docks": 3.81,
    "commerce": 1.09,
    "two islands": 2.71,
    "industrial": 1.35,
    "vector": 1.16,
    "mudpit": 1.06,
    "hammerhead": 1.16,
    "sewage": 1.50,
    "meltdown": 1.20,
    "speedway": 0.90,
    "stone park": 2.08,
    "convict": 1.64,
}

# Track ID -> Name table (auto-updated from GET /v2/racing/tracks)
TORN_TRACKS = {
    1: {"name": "Uptown", "length_mi": 2.25},
    2: {"name": "Withdrawal", "length_mi": 3.40},
    3: {"name": "Underdog", "length_mi": 1.73},
    4: {"name": "Parkland", "length_mi": 3.43},
    5: {"name": "Docks", "length_mi": 3.81},
    6: {"name": "Commerce", "length_mi": 1.09},
    7: {"name": "Two Islands", "length_mi": 2.71},
    8: {"name": "Industrial", "length_mi": 1.35},
    9: {"name": "Vector", "length_mi": 1.16},
    10: {"name": "Mudpit", "length_mi": 1.06},
    11: {"name": "Hammerhead", "length_mi": 1.16},
    12: {"name": "Sewage", "length_mi": 1.50},
    13: {"name": "Meltdown", "length_mi": 1.20},
    14: {"name": "Speedway", "length_mi": 0.90},
    15: {"name": "Stone Park", "length_mi": 2.08},
    16: {"name": "Convict", "length_mi": 1.64},
    19: {"name": "Sewage", "length_mi": 1.50},
}

TRACKS_LOADED_FROM_API = False
DRIVER_NAME_CACHE = {USER_ID: USER_NAME}
CAR_NAME_CACHE = {}

# Persistent in-memory + disk ledger for real Bazaar buyers
BAZAAR_BUYERS_LEDGER = {
    "seen_tx_ids": {},   # tx_id -> timestamp
    "buyers": {},        # key (name_lower or uid) -> {"name": str, "uid": int, "spent": int, "items": int, "tx_count": int, "last_ts": int}
}


def load_bazaar_ledger():
    global BAZAAR_BUYERS_LEDGER
    if os.path.exists(BAZAAR_LEDGER_PATH):
        try:
            with open(BAZAAR_LEDGER_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict) and "buyers" in loaded:
                    BAZAAR_BUYERS_LEDGER = loaded
        except Exception:
            pass


def save_bazaar_ledger():
    try:
        with open(BAZAAR_LEDGER_PATH, "w", encoding="utf-8") as f:
            json.dump(BAZAAR_BUYERS_LEDGER, f)
    except Exception:
        pass


load_bazaar_ledger()

# 2D Circuit Polygons (normalized 0.0..1.0) keyed by lowercase track name
TRACK_SHAPES = {
    "commerce": [
        (0.18, 0.28), (0.34, 0.14), (0.58, 0.36), (0.86, 0.68),
        (0.76, 0.88), (0.48, 0.62), (0.28, 0.46),
    ],
    "sewage": [
        (0.14, 0.72), (0.22, 0.26), (0.58, 0.18), (0.84, 0.48),
        (0.78, 0.68), (0.56, 0.84), (0.34, 0.72),
    ],
    "docks": [
        (0.10, 0.78), (0.10, 0.25), (0.35, 0.20), (0.42, 0.35),
        (0.65, 0.35), (0.72, 0.18), (0.86, 0.22), (0.84, 0.65),
        (0.68, 0.68), (0.60, 0.82), (0.30, 0.82),
    ],
    "withdrawal": [
        (0.08, 0.65), (0.18, 0.22), (0.55, 0.18), (0.88, 0.30),
        (0.84, 0.75), (0.50, 0.82), (0.25, 0.72),
    ],
    "speedway": [
        (0.12, 0.60), (0.15, 0.30), (0.50, 0.18), (0.85, 0.30),
        (0.86, 0.60), (0.75, 0.80), (0.25, 0.80),
    ],
}


def get_track_length(track_name: str, fallback: float = 2.0) -> float:
    return TRACK_LENGTH_BY_NAME.get(str(track_name).strip().lower(), fallback)


def get_track_polygon(track_id: int, track_name: str, box: tuple) -> list:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    pts = TRACK_SHAPES.get(str(track_name).strip().lower())
    if not pts:
        pts = []
        num_vertices = 10
        seed = sum(ord(c) for c in str(track_name)) or track_id
        for i in range(num_vertices):
            angle = (2 * math.pi * i) / num_vertices
            radius_x = 0.36 + 0.08 * math.sin(i * 2.0 + seed)
            radius_y = 0.32 + 0.09 * math.cos(i * 3.0 + seed)
            pts.append((0.5 + radius_x * math.cos(angle), 0.5 + radius_y * math.sin(angle)))
    return [(x0 + px * w, y0 + py * h) for px, py in pts]


def interpolate_along_circuit(poly: list, progress: float) -> tuple:
    closed = poly + [poly[0]]
    seg_lengths = []
    total_len = 0.0
    for i in range(len(closed) - 1):
        dx = closed[i + 1][0] - closed[i][0]
        dy = closed[i + 1][1] - closed[i][1]
        d = math.hypot(dx, dy)
        seg_lengths.append(d)
        total_len += d

    target = (progress % 1.0) * total_len
    acc = 0.0
    for i, seg_len in enumerate(seg_lengths):
        if acc + seg_len >= target and seg_len > 0:
            t = (target - acc) / seg_len
            cx = closed[i][0] + t * (closed[i + 1][0] - closed[i][0])
            cy = closed[i][1] + t * (closed[i + 1][1] - closed[i][1])
            return cx, cy
        acc += seg_len
    return closed[0]


def compute_racing_class(points: int) -> str:
    if points >= 475:
        return "CLASS A"
    if points >= 250:
        return "CLASS B"
    if points >= 100:
        return "CLASS C"
    if points >= 25:
        return "CLASS D"
    return "CLASS E"


def format_race_time(seconds_val) -> str:
    try:
        sec = float(seconds_val)
        if sec <= 0:
            return ""
        if sec >= 3600:
            hrs = int(sec // 3600)
            mins = int((sec % 3600) // 60)
            return f"{hrs}h {mins:02d}m"
        if sec >= 60:
            mins = int(sec // 60)
            rem = sec - (mins * 60)
            return f"{mins}:{rem:04.1f}s"
        return f"{sec:.1f}s"
    except Exception:
        return ""


def format_money(val: float, compact: bool = False) -> str:
    try:
        n = float(val)
    except Exception:
        return "$0"
    sign = "-" if n < 0 else ""
    a = abs(n)
    if compact:
        if a >= 1_000_000_000:
            return f"{sign}${a / 1_000_000_000:.2f}B"
        if a >= 1_000_000:
            return f"{sign}${a / 1_000_000:.2f}M"
        if a >= 10_000:
            return f"{sign}${a / 1_000:.1f}K"
    return f"{sign}${int(round(a)):,}"


def get_empty_telemetry() -> dict:
    return {
        "racing_skill": 0.0,
        "racing_points": 0,
        "racing_class": "CLASS E",
        "races_entered": 0,
        "races_won": 0,
        "win_rate": 0.0,
        "race": {
            "status": "IN PIT / AWAITING RACE",
            "track_id": 6,
            "track_name": "Commerce",
            "laps": 0,
            "current_lap": 0,
            "progress": 0.0,
            "is_official": True,
            "position": 1,
            "total_drivers": 1,
            "car_name": "Active Spec",
            "speed_mph": 0.0,
            "accel": 0.0,
            "best_lap": "--:--.--",
            "standings": [
                {"pos": 1, "label": f"{USER_NAME} [{USER_ID}]", "status": "READY", "is_me": True, "progress": 0.0},
            ],
        },
        "debug": {},
        "updated_at": time.strftime("%H:%M:%S TCT", time.gmtime()),
    }


def fetch_json(url: str, api_key: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"ApiKey {api_key}",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ensure_tracks_and_cars_loaded(api_key: str):
    global TRACKS_LOADED_FROM_API
    if TRACKS_LOADED_FROM_API or not api_key:
        return
    try:
        t_resp = fetch_json(f"https://api.torn.com/v2/racing/tracks?key={api_key}", api_key)
        tracks_data = t_resp.get("tracks", t_resp)
        if isinstance(tracks_data, list):
            for t in tracks_data:
                if isinstance(t, dict) and t.get("id"):
                    tid = int(t["id"])
                    tname = str(t.get("title") or t.get("name") or f"Track #{tid}")
                    tlen = get_track_length(tname, float(t.get("length") or t.get("distance") or 2.0))
                    TORN_TRACKS[tid] = {"name": tname, "length_mi": tlen}
        elif isinstance(tracks_data, dict):
            for k, t in tracks_data.items():
                if str(k).isdigit() and isinstance(t, dict):
                    tid = int(k)
                    tname = str(t.get("title") or t.get("name") or f"Track #{tid}")
                    tlen = get_track_length(tname, float(t.get("length") or t.get("distance") or 2.0))
                    TORN_TRACKS[tid] = {"name": tname, "length_mi": tlen}
        TRACKS_LOADED_FROM_API = True
    except Exception as exc:
        print(f"[TRACKS LOAD WARN] {exc}", file=sys.stderr, flush=True)

    try:
        c_resp = fetch_json(f"https://api.torn.com/v2/racing/cars?key={api_key}", api_key)
        cars_data = c_resp.get("cars", c_resp)
        if isinstance(cars_data, list):
            for c in cars_data:
                if isinstance(c, dict) and (c.get("car_item_id") or c.get("id")):
                    cid = int(c.get("car_item_id") or c.get("id"))
                    CAR_NAME_CACHE[cid] = str(c.get("car_item_name") or c.get("name") or f"Car #{cid}")
    except Exception:
        pass


def resolve_driver_name(uid: int, api_key: str) -> str:
    if not uid or uid <= 0:
        return "Unknown"
    if uid in DRIVER_NAME_CACHE:
        return DRIVER_NAME_CACHE[uid]
    if not api_key:
        return f"Player [{uid}]"
    try:
        b_resp = fetch_json(f"https://api.torn.com/user/{uid}?selections=basic&key={api_key}", api_key)
        name = b_resp.get("name") or (b_resp.get("profile", {}) or {}).get("name")
        if name:
            DRIVER_NAME_CACHE[uid] = str(name)
            return str(name)
    except Exception:
        pass
    return f"Player [{uid}]"


def extract_personalstats(raw: dict) -> dict:
    out = {
        "rs": 0.0,
        "pts": 0,
        "entered": 0,
        "won": 0,
        "bazaar_customers": 0,
        "bazaar_sales": 0,
        "bazaar_profit": 0,
    }
    pstats = raw.get("personalstats", raw)
    if isinstance(pstats, dict):
        racing_sub = pstats.get("racing")
        if isinstance(racing_sub, dict):
            out["rs"] = float(racing_sub.get("skill") or racing_sub.get("racingskill") or 0.0)
            out["pts"] = int(racing_sub.get("points") or racing_sub.get("racingpointsearned") or 0)
            out["entered"] = int(
                racing_sub.get("races", {}).get("entered")
                if isinstance(racing_sub.get("races"), dict)
                else (racing_sub.get("racesentered") or 0)
            )
            out["won"] = int(
                racing_sub.get("races", {}).get("won")
                if isinstance(racing_sub.get("races"), dict)
                else (racing_sub.get("raceswon") or 0)
            )
        else:
            out["rs"] = float(pstats.get("racingskill") or 0.0)
            out["pts"] = int(pstats.get("racingpointsearned") or 0)
            out["entered"] = int(pstats.get("racesentered") or 0)
            out["won"] = int(pstats.get("raceswon") or 0)

        bazaar_sub = pstats.get("bazaar") or (pstats.get("trading", {}).get("bazaar") if isinstance(pstats.get("trading"), dict) else None)
        if isinstance(bazaar_sub, dict):
            out["bazaar_customers"] = int(bazaar_sub.get("customers") or bazaar_sub.get("bazaarcustomers") or 0)
            out["bazaar_sales"] = int(bazaar_sub.get("sales") or bazaar_sub.get("bazaarsales") or 0)
            out["bazaar_profit"] = int(bazaar_sub.get("profit") or bazaar_sub.get("bazaarprofit") or 0)
        else:
            out["bazaar_customers"] = int(pstats.get("bazaarcustomers") or 0)
            out["bazaar_sales"] = int(pstats.get("bazaarsales") or 0)
            out["bazaar_profit"] = int(pstats.get("bazaarprofit") or 0)
    elif isinstance(pstats, list):
        for item in pstats:
            if isinstance(item, dict):
                k = str(item.get("name", "")).lower()
                v = item.get("value", 0)
                if k in ("racingskill", "skill"):
                    out["rs"] = float(v)
                elif k in ("racingpointsearned", "points"):
                    out["pts"] = int(v)
                elif k in ("racesentered", "entered"):
                    out["entered"] = int(v)
                elif k in ("raceswon", "won"):
                    out["won"] = int(v)
                elif k in ("bazaarcustomers", "customers"):
                    out["bazaar_customers"] = int(v)
                elif k in ("bazaarsales", "sales"):
                    out["bazaar_sales"] = int(v)
                elif k in ("bazaarprofit", "profit"):
                    out["bazaar_profit"] = int(v)
    return out


# ==============================================================================
# RACING TELEMETRY ENGINE (v2.5 preserved)
# ==============================================================================

def fetch_live_torn_telemetry(api_key: str) -> dict:
    data = get_empty_telemetry()
    now_ts = int(time.time())
    debug_info = {"ts": now_ts, "key_present": bool(api_key)}

    ensure_tracks_and_cars_loaded(api_key)

    # 1. Fetch personalstats
    try:
        v1_url = f"https://api.torn.com/user/?selections=personalstats&key={api_key}"
        v1_data = fetch_json(v1_url, api_key)
        if "error" not in v1_data:
            parsed_ps = extract_personalstats(v1_data)
            data["racing_skill"] = parsed_ps.get("rs", 0.0)
            data["racing_points"] = parsed_ps.get("pts", 0)
            data["racing_class"] = compute_racing_class(data["racing_points"])
            data["races_entered"] = parsed_ps.get("entered", 0)
            data["races_won"] = parsed_ps.get("won", 0)
            if data["races_entered"] > 0:
                data["win_rate"] = (data["races_won"] / data["races_entered"]) * 100.0
            debug_info["v1_personalstats_ok"] = parsed_ps
        else:
            debug_info["v1_personalstats_error"] = v1_data["error"]
    except Exception as exc:
        debug_info["v1_exception"] = str(exc)

    # 2. Fetch user races (/v2/user/races) + lobby races (/v2/racing/races)
    races_by_id = {}
    for url_variant in (
        f"https://api.torn.com/v2/user/races?sort=DESC&limit=20&key={api_key}",
        f"https://api.torn.com/v2/user/races?limit=20&key={api_key}",
    ):
        try:
            resp = fetch_json(url_variant, api_key)
            if "races" in resp and isinstance(resp["races"], list):
                for r in resp["races"]:
                    if isinstance(r, dict) and r.get("id"):
                        races_by_id[int(r["id"])] = r
        except Exception as exc:
            debug_info["races_fetch_warn"] = str(exc)

    try:
        lobby_resp = fetch_json(f"https://api.torn.com/v2/racing/races?limit=50&key={api_key}", api_key)
        for r in (lobby_resp.get("races") or []):
            if isinstance(r, dict) and r.get("id"):
                entries = r.get("results") or r.get("participants") or []
                if any(int((e.get("user_id") or e.get("driver_id") or e.get("id") or 0)) == USER_ID for e in entries if isinstance(e, dict)):
                    races_by_id[int(r["id"])] = r
    except Exception:
        pass

    races_list = sorted(
        races_by_id.values(),
        key=lambda r: (int(r.get("id") or 0), int((r.get("schedule") or {}).get("start") or 0)),
        reverse=True,
    )
    debug_info["merged_recent_races_count"] = len(races_list)

    if races_list:
        selected_race = None
        for r in races_list:
            s_raw = str(r.get("status", "")).lower()
            s_sched = r.get("schedule") or {}
            s_start = int(s_sched.get("start") or 0)
            s_end = int(s_sched.get("end") or 0)
            if (
                s_raw in ("in_progress", "open", "waiting", "preparing", "scheduled", "lobby")
                or (s_start > now_ts)
                or (s_start > 0 and s_start <= now_ts and (s_end == 0 or now_ts < s_end))
            ):
                selected_race = r
                break

        if not selected_race:
            selected_race = races_list[0]

        race_id = int(selected_race.get("id") or 0)
        if race_id > 0:
            try:
                detail_resp = fetch_json(f"https://api.torn.com/v2/racing/{race_id}/race?key={api_key}", api_key)
                if "race" in detail_resp and isinstance(detail_resp["race"], dict):
                    selected_race.update(detail_resp["race"])
            except Exception:
                pass

        track_id = int(selected_race.get("track_id") or 6)
        track_meta = TORN_TRACKS.get(track_id, {"name": f"Track #{track_id}", "length_mi": 2.0})
        track_name = track_meta["name"]
        track_len = get_track_length(track_name, track_meta.get("length_mi", 2.0))
        laps = int(selected_race.get("laps") or 5)
        status_raw = str(selected_race.get("status") or "finished").lower()
        sched = selected_race.get("schedule") or {}
        start_ts = int(sched.get("start") or 0)
        end_ts = int(sched.get("end") or 0)

        debug_info["selected_race"] = {
            "id": race_id,
            "track_id": track_id,
            "track_name": track_name,
            "track_len_mi": track_len,
            "status": status_raw,
            "laps": laps,
            "schedule": sched,
        }

        is_staging = (
            status_raw in ("open", "waiting", "preparing", "scheduled", "lobby")
            or (start_ts > now_ts)
            or (start_ts == 0 and end_ts == 0 and status_raw != "finished")
        )
        is_active = (
            not is_staging
            and (
                status_raw == "in_progress"
                or (start_ts > 0 and start_ts <= now_ts and (end_ts == 0 or now_ts < end_ts))
            )
        )

        if is_staging:
            if start_ts > now_ts:
                rem_sec = max(0, start_ts - now_ts)
                wait_m, wait_s = divmod(rem_sec, 60)
                race_status = f"WAITING • {wait_m}M {wait_s:02d}S ({track_name.upper()})"
            else:
                race_status = f"WAITING IN LOBBY • {track_name.upper()} ({laps}L)"
            cur_lap = 0
            lap_prog = 0.0
            est_speed = 0.0
            est_accel = 0.0
        elif is_active:
            est_duration = max(120, int(laps * track_len * 28))
            elapsed = max(0, now_ts - start_ts)
            overall_prog = min(0.99, elapsed / est_duration)
            cur_lap = min(laps, int(overall_prog * laps) + 1)
            lap_prog = (overall_prog * laps) % 1.0
            race_status = f"LIVE ON TRACK • LAP {cur_lap}/{laps}"
            est_speed = 132.0 + 18.0 * math.sin(lap_prog * 2 * math.pi)
            est_accel = 1.4 * math.cos(lap_prog * 2 * math.pi)
        else:
            cur_lap = laps
            lap_prog = 1.0
            est_speed = 0.0
            est_accel = 0.0
            race_status = f"LAST RACE: {track_name.upper()} • {laps}L"

        results = selected_race.get("results") or selected_race.get("participants") or []
        if isinstance(results, list):
            results_sorted = sorted(
                results,
                key=lambda x: int(x.get("position") or 99) if isinstance(x, dict) else 99,
            )
        else:
            results_sorted = []

        top_entries = results_sorted[:6]
        if not any(int((e.get("user_id") or e.get("driver_id") or e.get("id") or 0)) == USER_ID for e in top_entries if isinstance(e, dict)):
            for e in results_sorted[6:]:
                if isinstance(e, dict) and int(e.get("user_id") or e.get("driver_id") or e.get("id") or 0) == USER_ID:
                    if len(top_entries) == 6:
                        top_entries[5] = e
                    else:
                        top_entries.append(e)
                    break

        standings = []
        my_pos = 1
        for idx, entry in enumerate(top_entries):
            if not isinstance(entry, dict):
                continue
            uid = int(entry.get("user_id") or entry.get("driver_id") or entry.get("id") or 0)
            pos = int(entry.get("position") or (idx + 1))
            crashed = bool(entry.get("has_crashed") or entry.get("crashed"))
            car_id = int(entry.get("car_item_id") or entry.get("car_id") or 0)
            car_title = (
                entry.get("car_item_name")
                or entry.get("car_name")
                or CAR_NAME_CACHE.get(car_id)
                or (f"Car #{car_id}" if car_id else "Race Spec")
            )
            race_time_val = entry.get("race_time") or entry.get("time") or entry.get("best_lap_time")
            is_me = (uid == USER_ID)

            if is_me:
                my_pos = pos
                data["race"]["car_name"] = str(car_title)
                driver_label = f"{USER_NAME} [{USER_ID}]"
            else:
                r_name = entry.get("name") or entry.get("driver_name") or resolve_driver_name(uid, api_key)
                driver_label = f"{r_name}"

            if is_staging:
                driver_status = "WAITING"
                driver_prog = 0.0
            elif crashed:
                driver_status = "CRASHED"
                driver_prog = 0.0
            elif is_active:
                driver_status = "ON TRACK"
                driver_prog = (lap_prog - idx * 0.09) % 1.0
            else:
                t_str = format_race_time(race_time_val)
                driver_status = f"P{pos} • {t_str}" if t_str else f"FINISHED P{pos}"
                driver_prog = (1.0 - idx * 0.06) % 1.0

            standings.append({
                "pos": pos,
                "label": driver_label,
                "status": driver_status,
                "is_me": is_me,
                "progress": driver_prog,
            })

        data["race"].update({
            "status": race_status,
            "track_id": track_id,
            "track_name": track_name,
            "track_len_mi": track_len,
            "laps": laps,
            "current_lap": cur_lap,
            "progress": lap_prog,
            "is_official": bool(selected_race.get("is_official", True)),
            "position": my_pos,
            "total_drivers": max(len(results_sorted), 1),
            "speed_mph": est_speed,
            "accel": est_accel,
            "standings": standings if standings else data["race"]["standings"],
        })

    data["debug"] = debug_info
    data["updated_at"] = time.strftime("%H:%M:%S TCT", time.gmtime())
    return data


def parse_torn_racing_telemetry_payload(payload: dict, fallback_stats: dict = None) -> dict:
    base = fallback_stats or get_empty_telemetry()
    race_block = payload.get("race", payload)

    track_name = str(race_block.get("trackName") or race_block.get("track_name") or base["race"]["track_name"]).strip()
    track_id = int(
        race_block.get("trackId")
        or race_block.get("track_id")
        or next((k for k, v in TORN_TRACKS.items() if v["name"].lower() == track_name.lower()), base["race"]["track_id"])
    )
    track_len = get_track_length(track_name, TORN_TRACKS.get(track_id, {}).get("length_mi", 2.0))
    laps = int(race_block.get("laps") or base["race"]["laps"] or 15)
    current_lap = int(race_block.get("currentLap") if race_block.get("currentLap") is not None else (race_block.get("current_lap") or 0))
    lap_progress = float(race_block.get("lapProgress") if race_block.get("lapProgress") is not None else (race_block.get("progress") or 0.0))
    speed_mph = float(race_block.get("speed") if race_block.get("speed") is not None else (race_block.get("speed_mph") or 0.0))
    accel = float(race_block.get("accel") if race_block.get("accel") is not None else (race_block.get("acceleration") or 0.0))

    drivers_raw = (
        payload.get("drivers")
        or race_block.get("drivers")
        or race_block.get("standings")
        or []
    )
    standings = []
    my_pos = int(race_block.get("position") or 1)
    car_name = str(race_block.get("carName") or race_block.get("car_name") or base["race"]["car_name"])

    for idx, drv in enumerate(drivers_raw[:6]):
        uid = drv.get("userId") or drv.get("user_id") or drv.get("id") or 0
        raw_name = str(drv.get("name") or drv.get("driverName") or drv.get("label") or f"Driver #{idx + 1}")
        clean_name = raw_name.replace("(You)", "").strip()
        pos = int(drv.get("position") or drv.get("pos") or (idx + 1))
        is_me = bool(drv.get("is_me") or int(uid or 0) == USER_ID or "donyee" in raw_name.lower() or "(you)" in raw_name.lower())
        if is_me:
            my_pos = pos
            if drv.get("speed") is not None:
                speed_mph = float(drv["speed"])
            if drv.get("accel") is not None:
                accel = float(drv["accel"])
            if drv.get("lapProgress") is not None:
                lap_progress = float(drv["lapProgress"])
            if drv.get("carName"):
                car_name = str(drv["carName"])

        status_str = str(drv.get("status") or drv.get("telemetryText") or f"P{pos}").upper()
        is_waiting_drv = ("WAIT" in status_str)
        drv_prog = 0.0 if is_waiting_drv else float(drv.get("lapProgress") or drv.get("progress") or ((lap_progress - idx * 0.08) % 1.0))

        standings.append({
            "pos": pos,
            "label": f"{USER_NAME} [{USER_ID}]" if is_me else clean_name[:16],
            "status": status_str[:14],
            "is_me": is_me,
            "progress": drv_prog,
        })

    if payload.get("racingSkill") or payload.get("racing_skill"):
        base["racing_skill"] = float(payload.get("racingSkill") or payload.get("racing_skill"))
    if payload.get("racingPoints") or payload.get("racing_points"):
        base["racing_points"] = int(payload.get("racingPoints") or payload.get("racing_points"))
        base["racing_class"] = compute_racing_class(base["racing_points"])

    countdown_str = str(race_block.get("countdown") or "").strip()
    if countdown_str:
        status_banner = f"WAITING • {countdown_str.upper()} ({track_name.upper()})"
    elif race_block.get("status"):
        status_banner = str(race_block.get("status")).upper()
    elif any("WAIT" in s["status"] for s in standings):
        status_banner = f"WAITING • {track_name.upper()} ({laps}L)"
    else:
        status_banner = f"LIVE TELEMETRY • LAP {current_lap}/{laps}"

    base["race"].update({
        "status": status_banner,
        "track_id": track_id,
        "track_name": track_name,
        "track_len_mi": track_len,
        "laps": laps,
        "current_lap": current_lap,
        "progress": lap_progress,
        "position": my_pos,
        "total_drivers": max(len(standings), 1),
        "car_name": car_name,
        "speed_mph": speed_mph,
        "accel": accel,
        "standings": standings if standings else base["race"]["standings"],
    })
    base["updated_at"] = time.strftime("%H:%M:%S TCT", time.gmtime())
    return base


def get_demo_telemetry() -> dict:
    return {
        "racing_skill": 7.00,
        "racing_points": 207,
        "racing_class": "CLASS C",
        "races_entered": 213,
        "races_won": 30,
        "win_rate": 14.1,
        "race": {
            "status": "WAITING • 5M 47S (COMMERCE • 15L)",
            "track_id": 6,
            "track_name": "Commerce",
            "track_len_mi": 1.09,
            "laps": 15,
            "current_lap": 0,
            "progress": 0.0,
            "is_official": True,
            "position": 5,
            "total_drivers": 6,
            "car_name": "Class C Spec",
            "speed_mph": 0.0,
            "accel": 0.0,
            "best_lap": "--:--",
            "standings": [
                {"pos": 1, "label": "Primian", "status": "WAITING", "is_me": False, "progress": 0.0},
                {"pos": 2, "label": "LordFiendish", "status": "WAITING", "is_me": False, "progress": 0.0},
                {"pos": 3, "label": "Ifiron", "status": "WAITING", "is_me": False, "progress": 0.0},
                {"pos": 4, "label": "Medanox", "status": "WAITING", "is_me": False, "progress": 0.0},
                {"pos": 5, "label": "DONYEE [4498891]", "status": "WAITING", "is_me": True, "progress": 0.0},
                {"pos": 6, "label": "HusbandKitt", "status": "WAITING", "is_me": False, "progress": 0.0},
            ],
        },
        "updated_at": time.strftime("%H:%M:%S TCT", time.gmtime()),
    }


# ==============================================================================
# LIVE BAZAAR INVENTORY, MARKET VALUE & TOP BUYERS ENGINE (v3.0)
# ==============================================================================

EVENT_BUY_RE = re.compile(
    r"(?P<buyer>[A-Za-z0-9_\-]+)\s+bought\s+(?P<qty>[\d,]+)\s*x\s*(?P<item>.+?)\s+from your bazaar for\s+\$(?P<total>[\d,]+)",
    re.IGNORECASE,
)
XID_RE = re.compile(r"XID=(\d+)", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def record_bazaar_sale(tx_id: str, buyer_name: str, buyer_uid: int, qty: int, total_price: int, ts: int):
    """Records a unique Bazaar sale into the persistent Top Buyers ledger."""
    if not tx_id or total_price <= 0:
        return
    tx_key = str(tx_id)
    if tx_key in BAZAAR_BUYERS_LEDGER["seen_tx_ids"]:
        return

    BAZAAR_BUYERS_LEDGER["seen_tx_ids"][tx_key] = ts
    clean_name = str(buyer_name or "").strip() or (f"Player [{buyer_uid}]" if buyer_uid else "Anonymous")
    buyer_key = str(buyer_uid) if buyer_uid and buyer_uid > 0 else clean_name.lower()

    entry = BAZAAR_BUYERS_LEDGER["buyers"].get(
        buyer_key,
        {"name": clean_name, "uid": int(buyer_uid or 0), "spent": 0, "items": 0, "tx_count": 0, "last_ts": 0},
    )
    if clean_name and not clean_name.startswith("Player ["):
        entry["name"] = clean_name
    if buyer_uid and buyer_uid > 0:
        entry["uid"] = int(buyer_uid)
    entry["spent"] = int(entry.get("spent", 0)) + int(total_price)
    entry["items"] = int(entry.get("items", 0)) + max(1, int(qty))
    entry["tx_count"] = int(entry.get("tx_count", 0)) + 1
    entry["last_ts"] = max(int(entry.get("last_ts", 0)), int(ts or time.time()))
    BAZAAR_BUYERS_LEDGER["buyers"][buyer_key] = entry


def get_top_buyers_list(limit: int = 6) -> list:
    buyers = list(BAZAAR_BUYERS_LEDGER.get("buyers", {}).values())
    buyers.sort(key=lambda b: (int(b.get("spent", 0)), int(b.get("items", 0)), int(b.get("last_ts", 0))), reverse=True)
    out = []
    for idx, b in enumerate(buyers[:limit]):
        out.append({
            "rank": idx + 1,
            "name": str(b.get("name") or "Unknown"),
            "uid": int(b.get("uid") or 0),
            "spent": int(b.get("spent") or 0),
            "items": int(b.get("items") or 0),
            "tx_count": int(b.get("tx_count") or 1),
        })
    return out


def parse_events_for_bazaar_buyers(events_obj, api_key: str = "") -> int:
    """Extracts Bazaar purchases from Torn v1/v2 `events` payload."""
    added_before = len(BAZAAR_BUYERS_LEDGER["seen_tx_ids"])
    events_iter = []
    if isinstance(events_obj, dict):
        for eid, ev in events_obj.items():
            if isinstance(ev, dict):
                ev_copy = dict(ev)
                ev_copy.setdefault("id", eid)
                events_iter.append(ev_copy)
    elif isinstance(events_obj, list):
        events_iter = [e for e in events_obj if isinstance(e, dict)]

    for ev in events_iter:
        eid = str(ev.get("id") or ev.get("ID") or ev.get("timestamp") or "")
        raw_html = str(ev.get("event") or ev.get("text") or "")
        if "bazaar" not in raw_html.lower():
            continue
        xid_match = XID_RE.search(raw_html)
        buyer_uid = int(xid_match.group(1)) if xid_match else 0
        plain_text = html.unescape(TAG_RE.sub("", raw_html)).strip()
        m = EVENT_BUY_RE.search(plain_text)
        if m:
            buyer_name = m.group("buyer").strip()
            qty = int(m.group("qty").replace(",", ""))
            total = int(m.group("total").replace(",", ""))
            ts = int(ev.get("timestamp") or time.time())
            if not buyer_name and buyer_uid and api_key:
                buyer_name = resolve_driver_name(buyer_uid, api_key)
            record_bazaar_sale(f"ev_{eid}_{ts}_{total}", buyer_name, buyer_uid, qty, total, ts)

    added_after = len(BAZAAR_BUYERS_LEDGER["seen_tx_ids"])
    if added_after > added_before:
        save_bazaar_ledger()
    return added_after - added_before


def parse_logs_for_bazaar_buyers(log_obj, api_key: str = "") -> int:
    """Extracts Bazaar sales from Torn `user?selections=log` payload (log type 1225 / category Bazaars)."""
    added_before = len(BAZAAR_BUYERS_LEDGER["seen_tx_ids"])
    logs_iter = []
    if isinstance(log_obj, dict):
        for lid, entry in log_obj.items():
            if isinstance(entry, dict):
                e_copy = dict(entry)
                e_copy.setdefault("id", lid)
                logs_iter.append(e_copy)
    elif isinstance(log_obj, list):
        logs_iter = [e for e in log_obj if isinstance(e, dict)]

    for entry in logs_iter:
        lid = str(entry.get("id") or entry.get("ID") or "")
        log_type = int(entry.get("log") or 0)
        title = str(entry.get("title") or "").lower()
        cat = str(entry.get("category") or "").lower()
        if log_type == 1225 or "bazaar sell" in title or "bazaar sale" in title or ("bazaar" in cat and "sell" in title):
            d = entry.get("data") or {}
            if isinstance(d, dict):
                buyer_uid = int(d.get("buyer") or d.get("user") or 0)
                qty = int(d.get("quantity") or d.get("amount") or 1)
                total = int(d.get("cost_total") or d.get("total_cost") or d.get("money") or d.get("cost") or 0)
                if total > 0:
                    buyer_name = resolve_driver_name(buyer_uid, api_key) if buyer_uid else "Buyer"
                    ts = int(entry.get("timestamp") or time.time())
                    record_bazaar_sale(f"log_{lid}_{ts}", buyer_name, buyer_uid, qty, total, ts)

    added_after = len(BAZAAR_BUYERS_LEDGER["seen_tx_ids"])
    if added_after > added_before:
        save_bazaar_ledger()
    return added_after - added_before


def get_empty_bazaar_data() -> dict:
    return {
        "is_open": False,
        "status_banner": "AWAITING BAZAAR SYNC",
        "total_skus": 0,
        "total_units": 0,
        "total_ask_value": 0,
        "total_market_value": 0,
        "overall_vs_mv_pct": 0.0,
        "lifetime_revenue": 0,
        "lifetime_sales": 0,
        "lifetime_customers": 0,
        "inventory": [],
        "top_buyers": get_top_buyers_list(6),
        "needs_bazaar_perm": False,
        "needs_events_perm": False,
        "debug": {},
        "updated_at": time.strftime("%H:%M:%S TCT", time.gmtime()),
    }


def enrich_inventory_item(raw_item: dict) -> dict:
    item_id = int(raw_item.get("ID") or raw_item.get("id") or 0)
    name = str(raw_item.get("name") or f"Item #{item_id}").strip()
    qty = int(raw_item.get("quantity") or raw_item.get("amount") or 0)
    price = int(raw_item.get("price") or raw_item.get("cost") or 0)
    market_price = int(raw_item.get("market_price") or raw_item.get("market_value") or raw_item.get("value") or price)
    if market_price <= 0:
        market_price = price

    diff_pct = ((price - market_price) / market_price * 100.0) if market_price > 0 else 0.0
    return {
        "id": item_id,
        "name": name,
        "type": str(raw_item.get("type") or ""),
        "quantity": qty,
        "price": price,
        "market_price": market_price,
        "total_ask": price * qty,
        "total_mv": market_price * qty,
        "diff_pct": diff_pct,
    }


def fetch_live_bazaar_data(api_key: str) -> dict:
    data = get_empty_bazaar_data()
    now_ts = int(time.time())
    debug_info = {"ts": now_ts, "key_present": bool(api_key)}

    if not api_key:
        return get_demo_bazaar_data()

    # 1. Fetch personalstats (works immediately with existing Custom Racing key!)
    try:
        ps_resp = fetch_json(f"https://api.torn.com/user/?selections=personalstats&key={api_key}", api_key)
        if "error" not in ps_resp:
            parsed_ps = extract_personalstats(ps_resp)
            data["lifetime_customers"] = parsed_ps.get("bazaar_customers", 0)
            data["lifetime_sales"] = parsed_ps.get("bazaar_sales", 0)
            data["lifetime_revenue"] = parsed_ps.get("bazaar_profit", 0)
            debug_info["personalstats_ok"] = {
                "bazaarcustomers": data["lifetime_customers"],
                "bazaarsales": data["lifetime_sales"],
                "bazaarprofit": data["lifetime_revenue"],
            }
        else:
            debug_info["personalstats_error"] = ps_resp["error"]
    except Exception as exc:
        debug_info["personalstats_exception"] = str(exc)

    # 2. Fetch live Bazaar inventory (`user/4498891?selections=bazaar` or `user/?selections=bazaar`)
    bazaar_raw_list = None
    bazaar_open_flag = None
    for b_url in (
        f"https://api.torn.com/user/{USER_ID}?selections=bazaar&timestamp={now_ts}&key={api_key}",
        f"https://api.torn.com/user/?selections=bazaar&timestamp={now_ts}&key={api_key}",
    ):
        try:
            b_resp = fetch_json(b_url, api_key)
            if "error" not in b_resp and "bazaar" in b_resp:
                bazaar_raw_list = b_resp.get("bazaar") or []
                if "bazaar_is_open" in b_resp:
                    bazaar_open_flag = bool(b_resp.get("bazaar_is_open"))
                debug_info["bazaar_ok_count"] = len(bazaar_raw_list)
                break
            elif "error" in b_resp:
                debug_info["bazaar_error"] = b_resp["error"]
                if int(b_resp["error"].get("code") or 0) == 16:
                    data["needs_bazaar_perm"] = True
        except Exception as exc:
            debug_info["bazaar_exception"] = str(exc)

    if isinstance(bazaar_raw_list, list):
        items = [enrich_inventory_item(it) for it in bazaar_raw_list if isinstance(it, dict) and int(it.get("quantity") or 0) > 0]
        items.sort(key=lambda x: (x["total_ask"], x["quantity"]), reverse=True)
        data["inventory"] = items
        data["total_skus"] = len(items)
        data["total_units"] = sum(x["quantity"] for x in items)
        data["total_ask_value"] = sum(x["total_ask"] for x in items)
        data["total_market_value"] = sum(x["total_mv"] for x in items)
        if data["total_market_value"] > 0:
            data["overall_vs_mv_pct"] = (
                (data["total_ask_value"] - data["total_market_value"]) / data["total_market_value"]
            ) * 100.0
        data["is_open"] = bool(bazaar_open_flag) if bazaar_open_flag is not None else (len(items) > 0)

    # 3. Fetch recent Events & Logs to update real-time Top Buyers
    for ev_url in (
        f"https://api.torn.com/user/?selections=events&key={api_key}",
        f"https://api.torn.com/v2/user/events?limit=100&key={api_key}",
    ):
        try:
            ev_resp = fetch_json(ev_url, api_key)
            if "error" not in ev_resp and "events" in ev_resp:
                new_ev = parse_events_for_bazaar_buyers(ev_resp["events"], api_key)
                debug_info["events_parsed_new_buyers"] = new_ev
                break
            elif "error" in ev_resp:
                debug_info["events_error"] = ev_resp["error"]
                if int(ev_resp["error"].get("code") or 0) == 16:
                    data["needs_events_perm"] = True
        except Exception as exc:
            debug_info["events_exception"] = str(exc)

    try:
        log_resp = fetch_json(f"https://api.torn.com/user/?selections=log&log=1225&key={api_key}", api_key)
        if "error" not in log_resp and "log" in log_resp:
            new_logs = parse_logs_for_bazaar_buyers(log_resp["log"], api_key)
            debug_info["logs_parsed_new_buyers"] = new_logs
            data["needs_events_perm"] = False
    except Exception:
        pass

    data["top_buyers"] = get_top_buyers_list(6)

    if data["needs_bazaar_perm"] and not data["inventory"]:
        data["status_banner"] = "KEY NEEDS 'BAZAAR' & 'EVENTS' ACCESS"
    elif data["inventory"]:
        sign = "+" if data["overall_vs_mv_pct"] > 0 else ""
        data["status_banner"] = (
            f"● OPEN • {data['total_skus']} SKUs ({data['total_units']:,} ITEMS) • "
            f"{sign}{data['overall_vs_mv_pct']:.1f}% MV"
        )
    else:
        data["status_banner"] = "○ BAZAAR CLOSED / RESTOCKING SHELVES"

    data["debug"] = debug_info
    data["updated_at"] = time.strftime("%H:%M:%S TCT", time.gmtime())
    return data


def parse_bazaar_push_payload(payload: dict, fallback_data: dict = None) -> dict:
    """Ingests live Bazaar inventory & buyer events pushed from the browser Tampermonkey script."""
    base = fallback_data or get_empty_bazaar_data()

    if isinstance(payload.get("inventory"), list):
        items = [enrich_inventory_item(it) for it in payload["inventory"] if isinstance(it, dict) and int(it.get("quantity") or 0) > 0]
        items.sort(key=lambda x: (x["total_ask"], x["quantity"]), reverse=True)
        base["inventory"] = items
        base["total_skus"] = len(items)
        base["total_units"] = sum(x["quantity"] for x in items)
        base["total_ask_value"] = sum(x["total_ask"] for x in items)
        base["total_market_value"] = sum(x["total_mv"] for x in items)
        base["overall_vs_mv_pct"] = (
            ((base["total_ask_value"] - base["total_market_value"]) / base["total_market_value"]) * 100.0
            if base["total_market_value"] > 0 else 0.0
        )
        base["is_open"] = bool(payload.get("is_open", len(items) > 0))
        base["needs_bazaar_perm"] = False

    if payload.get("events"):
        parse_events_for_bazaar_buyers(payload["events"])
        base["needs_events_perm"] = False

    if isinstance(payload.get("sales"), list):
        for s in payload["sales"]:
            if isinstance(s, dict):
                record_bazaar_sale(
                    str(s.get("id") or f"{s.get('buyer')}_{s.get('ts')}_{s.get('total')}"),
                    str(s.get("buyer") or "Buyer"),
                    int(s.get("uid") or 0),
                    int(s.get("qty") or 1),
                    int(s.get("total") or 0),
                    int(s.get("ts") or time.time()),
                )
        save_bazaar_ledger()
        base["needs_events_perm"] = False

    base["top_buyers"] = get_top_buyers_list(6)
    if base["inventory"]:
        sign = "+" if base["overall_vs_mv_pct"] > 0 else ""
        base["status_banner"] = (
            f"● OPEN • {base['total_skus']} SKUs ({base['total_units']:,} ITEMS) • "
            f"{sign}{base['overall_vs_mv_pct']:.1f}% MV"
        )
    else:
        base["status_banner"] = "○ BAZAAR CLOSED / RESTOCKING SHELVES"
    base["updated_at"] = time.strftime("%H:%M:%S TCT", time.gmtime())
    return base


def get_demo_bazaar_data() -> dict:
    demo_items = [
        enrich_inventory_item({"ID": 206, "name": "Xanax", "quantity": 24, "price": 828000, "market_price": 842000}),
        enrich_inventory_item({"ID": 367, "name": "Feathery Hotel Coupon", "quantity": 4, "price": 13150000, "market_price": 13390000}),
        enrich_inventory_item({"ID": 366, "name": "Erotic DVD", "quantity": 6, "price": 4650000, "market_price": 4725000}),
        enrich_inventory_item({"ID": 530, "name": "Can of Munster", "quantity": 18, "price": 1890000, "market_price": 1935000}),
        enrich_inventory_item({"ID": 258, "name": "Jaguar Plushie", "quantity": 45, "price": 17200, "market_price": 17500}),
        enrich_inventory_item({"ID": 260, "name": "Peony", "quantity": 30, "price": 68500, "market_price": 69800}),
    ]
    total_ask = sum(x["total_ask"] for x in demo_items)
    total_mv = sum(x["total_mv"] for x in demo_items)
    diff_pct = ((total_ask - total_mv) / total_mv) * 100.0
    return {
        "is_open": True,
        "status_banner": f"● OPEN • {len(demo_items)} SKUs (127 ITEMS) • {diff_pct:.1f}% MV",
        "total_skus": len(demo_items),
        "total_units": sum(x["quantity"] for x in demo_items),
        "total_ask_value": total_ask,
        "total_market_value": total_mv,
        "overall_vs_mv_pct": diff_pct,
        "lifetime_revenue": 482950000,
        "lifetime_sales": 3420,
        "lifetime_customers": 418,
        "inventory": demo_items,
        "top_buyers": [
            {"rank": 1, "name": "A-N-T-I-D-O-T-E", "uid": 4548760, "spent": 52600000, "items": 28, "tx_count": 5},
            {"rank": 2, "name": "Primian", "uid": 2910411, "spent": 34020000, "items": 18, "tx_count": 3},
            {"rank": 3, "name": "LordFiendish", "uid": 1984220, "spent": 19872000, "items": 24, "tx_count": 4},
            {"rank": 4, "name": "Ifiron", "uid": 3301920, "spent": 13950000, "items": 3, "tx_count": 2},
            {"rank": 5, "name": "Medanox", "uid": 3891044, "spent": 9936000, "items": 12, "tx_count": 2},
            {"rank": 6, "name": "HusbandKitt", "uid": 4102931, "spent": 4968000, "items": 6, "tx_count": 1},
        ],
        "needs_bazaar_perm": False,
        "needs_events_perm": False,
        "debug": {},
        "updated_at": time.strftime("%H:%M:%S TCT", time.gmtime()),
    }


def load_font(size: int, bold: bool = False):
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
    )
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def render_telemetry_banner(data: dict) -> bytes:
    width, height = 764, 260
    bg_dark = (11, 9, 8)
    card_bg = (20, 16, 15)
    gold = (212, 175, 55)
    crimson = (192, 57, 43)
    crimson_dark = (90, 24, 24)
    emerald = (46, 204, 113)
    ivory = (228, 218, 201)
    muted = (150, 132, 110)

    img = Image.new("RGB", (width, height), bg_dark)
    draw = ImageDraw.Draw(img)

    for y in range(0, height, 6):
        for x in range(0, width, 6):
            if (x + y) % 12 == 0:
                draw.rectangle([x, y, x + 2, y + 2], fill=(17, 14, 13))

    draw.rectangle([2, 2, width - 3, height - 3], outline=crimson_dark, width=2)
    draw.rectangle([6, 6, width - 7, height - 7], outline=(65, 50, 30), width=1)

    f_title = load_font(15, bold=True)
    f_sub = load_font(11, bold=True)
    f_body = load_font(12, bold=False)
    f_bold = load_font(11, bold=True)
    f_small = load_font(10, bold=False)
    f_big_stat = load_font(14, bold=True)

    # Top Header Bar
    draw.rectangle([10, 10, width - 11, 42], fill=(24, 15, 14), outline=crimson_dark)
    draw.text((20, 18), f"♦ {USER_NAME} [{USER_ID}] — LIVE TELEMETRY HUD", fill=gold, font=f_title)
    race = data["race"]
    status_color = emerald if ("LIVE" in race["status"] or "STAGING" in race["status"] or "WAITING" in race["status"]) else gold
    draw.text((width - 20, 19), str(race["status"])[:40], fill=status_color, font=f_bold, anchor="ra")

    # --- COLUMN 1: DRIVER DOSSIER ---
    draw.rectangle([10, 50, 240, 248], fill=card_bg, outline=(55, 38, 28))
    draw.text((22, 58), "DRIVER DOSSIER", fill=gold, font=f_sub)
    draw.line([20, 75, 230, 75], fill=crimson_dark, width=1)

    draw.text((22, 84), "Racing Class:", fill=muted, font=f_body)
    draw.text((228, 84), data["racing_class"], fill=gold, font=f_bold, anchor="ra")

    draw.text((22, 108), "Driver Skill:", fill=muted, font=f_body)
    draw.text((228, 107), f"{data['racing_skill']:.2f} / 100", fill=emerald, font=f_big_stat, anchor="ra")

    draw.text((22, 134), "Racing Points:", fill=muted, font=f_body)
    draw.text((228, 134), f"{data['racing_points']:,} pts", fill=ivory, font=f_bold, anchor="ra")

    draw.text((22, 158), "Races Won / Total:", fill=muted, font=f_body)
    draw.text((228, 158), f"{data['races_won']:,} / {data['races_entered']:,}", fill=ivory, font=f_bold, anchor="ra")

    draw.text((22, 182), "Podium / Win Rate:", fill=muted, font=f_body)
    draw.text((228, 182), f"{data['win_rate']:.1f}%", fill=gold, font=f_bold, anchor="ra")

    draw.text((22, 208), "Active Spec:", fill=muted, font=f_small)
    draw.text((228, 208), str(race["car_name"])[:20], fill=crimson, font=f_bold, anchor="ra")
    draw.text((22, 229), f"Synced: {data['updated_at']}", fill=(110, 95, 80), font=f_small)

    # --- COLUMN 2: 2D TRACK CIRCUIT MAP & SPEED/ACCEL TELEMETRY ---
    draw.rectangle([250, 50, 505, 248], fill=card_bg, outline=(55, 38, 28))
    t_name = str(race.get("track_name") or TORN_TRACKS.get(race["track_id"], {}).get("name", "Track"))
    t_len = float(race.get("track_len_mi") or get_track_length(t_name, 2.0))
    draw.text((262, 58), f"TRACK: {t_name.upper()[:18]}", fill=gold, font=f_sub)
    draw.text((495, 58), f"{t_len:.2f} mi", fill=muted, font=f_small, anchor="ra")
    draw.line([260, 75, 495, 75], fill=crimson_dark, width=1)

    spd = float(race.get("speed_mph", 0.0))
    acc = float(race.get("accel", 0.0))
    acc_color = emerald if acc >= 0.05 else (crimson if acc <= -0.05 else muted)
    acc_sign = "+" if acc >= 0 else ""
    draw.text((262, 80), f"SPD: {spd:.1f} MPH", fill=ivory, font=f_small)
    draw.text((495, 80), f"ACCEL: {acc_sign}{acc:.2f} mph/s", fill=acc_color, font=f_small, anchor="ra")

    map_box = (270, 98, 485, 206)
    poly = get_track_polygon(race["track_id"], t_name, map_box)
    closed_poly = poly + [poly[0]]
    draw.line(closed_poly, fill=(65, 30, 25), width=7, joint="curve")
    draw.line(closed_poly, fill=(140, 120, 95), width=3, joint="curve")

    sx, sy = poly[0]
    draw.rectangle([sx - 5, sy - 5, sx + 5, sy + 5], fill=ivory, outline=bg_dark)

    standings = race.get("standings", [])
    for idx, drv in enumerate(standings):
        if not drv.get("is_me"):
            r_prog = float(drv.get("progress", (race["progress"] - (idx + 1) * 0.09) % 1.0))
            rx, ry = interpolate_along_circuit(poly, r_prog)
            dot_col = (90, 30, 30) if "CRASH" in drv.get("status", "") else (120, 110, 100)
            draw.ellipse([rx - 4, ry - 4, rx + 4, ry + 4], fill=dot_col, outline=bg_dark)

    cx, cy = interpolate_along_circuit(poly, race["progress"])
    draw.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], fill=crimson, outline=gold, width=2)
    if cx > 400:
        draw.text((cx - 11, cy - 6), f"P{race['position']} DONYEE", fill=gold, font=f_small, anchor="ra")
    else:
        draw.text((cx + 11, cy - 6), f"P{race['position']} DONYEE", fill=gold, font=f_small)

    bar_x0, bar_y0, bar_x1, bar_y1 = 264, 222, 491, 236
    draw.rectangle([bar_x0, bar_y0, bar_x1, bar_y1], fill=(12, 10, 10), outline=(70, 50, 35))
    fill_w = int((bar_x1 - bar_x0) * max(0.04, min(1.0, race["current_lap"] / max(1, race["laps"]))))
    draw.rectangle([bar_x0, bar_y0, bar_x0 + fill_w, bar_y1], fill=crimson)
    if "STAGING" in race["status"] or "WAITING" in race["status"]:
        pct_txt = f"GRID STAGING • POS {race['position']}/{race['total_drivers']} • LAP 0/{race['laps']}"
    else:
        pct_txt = f"LAP {race['current_lap']}/{race['laps']} • CUR LAP: {int(race['progress'] * 100)}%"
    draw.text(((bar_x0 + bar_x1) // 2, bar_y0 + 2), pct_txt, fill=ivory, font=f_small, anchor="ma")

    # --- COLUMN 3: RACE POSITIONING / LEADERBOARD ---
    draw.rectangle([515, 50, 753, 248], fill=card_bg, outline=(55, 38, 28))
    draw.text((527, 58), "LIVE GRID / TELEMETRY", fill=gold, font=f_sub)
    draw.line([525, 75, 743, 75], fill=crimson_dark, width=1)

    y_row = 84
    for row in standings[:6]:
        is_me = row.get("is_me", False)
        if is_me:
            draw.rectangle([522, y_row - 2, 746, y_row + 20], fill=(55, 20, 18), outline=gold, width=1)
        pos_color = gold if is_me else muted
        name_color = gold if is_me else ivory
        stat_color = emerald if is_me else (crimson if "CRASH" in row["status"] else muted)

        draw.text((527, y_row + 3), f"P{row['pos']}", fill=pos_color, font=f_bold)
        draw.text((548, y_row + 3), str(row["label"])[:16], fill=name_color, font=f_small)
        draw.text((741, y_row + 3), str(row["status"])[:14], fill=stat_color, font=f_small, anchor="ra")
        y_row += 26

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_bazaar_banner(data: dict) -> bytes:
    """Renders the 764x260 Live Bazaar Inventory, Market Valuation & Top Buyers HUD."""
    width, height = 764, 260
    bg_dark = (11, 9, 8)
    card_bg = (20, 16, 15)
    gold = (212, 175, 55)
    crimson = (192, 57, 43)
    crimson_dark = (90, 24, 24)
    emerald = (46, 204, 113)
    ivory = (228, 218, 201)
    muted = (150, 132, 110)

    img = Image.new("RGB", (width, height), bg_dark)
    draw = ImageDraw.Draw(img)

    # Carbon-fiber background weave
    for y in range(0, height, 6):
        for x in range(0, width, 6):
            if (x + y) % 12 == 0:
                draw.rectangle([x, y, x + 2, y + 2], fill=(17, 14, 13))

    draw.rectangle([2, 2, width - 3, height - 3], outline=crimson_dark, width=2)
    draw.rectangle([6, 6, width - 7, height - 7], outline=(65, 50, 30), width=1)

    f_title = load_font(15, bold=True)
    f_sub = load_font(11, bold=True)
    f_body = load_font(11, bold=False)
    f_bold = load_font(11, bold=True)
    f_small = load_font(10, bold=False)
    f_tiny = load_font(9, bold=False)

    # Top Header Bar
    draw.rectangle([10, 10, width - 11, 42], fill=(24, 15, 14), outline=crimson_dark)
    draw.text((20, 18), f"♦ {USER_NAME} [{USER_ID}] — LIVE BAZAAR HUD", fill=gold, font=f_title)
    banner_str = str(data.get("status_banner") or "")
    banner_col = emerald if "OPEN" in banner_str else (crimson if "KEY NEEDS" in banner_str else gold)
    draw.text((width - 20, 19), banner_str[:42], fill=banner_col, font=f_bold, anchor="ra")

    # --- COLUMN 1: MERCHANT DOSSIER (x: 10..218) ---
    draw.rectangle([10, 50, 218, 248], fill=card_bg, outline=(55, 38, 28))
    draw.text((20, 58), "MERCHANT DOSSIER", fill=gold, font=f_sub)
    draw.line([18, 75, 210, 75], fill=crimson_dark, width=1)

    status_txt = "OPEN (LIVE)" if data.get("is_open") else "RESTOCKING"
    status_col = emerald if data.get("is_open") else gold
    draw.text((20, 84), "Status:", fill=muted, font=f_body)
    draw.text((208, 84), status_txt, fill=status_col, font=f_bold, anchor="ra")

    draw.text((20, 106), "Shelf Value:", fill=muted, font=f_body)
    draw.text((208, 106), format_money(data.get("total_ask_value", 0), compact=True), fill=ivory, font=f_bold, anchor="ra")

    draw.text((20, 128), "Market Value:", fill=muted, font=f_body)
    draw.text((208, 128), format_money(data.get("total_market_value", 0), compact=True), fill=muted, font=f_bold, anchor="ra")

    diff_pct = float(data.get("overall_vs_mv_pct") or 0.0)
    diff_sign = "+" if diff_pct > 0 else ""
    diff_col = emerald if diff_pct <= 0.0 else crimson
    draw.text((20, 150), "Avg vs. Market:", fill=muted, font=f_body)
    draw.text((208, 150), f"{diff_sign}{diff_pct:.1f}% MV", fill=diff_col, font=f_bold, anchor="ra")

    draw.text((20, 172), "Total Revenue:", fill=muted, font=f_body)
    draw.text((208, 172), format_money(data.get("lifetime_revenue", 0), compact=True), fill=gold, font=f_bold, anchor="ra")

    cust = int(data.get("lifetime_customers") or 0)
    sales = int(data.get("lifetime_sales") or 0)
    draw.text((20, 194), "Buyers / Sold:", fill=muted, font=f_body)
    draw.text((208, 194), f"{cust:,} / {sales:,}", fill=ivory, font=f_bold, anchor="ra")

    draw.text((20, 214), "Zero Tax • Direct Buy", fill=crimson, font=f_tiny)
    draw.text((20, 229), f"Synced: {data.get('updated_at', '')}", fill=(110, 95, 80), font=f_small)

    # --- COLUMN 2: LIVE INVENTORY & MARKET VALUE DELTA (x: 226..514) ---
    draw.rectangle([226, 50, 514, 248], fill=card_bg, outline=(55, 38, 28))
    draw.text((236, 58), "LIVE INVENTORY", fill=gold, font=f_sub)
    draw.text((438, 59), "ASK PRICE", fill=muted, font=f_tiny, anchor="ra")
    draw.text((504, 59), "VS MV", fill=muted, font=f_tiny, anchor="ra")
    draw.line([234, 75, 506, 75], fill=crimson_dark, width=1)

    inv = data.get("inventory") or []
    if inv:
        y_row = 83
        max_rows = 6
        for idx, item in enumerate(inv[:max_rows]):
            if idx % 2 == 0:
                draw.rectangle([232, y_row - 2, 508, y_row + 19], fill=(26, 20, 18))
            qty_str = f"{item['quantity']}x"
            name_str = str(item["name"])[:16]
            ask_str = format_money(item["price"], compact=(item["price"] >= 10_000_000))
            d_pct = float(item.get("diff_pct") or 0.0)
            if abs(d_pct) < 0.05:
                mv_str = "AT MV"
                mv_col = gold
            elif d_pct < 0:
                mv_str = f"{d_pct:.1f}%"
                mv_col = emerald
            else:
                mv_str = f"+{d_pct:.1f}%"
                mv_col = crimson

            draw.text((236, y_row + 2), qty_str, fill=gold, font=f_bold)
            draw.text((274, y_row + 2), name_str, fill=ivory, font=f_small)
            draw.text((438, y_row + 2), ask_str, fill=ivory, font=f_small, anchor="ra")
            draw.text((504, y_row + 2), mv_str, fill=mv_col, font=f_bold, anchor="ra")
            y_row += 25

        if len(inv) > max_rows:
            rem_skus = len(inv) - max_rows
            rem_units = sum(x["quantity"] for x in inv[max_rows:])
            draw.text(
                (370, 232),
                f"+ {rem_skus} more item types in stock ({rem_units:,} units)",
                fill=muted,
                font=f_tiny,
                anchor="ma",
            )
    else:
        if data.get("needs_bazaar_perm"):
            draw.text((370, 130), "API KEY NEEDS 'BAZAAR' ACCESS", fill=crimson, font=f_bold, anchor="ma")
            draw.text((370, 152), "Enable 'bazaar' on your Torn API key", fill=ivory, font=f_small, anchor="ma")
            draw.text((370, 170), "or open your Bazaar with Tampermonkey active", fill=muted, font=f_tiny, anchor="ma")
        else:
            draw.text((370, 138), "NO ITEMS CURRENTLY ON SHELF", fill=gold, font=f_bold, anchor="ma")
            draw.text((370, 160), "Restocking inventory — check back shortly", fill=muted, font=f_small, anchor="ma")

    # --- COLUMN 3: REAL-TIME TOP BUYERS LEADERBOARD (x: 522..753) ---
    draw.rectangle([522, 50, 753, 248], fill=card_bg, outline=(55, 38, 28))
    draw.text((532, 58), "TOP BUYERS (LIVE)", fill=gold, font=f_sub)
    draw.text((743, 59), "TOTAL SPENT", fill=muted, font=f_tiny, anchor="ra")
    draw.line([530, 75, 745, 75], fill=crimson_dark, width=1)

    buyers = data.get("top_buyers") or []
    if buyers:
        y_row = 83
        for idx, b in enumerate(buyers[:6]):
            is_top1 = (idx == 0)
            if is_top1:
                draw.rectangle([528, y_row - 2, 747, y_row + 20], fill=(55, 20, 18), outline=gold, width=1)
            rank_col = gold if is_top1 else muted
            name_col = gold if is_top1 else ivory
            spent_str = format_money(b["spent"], compact=(b["spent"] >= 1_000_000))
            items_str = f"({b['items']}x)"

            draw.text((533, y_row + 3), f"#{b['rank']}", fill=rank_col, font=f_bold)
            draw.text((556, y_row + 3), str(b["name"])[:12], fill=name_col, font=f_small)
            draw.text((662, y_row + 4), items_str[:7], fill=muted, font=f_tiny, anchor="ra")
            draw.text((743, y_row + 3), spent_str, fill=emerald, font=f_bold, anchor="ra")
            y_row += 26
    else:
        if data.get("needs_events_perm"):
            draw.text((637, 130), "NEEDS 'EVENTS/LOG' KEY", fill=crimson, font=f_bold, anchor="ma")
            draw.text((637, 152), "Add 'events' to your Torn API key", fill=ivory, font=f_tiny, anchor="ma")
            draw.text((637, 168), "to rank real-time Top Buyers", fill=muted, font=f_tiny, anchor="ma")
        else:
            draw.text((637, 138), "AWAITING FIRST SALE", fill=gold, font=f_bold, anchor="ma")
            draw.text((637, 160), "Buyers rank automatically on purchase", fill=muted, font=f_tiny, anchor="ma")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class TelemetryRequestHandler(BaseHTTPRequestHandler):
    cache_bytes = None
    cache_ts = 0.0
    last_fetched_data = None
    live_push_data = None
    live_push_ts = 0.0

    bazaar_cache_bytes = None
    bazaar_cache_ts = 0.0
    last_bazaar_data = None
    live_bazaar_push = None
    live_bazaar_push_ts = 0.0

    api_key = ""
    bazaar_api_key = ""

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    @classmethod
    def get_or_refresh_bytes(cls) -> bytes:
        now = time.time()
        if cls.live_push_data and (now - cls.live_push_ts) < LIVE_PUSH_TTL_SECONDS:
            cls.cache_bytes = render_telemetry_banner(cls.live_push_data)
        elif not cls.cache_bytes or (now - cls.cache_ts) > CACHE_TTL_SECONDS:
            data = fetch_live_torn_telemetry(cls.api_key) if cls.api_key else get_demo_telemetry()
            cls.last_fetched_data = data
            cls.cache_bytes = render_telemetry_banner(data)
            cls.cache_ts = now
        return cls.cache_bytes

    @classmethod
    def get_or_refresh_bazaar_bytes(cls) -> bytes:
        now = time.time()
        active_key = cls.bazaar_api_key or cls.api_key
        if cls.live_bazaar_push and (now - cls.live_bazaar_push_ts) < LIVE_PUSH_TTL_SECONDS:
            cls.bazaar_cache_bytes = render_bazaar_banner(cls.live_bazaar_push)
        elif not cls.bazaar_cache_bytes or (now - cls.bazaar_cache_ts) > CACHE_TTL_SECONDS:
            data = fetch_live_bazaar_data(active_key) if active_key else get_demo_bazaar_data()
            cls.last_bazaar_data = data
            cls.bazaar_cache_bytes = render_bazaar_banner(data)
            cls.bazaar_cache_ts = now
        return cls.bazaar_cache_bytes

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_HEAD(self):
        if self.path.startswith("/donyee_sig.png"):
            png_data = self.get_or_refresh_bytes()
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png_data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
        elif self.path.startswith("/donyee_bazaar.png"):
            png_data = self.get_or_refresh_bazaar_bytes()
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png_data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
        elif self.path in ("/", "/health", "/healthz", "/debug", "/debug-bazaar"):
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/telemetry"):
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                payload = json.loads(raw_body)
                base_stats = self.last_fetched_data or (
                    fetch_live_torn_telemetry(self.api_key) if self.api_key else get_empty_telemetry()
                )
                TelemetryRequestHandler.live_push_data = parse_torn_racing_telemetry_payload(
                    payload, fallback_stats=base_stats
                )
                TelemetryRequestHandler.live_push_ts = time.time()
                TelemetryRequestHandler.cache_bytes = render_telemetry_banner(
                    TelemetryRequestHandler.live_push_data
                )
                TelemetryRequestHandler.cache_ts = time.time()
                self.send_response(200)
                self._send_cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            except Exception as exc:
                self.send_response(400)
                self._send_cors()
                self.end_headers()
                self.wfile.write(f'{{"error":"{exc}"}}'.encode("utf-8"))
        elif self.path.startswith("/bazaar-telemetry"):
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                payload = json.loads(raw_body)
                active_key = self.bazaar_api_key or self.api_key
                base_bazaar = self.last_bazaar_data or (
                    fetch_live_bazaar_data(active_key) if active_key else get_empty_bazaar_data()
                )
                TelemetryRequestHandler.live_bazaar_push = parse_bazaar_push_payload(
                    payload, fallback_data=base_bazaar
                )
                TelemetryRequestHandler.live_bazaar_push_ts = time.time()
                TelemetryRequestHandler.bazaar_cache_bytes = render_bazaar_banner(
                    TelemetryRequestHandler.live_bazaar_push
                )
                TelemetryRequestHandler.bazaar_cache_ts = time.time()
                self.send_response(200)
                self._send_cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            except Exception as exc:
                self.send_response(400)
                self._send_cors()
                self.end_headers()
                self.wfile.write(f'{{"error":"{exc}"}}'.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path.startswith("/donyee_sig.png"):
            png_data = self.get_or_refresh_bytes()
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png_data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(png_data)
        elif self.path.startswith("/donyee_bazaar.png"):
            png_data = self.get_or_refresh_bazaar_bytes()
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png_data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(png_data)
        elif self.path.startswith("/debug-bazaar"):
            self.get_or_refresh_bazaar_bytes()
            debug_payload = {
                "live_bazaar_push_active": bool(
                    self.live_bazaar_push and (time.time() - self.live_bazaar_push_ts) < LIVE_PUSH_TTL_SECONDS
                ),
                "last_bazaar_data": self.last_bazaar_data,
                "ledger_buyers_count": len(BAZAAR_BUYERS_LEDGER.get("buyers", {})),
            }
            body = json.dumps(debug_payload, indent=2).encode("utf-8")
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/debug"):
            self.get_or_refresh_bytes()
            debug_payload = {
                "live_push_active": bool(
                    self.live_push_data and (time.time() - self.live_push_ts) < LIVE_PUSH_TTL_SECONDS
                ),
                "live_push_data": self.live_push_data,
                "last_fetched_data": self.last_fetched_data,
            }
            body = json.dumps(debug_payload, indent=2).encode("utf-8")
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/health", "/healthz"):
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Donyee Racing & Bazaar Telemetry Server v3.0 Online\n")
        else:
            self.send_response(404)
            self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="Donyee [4498891] Live Torn Racing & Bazaar Signature Server")
    parser.add_argument("--key", default=os.environ.get("TORN_API_KEY", ""), help="Torn API Key")
    parser.add_argument("--bazaar-key", default=os.environ.get("TORN_BAZAAR_API_KEY", ""), help="Optional separate Bazaar API Key")
    parser.add_argument("--telemetry-json", default="", help="Path to Torn Racing Telemetry Detailed JSON export")
    parser.add_argument("--demo", action="store_true", help="Render with sample telemetry")
    parser.add_argument("--bazaar", action="store_true", help="Render Bazaar PNG instead of Racing PNG in CLI mode")
    parser.add_argument("--out", default="donyee_live_telemetry.png", help="Output PNG path")
    parser.add_argument("--serve", action="store_true", help="Run live HTTP server for Torn <img src> hosting")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)), help="HTTP server port")
    args = parser.parse_args()

    if args.serve:
        TelemetryRequestHandler.api_key = args.key
        TelemetryRequestHandler.bazaar_api_key = args.bazaar_key or args.key
        server = HTTPServer(("0.0.0.0", args.port), TelemetryRequestHandler)
        print(f"Serving Racing HUD at http://0.0.0.0:{args.port}/donyee_sig.png", flush=True)
        print(f"Serving Bazaar HUD at http://0.0.0.0:{args.port}/donyee_bazaar.png", flush=True)
        server.serve_forever()
    else:
        if args.bazaar:
            b_key = args.bazaar_key or args.key
            data = get_demo_bazaar_data() if (args.demo or not b_key) else fetch_live_bazaar_data(b_key)
            png_bytes = render_bazaar_banner(data)
        else:
            if args.telemetry_json and os.path.exists(args.telemetry_json):
                with open(args.telemetry_json, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                data = parse_torn_racing_telemetry_payload(payload)
            elif args.demo or not args.key:
                data = get_demo_telemetry()
            else:
                data = fetch_live_torn_telemetry(args.key)
            png_bytes = render_telemetry_banner(data)

        with open(args.out, "wb") as f:
            f.write(png_bytes)
        print(f"Saved {len(png_bytes)} bytes to {args.out}")


if __name__ == "__main__":
    main()
