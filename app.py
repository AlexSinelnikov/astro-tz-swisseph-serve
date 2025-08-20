from __future__ import annotations
import os, traceback, threading
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify
from timezonefinder import TimezoneFinder
import swisseph as swe
from fetch_ephe import ensure_ephe

API_VERSION = "1.0"

# ===== EPHEMERIS PATH =====
EPHE_PATH = os.environ.get("EPHE_PATH", "/app/ephe")
_CWD = os.getcwd()
_CANDIDATE_PATHS = [
    EPHE_PATH, os.path.join(_CWD, "ephe"), "/app/ephe", "./ephe", "/users/ephe2", "/users/ephe",
]
_SE_PATH = ":".join(dict.fromkeys([p for p in _CANDIDATE_PATHS if p]))
os.environ["SE_EPHE_PATH"] = _SE_PATH
try:
    swe.set_ephe_path(_SE_PATH)
except Exception:
    pass

app = Flask(__name__)

# ------------ INIT ------------
READY = False
INIT_ERROR: Optional[str] = None
USE_MOS: bool = False  # если не найдём *.se1, падаем в Moshier

def _bg_init():
    global READY, INIT_ERROR, USE_MOS
    try:
        print("[app] init: ensure_ephe() starting...", flush=True)
        ensure_ephe()  # проверяет/копирует из ./ephe, ничего не качает
        swe.set_ephe_path(_SE_PATH)
        has_se1 = any(fn.endswith(".se1") for fn in os.listdir(EPHE_PATH)) if os.path.isdir(EPHE_PATH) else False
        USE_MOS = not has_se1
        print(f"[app] Swiss Ephemeris path = {_SE_PATH}; USE_MOS={USE_MOS}", flush=True)
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
    if not isinstance(s, str): return None
    s = s.strip()
    if not s or s[0] not in "+-" or ":" not in s: return None
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
    ("Ceres",   swe.CERES,     "asteroid", 3.0),
    ("Pallas",  swe.PALLAS,    "asteroid", 3.0),
    ("Juno",    swe.JUNO,      "asteroid", 3.0),
    ("Vesta",   swe.VESTA,     "asteroid", 3.0),
    ("Chiron",  swe.CHIRON,    "asteroid", 3.0),
    ("Node",    swe.TRUE_NODE, "point",    3.0),
    ("Lilith",  swe.MEAN_APOG, "point",    3.0),
]

# базовый набор аспектов
ASPECTS: List[Tuple[str, float, float]] = [
    ("Conjunction", 0.0,   8.0),
    ("Sextile",     60.0,  4.0),
    ("Square",      90.0,  6.0),
    ("Trine",       120.0, 6.0),
    ("Opposition",  180.0, 8.0),
    ("Quincunx",    150.0, 3.0),
]

# ---- Aspect filters helpers ----
ASPECT_NAME_TO_REC = {name: (name, angle, orb) for (name, angle, orb) in ASPECTS}

def build_aspect_set(aspect_types: Optional[List[str]], orbs_override: Optional[Dict[str, float]], max_orb_deg: Optional[float]):
    records = []
    base = ASPECTS if not aspect_types else [ASPECT_NAME_TO_REC[t] for t in aspect_types if t in ASPECT_NAME_TO_REC]
    for name, angle, orb in base:
        o = orb
        if orbs_override and name in orbs_override:
            o = float(orbs_override[name])
        if max_orb_deg is not None:
            o = min(o, float(max_orb_deg))
        records.append((name, angle, o))
    return records

def _sep_to_angle(l1: float, l2: float, angle: float) -> float:
    return abs(angle_diff(l1, l2) - angle)

def _is_applying(b1: Dict[str,Any], b2: Dict[str,Any], angle: float) -> bool:
    dt = 0.01  # ~14.4 мин
    c0 = _sep_to_angle(b1["lon"], b2["lon"], angle)
    l1n = norm360(b1["lon"] + b1["speed"] * dt)
    l2n = norm360(b2["lon"] + b2["speed"] * dt)
    c1 = _sep_to_angle(l1n, l2n, angle)
    return c1 < c0

