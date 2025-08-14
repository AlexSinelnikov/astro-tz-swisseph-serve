from __future__ import annotations
import os, io, math, zipfile, glob, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

from flask import Flask, request, jsonify, g
from timezonefinder import TimezoneFinder

# ===== Swiss Ephemeris (pyswisseph) =====
try:
    import swisseph as swe
except Exception:
    import pyswisseph as swe  # type: ignore

# ===== Config / Paths =====
EPHE_PATH = os.environ.get("EPHE_PATH", "/app/ephe")
EPHE_ZIP_URL = os.environ.get("EPHE_ZIP_URL", "").strip()
_EPHE_REQUIRED_LEGACY = os.environ.get("EPHE_REQUIRED", "sepl_*.se1,semo_*.se1,seas_*.se1")
EPHE_REQUIRED_GLOBS = os.environ.get("EPHE_REQUIRED_GLOBS", _EPHE_REQUIRED_LEGACY)
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "60"))  # per IP

swe.set_ephe_path(EPHE_PATH)
print(f"[app] Swiss Ephemeris path = {EPHE_PATH}")

# ===== TZ handling =====
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    from pytz import timezone as PytzTZ
    class ZoneInfo:
        def __init__(self, name):
            self._tz = PytzTZ(name)
        def utcoffset(self, dt): return self._tz.utcoffset(dt)
        def dst(self, dt): return self._tz.dst(dt)
        def tzname(self, dt): return self._tz.tzname(dt)

class FixedOffsetTZ(datetime.tzinfo):
    def __init__(self, minutes: int): self._offset = minutes
    def utcoffset(self, dt): return timedelta(minutes=self._offset)
    def tzname(self, dt):
        s = self._offset; sign = "+" if s >= 0 else "-"; s = abs(s)
        return f"{sign}{s//60:02d}:{s%60:02d}"
    def dst(self, dt): return timedelta(0)

def parse_offset(s: Optional[str]) -> Optional[FixedOffsetTZ]:
    if not s: return None
    try:
        s = s.strip(); sign = 1 if s[0] == "+" else -1
        hhmm = s[1:].split(":")
        hh, mm = (hhmm[0], "00") if len(hhmm)==1 else hhmm
        return FixedOffsetTZ(sign * (int(hh)*60 + int(mm)))
    except Exception: return None

app = Flask(__name__)
_tf = TimezoneFinder(in_memory=True)

# ===== Simple in‑memory rate limiter (token bucket) =====
# NOTE: на нескольких воркерах/инстансах нужен внешний стор (Redis). Здесь — легкий in‑memory.
_rate_state: Dict[str, Dict[str, float]] = {}  # ip -> {"tokens": float, "ts": float}
_BUCKET_SIZE = max(RATE_LIMIT_PER_MIN, 1)
_REFILL_PER_SEC = RATE_LIMIT_PER_MIN / 60.0

def _rate_check(ip: str) -> bool:
    now = time.time()
    st = _rate_state.get(ip)
    if not st:
        _rate_state[ip] = {"tokens": _BUCKET_SIZE - 1, "ts": now}
        return True
    # refill
    elapsed = now - st["ts"]
    st["tokens"] = min(_BUCKET_SIZE, st["tokens"] + elapsed * _REFILL_PER_SEC)
    st["ts"] = now
    if st["tokens"] >= 1.0:
        st["tokens"] -= 1.0
        return True
    return False

@app.before_request
def _apply_rate_limit():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    g.client_ip = ip
    if request.path in ("/health", "/ephe-check", "/calc/test"):  # не лимитируем диагностику
        return
    if not _rate_check(ip):
        return jsonify({"ok": False, "error": "Rate limit exceeded", "limit_per_min": RATE_LIMIT_PER_MIN}), 429

# ===== Ephemeris bootstrap =====
def _list_ephe_files() -> List[str]:
    p = Path(EPHE_PATH); 
    return [x.name for x in p.glob("*") if x.is_file()] if p.exists() else []

