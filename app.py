from __future__ import annotations
import os, math
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

from flask import Flask, request, jsonify
from timezonefinder import TimezoneFinder

# Swiss Ephemeris (pyswisseph)
try:
    import swisseph as swe
except Exception:
    import pyswisseph as swe  # type: ignore

# === Paths / init ===
EPHE_PATH = os.environ.get("EPHE_PATH", "/app/ephe")
swe.set_ephe_path(EPHE_PATH)
print(f"[app] Swiss Ephemeris path = {EPHE_PATH}")

# tz handling
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    from pytz import timezone as PytzTZ
    class ZoneInfo:
        def __init__(self, name):
            self._tz = PytzTZ(name)
        def utcoffset(self, dt):
            return self._tz.utcoffset(dt)
        def dst(self, dt):
            return self._tz.dst(dt)
        def tzname(self, dt):
            return self._tz.tzname(dt)

app = Flask(__name__)
tf = TimezoneFinder(in_memory=True)

# === Helpers ===
def norm360(x: float) -> float:
    v = x % 360.0
    return v if v >= 0 else v + 360.0

def angdist(a: float, b: float) -> float:
    """Minimal angular distance 0..180."""
    d = abs(norm360(a) - norm360(b))
    return d if d <= 180.0 else 360.0 - d

def find_timezone_name(lat: float, lng: float) -> Optional[str]:
    name = tf.timezone_at(lng=lng, lat=lat)
    if not name:
        name = tf.closest_timezone_at(lng=lng, lat=lat)
    return name

def parse_date_time(date_str: Optional[str], time_str: Optional[str]) -> datetime:
    base = date_str or datetime.utcnow().date().isoformat()
    t = time_str or "12:00"
    # поддерживаем HH:MM и HH:MM:SS
    if len(t.split(":")) == 2:
        t = f"{t}:00"
    return datetime.fromisoformat(f"{base}T{t}")