def calc_bodies(jd_ut: float, include: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    include_set = set([n.lower() for n in include]) if include else None
    out: List[Dict[str, Any]] = []
    flags = _flags()
    for name, code, kind, orb_body in BODY_REGISTRY:
        if include_set and name.lower() not in include_set: continue
        pos, _ret = swe.calc_ut(jd_ut, code, flags)
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

def calc_aspects(bodies: List[Dict[str,Any]], aspects=ASPECTS, applying_only: bool=False) -> List[Dict[str,Any]]:
    res: List[Dict[str,Any]] = []
    n = len(bodies)
    for i in range(n):
        for j in range(i+1, n):
            A, B = bodies[i], bodies[j]
            for asp_name, asp_angle, asp_orb in aspects:
                orb_allowed = min(asp_orb, A["orb_body"], B["orb_body"])
                diff = _sep_to_angle(A["lon"], B["lon"], asp_angle)
                if diff <= orb_allowed:
                    applying = _is_applying(A, B, asp_angle)
                    if applying_only and not applying:
                        continue
                    res.append({
                        "a": A["name"], "b": B["name"], "type": asp_name, "angle": asp_angle,
                        "orb_allowed": orb_allowed, "delta": diff,
                        "applying": applying, "exact": abs(diff) < 1e-6
                    })
    res.sort(key=lambda x: (x["delta"], x["angle"], x["a"], x["b"]))
    return res

def calc_aspects_between(bodiesA: List[Dict[str,Any]], bodiesB: List[Dict[str,Any]], aspects=ASPECTS, applying_only: bool=False) -> List[Dict[str,Any]]:
    res: List[Dict[str,Any]] = []
    for A in bodiesA:
        for B in bodiesB:
            for asp_name, asp_angle, asp_orb in aspects:
                orb_allowed = min(asp_orb, A["orb_body"], B["orb_body"])
                diff = _sep_to_angle(A["lon"], B["lon"], asp_angle)
                if diff <= orb_allowed:
                    applying = _is_applying(A, B, asp_angle)
                    if applying_only and not applying:
                        continue
                    res.append({
                        "a": A["name"], "b": B["name"], "type": asp_name, "angle": asp_angle,
                        "orb_allowed": orb_allowed, "delta": diff,
                        "applying": applying, "exact": abs(diff) < 1e-6
                    })
    res.sort(key=lambda x: (x["delta"], x["angle"], x["a"], x["b"]))
    return res

# --------- перестраховка: ставим путь на каждый запрос ---------
def _ensure_swe_path():
    if os.environ.get("SE_EPHE_PATH") != _SE_PATH:
        os.environ["SE_EPHE_PATH"] = _SE_PATH
    try:
        swe.set_ephe_path(_SE_PATH)
    except Exception:
        pass

# ------------ HTTP ------------
@app.get("/healthz")
def healthz():
    _ensure_swe_path()
    return ("ok", 200)

@app.get("/status")
def status():
    _ensure_swe_path()
    mode = "Moshier" if USE_MOS else "SwissEphemeris"
    return jsonify({"api_version": API_VERSION, "service": "status",
                    "ready": READY, "error": INIT_ERROR, "ephe_path": EPHE_PATH, "mode": mode})

@app.get("/")
def root():
    _ensure_swe_path()
    return jsonify({"api_version": API_VERSION, "service": "root",
                    "name": "ИИ-Астролог API", "version": "1.3", "ready": READY})

# ---- Диагностика: можно отключить на проде через ENV ----
if os.environ.get("DEBUG_ROUTES", "false").lower() == "true":
    @app.get("/debug/ephe")
    def debug_ephe():
        info = {
            "EPHE_PATH": EPHE_PATH,
            "SE_EPHE_PATH": os.environ.get("SE_EPHE_PATH"),
            "cwd": os.getcwd(),
            "candidates": _CANDIDATE_PATHS,
            "exists": {},
            "samples": {}
        }
        try:
            for p in _CANDIDATE_PATHS:
                try:
                    ok = os.path.isdir(p)
                    info["exists"][p] = ok
                    if ok:
                        files = sorted(os.listdir(p))
                        info["samples"][p] = files[:15]
                except Exception as e:
                    info["exists"][p] = f"error: {type(e).__name__}: {e}"
            return jsonify({"api_version": API_VERSION, "service": "debug_ephe", **info})
        except Exception as e:
            return jsonify({"api_version": API_VERSION, "service": "debug_ephe",
                            "error": f"{type(e).__name__}: {e}", **info}), 500

# ---- TZ by coords ----
@app.get("/tz")
def tz_route():
    _ensure_swe_path()
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except Exception:
        return jsonify({"api_version": API_VERSION, "service": "tz", "error": "Pass lat & lon as query params"}), 400
    tzname = guess_iana_tz(lat, lon)
    return jsonify({"api_version": API_VERSION, "service": "tz", "tz": tzname})

# ---- NATAL (GET) ----
@app.get("/natal")
def natal_get():
    _ensure_swe_path()
    if not READY:
        return jsonify({"api_version": API_VERSION, "service": "natal",
                        "error": "Ephemeris are not ready yet.",
                        "status": {"ready": READY, "error": INIT_ERROR}}), 503
    try:
        date_str = request.args.get("date")
        time_str = request.args.get("time")
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
        hsys = (request.args.get("hsys") or "P").strip()[:1]
        tz_in = request.args.get("tz")
        guess = request.args.get("guess_tz", "true").lower() != "false"

        if tz_in: tz_obj = get_tz(tz_in)
        else:
            if guess:
                tzname = guess_iana_tz(lat, lon)
                if not tzname: return jsonify({"api_version": API_VERSION, "service": "natal", "error": "Cannot guess timezone, pass 'tz'."}), 400
                tz_obj = get_tz(tzname); tz_in = tzname
            else:
                return jsonify({"api_version": API_VERSION, "service": "natal", "error": "Missing 'tz'."}), 400

        jd_ut = to_julday_utc(date_str, time_str, tz_obj, lat, lon)
        houses = calc_houses(jd_ut, lat, lon, hsys)
        bodies = calc_bodies(jd_ut, include=None)
        aspects = calc_aspects(bodies)

        mode = "Moshier" if USE_MOS else "SwissEphemeris"
        return jsonify({"api_version": API_VERSION, "service": "natal",
                        "input":{"date":date_str,"time":time_str,"lat":lat,"lon":lon,"tz":tz_in,"hsys":hsys},
                        "julday_ut": jd_ut, "houses": houses, "bodies": bodies, "aspects": aspects, "mode": mode})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"api_version": API_VERSION, "service": "natal",
                        "error": f"{type(e).__name__}: {e}"}), 500

