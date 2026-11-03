#!/usr/bin/env python3
"""
Donyee [4498891] — Torn City Live Racing Telemetry & Map Signature Server (v2.4)
================================================================================
What's Fixed in v2.4:
  1. Fixed Oldest-Race Bug (`Papani Colé` Class E race): `/v2/user/races` defaults to
     oldest-first (`ASC`) when `sort=DESC` is omitted. v2.4 queries `sort=DESC&limit=20`
     AND `from=<last_7_days>` so it always grabs your newest/current race.
  2. Live Track Name Lookup (`TRACK #19` fixed): Automatically fetches and caches the
     official track list from `GET /v2/racing/tracks` so every track ID (including
     IDs 17–24 like Track #19) displays its real Torn track name and distance.
  3. Real Rival Racer Names (`Driver [4548760]` fixed): Resolves competitor user IDs
     via Torn's public `/user/{id}?selections=basic` endpoint (cached in memory) so
     real player names appear on the leaderboard instead of `Driver [ID]`.
  4. Fixed Column 3 Text Overlap: Formats finish times concisely (`3:07.4s` / `WAITING`
     / `ON TRACK`) so status text never collides with `DONYEE [4498891]`.
  5. Live Lobby / Waiting Detection: Queries `/v2/racing/{raceId}/race` for full lobby
     participant details and supports instant browser pushes via `POST /telemetry`.
"""

import argparse
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from PIL import Image, ImageDraw, ImageFont

USER_ID = 4498891
USER_NAME = "DONYEE"
CACHE_TTL_SECONDS = 15
LIVE_PUSH_TTL_SECONDS = 90

# Fallback Track Table (auto-updated at runtime from GET /v2/racing/tracks)
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
    17: {"name": "Speedway", "length_mi": 0.90},
    18: {"name": "Stone Park", "length_mi": 2.08},
    19: {"name": "Convict", "length_mi": 1.64},
    20: {"name": "Uptown", "length_mi": 2.25},
    21: {"name": "Withdrawal", "length_mi": 3.40},
    22: {"name": "Underdog", "length_mi": 1.73},
    23: {"name": "Parkland", "length_mi": 3.43},
    24: {"name": "Docks", "length_mi": 3.81},
}

TRACKS_LOADED_FROM_API = False
DRIVER_NAME_CACHE = {USER_ID: USER_NAME}
CAR_NAME_CACHE = {}