def _have_required_ephe() -> bool:
    patterns = [g.strip() for g in EPHE_REQUIRED_GLOBS.split(",") if g.strip()]
    if not patterns: return True
    p = Path(EPHE_PATH)
    if not p.exists(): return False
    return all(list(p.glob(pat)) for pat in patterns)

def _download_and_unzip_ephe(url: str, dest_dir: str) -> None:
    from urllib.request import urlopen
    print(f"[EPHE] Downloading: {url}")
    with urlopen(url) as resp: blob = resp.read()
    print(f"[EPHE] Downloaded {len(blob)} bytes")
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf: zf.extractall(dest_dir)
    print(f"[EPHE] Unzipped into {dest_dir}")

def ensure_ephe() -> None:
    try:
        if _have_required_ephe():
            print("[EPHE] Ready; sample:", _list_ephe_files()[:20]); return
        if not EPHE_ZIP_URL:
            print("[EPHE][WARN] EPHE_ZIP_URL not set, but required files missing."); return
        lock = Path(EPHE_PATH, ".ephe.lock")
        if not lock.exists():
            try:
                lock.write_text(datetime.now(timezone.utc).isoformat())
                _download_and_unzip_ephe(EPHE_ZIP_URL, EPHE_PATH)
            finally:
                pass
        else:
            print("[EPHE] Lock found, assuming already fetched.")
        print("[EPHE] After fetch; sample:", _list_ephe_files()[:20])
        if not _have_required_ephe(): print("[EPHE][ERROR] Required files still missing!")
        else: print("[EPHE] Ephemeris ready.")
    except Exception as e:
        print(f"[EPHE][ERROR] {e}")

ensure_ephe()

# ===== Helpers =====
SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]

def norm360(x: float) -> float:
    v = x % 360.0
    return v if v >= 0 else v + 360.0

def angdist(a: float, b: float) -> float:
    d = abs(norm360(a) - norm360(b))
    return d if d <= 180.0 else 360.0 - d