# ---- CALC (POST) ----
@app.post("/calc")
def calc():
    _ensure_swe_path()
    if not READY:
        return jsonify({"api_version": API_VERSION, "service": "calc",
                        "error": "Ephemeris are not ready yet.",
                        "status": {"ready": READY, "error": INIT_ERROR}}), 503
    try:
        data = request.get_json(force=True) or {}
        date_str = data["date"]; time_str = data["time"]
        lat = float(data["lat"]); lon = float(data["lon"])
        hsys = (data.get("hsys") or "P").strip()[:1]
        tz_in = data.get("tz"); guess_tz = data.get("guess_tz", True)
        include_bodies = data.get("bodies")

        # aspect filters
        aspect_types  = data.get("aspect_types")
        max_orb_deg   = data.get("max_orb_deg")
        orbs_override = data.get("orbs_override")
        applying_only = bool(data.get("applying_only", False))
        aspect_set    = build_aspect_set(aspect_types, orbs_override, max_orb_deg)

        if tz_in: tz_obj = get_tz(tz_in)
        else:
            if guess_tz:
                tzname = guess_iana_tz(lat, lon)
                if not tzname: return jsonify({"api_version": API_VERSION, "service": "calc", "error": "Cannot guess timezone, pass 'tz'."}), 400
                tz_obj = get_tz(tzname); tz_in = tzname
            else:
                return jsonify({"api_version": API_VERSION, "service": "calc", "error": "Missing 'tz'."}), 400

        jd_ut = to_julday_utc(date_str, time_str, tz_obj, lat, lon)
        houses = calc_houses(jd_ut, lat, lon, hsys)
        bodies = calc_bodies(jd_ut, include=include_bodies)
        aspects = calc_aspects(bodies, aspects=aspect_set, applying_only=applying_only)

        mode = "Moshier" if USE_MOS else "SwissEphemeris"
        return jsonify({"api_version": API_VERSION, "service": "calc",
                        "input":{"date":date_str,"time":time_str,"lat":lat,"lon":lon,"tz":tz_in,"hsys":hsys},
                        "julday_ut": jd_ut, "houses": houses, "bodies": bodies, "aspects": aspects, "mode": mode})
    except KeyError as ke:
        return jsonify({"api_version": API_VERSION, "service": "calc",
                        "error": f"Missing field: {str(ke)}"}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"api_version": API_VERSION, "service": "calc",
                        "error": f"{type(e).__name__}: {e}"}), 500

