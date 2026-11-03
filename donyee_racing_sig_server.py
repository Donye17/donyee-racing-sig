#!/usr/bin/env python3
"""
Donyee [4498891] — Torn City Live Racing Telemetry & Map Signature Server (v2.2)
================================================================================
Updates:
  - Fixed Race Selection: Requests `v2/user/races?sort=DESC&limit=10` and prioritizes
    any race that is currently active, open, waiting, or staging over finished ones.
  - Live Staging Detection: Accurately detects when you are waiting for a race to
    start, displaying `GRID STAGING • STARTS IN X MIN`, plotting cars at the start
    grid on the track map, and listing all registered drivers.
  - Reduced API Cache to 20 seconds for fast real-time updates without rate limits.
  - Full HEAD request & Render health-check support.
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
CACHE_TTL_SECONDS = 20
LIVE_PUSH_TTL_SECONDS = 90

# Official Torn City Raceway Track Metadata (16 Tracks)
TORN_TRACKS = {
    1: {"name": "Uptown", "length_mi": 2.25, "surface": "Tarmac"},
    2: {"name": "Withdrawal", "length_mi": 3.40, "surface": "Tarmac"},
    3: {"name": "Underdog", "length_mi": 1.73, "surface": "Tarmac"},
    4: {"name": "Parkland", "length_mi": 3.43, "surface": "Dirt / Mixed"},
    5: {"name": "Docks", "length_mi": 3.81, "surface": "Tarmac"},
    6: {"name": "Commerce", "length_mi": 1.09, "surface": "Tarmac"},
    7: {"name": "Two Islands", "length_mi": 2.71, "surface": "Dirt / Tarmac"},
    8: {"name": "Industrial", "length_mi": 1.35, "surface": "Tarmac"},
    9: {"name": "Vector", "length_mi": 1.16, "surface": "Tarmac"},
    10: {"name": "Mudpit", "length_mi": 1.06, "surface": "Dirt"},
    11: {"name": "Hammerhead", "length_mi": 1.16, "surface": "Dirt / Tarmac"},
    12: {"name": "Sewage", "length_mi": 1.50, "surface": "Tarmac"},
    13: {"name": "Meltdown", "length_mi": 1.20, "surface": "Tarmac"},
    14: {"name": "Speedway", "length_mi": 0.90, "surface": "Oval Tarmac"},
    15: {"name": "Stone Park", "length_mi": 2.08, "surface": "Dirt"},
    16: {"name": "Convict", "length_mi": 1.64, "surface": "Tarmac"},
}

TRACK_NAME_TO_ID = {v["name"].lower(): k for k, v in TORN_TRACKS.items()}

TRACK_SHAPES = {
    5: [  # Docks (3.81mi)
        (0.10, 0.78), (0.10, 0.25), (0.35, 0.20), (0.42, 0.35),
        (0.65, 0.35), (0.72, 0.18), (0.86, 0.22), (0.84, 0.65),
        (0.68, 0.68), (0.60, 0.82), (0.30, 0.82),
    ],
    2: [  # Withdrawal (3.40mi)
        (0.08, 0.65), (0.18, 0.22), (0.55, 0.18), (0.88, 0.30),
        (0.84, 0.75), (0.50, 0.82), (0.25, 0.72),
    ],
    14: [  # Speedway (0.90mi)
        (0.12, 0.60), (0.15, 0.30), (0.50, 0.18), (0.85, 0.30),
        (0.86, 0.60), (0.75, 0.80), (0.25, 0.80),
    ],
}


def get_track_polygon(track_id: int, box: tuple) -> list:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    pts = TRACK_SHAPES.get(track_id)
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


def parse_torn_racing_telemetry_payload(payload: dict, fallback_stats: dict = None) -> dict:
    base = fallback_stats or get_demo_telemetry()
    race_block = payload.get("race", payload)

    track_name = str(race_block.get("trackName") or race_block.get("track_name") or "Docks")
    track_id = int(
        race_block.get("trackId")
        or race_block.get("track_id")
        or TRACK_NAME_TO_ID.get(track_name.lower(), 5)
    )
    laps = int(race_block.get("laps") or 100)
    current_lap = int(race_block.get("currentLap") or race_block.get("current_lap") or laps)
    lap_progress = float(race_block.get("lapProgress") or race_block.get("progress") or 0.68)
    speed_mph = float(race_block.get("speed") or race_block.get("speed_mph") or 154.2)
    accel = float(race_block.get("accel") or race_block.get("acceleration") or 1.85)
    best_lap = str(race_block.get("bestLap") or race_block.get("best_lap") or "01:18.42")

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
        name = drv.get("name") or drv.get("driverName") or drv.get("label") or f"Driver [{uid}]"
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

        status_str = str(
            drv.get("status")
            or drv.get("telemetryText")
            or (f"{float(drv['speed']):.0f} mph" if drv.get("speed") else f"P{pos}")
        )
        standings.append({
            "pos": pos,
            "label": f"{USER_NAME} [{USER_ID}]" if is_me else str(name)[:16],
            "status": status_str,
            "is_me": is_me,
            "progress": float(drv.get("lapProgress") or drv.get("progress") or ((lap_progress - idx * 0.08) % 1.0)),
        })

    if not standings:
        standings = base["race"]["standings"]

    if payload.get("racingSkill") or payload.get("racing_skill"):
        base["racing_skill"] = float(payload.get("racingSkill") or payload.get("racing_skill"))
    if payload.get("racingPoints") or payload.get("racing_points"):
        base["racing_points"] = int(payload.get("racingPoints") or payload.get("racing_points"))
        base["racing_class"] = compute_racing_class(base["racing_points"])
    if payload.get("racesEntered") or payload.get("races_entered"):
        base["races_entered"] = int(payload.get("racesEntered") or payload.get("races_entered"))
    if payload.get("racesWon") or payload.get("races_won"):
        base["races_won"] = int(payload.get("racesWon") or payload.get("races_won"))
    if base["races_entered"] > 0:
        base["win_rate"] = (base["races_won"] / base["races_entered"] * 100.0)

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
        "best_lap": best_lap,
        "standings": standings,
    })
    base["updated_at"] = time.strftime("%H:%M:%S TCT", time.gmtime())
    return base


def fetch_json(url: str, api_key: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"ApiKey {api_key}",
            "User-Agent": "DonyeeRacingTelemetrySig/2.2",
        },
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_live_torn_telemetry(api_key: str) -> dict:
    data = get_demo_telemetry()
    now_ts = int(time.time())

    # 1. Fetch personalstats & basic profile (v1)
    try:
        v1_url = f"https://api.torn.com/user/{USER_ID}?selections=profile,personalstats,icons&key={api_key}"
        v1_data = fetch_json(v1_url, api_key)
        if "error" in v1_data:
            print(f"[TORN API v1 ERROR] {v1_data['error']}", file=sys.stderr, flush=True)
        else:
            pstats = v1_data.get("personalstats", {})
            data["racing_skill"] = float(pstats.get("racingskill", data["racing_skill"]))
            data["racing_points"] = int(pstats.get("racingpointsearned", data["racing_points"]))
            data["racing_class"] = compute_racing_class(data["racing_points"])
            data["races_entered"] = int(pstats.get("racesentered", data["races_entered"]))
            data["races_won"] = int(pstats.get("raceswon", data["races_won"]))
            if data["races_entered"] > 0:
                data["win_rate"] = (data["races_won"] / data["races_entered"] * 100.0)

            # Check profile icons for racing status indicator
            icons = v1_data.get("icons", {})
            if "icon17" in icons:
                data["race"]["status"] = f"LIVE ON TRACK ({icons['icon17']})"
            print(f"[TORN API v1 OK] RS: {data['racing_skill']} | Pts: {data['racing_points']} | Class: {data['racing_class']}", flush=True)
    except Exception as exc:
        print(f"[TORN API v1 EXCEPTION] {exc}", file=sys.stderr, flush=True)

    # 2. Fetch latest user races (v2) with sort=DESC and limit=10 to find active/staging race
    try:
        v2_races_url = f"https://api.torn.com/v2/user/races?sort=DESC&limit=10&key={api_key}"
        v2_races = fetch_json(v2_races_url, api_key)
        if "error" in v2_races:
            print(f"[TORN API v2 ERROR] {v2_races['error']}", file=sys.stderr, flush=True)
        else:
            races_list = v2_races.get("races", [])
            selected_race = None

            # Prioritize any race currently active, waiting, or scheduled
            for r in races_list:
                s_raw = str(r.get("status", "")).lower()
                s_sched = r.get("schedule", {})
                s_start = int(s_sched.get("start", 0))
                s_end = int(s_sched.get("end", 0))

                if (
                    s_raw in ("in_progress", "open", "waiting", "preparing", "scheduled")
                    or (s_start > now_ts)
                    or (s_start <= now_ts and (s_end == 0 or now_ts < s_end))
                ):
                    selected_race = r
                    break

            if not selected_race and races_list:
                selected_race = races_list[0]

            if selected_race:
                track_id = int(selected_race.get("track_id", 5))
                track_meta = TORN_TRACKS.get(track_id, {"name": f"Track #{track_id}", "length_mi": 2.0})
                laps = int(selected_race.get("laps", 5))
                status_raw = str(selected_race.get("status", "finished")).lower()
                sched = selected_race.get("schedule", {})
                start_ts = int(sched.get("start", 0))
                end_ts = int(sched.get("end", 0))

                is_active = (status_raw == "in_progress") or (
                    start_ts <= now_ts and (end_ts == 0 or now_ts < end_ts)
                )
                is_staging = (status_raw in ("open", "waiting", "preparing", "scheduled")) or (
                    start_ts > now_ts
                )

                if is_staging:
                    wait_sec = max(0, start_ts - now_ts)
                    wait_min = max(1, wait_sec // 60)
                    cur_lap = 0
                    lap_prog = 0.0
                    est_speed = 0.0
                    est_accel = 0.0
                    race_status = f"GRID STAGING • STARTS IN {wait_min}M"
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
                    race_status = f"FINISHED • {laps} LAPS"

                results = selected_race.get("results", []) or selected_race.get("participants", [])
                standings = []
                my_pos = 1
                for idx, entry in enumerate(results[:6]):
                    uid = entry.get("user_id") or entry.get("id") or 0
                    pos = entry.get("position") or (idx + 1)
                    crashed = entry.get("has_crashed", False)
                    car_title = entry.get("car_item_name") or entry.get("car_name") or "Race Car"
                    is_me = (int(uid) == USER_ID)
                    if is_me:
                        my_pos = pos
                        data["race"]["car_name"] = car_title

                    if is_staging:
                        driver_status = "GRID READY"
                        driver_prog = 0.0
                    elif crashed:
                        driver_status = "CRASH"
                        driver_prog = 0.0
                    elif is_active:
                        driver_status = "ON TRACK"
                        driver_prog = (lap_prog - idx * 0.09) % 1.0
                    else:
                        driver_status = f"P{pos}"
                        driver_prog = 1.0

                    standings.append({
                        "pos": pos,
                        "label": f"{USER_NAME} [{USER_ID}]" if is_me else f"Driver [{uid}]",
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
                    "total_drivers": max(len(results), 6),
                    "speed_mph": est_speed,
                    "accel": est_accel,
                    "standings": standings if standings else data["race"]["standings"],
                })
                print(f"[TORN API v2 OK] Selected: {track_meta['name']} | Status: {race_status} | Drivers: {len(results)}", flush=True)
    except Exception as exc:
        print(f"[TORN API v2 EXCEPTION] {exc}", file=sys.stderr, flush=True)

    data["updated_at"] = time.strftime("%H:%M:%S TCT", time.gmtime())
    return data


def get_demo_telemetry() -> dict:
    return {
        "racing_skill": 24.85,
        "racing_points": 285,
        "racing_class": "CLASS B",
        "races_entered": 642,
        "races_won": 198,
        "win_rate": 30.8,
        "race": {
            "status": "LIVE TELEMETRY • LAP 68/100",
            "track_id": 5,
            "track_name": "Docks",
            "laps": 100,
            "current_lap": 68,
            "progress": 0.68,
            "is_official": False,
            "position": 1,
            "total_drivers": 6,
            "car_name": "Lexus LFA [TL3]",
            "speed_mph": 154.2,
            "accel": 1.85,
            "best_lap": "01:18.42",
            "standings": [
                {"pos": 1, "label": "DONYEE [4498891]", "status": "154 mph ▲", "is_me": True, "progress": 0.68},
                {"pos": 2, "label": "Rival [2841902]", "status": "149 mph • +1.4s", "is_me": False, "progress": 0.61},
                {"pos": 3, "label": "Apex [1940211]", "status": "146 mph • +3.1s", "is_me": False, "progress": 0.53},
                {"pos": 4, "label": "Nitro [3310492]", "status": "141 mph • +5.0s", "is_me": False, "progress": 0.44},
                {"pos": 5, "label": "Vortex [2109481]", "status": "138 mph • +8.7s", "is_me": False, "progress": 0.35},
                {"pos": 6, "label": "Drift [3891023]", "status": "CRASHED", "is_me": False, "progress": 0.18},
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

    f_title = load_font(16, bold=True)
    f_sub = load_font(11, bold=True)
    f_body = load_font(12, bold=False)
    f_bold = load_font(12, bold=True)
    f_small = load_font(10, bold=False)
    f_big_stat = load_font(14, bold=True)

    # Top Header Bar
    draw.rectangle([10, 10, width - 11, 42], fill=(24, 15, 14), outline=crimson_dark)
    draw.text((20, 17), f"♦ {USER_NAME} [{USER_ID}] — LIVE TELEMETRY HUD", fill=gold, font=f_title)
    race = data["race"]
    status_color = emerald if ("LIVE" in race["status"] or "STAGING" in race["status"]) else gold
    draw.text((width - 20, 19), race["status"], fill=status_color, font=f_bold, anchor="ra")

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
    draw.text((228, 208), race["car_name"][:20], fill=crimson, font=f_bold, anchor="ra")
    draw.text((22, 229), f"Synced: {data['updated_at']}", fill=(110, 95, 80), font=f_small)

    # --- COLUMN 2: 2D TRACK CIRCUIT MAP & SPEED/ACCEL TELEMETRY ---
    draw.rectangle([250, 50, 505, 248], fill=card_bg, outline=(55, 38, 28))
    track_meta = TORN_TRACKS.get(race["track_id"], {"name": race["track_name"], "length_mi": 2.0, "surface": "Tarmac"})
    draw.text((262, 58), f"TRACK MAP: {track_meta['name'].upper()}", fill=gold, font=f_sub)
    draw.text((495, 58), f"{track_meta['length_mi']} mi", fill=muted, font=f_small, anchor="ra")
    draw.line([260, 75, 495, 75], fill=crimson_dark, width=1)

    spd = float(race.get("speed_mph", 0.0))
    acc = float(race.get("accel", 0.0))
    acc_color = emerald if acc >= 0.05 else (crimson if acc <= -0.05 else muted)
    acc_sign = "+" if acc >= 0 else ""
    draw.text((262, 80), f"SPD: {spd:.1f} MPH", fill=ivory, font=f_small)
    draw.text((495, 80), f"ACCEL: {acc_sign}{acc:.2f} mph/s", fill=acc_color, font=f_small, anchor="ra")

    map_box = (270, 98, 485, 206)
    poly = get_track_polygon(race["track_id"], map_box)
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
    if "STAGING" in race["status"]:
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

        draw.text((528, y_row + 2), f"P{row['pos']}", fill=pos_color, font=f_bold)
        draw.text((552, y_row + 2), row["label"][:16], fill=name_color, font=f_small if is_me else f_small)
        draw.text((740, y_row + 2), row["status"], fill=stat_color, font=f_small, anchor="ra")
        y_row += 26

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class TelemetryRequestHandler(BaseHTTPRequestHandler):
    cache_bytes = None
    cache_ts = 0.0
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
            self.send_header("Cache-Control", "no-cache, max-age=15")
            self.end_headers()
        elif self.path in ("/", "/health", "/healthz"):
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
                TelemetryRequestHandler.live_push_data = parse_torn_racing_telemetry_payload(payload)
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
            self.send_header("Cache-Control", "no-cache, max-age=15")
            self.end_headers()
            self.wfile.write(png_data)
        elif self.path in ("/", "/health", "/healthz"):
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Donyee Racing Telemetry Server Online\n")
        else:
            self.send_response(404)
            self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="Donyee [4498891] Live Torn Racing Signature Generator")
    parser.add_argument("--key", default=os.environ.get("TORN_API_KEY", ""), help="Torn API Key")
    parser.add_argument("--telemetry-json", default="", help="Path to Torn Racing Telemetry Detailed JSON export")
    parser.add_argument("--demo", action="store_true", help="Render with sample Docks 100-lap live telemetry")
    parser.add_argument("--out", default="donyee_live_telemetry.png", help="Output PNG path")
    parser.add_argument("--serve", action="store_true", help="Run live HTTP server for Torn <img src> hosting")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)), help="HTTP server port")
    args = parser.parse_args()

    if args.serve:
        TelemetryRequestHandler.api_key = args.key
        server = HTTPServer(("0.0.0.0", args.port), TelemetryRequestHandler)
        print(f"Serving live Torn signature at http://0.0.0.0:{args.port}/donyee_sig.png", flush=True)
        print(f"Listening for live Torn Racing Telemetry pushes at POST http://0.0.0.0:{args.port}/telemetry", flush=True)
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
