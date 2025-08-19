# app.py
# Swiss Ephemeris API — prod
# - без сетевых операций (эфемериды готовит fetch_ephe.py)
# - мульти‑путь по *.se1 (рекурсивно)
# - EPHE_REQUIRED_GLOBS поддерживает ИЛИ: (sepl_*.se1|sepm*.se1)
# - строгая валидация, rate‑limit, таймзоны с кэшем
# - полный набор тел + астероиды, точки, дома, аспекты, GPT‑ready JSON

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, g, jsonify, request
from timezonefinder import TimezoneFinder

try:
    import swisseph as swe
except Exception:
    import pyswisseph as swe  # type: ignore

EPHE_PATH = os.environ.get("EPHE_PATH", "/app/ephe").rstrip("/")
EPHE_REQUIRED_GLOBS = os.environ.get(
    "EPHE_REQUIRED_GLOBS",
    "(sepl_*.se1|sepm*.se1),(semo_*.se1|sepm*.se1),(seas_*.se1|sepm*.se1)",
)
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "120"))

print(f"[app] EPHE_PATH = {EPHE_PATH}")
swe.set_ephe_path(EPHE_PATH)  # временно, до мульти‑пути

# ----- TZ -----
try:
    from zoneinfo import ZoneInfo
except Exception:
    from pytz import timezone as PytzTZ
    class ZoneInfo:
        def __init__(self, name): self._tz = PytzTZ(name)
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
        s = s.strip()
        sign = 1 if s[0] == "+" else -1
        hhmm = s[1:].split(":")
        hh, mm = (hhmm[0], "00") if len(hhmm) == 1 else hhmm
        return FixedOffsetTZ(sign * (int(hh)*60 + int(mm)))
    except Exception:
        return None

app = Flask(__name__)
_tf = TimezoneFinder(in_memory=True)

@lru_cache(maxsize=16384)
def _tf_tz_at(lat5: float, lon5: float) -> Optional[str]:
    return _tf.timezone_at(lng=lon5, lat=lat5) or _tf.closest_timezone_at(lng=lon5, lat=lat5)

def find_timezone_name(lat: float, lon: float) -> Optional[str]:
    return _tf_tz_at(round(lat, 5), round(lon, 5))

@lru_cache(maxsize=4096)
def _zoneinfo_cached(name: str):
    return ZoneInfo(name)

# ----- Rate limit -----
_rate_state: Dict[str, Dict[str, float]] = {}
_BUCKET_SIZE = max(RATE_LIMIT_PER_MIN, 1)
_REFILL_PER_SEC = RATE_LIMIT_PER_MIN / 60.0
def _rate_check(ip: str) -> bool:
    now = time.time()
    st = _rate_state.get(ip)
    if not st:
        _rate_state[ip] = {"tokens": _BUCKET_SIZE - 1, "ts": now}
        return True
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
    if request.path in ("/health", "/ephe-check", "/calc/test"):
        return
    if not _rate_check(ip):
        return jsonify({"ok": False, "error": "Rate limit exceeded", "limit_per_min": RATE_LIMIT_PER_MIN}), 429

# ----- Ephemeris multipath + проверка с ИЛИ -----
def _glob_or_casefold(root: Path, pattern_or: str) -> List[str]:
    pats = []
    s = pattern_or.strip()
    if "|" in s:
        if s.startswith("(") and s.endswith(")"):
            s = s[1:-1]
        pats = [p.strip() for p in s.split("|") if p.strip()]
    else:
        pats = [s]

    all_files = [p for p in root.rglob("*") if p.is_file()]
    names_low = [(p, p.name.lower()) for p in all_files]
    out: List[str] = []
    for pat in pats:
        pl = pat.lower()
        def match(name: str) -> bool:
            if pl.startswith("sepl_") and pl.endswith(".se1"): return name.startswith("sepl_") and name.endswith(".se1")
            if pl.startswith("semo_") and pl.endswith(".se1"): return name.startswith("semo_") and name.endswith(".se1")
            if pl.startswith("seas_") and pl.endswith(".se1"): return name.startswith("seas_") and name.endswith(".se1")
            if pl.startswith("sepm") and pl.endswith(".se1"):  return name.startswith("sepm")  and name.endswith(".se1")
            if pl == "*.se1": return name.endswith(".se1")
            return False
        for p, low in names_low:
            if match(low): out.append(str(p))
    return sorted(set(out))

