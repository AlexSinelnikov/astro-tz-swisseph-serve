from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify
from timezonefinder import TimezoneFinder

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:
    # Fallback for older envs
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

# Swiss Ephemeris
try:
    import swisseph as swe
except Exception:
    # Some distributions use 'pyswisseph' as package name
    import pyswisseph as swe  # type: ignore

app = Flask(__name__)

tf = TimezoneFinder(in_memory=True)

PLANETS = {
    "Sun": swe.SUN,
    "Moon": swe.MOON,
    "Mercury": swe.MERCURY,
    "Venus": swe.VENUS,
    "Mars": swe.MARS,
    "Jupiter": swe.JUPITER,
    "Saturn": swe.SATURN,
    "Uranus": swe.URANUS,
    "Neptune": swe.NEPTUNE,
    "Pluto": swe.PLUTO,
}

def find_timezone_name(lat: float, lng: float) -> Optional[str]:
    name = tf.timezone_at(lng=lng, lat=lat)
    if not name:
        name = tf.closest_timezone_at(lng=lng, lat=lat)
    return name

def parse_date_time(date_str: Optional[str], time_str: Optional[str]) -> datetime:
    # date: YYYY-MM-DD, time: HH:MM (24h). Defaults to today's date 12:00 if omitted.
    if not date_str:
        # default to today UTC noon to avoid DST ambiguity
        base = datetime.utcnow().date().isoformat()
    else:
        base = date_str
    if not time_str:
        t = "12:00"
    else:
        t = time_str
    # Construct naive local datetime (we will attach tz later)
    return datetime.fromisoformat(f"{base}T{t}:00")

@app.get("/health")
def health() -> Any:
    return jsonify({"ok": True, "version": "1.0.0"})

@app.get("/timezone")
def timezone_endpoint():
    """Return historical-correct timezone data for given lat/lng and date/time.
    Query params:
      lat, lng (required)
      date=YYYY-MM-DD (optional, default today)
      time=HH:MM      (optional, default 12:00)
    Response:
      {
        "zoneName": "Europe/Moscow",
        "gmtOffsetSeconds": 10800,
        "utcOffsetString": "+03:00",
        "dstSeconds": 0,
        "atLocal": "1994-05-17T04:40:00",
        "atUTC": "1994-05-17T01:40:00Z"
      }
    """
    try:
        lat = float(request.args.get("lat", ""))
        lng = float(request.args.get("lng", ""))
    except Exception:
        return jsonify({"error": "lat/lng required as numbers"}), 400

    date_str = request.args.get("date")  # YYYY-MM-DD
    time_str = request.args.get("time")  # HH:MM

    tz_name = request.args.get("tz")  # allow direct tz if user already knows it
    if not tz_name:
        tz_name = find_timezone_name(lat, lng)
    if not tz_name:
        return jsonify({"error": "Cannot resolve timezone for given coordinates"}), 422

    dt_local = parse_date_time(date_str, time_str)

    try:
        tz = ZoneInfo(tz_name)  # IANA tz
    except Exception:
        return jsonify({"error": f"Unknown timezone '{tz_name}'"}), 422

    # Attach tz and compute offsets
    dt_local = dt_local.replace(tzinfo=tz)
    offset = dt_local.utcoffset() or dt_local - dt_local.astimezone(timezone.utc).replace(tzinfo=tz)
    dst_off = dt_local.dst() or (offset - (dt_local.replace(tzinfo=None).astimezone(timezone.utc) - dt_local))
    # Fallbacks for robustness
    off_seconds = int(offset.total_seconds()) if offset else 0
    dst_seconds = int(dst_off.total_seconds()) if dst_off else 0

    # Format +HH:MM
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
    """Compute basic Swiss Ephemeris data.
    Query params (required):
      date=YYYY-MM-DD
      time=HH:MM
      lat, lng  (floats, in degrees)
      tz (optional IANA tzid). If omitted, will be derived from lat/lng.
    Optional:
      hs=P (house system, default Placidus)
      sidereal=false (true/false) - if true, use Lahiri ayanamsa
    """
    date_str = request.args.get("date")
    time_str = request.args.get("time")
    if not date_str or not time_str:
        return jsonify({"error": "date (YYYY-MM-DD) and time (HH:MM) are required"}), 400

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

    # Local datetime with tz
    dt_local = parse_date_time(date_str, time_str).replace(tzinfo=ZoneInfo(tz_name))
    dt_utc = dt_local.astimezone(timezone.utc)

    year, month, day = dt_utc.year, dt_utc.month, dt_utc.day
    hour = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0

    # Swiss Ephemeris settings
    swe.set_ephe_path(os.environ.get("EPHE_PATH", ""))  # can mount SE files if needed

    sidereal = request.args.get("sidereal", "false").lower() == "true"
    if sidereal:
        swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
    else:
        swe.set_sid_mode(swe.SIDM_FAGAN_BRADLEY, 0, 0)  # will be ignored in tropical

    # Julian Day in UT
    jd_ut = swe.julday(year, month, day, hour, swe.GREG_CAL)

    # Planets positions (tropical by default)
    flags = swe.FLG_SWIEPH | swe.FLG_SPEED
    if sidereal:
        flags |= swe.FLG_SIDEREAL

    planets = {}
    for name, code in PLANETS.items():
        lon, latp, dist, lon_speed = None, None, None, None
        try:
            pos, ret = swe.calc_ut(jd_ut, code, flags)
            lon, latp, dist, lon_speed = pos[0], pos[1], pos[2], pos[3] if len(pos) > 3 else None
        except Exception as e:
            planets[name] = {"error": str(e)}
            continue
        planets[name] = {
            "lon": lon,       # ecliptic longitude in degrees
            "lat": latp,      # ecliptic latitude
            "dist": dist,
            "speed_lon": lon_speed,
        }

    # Houses (Placidus by default)
    hs = (request.args.get("hs") or "P").upper()
    try:
        houses, ascmc = swe.houses_ex(jd_ut, flags, lat, lng, hs)
        house_cusps = {str(i+1): houses[i] for i in range(12)}
        asc = ascmc[0]
        mc = ascmc[1]
    except Exception as e:
        house_cusps = {}
        asc, mc = None, None

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
            "cusps": house_cusps,
            "asc": asc,
            "mc": mc
        }
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