# ---- SYNASTRY (POST) ----
@app.post("/synastry")
def synastry():
    _ensure_swe_path()
    if not READY:
        return jsonify({"api_version": API_VERSION, "service": "synastry",
                        "error": "Ephemeris are not ready yet.",
                        "status": {"ready": READY, "error": INIT_ERROR}}), 503
    try:
        data = request.get_json(force=True) or {}
        A = data["a"]; B = data["b"]
        bodies_filter = data.get("bodies")

        # aspect filters
        aspect_types  = data.get("aspect_types")
        max_orb_deg   = data.get("max_orb_deg")
        orbs_override = data.get("orbs_override")
        applying_only = bool(data.get("applying_only", False))
        aspect_set    = build_aspect_set(aspect_types, orbs_override, max_orb_deg)

        def _prep(x):
            lat = float(x["lat"]); lon = float(x["lon"])
            tz_in = x.get("tz"); guess = x.get("guess_tz", True)
            if tz_in: tz_obj = get_tz(tz_in)
            else:
                if guess:
                    tzname = guess_iana_tz(lat, lon)
                    if not tzname: raise ValueError("Cannot guess timezone for chart")
                    tz_obj = get_tz(tzname); x["tz"] = tzname
                else:
                    raise ValueError("Missing 'tz' for chart")
            jd = to_julday_utc(x["date"], x["time"], tz_obj, lat, lon)
            return jd

        jdA = _prep(A)
        jdB = _prep(B)

        bodiesA = calc_bodies(jdA, include=bodies_filter)
        bodiesB = calc_bodies(jdB, include=bodies_filter)
        aspectsAB = calc_aspects_between(bodiesA, bodiesB, aspects=aspect_set, applying_only=applying_only)

        return jsonify({
            "api_version": API_VERSION, "service": "synastry",
            "input": {"a": A, "b": B, "filters": {
                "aspect_types": aspect_types, "max_orb_deg": max_orb_deg,
                "orbs_override": orbs_override, "applying_only": applying_only
            }},
            "a": {"julday_ut": jdA, "bodies": bodiesA},
            "b": {"julday_ut": jdB, "bodies": bodiesB},
            "aspects": aspectsAB
        })
    except KeyError as ke:
        return jsonify({"api_version": API_VERSION, "service": "synastry",
                        "error": f"Missing field: {str(ke)}"}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"api_version": API_VERSION, "service": "synastry",
                        "error": f"{type(e).__name__}: {e}"}), 500

# ---- TRANSITS (POST) ----
@app.post("/transits")
def transits():
    _ensure_swe_path()
    if not READY:
        return jsonify({"api_version": API_VERSION, "service": "transits",
                        "error": "Ephemeris are not ready yet.",
                        "status": {"ready": READY, "error": INIT_ERROR}}), 503
    try:
        data = request.get_json(force=True) or {}
        natal = data["natal"]
        t_date = data.get("date")
        t_time = data.get("time", "12:00")
        tz_in = data.get("tz") or natal.get("tz")
        bodies_transit = data.get("bodies_transit")
        bodies_natal = data.get("bodies_natal") or data.get("bodies")
        if not t_date:
            return jsonify({"api_version": API_VERSION, "service": "transits",
                            "error": "Missing field: 'date'"}), 400

        # aspect filters
        aspect_types  = data.get("aspect_types")
        max_orb_deg   = data.get("max_orb_deg")
        orbs_override = data.get("orbs_override")
        applying_only = bool(data.get("applying_only", False))
        aspect_set    = build_aspect_set(aspect_types, orbs_override, max_orb_deg)

        # natal
        n_lat = float(natal["lat"]); n_lon = float(natal["lon"])
        n_tz = natal.get("tz")
        if not n_tz:
            tzname = guess_iana_tz(n_lat, n_lon)
            if not tzname: return jsonify({"api_version": API_VERSION, "service": "transits", "error": "Cannot guess natal timezone"}), 400
            n_tz = tzname
        n_tz_obj = get_tz(n_tz)
        n_jd = to_julday_utc(natal["date"], natal["time"], n_tz_obj, n_lat, n_lon)
        natal_bodies = calc_bodies(n_jd, include=bodies_natal)

        # transit moment
        if not tz_in: tz_in = n_tz
        t_tz_obj = get_tz(tz_in)
        t_jd = to_julday_utc(t_date, t_time, t_tz_obj, n_lat, n_lon)
        transit_bodies = calc_bodies(t_jd, include=bodies_transit)

        aspects_to_natal = calc_aspects_between(transit_bodies, natal_bodies, aspects=aspect_set, applying_only=applying_only)
        return jsonify({
            "api_version": API_VERSION, "service": "transits",
            "input": {"natal": natal, "date": t_date, "time": t_time, "tz": tz_in,
                      "filters": {"aspect_types": aspect_types, "max_orb_deg": max_orb_deg,
                                  "orbs_override": orbs_override, "applying_only": applying_only}},
            "julday_ut": t_jd,
            "transit_bodies": transit_bodies,
            "natal_bodies": natal_bodies,
            "aspects_to_natal": aspects_to_natal
        })
    except KeyError as ke:
        return jsonify({"api_version": API_VERSION, "service": "transits",
                        "error": f"Missing field: {str(ke)}"}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"api_version": API_VERSION, "service": "transits",
                        "error": f"{type(e).__name__}: {e}"}), 500