def _have_required_ephe() -> bool:
    root = Path(EPHE_PATH)
    if not root.exists(): return False
    pats = [g.strip() for g in EPHE_REQUIRED_GLOBS.split(",") if g.strip()]
    if not pats: return True
    for pat in pats:
        if not _glob_or_casefold(root, pat):
            return False
    return True

def _build_swisseph_search_path() -> str:
    dirs = set()
    root = Path(EPHE_PATH)
    if not root.exists(): return EPHE_PATH
    for f in root.rglob("*.se1"):
        if f.is_file(): dirs.add(str(f.parent))
    dirs.add(EPHE_PATH)
    return os.pathsep.join(sorted(dirs))

def _reset_swisseph_path():
    multi = _build_swisseph_search_path()
    swe.set_ephe_path(multi)
    print("[EPHE] swisseph search path set to:", multi)

_reset_swisseph_path()

# ----- Helpers -----
SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]

def norm360(x: float) -> float:
    v = x % 360.0
    return v if v >= 0 else v + 360.0

def angdist(a: float, b: float) -> float:
    d = abs(norm360(a) - norm360(b))
    return d if d <= 180.0 else 360.0 - d

def sign_name(lon: float) -> str:
    return SIGNS[int(norm360(lon)//30) % 12]

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
    try: datetime.fromisoformat(date_str)
    except Exception: raise ValueError("date must be ISO YYYY-MM-DD")

def validate_time(time_str: str) -> None:
    parts = time_str.split(":")
    if len(parts) not in (2,3): raise ValueError("time must be HH:MM or HH:MM:SS")

# ----- Swiss helpers -----
def compute_planets(jd_ut: float, flags: int) -> Tuple[Dict[str, Dict[str, float]], Dict[str, float]]:
    obj_codes: Dict[str, int] = {
        "Sun": swe.SUN, "Moon": swe.MOON, "Mercury": swe.MERCURY, "Venus": swe.VENUS,
        "Mars": swe.MARS, "Jupiter": swe.JUPITER, "Saturn": swe.SATURN,
        "Uranus": swe.URANUS, "Neptune": swe.NEPTUNE, "Pluto": swe.PLUTO,
        "Mean Node": swe.MEAN_NODE, "True Node": swe.TRUE_NODE,
        "Lilith (Mean)": swe.MEAN_APOG, "Lilith (Oscu)": swe.OSCU_APOG,
        "Chiron": getattr(swe, "CHIRON", 15),
        "Pholus": getattr(swe, "PHOLUS", 16),
        "Ceres": getattr(swe, "CERES", 17),
        "Pallas": getattr(swe, "PALLAS", 18),
        "Juno": getattr(swe, "JUNO", 19),
        "Vesta": getattr(swe, "VESTA", 20),
    }

    # EPHE_ASTEROIDS="433:Eros,7066:Nessus,136199:Eris"
    extra = os.environ.get("EPHE_ASTEROIDS", "").strip()
    if extra:
        ast_off = getattr(swe, "AST_OFFSET", getattr(swe, "SE_AST_OFFSET", None))
        if isinstance(ast_off, int):
            for chunk in extra.split(","):
                if not chunk.strip(): continue
                parts = chunk.strip().split(":", 1)
                try: num = int(parts[0])
                except ValueError: continue
                name = parts[1].strip() if len(parts)==2 and parts[1].strip() else f"Asteroid {num}"
                obj_codes[name] = ast_off + num

    res: Dict[str, Dict[str, float]] = {}
    longitudes: Dict[str, float] = {}
    for name, code in obj_codes.items():
        try:
            pos, _ = swe.calc_ut(jd_ut, code, flags)
            lon, lat, dist = pos[0], pos[1], pos[2]
            speed_lon = pos[3] if len(pos) > 3 else None
            lon_n = norm360(lon)
            res[name] = {"lon": lon_n, "lat": lat, "dist": dist, "speed_lon": speed_lon}
            if isinstance(speed_lon, (int,float)): res[name]["retrograde"] = speed_lon < 0
            longitudes[name] = lon_n
        except Exception as e:
            res[name] = {"error": str(e)}
    return res, longitudes

def _houses_ex_safe(jd_ut: float, lat: float, lon: float, hs: str, flags: int):
    try: return swe.houses_ex(jd_ut, lat, lon, hs, flags)
    except TypeError: return swe.houses_ex(jd_ut, lat, lon, hs.encode("ascii"), flags)

def compute_houses(jd_ut: float, flags: int, lat: float, lon: float, hs: str):
    try:
        houses, ascmc = _houses_ex_safe(jd_ut, lat, lon, hs, flags)
        cusps = {str(i+1): norm360(houses[i]) for i in range(12)}
        asc = norm360(ascmc[0]); mc = norm360(ascmc[1]); armc = ascmc[2]
        vertex = norm360(ascmc[3])
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
    return norm360(asc + (moon - sun) if is_day else asc - moon + sun)

def spirit_longitude(is_day: bool, asc: float, sun: float, moon: float) -> float:
    return norm360(asc + (sun - moon) if is_day else asc - sun + moon)

def lot_of_eros(is_day: bool, asc: float, venus: float, spirit: float) -> float:
    return norm360(asc + venus - spirit) if is_day else norm360(asc + spirit - venus)

def lot_of_courage(is_day: bool, asc: float, mars: float, spirit: float) -> float:
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
                    mc: Optional[float],
                    skip_trivial_geom: bool) -> List[Dict[str, Any]]:
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

    def is_trivial_geom(a: str, b: str) -> bool:
        if not skip_trivial_geom: return False
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
                if x in ("Chiron","Pholus"): return "chiron"
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
    for k in ("lat","dist","speed_lon","retrograde"):
        if k in data and data[k] is not None: out[k] = data[k]
    if house_pos is not None:
        out["house_float"] = round(house_pos, 4)
        out["house"] = int(math.ceil(house_pos)) if house_pos>0 else 1
    if "error" in data: out["error"] = data["error"]
    return out

# ----- errors -----
@app.errorhandler(404)
def _404(_e): return jsonify({"ok": False, "error": "Not found"}), 404

@app.errorhandler(500)
def _500(e): return jsonify({"ok": False, "error": "Internal error", "detail": str(e)}), 500

# ----- diagnostics -----
@app.get("/health")
def health() -> Any:
    root = Path(EPHE_PATH)
    pats = [g.strip() for g in EPHE_REQUIRED_GLOBS.split(",") if g.strip()]
    required_ok = root.exists() and all(_glob_or_casefold(root, p) for p in pats)
    files = [str(p) for p in root.rglob("*.se1")][:25] if root.exists() else []
    return jsonify({
        "ok": True,
        "version": "3.6.0",
        "rate_limit_per_min": RATE_LIMIT_PER_MIN,
        "ephe": {"path": EPHE_PATH, "required_globs": EPHE_REQUIRED_GLOBS,
                 "required_ok": required_ok, "files_sample": files}
    })

@app.get("/ephe-check")
def ephe_check():
    root = Path(EPHE_PATH)
    details = {pat: _glob_or_casefold(root, pat) for pat in [g.strip() for g in EPHE_REQUIRED_GLOBS.split(",") if g.strip()]}
    return jsonify({"path": EPHE_PATH, "required_globs": EPHE_REQUIRED_GLOBS
