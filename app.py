from __future__ import annotations
import os, math, traceback, threading
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify
from timezonefinder import TimezoneFinder
import swisseph as swe

from fetch_ephe import ensure_ephe

EPHE_PATH = os.environ.get("EPHE_PATH", "/app/ephe")

app = Flask(__name__)

# ------------ INIT ------------
READY = False
INIT_ERROR: Optional[str] = None
USE_MOS: bool = False  # если не найдём *.se1, падаем в Moshier

def _bg_init():
    global READY, INIT_ERROR, USE_MOS
    try:
        print("[app] init: ensure_ephe() starting...", flush=True)
        ensure_ephe()  # НИЧЕГО НЕ СКАЧИВАЕТ, только проверяет/копирует из ./ephe
        swe.set_ephe_path(EPHE_PATH)
        # если в EPHE_PATH нет *.se1 — работаем через Moshier (но сервис поднимется)
        has_se1 = any(fn.endswith(".se1") for fn in os.listdir(EPHE_PATH)) if os.path.isdir(EPHE_PATH) else False
        USE_MOS = not has_se1
        print(f"[app] Swiss Ephemeris path = {EPHE_PATH}; USE_MOS={USE_MOS}", flush=True)
        READY = True
        print("[app] init: READY", flush=True)
    except Exception as e:
        INIT_ERROR = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        print("[app] init ERROR:", INIT_ERROR, flush=True)

threading.Thread(target=_bg_init, daemon=True).start()

# ------------ TZ utils ------------
try:
    from zoneinfo import ZoneInfo  # py>=3.9
    _HAS_ZONEINFO = True
except Exception:
    from pytz import timezone as PytzTZ
    _HAS_ZONEINFO = False

def _parse_fixed_offset(s: str) -> Optional[int]:
    s = s.strip()
    if not s or s[0] not in "+-": return None
    if ":" not in s: return None
    sign = 1 if s[0] == "+" else -1
    hh, mm = s[1:].split(":")
    return sign * (int(hh) * 60 + int(mm))

def get_tz(tz_name_or_offset: str):
    offs = _parse_fixed_offset(tz_name_or_offset)
    if offs is not None:
        return ("FIXED_OFFSET", offs)
    return ZoneInfo(tz_name_or_offset) if _HAS_ZONEINFO else PytzTZ(tz_name_or_offset)

tf = TimezoneFinder()
def guess_iana_tz(lat: float, lon: float) -> str | None:
    try: return tf.timezone_at(lat=lat, lng=lon)
    except Exception: return None

def to_julday_utc(date_str: str, time_str: str, tz_obj, lat: float, lon: float) -> float:
    yyyy, mm, dd = [int(x) for x in date_str.split("-")]
    hh, mi = [int(x) for x in time_str.split(":")]
    naive = datetime(yyyy, mm, dd, hh, mi)
    if isinstance(tz_obj, tuple) and tz_obj and tz_obj[0] == "FIXED_OFFSET":
        utc_dt = naive - timedelta(minutes=tz_obj[1])
    else:
        if _HAS_ZONEINFO: local_dt = naive.replace(tzinfo=tz_obj)
        else:             local_dt = tz_obj.localize(naive)
        utc_dt = local_dt.astimezone(timezone.utc).replace(tzinfo=None)
    y, m, d = utc_dt.year, utc_dt.month, utc_dt.day
    h = utc_dt.hour + utc_dt.minute/60.0 + utc_dt.second/3600.0
    return swe.julday(y, m, d, h, swe.GREG_CAL)

# ------------ helpers ------------
SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]

def norm360(x: float) -> float: return x % 360.0
def angle_diff(a: float, b: float) -> float:
    d = abs(norm360(a) - norm360(b)) % 360.0
    return d if d <= 180.0 else 360.0 - d