# ---- FORECAST (POST) ----
@app.post("/forecast")
def forecast():
    """
    Ежедневный прогноз: транзитные аспекты к наталу в диапазоне дат.
    """
    _ensure_swe_path()
    if not READY:
        return jsonify({"api_version": API_VERSION, "service": "forecast",
                        "error": "Ephemeris are not ready yet.",
                        "status": {"ready": READY, "error": INIT_ERROR}}), 503
    try:
        data = request.get_json(force=True) or {}
        natal = data["natal"]
        d_from = data["from"]
        d_to = data["to"]
        step_days = int(data.get("step_days", 1))
        time_of_day = data.get("time", "12:00")
        include_empty = bool(data.get("include_empty_days", False))

        if step_days <= 0 or step_days > 14:
            return jsonify({"api_version": API_VERSION, "service": "forecast",
                            "error": "step_days must be in [1..14]"}), 400

        # aspect filters
        aspect_types  = data.get("aspect_types")
        max_orb_deg   = data.get("max_orb_deg")
        orbs_override = data.get("orbs_override")
        applying_only = bool(data.get("applying_only", False))
        aspect_set    = build_aspect_set(aspect_types, orbs_override, max_orb_deg)

        # natal bodies
        n_lat = float(natal["lat"]); n_lon = float(natal["lon"])
        n_tz = natal.get("tz")
        if not n_tz:
            tzname = guess_iana_tz(n_lat, n_lon)
            if not tzname: return jsonify({"api_version": API_VERSION, "service": "forecast", "error": "Cannot guess natal timezone"}), 400
            n_tz = tzname
        n_tz_obj = get_tz(n_tz)
        n_jd = to_julday_utc(natal["date"], natal["time"], n_tz_obj, n_lat, n_lon)
        bodies_natal = calc_bodies(n_jd, include=data.get("bodies_natal"))

        # диапазон дат
        y1, m1, d1 = [int(x) for x in d_from.split("-")]
        y2, m2, d2 = [int(x) for x in d_to.split("-")]
        dt_start = datetime(y1, m1, d1)
        dt_end   = datetime(y2, m2, d2)
        if dt_end < dt_start:
            return jsonify({"api_version": API_VERSION, "service": "forecast",
                            "error": "'to' must be >= 'from'"}), 400
        if (dt_end - dt_start).days > 370:
            return jsonify({"api_version": API_VERSION, "service": "forecast",
                            "error": "Range too large, max 370 days"}), 400

        run_tz = data.get("tz") or n_tz
        run_tz_obj = get_tz(run_tz)

        res_days: List[Dict[str,Any]] = []
        cur = dt_start
        while cur <= dt_end:
            date_str = f"{cur.year:04d}-{cur.month:02d}-{cur.day:02d}"
            t_jd = to_julday_utc(date_str, time_of_day, run_tz_obj, n_lat, n_lon)
            transit_bodies = calc_bodies(t_jd, include=data.get("bodies_transit"))
            aspects_to_natal = calc_aspects_between(transit_bodies, bodies_natal, aspects=aspect_set, applying_only=applying_only)
            if aspects_to_natal or include_empty:
                res_days.append({"date": date_str, "julday_ut": t_jd, "aspects_to_natal": aspects_to_natal})
            cur = cur + timedelta(days=step_days)

        return jsonify({
            "api_version": API_VERSION, "service": "forecast",
            "input": {"natal": natal, "from": d_from, "to": d_to, "time": time_of_day, "tz": run_tz, "step_days": step_days,
                      "filters": {"aspect_types": aspect_types, "max_orb_deg": max_orb_deg,
                                  "orbs_override": orbs_override, "applying_only": applying_only}},
            "natal_bodies": bodies_natal,
            "days": res_days
        })
    except KeyError as ke:
        return jsonify({"api_version": API_VERSION, "service": "forecast",
                        "error": f"Missing field: {str(ke)}"}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"api_version": API_VERSION, "service": "forecast",
                        "error": f"{type(e).__name__}: {e}"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT","8080"))
    app.run(host="0.0.0.0", port=port)