def sign_name(lon: float) -> str:
    return SIGNS[int(norm360(lon)//30) % 12]

def find_timezone_name(lat: float, lon: float) -> Optional[str]:
    # лёгкий кэш не вводим: rate-limit и так снижает нагрузку; _tf.in_memory=True
    return _tf.timezone_at(lng=lon, lat=lat) or _tf.closest_timezone_at(lng=lon, lat=lat)

def parse_date_time(date_str: Optional[str], time_str: Optional[str]) -> datetime:
    base = date_str or datetime.utcnow().date().isoformat()
    t = time_str or "12:00"
    if len(t.split(":")) == 2: t = f"{t}:00"
    return datetime.fromisoformat(f"{base}T{t}")

def validate_coords(lat: Any, lon: Any) -> Tuple[float, float]:
    try:
        latf = float(lat); lonf = float(lon)
    except Exception:
        raise ValueError("lat/lon must be numbers")
    if not (-90.0 <= latf <= 90.0 and -180.0 <= lonf <= 180.0):
        raise ValueError("lat must be [-90..90], lon must be [-180..180]")
    return latf, lonf

def validate_date(date_str: str) -> None:
    try:
        datetime.fromisoformat(date_str)
    except Exception:
        raise ValueError("date must be ISO YYYY-MM-DD")

def validate_time(time_str: str) -> None:
    # Accept HH:MM or HH:MM:SS
    parts = time_str.split(":")
    if len(parts) not in (2,3): raise ValueError("time must be HH:MM or HH:MM:SS")

# ===== Swiss helpers =====
def compute_planets(jd_ut: float, flags: int) -> Tuple[Dict[str, Dict[str, float]], Dict[str, float]]:
    obj_codes = {
        "Sun": swe.SUN, "Moon": swe.MOON, "Mercury": swe.MERCURY, "Venus": swe.VENUS,
        "Mars": swe.MARS, "Jupiter": swe.JUPITER, "Saturn": swe.SATURN,
        "Uranus": swe.URANUS, "Neptune": swe.NEPTUNE, "Pluto": swe.PLUTO,
        "Chiron": getattr(swe, "CHIRON", 15),
        "Mean Node": swe.MEAN_NODE, "True Node": swe.TRUE_NODE,
        "Lilith (Mean)": swe.MEAN_APOG, "Lilith (Oscu)": swe.OSCU_APOG,
    }
    res: Dict[str, Dict[str, float]] = {}
    longitudes: Dict[str, float] = {}
    for name, code in obj_codes.items():
        try:
            pos, _ = swe.calc_ut(jd_ut, code, flags)
            lon, lat, dist = pos[0], pos[1], pos[2]
            speed_lon = pos[3] if len(pos) > 3 else None
            lon_n = norm360(lon)
            res[name] = {"lon": lon_n, "lat": lat, "dist": dist, "speed_lon": speed_lon}
            longitudes[name] = lon_n
        except Exception as e:
            res[name] = {"error": str(e)}
    return res, longitudes

def compute_houses(jd_ut: float, flags: int, lat: float, lon: float, hs: str):
    try:
        houses, ascmc = swe.houses_ex(jd_ut, lat, lon, hs.encode("ascii"), flags)
        cusps = {str(i+1): norm360(houses[i]) for i in range(12)}
        asc = norm360(ascmc[0]); mc = norm360(ascmc[1]); armc = ascmc[2]
        vertex = norm360(ascmc[3])  # 3: Vertex
        # ecliptic true obliquity
        ecl, _ = swe.calc_ut(jd_ut, swe.ECL_NUT, 0); eps_true = ecl[0]
        return cusps, asc, mc, armc, eps_true, vertex, None
    except Exception as e:
        return {}, None, None, None, None, None, str(e)

def house_pos_float(armc: float, geolat: float, eps: float, hs: str, lon: float, lat_ecl: float = 0.0) -> Optional[float]:
    try: return swe.house_pos(armc, geolat, eps, hs.encode("ascii"), lon, lat_ecl)
    except Exception: return None

def detect_day_chart(armc: float, geolat: float, eps: float, hs: str, sun_lon: float) -> bool:
    pos = house_pos_float(armc, geolat, eps, hs, sun_lon, 0.0)
    return True if (pos is None or pos > 6.0) else False

def antipodal(lon: float) -> float:
    return norm360(lon + 180.0)

def fortune_longitude(is_day: bool, asc: float, sun: float, moon: float) -> float:
    # Lot of Fortune
    return norm360(asc + (moon - sun) if is_day else asc - moon + sun)

def spirit_longitude(is_day: bool, asc: float, sun: float, moon: float) -> float:
    # Lot of Spirit (complement to Fortune)
    return norm360(asc + (sun - moon) if is_day else asc - sun + moon)

def lot_of_eros(is_day: bool, asc: float, venus: float, spirit: float) -> float:
    # One traditional definition:
    # Day:   Eros = ASC + Venus − Spirit
    # Night: Eros = ASC + Spirit − Venus
    return norm360(asc + venus - spirit) if is_day else norm360(asc + spirit - venus)

def lot_of_courage(is_day: bool, asc: float, mars: float, spirit: float) -> float:
    # One traditional definition:
    # Day:   Courage = ASC + Mars − Spirit
    # Night: Courage = ASC + Spirit − Mars
    return norm360(asc + mars - spirit) if is_day else norm360(asc + spirit - mars)

def aspect_type(angle: float) -> Optional[str]:
    return {0:"conjunction", 60:"sextile", 90:"square", 120:"trine", 180:"opposition"}.get(int(angle))

def compute_aspects(longitudes: Dict[str, float],
                    speeds: Dict[str, Optional[float]],
                    include: List[str],
                    orbs: Dict[str, float],
                    include_cusps: bool,
                    cusps: Dict[str, float],
                    include_angles: bool,
                    asc: Optional[float],
                    mc: Optional[float]) -> List[Dict[str, Any]]:
    angles = [0, 60, 90, 120, 180]
    names = [n for n in include if n in longitudes]

    if include_angles and asc is not None and mc is not None:
        longitudes["ASC"] = asc; longitudes["MC"] = mc
        speeds.setdefault("ASC", None); speeds.setdefault("MC", None)
        names += ["ASC","MC"]

    if include_cusps and cusps:
        for i in range(1,13):
            key = f"Cusp {i}"
            longitudes[key] = cusps.get(str(i))
            speeds.setdefault(key, None)
            names.append(key)

    skip_geom = (request.args.get("skip_geom", "1") not in ("0","false","False"))
    def is_trivial_geom(a: str, b: str) -> bool:
        if not skip_geom: return False
        def is_cusp(x): return isinstance(x, str) and x.startswith("Cusp ")
        pair = {a,b}
        if pair in [{"ASC","Cusp 1"},{"MC","Cusp 10"}]: return True
        if is_cusp(a) and is_cusp(b):
            ai, bi = int(a.split()[1]), int(b.split()[1])
            if (ai-bi)%12==6 or (ai-bi)%6==0: return True
        return False

    aspects: List[Dict[str, Any]] = []
    N = len(names)
    for i in range(N):
        for j in range(i+1, N):
            a,b = names[i], names[j]
            if is_trivial_geom(a,b): continue
            la, lb = longitudes.get(a), longitudes.get(b)
            if la is None or lb is None: continue
            delta = angdist(la, lb)
            def cat(x: str) -> str:
                if x in ("Sun","Moon"): return "lum"
                if isinstance(x,str) and (x.startswith("Cusp") or x in ("ASC","MC")): return "angle"
                if isinstance(x,str) and "Node" in x: return "node"
                if x=="Chiron": return "chiron"
                if isinstance(x,str) and "Lilith" in x: return "lilith"
                return "main"
            orb_allow = max(orbs.get(cat(a), orbs["main"]), orbs.get(cat(b), orbs["main"]))
            for ang in angles:
                if abs(delta-ang) <= orb_allow:
                    aspects.append({
                        "a": a, "b": b, "type": aspect_type(ang),
                        "exact": ang, "delta": round(delta,4),
                        "orb": round(abs(delta-ang),4),
                        "speed_a": speeds.get(a), "speed_b": speeds.get(b),
                    })
                    break
    aspects.sort(key=lambda x: x["orb"])
    return aspects

def planet_pack(name: str, data: Dict[str, Any], house_pos: Optional[float]) -> Dict[str, Any]:
    out = {"name": name}
    if "lon" in data:
        lon = data["lon"]; out["lon"] = lon; out["sign"] = sign_name(lon); out["deg_in_sign"] = round(lon % 30, 4)
    for k in ("lat","dist","speed_lon"): 
        if k in data and data[k] is not None: out[k] = data[k]
    if house_pos is not None:
        out["house_float"] = round(house_pos, 4)
        out["house"] = int(math.ceil(house_pos)) if house_pos>0 else 1
    if "speed_lon" in data and isinstance(data["speed_lon"], (int,float)):
        out["retrograde"] = data["speed_lon"] < 0
    if "error" in data: out["error"] = data["error"]
    return out

# ===== Error handlers =====
@app.errorhandler(404)
def _404(_e): return jsonify({"ok": False, "error": "Not found"}), 404

@app.errorhandler(500)
def _500(e): return jsonify({"ok": False, "error": "Internal error", "detail": str(e)}), 500

# ===== Diagnostics =====
@app.get("/health")
def health() -> Any:
    files = [os.path.basename(p) for p in glob.glob(os.path.join(EPHE_PATH, "*"))]
    required_ok = True
    for pat in [g.strip() for g in EPHE_REQUIRED_GLOBS.split(",") if g.strip()]:
        if not glob.glob(os.path.join(EPHE_PATH, pat)):
            required_ok = False; break
    return jsonify({"ok": True, "version": "3.1.0",
                    "rate_limit_per_min": RATE_LIMIT_PER_MIN,
                    "ephe": {"path": EPHE_PATH, "required_globs": EPHE_REQUIRED_GLOBS,
                             "required_ok": required_ok, "count": len(files), "sample": sorted(files)[:30]}})

@app.get("/ephe-check")
def ephe_check():
    return jsonify({"required_globs": EPHE_REQUIRED_GLOBS,
                    "have_required": _have_required_ephe(),
                    "files": _list_ephe_files()[:100], "count": len(_list_ephe_files()),
                    "path": EPHE_PATH})

@app.get("/calc/test")
def calc_test():
    try:
        jd = swe.julday(2000,1,1,0.0, swe.GREG_CAL)
        lon, lat, dist = swe.calc_ut(jd, swe.SUN)[0][:3]
        return jsonify({"ok": True, "jd": jd, "sun_lon": lon, "sun_lat": lat, "sun_dist": dist})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ===== Legacy GET: /swisseph (kept for convenience) =====
@app.get("/swisseph")
def swisseph_endpoint():
    date_str = request.args.get("date"); time_str = request.args.get("time")
    if not date_str or not time_str:
        return jsonify({"error":"date (YYYY-MM-DD) and time (HH:MM[:SS]) are required"}), 400
    validate_date(date_str); validate_time(time_str)

    try:
        lat, lon = validate_coords(request.args.get("lat",""), request.args.get("lng",""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    tz_name = request.args.get("tz"); offset_arg = request.args.get("offset")
    if not tz_name and not offset_arg:
        tz_name = find_timezone_name(lat, lon)
        if not tz_name: return jsonify({"error":"Cannot resolve timezone for given coordinates"}), 422

    # Orbs
    orbs = {"main": float(request.args.get("orb_main",6)),
            "lum": float(request.args.get("orb_lum",8)),
            "angle": float(request.args.get("orb_angle",4)),
            "node": float(request.args.get("orb_node",3)),
            "chiron": float(request.args.get("orb_chiron",3)),
            "lilith": float(request.args.get("orb_lilith",3))}
    include_angles = request.args.get("include_angles","true").lower()=="true"
    include_cusps  = request.args.get("include_cusps","true").lower()=="true"

    tz = parse_offset(offset_arg) if offset_arg else None
    if tz is None:
        try: tz = ZoneInfo(tz_name or find_timezone_name(lat, lon))
        except Exception: return jsonify({"error": f"Unknown timezone '{tz_name}'"}), 422

    dt_local = parse_date_time(date_str, time_str).replace(tzinfo=tz)
    dt_utc = dt_local.astimezone(timezone.utc)
    jd_ut = swe.julday(dt_utc.year, dt_utc.month, dt_utc.day,
                       dt_utc.hour + dt_utc.minute/60.0 + dt_utc.second/3600.0, swe.GREG_CAL)

    sidereal = request.args.get("sidereal","false").lower()=="true"
    flags = swe.FLG_SWIEPH | swe.FLG_SPEED
    if sidereal:
        flags |= swe.FLG_SIDEREAL
        swe.set_sid_mode(getattr(swe,"SIDM_LAHIRI",1),0,0)

    planets, longs = compute_planets(jd_ut, flags)
    hs = (request.args.get("hs") or "P").upper()
    cusps, asc, mc, armc, eps_true, vertex, err = compute_houses(jd_ut, flags, lat, lon, hs)
    if err: return jsonify({"error": f"houses_ex failed: {err}"}), 500

    # Core points
    dsc = antipodal(asc) if asc is not None else None
    ic  = antipodal(mc)  if mc  is not None else None
    antivertex = antipodal(vertex) if vertex is not None else None

    # Day/Night & lots
    day_chart = detect_day_chart(armc, lat, eps_true, hs, longs.get("Sun", 0.0))
    pof = fortune_longitude(day_chart, asc, longs.get("Sun",0.0), longs.get("Moon",0.0)) if asc is not None else None
    pos = spirit_longitude(day_chart, asc, longs.get("Sun",0.0), longs.get("Moon",0.0)) if asc is not None else None
    eros = lot_of_eros(day_chart, asc, longs.get("Venus",0.0), pos or 0.0) if asc is not None and pos is not None else None
    courage = lot_of_courage(day_chart, asc, longs.get("Mars",0.0), pos or 0.0) if asc is not None and pos is not None else None

    # Aspects
    speeds = {k: planets.get(k,{}).get("speed_lon") for k in longs.keys()}
    include_names = list(longs.keys())
    for nm, lv in [("Part of Fortune", pof), ("Part of Spirit", pos),
                   ("Vertex", vertex), ("Anti-Vertex", antivertex),
                   ("IC", ic), ("DSC", dsc), ("Lot of Eros", eros), ("Lot of Courage", courage)]:
        if lv is not None: longs[nm] = lv; speeds[nm] = None; include_names.append(nm)

    aspects = compute_aspects(longs, speeds, include_names, orbs, include_cusps, cusps, include_angles, asc, mc)

    # Pack planets with houses
    packed_planets = {}
    for name, pdata in planets.items():
        hpos = house_pos_float(armc, lat, eps_true, hs, pdata.get("lon", 0.0), pdata.get("lat", 0.0)) if asc is not None else None
        packed_planets[name] = planet_pack(name, pdata, hpos)

    def pack_point(nm: str, lonval: Optional[float]) -> Optional[Dict[str,Any]]:
        if lonval is None: return None
        hpos = house_pos_float(armc, lat, eps_true, hs, lonval, 0.0)
        return {"name": nm, "lon": lonval, "sign": sign_name(lonval), "deg_in_sign": round(lonval%30,4),
                "house_float": round(hpos,4) if hpos else None, "house": int(math.ceil(hpos)) if hpos else None}

    points = {}
    for nm, lv in [("ASC", asc), ("MC", mc), ("DSC", dsc), ("IC", ic),
                   ("Vertex", vertex), ("Anti-Vertex", antivertex),
                   ("Part of Fortune", pof), ("Part of Spirit", pos),
                   ("Lot of Eros", eros), ("Lot of Courage", courage)]:
        p = pack_point(nm, lv)
        if p: points[nm] = p

    return jsonify({
        "schema_version": "3.1",
        "meta": {"ip": getattr(g, "client_ip", None),
                 "sidereal": sidereal, "house_system": hs,
                 "tz": tz_name or (tz.tzname(None) if tz else None),
                 "coords": {"lat": lat, "lon": lon}},
        "datetime": {"local": dt_local.replace(microsecond=0).isoformat(),
                     "utc": dt_utc.replace(microsecond=0).isoformat().replace("+00:00","Z")},
        "jd_ut": jd_ut,
        "houses": {"cusps": cusps, "asc": asc, "mc": mc, "armc": armc, "eps_true": eps_true},
        "planets": packed_planets,
        "points": points,
        "aspects": aspects
    })

# ===== GPT‑ready POST: /calc/natal =====
@app.post("/calc/natal")
def calc_natal():
    try:
        body = request.get_json(force=True)
        date_str: str = body["date"]; time_str: str = body.get("time","00:00")
        validate_date(date_str); validate_time(time_str)
        lat, lon = validate_coords(body["lat"], body["lon"])
        tzname = body.get("tzname"); offset_arg = body.get("offset")
        sidereal = bool(body.get("sidereal", False))
        hs = (body.get("house_system") or "P").upper()
    except KeyError as e:
        return jsonify({"ok": False, "error": f"Missing field: {e}"}), 400
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Bad input: {e}"}), 400

    if not tzname and not offset_arg:
        tzname = find_timezone_name(lat, lon)
        if not tzname:
            return jsonify({"ok": False, "error":"Cannot resolve timezone from coordinates"}), 422

    tz = parse_offset(offset_arg) if offset_arg else None
    if tz is None:
        try: tz = ZoneInfo(tzname)
        except Exception: return jsonify({"ok": False, "error": f"Unknown timezone '{tzname}'"}), 422

    dt_local = parse_date_time(date_str, time_str).replace(tzinfo=tz)
    dt_utc = dt_local.astimezone(timezone.utc)
    jd_ut = swe.julday(dt_utc.year, dt_utc.month, dt_utc.day,
                       dt_utc.hour + dt_utc.minute/60.0 + dt_utc.second/3600.0, swe.GREG_CAL)

    flags = swe.FLG_SWIEPH | swe.FLG_SPEED
    if sidereal:
        flags |= swe.FLG_SIDEREAL
        swe.set_sid_mode(getattr(swe,"SIDM_LAHIRI",1),0,0)

    planets, longs = compute_planets(jd_ut, flags)
    cusps, asc, mc, armc, eps_true, vertex, err = compute_houses(jd_ut, flags, lat, lon, hs)
    if err: return jsonify({"ok": False, "error": f"houses_ex failed: {err}"}), 500

    dsc = antipodal(asc) if asc is not None else None
    ic  = antipodal(mc)  if mc  is not None else None
    antivertex = antipodal(vertex) if vertex is not None else None

    day_chart = detect_day_chart(armc, lat, eps_true, hs, longs.get("Sun", 0.0))
    pof = fortune_longitude(day_chart, asc, longs.get("Sun",0.0), longs.get("Moon",0.0)) if asc is not None else None
    pos = spirit_longitude(day_chart, asc, longs.get("Sun",0.0), longs.get("Moon",0.0)) if asc is not None else None
    eros = lot_of_eros(day_chart, asc, longs.get("Venus",0.0), pos or 0.0) if asc is not None and pos is not None else None
    courage = lot_of_courage(day_chart, asc, longs.get("Mars",0.0), pos or 0.0) if asc is not None and pos is not None else None

    # aspects
    speeds = {k: planets.get(k,{}).get("speed_lon") for k in longs.keys()}
    include_names = list(longs.keys())
    for nm, lv in [("Part of Fortune", pof), ("Part of Spirit", pos),
                   ("Vertex", vertex), ("Anti-Vertex", antivertex),
                   ("IC", ic), ("DSC", dsc), ("Lot of Eros", eros), ("Lot of Courage", courage)]:
        if lv is not None: longs[nm] = lv; speeds[nm] = None; include_names.append(nm)
    # дефолтные орбы под интерпретацию
    orbs = {"main":6.0,"lum":8.0,"angle":4.0,"node":3.0,"chiron":3.0,"lilith":3.0}
    aspects = compute_aspects(longs, speeds, include_names, orbs, True, cusps, True, asc, mc)

    packed_planets = {}
    for name, pdata in planets.items():
        hpos = house_pos_float(armc, lat, eps_true, hs, pdata.get("lon",0.0), pdata.get("lat",0.0)) if asc is not None else None
        packed_planets[name] = planet_pack(name, pdata, hpos)

    def pack_point(nm: str, lonval: Optional[float]) -> Optional[Dict[str,Any]]:
        if lonval is None: return None
        hpos = house_pos_float(armc, lat, eps_true, hs, lonval, 0.0)
        return {"name": nm, "lon": lonval, "sign": sign_name(lonval), "deg_in_sign": round(lonval%30,4),
                "house_float": round(hpos,4) if hpos else None, "house": int(math.ceil(hpos)) if hpos else None}

    points = {}
    for nm, lv in [("ASC", asc), ("MC", mc), ("DSC", dsc), ("IC", ic),
                   ("Vertex", vertex), ("Anti-Vertex", antivertex),
                   ("Part of Fortune", pof), ("Part of Spirit", pos),
                   ("Lot of Eros", eros), ("Lot of Courage", courage)]:
        p = pack_point(nm, lv)
        if p: points[nm] = p

    return jsonify({
        "ok": True,
        "schema_version": "3.1",
        "meta": {
            "ip": getattr(g, "client_ip", None),
            "sidereal": sidereal,
            "house_system": hs,
            "tz": tzname or tz.tzname(None),
            "coords": {"lat": lat, "lon": lon}
        },
        "datetime": {
            "local": dt_local.replace(microsecond=0).isoformat(),
            "utc": dt_utc.replace(microsecond=0).isoformat().replace("+00:00","Z")
        },
        "jd_ut": jd_ut,
        "houses": {"cusps": cusps, "asc": asc, "mc": mc, "armc": armc, "eps_true": eps_true},
        "planets": packed_planets,
        "points": points,
        "aspects": aspects
    })

# ===== Entrypoint =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