# --- Swiss helpers ---
def compute_planets(jd_ut: float, flags: int) -> Tuple[Dict[str, Dict[str, float]], Dict[str, float]]:
    obj_codes = {
        "Sun": swe.SUN, "Moon": swe.MOON, "Mercury": swe.MERCURY, "Venus": swe.VENUS,
        "Mars": swe.MARS, "Jupiter": swe.JUPITER, "Saturn": swe.SATURN,
        "Uranus": swe.URANUS, "Neptune": swe.NEPTUNE, "Pluto": swe.PLUTO,
        "Chiron": swe.CHIRON,
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

def compute_houses(jd_ut: float, flags: int, lat: float, lng: float, hs: str):
    try:
        # ВАЖНО: порядок аргументов — (jd_ut, lat, lon, hsys[, iflag])
        houses, ascmc = swe.houses_ex(jd_ut, lat, lng, hs.encode("ascii"), flags)
        house_cusps = {str(i+1): norm360(houses[i]) for i in range(12)}
        asc = norm360(ascmc[0])
        mc  = norm360(ascmc[1])
        armc = ascmc[2]  # понадобится для house_pos
        # наклон эклиптики (истинный)
        ecl, _ = swe.calc_ut(jd_ut, swe.ECL_NUT, 0)
        eps_true = ecl[0]
        return house_cusps, asc, mc, armc, eps_true, None
    except Exception as e:
        return {}, None, None, None, None, str(e)

def house_of_body(armc: float, geolat: float, eps: float, hs: str, lon: float, lat_ecl: float = 0.0) -> Optional[float]:
    try:
        return swe.house_pos(armc, geolat, eps, hs.encode("ascii"), lon, lat_ecl)
    except Exception:
        return None

def detect_day_chart(armc: float, geolat: float, eps: float, hs: str, sun_lon: float) -> bool:
    """Солнце над горизонтом => дома 7..12."""
    pos = house_of_body(armc, geolat, eps, hs, sun_lon, 0.0)
    if pos is None:
        return True  # по умолчанию — day
    return pos > 6.0

def fortune_longitude(is_day: bool, asc: float, sun: float, moon: float) -> float:
    L = asc + (moon - sun) if is_day else asc - moon + sun
    return norm360(L)

def aspect_type(angle: float) -> Optional[str]:
    mapping = {0: "conjunction", 60: "sextile", 90: "square", 120: "trine", 180: "opposition"}
    return mapping.get(int(angle))

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

    # add angles
    if include_angles and asc is not None and mc is not None:
        longitudes["ASC"] = asc
        longitudes["MC"] = mc
        speeds.setdefault("ASC", None)
        speeds.setdefault("MC", None)
        names += ["ASC", "MC"]

    # add cusps
    if include_cusps and cusps:
        for i in range(1, 13):
            key = f"Cusp {i}"
            longitudes[key] = cusps.get(str(i))
            speeds.setdefault(key, None)
            names.append(key)

    aspects: List[Dict[str, Any]] = []
    N = len(names)
    for i in range(N):
        for j in range(i+1, N):
            a, b = names[i], names[j]
            la, lb = longitudes.get(a), longitudes.get(b)
            if la is None or lb is None:
                continue
            delta = angdist(la, lb)  # 0..180

            def orb_for(a: str, b: str) -> float:
                def cat(x: str) -> str:
                    if x in ("Sun", "Moon"): return "lum"
                    if x.startswith("Cusp") or x in ("ASC", "MC"): return "angle"
                    if "Node" in x: return "node"
                    if "Chiron" in x: return "chiron"
                    if "Lilith" in x: return "lilith"
                    return "main"
                return max(orbs.get(cat(a), orbs["main"]), orbs.get(cat(b), orbs["main"]))

            orb_allow = orb_for(a, b)
            for ang in angles:
                if abs(delta - ang) <= orb_allow:
                    aspects.append({
                        "a": a, "b": b,
                        "type": aspect_type(ang),
                        "exact": ang,
                        "delta": round(delta, 4),
                        "orb": round(abs(delta - ang), 4),
                        "speed_a": speeds.get(a),
                        "speed_b": speeds.get(b),
                    })
                    break
    aspects.sort(key=lambda x: x["orb"])
    return aspects

# === Endpoints ===
@app.get("/health")
def health() -> Any:
    return jsonify({"ok": True, "version": "2.0.0"})

@app.get("/timezone")
def timezone_endpoint():
    try:
        lat = float(request.args.get("lat", ""))
        lng = float(request.args.get("lng", ""))
    except Exception:
        return jsonify({"error": "lat/lng required as numbers"}), 400

    date_str = request.args.get("date")
    time_str = request.args.get("time")
    tz_name = request.args.get("tz")

    if not tz_name:
        tz_name = find_timezone_name(lat, lng)
    if not tz_name:
        return jsonify({"error": "Cannot resolve timezone for given coordinates"}), 422

    dt_local = parse_date_time(date_str, time_str)

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return jsonify({"error": f"Unknown timezone '{tz_name}'"}), 422

    dt_local = dt_local.replace(tzinfo=tz)
    offset = dt_local.utcoffset()
    dst_off = dt_local.dst()
    off_seconds = int(offset.total_seconds()) if offset else 0
    dst_seconds = int(dst_off.total_seconds()) if dst_off else 0

    sign = "+" if off_seconds >= 0 else "-"
    abs_sec = abs(off_seconds)
    hh, rem = divmod(abs_sec, 3600)
    mm = rem // 60
    utc_offset_str = f"{sign}{hh:02d}:{mm:02d}"

    dt_utc = dt_local.astimezone(timezone.utc)

    return jsonify({
        "zoneName": tz_name,
        "gmtOffsetSeconds": off_seconds,
        "utcOffsetString": utc_offset_str,
        "dstSeconds": dst_seconds,
        "atLocal": dt_local.replace(microsecond=0).isoformat(),
        "atUTC": dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    })

@app.get("/swisseph")
def swisseph_endpoint():
    date_str = request.args.get("date")
    time_str = request.args.get("time")
    if not date_str or not time_str:
        return jsonify({"error": "date (YYYY-MM-DD) and time (HH:MM[:SS]) are required"}), 400

    try:
        lat = float(request.args.get("lat", ""))
        lng = float(request.args.get("lng", ""))
    except Exception:
        return jsonify({"error": "lat/lng required as numbers"}), 400

    tz_name = request.args.get("tz")
    if not tz_name:
        tz_name = find_timezone_name(lat, lng)
        if not tz_name:
            return jsonify({"error": "Cannot resolve timezone for given coordinates"}), 422

    # Orbs (deg)
    orbs = {
        "main": float(request.args.get("orb_main", 6)),
        "lum": float(request.args.get("orb_lum", 8)),
        "angle": float(request.args.get("orb_angle", 4)),
        "node": float(request.args.get("orb_node", 3)),
        "chiron": float(request.args.get("orb_chiron", 3)),
        "lilith": float(request.args.get("orb_lilith", 3)),
    }
    include_angles = request.args.get("include_angles", "true").lower() == "true"
    include_cusps  = request.args.get("include_cusps", "true").lower() == "true"

    # Build local and UTC datetime
    dt_local = parse_date_time(date_str, time_str).replace(tzinfo=ZoneInfo(tz_name))
    dt_utc = dt_local.astimezone(timezone.utc)

    year, month, day = dt_utc.year, dt_utc.month, dt_utc.day
    hour = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0

    # Swiss flags
    sidereal = request.args.get("sidereal", "false").lower() == "true"
    flags = swe.FLG_SWIEPH | swe.FLG_SPEED
    if sidereal:
        flags |= swe.FLG_SIDEREAL
        swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)

    # Julian day UT
    jd_ut = swe.julday(year, month, day, hour, swe.GREG_CAL)

    # Planets
    planets, longitudes = compute_planets(jd_ut, flags)

    # Houses (Placidus default; hs param)
    hs = (request.args.get("hs") or "P").upper()
    cusps, asc, mc, armc, eps_true, err = compute_houses(jd_ut, flags, lat, lng, hs)
    if err:
        return jsonify({"error": f"houses_ex failed: {err}"}), 500

    # Day/Night & Part of Fortune
    pof = None
    day_chart = None
    if "Sun" in longitudes and asc is not None and armc is not None and eps_true is not None:
        day_chart = detect_day_chart(armc, lat, eps_true, hs, longitudes["Sun"])
        pof = fortune_longitude(day_chart, asc, longitudes["Sun"], longitudes.get("Moon", 0.0))

    # Speeds (для аспектов)
    speeds = {name: planets.get(name, {}).get("speed_lon") for name in longitudes.keys()}

    # Aspect set: planets + nodes + lilith + chiron (+ angles/cusps по флагам)
    include_names = list(longitudes.keys())
    if pof is not None:
        longitudes["Part of Fortune"] = pof
        speeds["Part of Fortune"] = None
        include_names.append("Part of Fortune")

    aspects = compute_aspects(longitudes, speeds, include_names, orbs, include_cusps, cusps, include_angles, asc, mc)

    return jsonify({
        "input": {
            "date_local": date_str,
            "time_local": time_str,
            "tz": tz_name,
            "lat": lat,
            "lng": lng,
            "sidereal": sidereal,
            "house_system": hs
        },
        "utc": dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "jd_ut": jd_ut,
        "planets": planets,
        "houses": {
            "cusps": cusps,
            "asc": asc,
            "mc": mc
        },
        "lots": {
            "part_of_fortune": pof,
            "day_chart": day_chart
        },
        "aspects": aspects
    })

if __name__ == "__main__":
    # Порт для Docker/Render/где угодно
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