TRACK_SHAPES = {
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


def get_track_polygon(track_id: int, track_name: str, box: tuple) -> list:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    pts = TRACK_SHAPES.get(str(track_name).lower())
    if not pts:
        pts = []
        num_vertices = 10
        for i in range(num_vertices):
            angle = (2 * math.pi * i) / num_vertices
            radius_x = 0.36 + 0.08 * math.sin(i * 2.0 + track_id)
            radius_y = 0.32 + 0.09 * math.cos(i * 3.0 + track_id)
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
    """Formats seconds (e.g. 187.4) into compact m:ss.s or ss.ss."""
    try:
        sec = float(seconds_val)
        if sec <= 0:
            return ""
        if sec >= 60:
            mins = int(sec // 60)
            rem = sec - (mins * 60)
            return f"{mins}:{rem:04.1f}s"
        return f"{sec:.1f}s"
    except Exception:
        return ""


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
            "track_id": 5,
            "track_name": "Docks",
            "laps": 0,
            "current_lap": 0,
            "progress": 0.0,
            "is_official": True,
            "position": 1,
            "total_drivers": 1,
            "car_name": "Active Racer",
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
    """Fetches official Torn track IDs/names and car names from /v2/racing/tracks & /v2/racing/cars."""
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
                    tlen = float(t.get("length") or t.get("distance") or TORN_TRACKS.get(tid, {}).get("length_mi", 2.0))
                    TORN_TRACKS[tid] = {"name": tname, "length_mi": tlen}
        elif isinstance(tracks_data, dict):
            for k, t in tracks_data.items():
                if str(k).isdigit() and isinstance(t, dict):
                    tid = int(k)
                    tname = str(t.get("title") or t.get("name") or f"Track #{tid}")
                    tlen = float(t.get("length") or t.get("distance") or 2.0)
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
    """Resolves a Torn user ID to their player name via public /user/{uid}?selections=basic."""
    if not uid or uid <= 0:
        return "Unknown"
    if uid in DRIVER_NAME_CACHE:
        return DRIVER_NAME_CACHE[uid]
    if not api_key:
        return f"Racer [{uid}]"
    try:
        b_resp = fetch_json(f"https://api.torn.com/user/{uid}?selections=basic&key={api_key}", api_key)
        name = b_resp.get("name") or (b_resp.get("profile", {}) or {}).get("name")
        if name:
            DRIVER_NAME_CACHE[uid] = str(name)
            return str(name)
    except Exception:
        pass
    return f"Racer"


def extract_personalstats(raw: dict) -> dict:
    out = {}
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
    return out


def fetch_live_torn_telemetry(api_key: str) -> dict:
    data = get_empty_telemetry()
    now_ts = int(time.time())
    debug_info = {"ts": now_ts, "key_present": bool(api_key)}

    # Load official track and car dictionaries from Torn API v2
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

    # 2. Fetch newest user races using BOTH `sort=DESC` and `from=<last_14_days>`
    #    so we NEVER get stuck on old Class E races from months ago!
    races_by_id = {}
    for url_variant in (
        f"https://api.torn.com/v2/user/races?sort=DESC&limit=20&key={api_key}",
        f"https://api.torn.com/v2/user/races?from={now_ts - 86400 * 14}&limit=50&key={api_key}",
    ):
        try:
            resp = fetch_json(url_variant, api_key)
            if "races" in resp and isinstance(resp["races"], list):
                for r in resp["races"]:
                    if isinstance(r, dict) and r.get("id"):
                        races_by_id[int(r["id"])] = r
        except Exception as exc:
            debug_info["races_fetch_warn"] = str(exc)

    races_list = sorted(
        races_by_id.values(),
        key=lambda r: (int((r.get("schedule") or {}).get("start") or 0), int(r.get("id") or 0)),
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

        # Also query /v2/racing/{raceId}/race for full lobby/participant details if available
        race_id = int(selected_race.get("id") or 0)
        if race_id > 0:
            try:
                detail_resp = fetch_json(f"https://api.torn.com/v2/racing/{race_id}/race?key={api_key}", api_key)
                if "race" in detail_resp and isinstance(detail_resp["race"], dict):
                    selected_race.update(detail_resp["race"])
            except Exception:
                pass

        track_id = int(selected_race.get("track_id") or 5)
        track_meta = TORN_TRACKS.get(track_id, {"name": f"Track #{track_id}", "length_mi": 2.0})
        laps = int(selected_race.get("laps") or 5)
        status_raw = str(selected_race.get("status") or "finished").lower()
        sched = selected_race.get("schedule") or {}
        start_ts = int(sched.get("start") or 0)
        end_ts = int(sched.get("end") or 0)

        debug_info["selected_race"] = {
            "id": race_id,
            "track_id": track_id,
            "track_name": track_meta["name"],
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
                wait_min = max(1, (start_ts - now_ts) // 60)
                race_status = f"WAITING • STARTS IN {wait_min}M ({track_meta['name'].upper()})"
            else:
                race_status = f"WAITING IN LOBBY • {track_meta['name'].upper()} ({laps}L)"
            cur_lap = 0
            lap_prog = 0.0
            est_speed = 0.0
            est_accel = 0.0
        elif is_active:
            est_duration = max(120, int(laps * track_meta["length_mi"] * 28))
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
            race_status = f"LAST RACE: {track_meta['name'].upper()} • {laps}L"

        results = selected_race.get("results") or selected_race.get("participants") or []
        if isinstance(results, list):
            results_sorted = sorted(
                results,
                key=lambda x: int(x.get("position") or 99) if isinstance(x, dict) else 99,
            )
        else:
            results_sorted = []

        # Make sure Donyee is always visible in the top 6 rows even in large fields
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
            "track_name": track_meta["name"],
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

    track_name = str(race_block.get("trackName") or race_block.get("track_name") or base["race"]["track_name"])
    track_id = int(
        race_block.get("trackId")
        or race_block.get("track_id")
        or next((k for k, v in TORN_TRACKS.items() if v["name"].lower() == track_name.lower()), base["race"]["track_id"])
    )
    laps = int(race_block.get("laps") or base["race"]["laps"] or 100)
    current_lap = int(race_block.get("currentLap") or race_block.get("current_lap") or 0)
    lap_progress = float(race_block.get("lapProgress") or race_block.get("progress") or 0.0)
    speed_mph = float(race_block.get("speed") or race_block.get("speed_mph") or 0.0)
    accel = float(race_block.get("accel") or race_block.get("acceleration") or 0.0)

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
        name = drv.get("name") or drv.get("driverName") or drv.get("label") or f"Driver #{idx + 1}"
        pos = int(drv.get("position") or drv.get("pos") or (idx + 1))
        is_me = bool(drv.get("is_me") or int(uid or 0) == USER_ID or "donyee" in str(name).lower())
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

        status_str = str(drv.get("status") or drv.get("telemetryText") or f"P{pos}")
        standings.append({
            "pos": pos,
            "label": f"{USER_NAME} [{USER_ID}]" if is_me else str(name)[:15],
            "status": status_str[:14],
            "is_me": is_me,
            "progress": float(drv.get("lapProgress") or drv.get("progress") or ((lap_progress - idx * 0.08) % 1.0)),
        })

    if payload.get("racingSkill") or payload.get("racing_skill"):
        base["racing_skill"] = float(payload.get("racingSkill") or payload.get("racing_skill"))
    if payload.get("racingPoints") or payload.get("racing_points"):
        base["racing_points"] = int(payload.get("racingPoints") or payload.get("racing_points"))
        base["racing_class"] = compute_racing_class(base["racing_points"])

    base["race"].update({
        "status": str(race_block.get("status") or f"LIVE TELEMETRY • LAP {current_lap}/{laps}"),
        "track_id": track_id,
        "track_name": TORN_TRACKS.get(track_id, {"name": track_name})["name"],
        "laps": laps,
        "current_lap": current_lap,
        "progress": lap_progress,
        "position": my_pos,
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
            "status": "WAITING • STARTS IN 4M (CONVICT)",
            "track_id": 19,
            "track_name": "Convict",
            "laps": 100,
            "current_lap": 0,
            "progress": 0.0,
            "is_official": False,
            "position": 1,
            "total_drivers": 2,
            "car_name": "Papani Colé",
            "speed_mph": 0.0,
            "accel": 0.0,
            "best_lap": "01:18.42",
            "standings": [
                {"pos": 1, "label": "NightHawk", "status": "P1 • 3:04.6s", "is_me": False, "progress": 0.0},
                {"pos": 2, "label": "DONYEE [4498891]", "status": "P2 • 3:07.4s", "is_me": True, "progress": 0.0},
            ],
        },
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
    draw.text((width - 20, 19), str(race["status"])[:38], fill=status_color, font=f_bold, anchor="ra")

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
    track_meta = TORN_TRACKS.get(race["track_id"], {"name": race["track_name"], "length_mi": 2.0})
    t_display_name = str(race.get("track_name") or track_meta["name"]).upper()
    draw.text((262, 58), f"TRACK: {t_display_name[:18]}", fill=gold, font=f_sub)
    draw.text((495, 58), f"{track_meta['length_mi']:.2f} mi", fill=muted, font=f_small, anchor="ra")
    draw.line([260, 75, 495, 75], fill=crimson_dark, width=1)

    spd = float(race.get("speed_mph", 0.0))
    acc = float(race.get("accel", 0.0))
    acc_color = emerald if acc >= 0.05 else (crimson if acc <= -0.05 else muted)
    acc_sign = "+" if acc >= 0 else ""
    draw.text((262, 80), f"SPD: {spd:.1f} MPH", fill=ivory, font=f_small)
    draw.text((495, 80), f"ACCEL: {acc_sign}{acc:.2f} mph/s", fill=acc_color, font=f_small, anchor="ra")

    map_box = (270, 98, 485, 206)
    poly = get_track_polygon(race["track_id"], t_display_name, map_box)
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
        pct_txt = "GRID STAGING • CARS AT START LINE"
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


class TelemetryRequestHandler(BaseHTTPRequestHandler):
    cache_bytes = None
    cache_ts = 0.0
    last_fetched_data = None
    live_push_data = None
    live_push_ts = 0.0
    api_key = ""

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
        elif self.path in ("/", "/health", "/healthz", "/debug"):
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
        elif self.path.startswith("/debug"):
            self.get_or_refresh_bytes()
            debug_payload = {
                "live_push_active": bool(
                    self.live_push_data and (time.time() - self.live_push_ts) < LIVE_PUSH_TTL_SECONDS
                ),
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
            self.wfile.write(b"Donyee Racing Telemetry Server v2.4 Online\n")
        else:
            self.send_response(404)
            self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="Donyee [4498891] Live Torn Racing Signature Generator")
    parser.add_argument("--key", default=os.environ.get("TORN_API_KEY", ""), help="Torn API Key")
    parser.add_argument("--telemetry-json", default="", help="Path to Torn Racing Telemetry Detailed JSON export")
    parser.add_argument("--demo", action="store_true", help="Render with sample telemetry")
    parser.add_argument("--out", default="donyee_live_telemetry.png", help="Output PNG path")
    parser.add_argument("--serve", action="store_true", help="Run live HTTP server for Torn <img src> hosting")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)), help="HTTP server port")
    args = parser.parse_args()

    if args.serve:
        TelemetryRequestHandler.api_key = args.key
        server = HTTPServer(("0.0.0.0", args.port), TelemetryRequestHandler)
        print(f"Serving live Torn signature at http://0.0.0.0:{args.port}/donyee_sig.png", flush=True)
        print(f"Diagnostics available at http://0.0.0.0:{args.port}/debug", flush=True)
        server.serve_forever()
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