def sign_name(lon: float) -> str: return SIGNS[int(norm360(lon)//30)]
def dms(x: float) -> Dict[str,int]:
    x = norm360(x); deg = int(x); m = (x-deg)*60; minute=int(m); sec=int(round((m-minute)*60))
    if sec==60: sec=0; minute+=1
    if minute==60: minute=0; deg+=1
    return {"deg":deg,"min":minute,"sec":sec}

# ------------ bodies & aspects ------------
def _flags() -> int:
    base = swe.FLG_SPEED
    return base | (swe.FLG_MOSEPH if USE_MOS else swe.FLG_SWIEPH)

# name, code, kind, orb
BODY_REGISTRY: List[Tuple[str, int, str, float]] = [
    ("Sun",     swe.SUN,     "planet", 8.0),
    ("Moon",    swe.MOON,    "planet", 8.0),
    ("Mercury", swe.MERCURY, "planet", 6.0),
    ("Venus",   swe.VENUS,   "planet", 6.0),
    ("Mars",    swe.MARS,    "planet", 6.0),
    ("Jupiter", swe.JUPITER, "planet", 6.0),
    ("Saturn",  swe.SATURN,  "planet", 6.0),
    ("Uranus",  swe.URANUS,  "planet", 5.0),
    ("Neptune", swe.NEPTUNE, "planet", 5.0),
    ("Pluto",   swe.PLUTO,   "planet", 5.0),
    # астероиды / точки
    ("Ceres",   swe.CERES,     "asteroid", 3.0),
    ("Pallas",  swe.PALLAS,    "asteroid", 3.0),
    ("Juno",    swe.JUNO,      "asteroid", 3.0),
    ("Vesta",   swe.VESTA,     "asteroid", 3.0),
    ("Chiron",  swe.CHIRON,    "asteroid", 3.0),
    ("Node",    swe.TRUE_NODE, "point",    3.0),
    ("Lilith",  swe.MEAN_APOG, "point",    3.0),
]

ASPECTS: List[Tuple[str, float, float]] = [
    ("Conjunction", 0.0,   8.0),
    ("Sextile",     60.0,  4.0),
    ("Square",      90.0,  6.0),
    ("Trine",       120.0, 6.0),
    ("Opposition",  180.0, 8.0),
    ("Quincunx",    150.0, 3.0),
]

def calc_bodies(jd_ut: float, include: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    include_set = set([n.lower() for n in include]) if include else None
    out: List[Dict[str, Any]] = []
    flags = _flags()
    for name, code, kind, orb_body in BODY_REGISTRY:
        if include_set and name.lower() not in include_set: continue
        pos, ret = swe.calc_ut(jd_ut, code, flags)
        lon, lat, dist, lon_speed = pos[0], pos[1], pos[2], pos[3]
        out.append({
            "name": name, "kind": kind, "lon": lon, "lat": lat, "dist": dist,
            "speed": lon_speed, "retrograde": lon_speed < 0,
            "sign": sign_name(lon), "dms": dms(lon), "orb_body": orb_body
        })
    return out

def calc_houses(jd_ut: float, lat: float, lon: float, hsys: str="P") -> Dict[str, Any]:
    cusps, ascmc = swe.houses(jd_ut, lat, lon, hsys.encode("ascii"))
    return {"system": hsys, "cusps": {str(i+1): cusps[i] for i in range(12)},
            "angles": {"ASC": ascmc[0], "MC": ascmc[1], "ARMC": ascmc[2], "Vertex": ascmc[3]}}

def _sep_to_angle(l1: float, l2: float, angle: float) -> float:
    return abs(angle_diff(l1, l2) - angle)

def _is_applying(b1: Dict[str,Any], b2: Dict[str,Any], angle: float) -> bool:
    dt = 0.01  # ~14.4 мин
    c0 = _sep_to_angle(b1["lon"], b2["lon"], angle)
    l1n = norm360(b1["lon"] + b1["speed"] * dt)
    l2n = norm360(b2["lon"] + b2["speed"] * dt)
    c1 = _sep_to_angle(l1n, l2n, angle)
    return c1 < c0

def calc_aspects(bodies: List[Dict[str,Any]], aspects=ASPECTS) -> List[Dict[str,Any]]:
    res: List[Dict[str,Any]] = []
    n = len(bodies)
    for i in range(n):
        for j in range(i+1, n):
            A, B = bodies[i], bodies[j]
            for asp_name, asp_angle, asp_orb in aspects:
                orb_allowed = min(asp_orb, A["orb_body"], B["orb_body"])
                diff = _sep_to_angle(A["lon"], B["lon"], asp_angle)
                if diff <= orb_allowed:
                    res.append({
                        "a": A["name"], "b": B["name"], "type": asp_name, "angle": asp_angle,
                        "orb_allowed": orb_allowed, "delta": diff,
                        "applying": _is_applying(A, B, asp_angle),
                        "exact": abs(diff) < 1e-6
                    })
    res.sort(key=lambda x: (x["delta"], x["angle"]))
    return res

# ------------ HTTP ------------
@app.get("/healthz")
def healthz(): return ("ok", 200)

@app.get("/status")
def status():
    mode = "Moshier" if USE_MOS else "SwissEphemeris"
    return jsonify({"ready": READY, "error": INIT_ERROR, "ephe_path": EPHE_PATH, "mode": mode})

@app.get("/")
def root():
    return jsonify({"name": "ИИ-Астролог API", "version": "1.2", "ready": READY})

@app.post("/calc")
def calc():
    if not READY:
        return jsonify({"error": "Ephemeris are not ready yet. Try again shortly.",
                        "status": {"ready": READY, "error": INIT_ERROR}}), 503
    try:
        data = request.get_json(force=True) or {}
        date_str = data["date"]; time_str = data["time"]
        lat = float(data["lat"]); lon = float(data["lon"])
        hsys = (data.get("hsys") or "P").strip()[:1]
        tz_in = data.get("tz"); guess_tz = data.get("guess_tz", True)
        include_bodies = data.get("bodies")

        if tz_in:
            tz_obj = get_tz(tz_in)
        else:
            if guess_tz:
                tzname = guess_iana_tz(lat, lon)
                if not tzname: return jsonify({"error": "Cannot guess timezone, pass 'tz'."}), 400
                tz_obj = get_tz(tzname); tz_in = tzname
            else:
                return jsonify({"error": "Missing 'tz'."}), 400

        jd_ut = to_julday_utc(date_str, time_str, tz_obj, lat, lon)
        houses = calc_houses(jd_ut, lat, lon, hsys)
        bodies = calc_bodies(jd_ut, include=include_bodies)
        aspects = calc_aspects(bodies)

        mode = "Moshier" if USE_MOS else "SwissEphemeris"
        return jsonify({"input":{"date":date_str,"time":time_str,"lat":lat,"lon":lon,"tz":tz_in,"hsys":hsys},
                        "julday_ut": jd_ut, "houses": houses, "bodies": bodies, "aspects": aspects, "mode": mode})
    except KeyError as ke:
        return jsonify({"error": f"Missing field: {str(ke)}"}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT","8080"))
    app.run(host="0.0.0.0", port=port)
