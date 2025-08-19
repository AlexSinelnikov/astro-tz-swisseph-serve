from __future__ import annotations
import os, math, traceback, threading
from typing import Dict, Any, List, Optional
from datetime import datetime

from flask import Flask, request, jsonify
from timezonefinder import TimezoneFinder
import swisseph as swe

from fetch_ephe import ensure_ephe

EPHE_PATH = os.environ.get("EPHE_PATH", "/app/ephe")

app = Flask(__name__)

# --- Инициализация в фоне ---
READY = False
INIT_ERROR: Optional[str] = None

def _bg_init():
    global READY, INIT_ERROR
    try:
        print("[app] init: ensure_ephe() starting...", flush=True)
        ensure_ephe()                      # докачка/распаковка
        swe.set_ephe_path(EPHE_PATH)       # путь к *.se1
        print(f"[app] Swiss Ephemeris path = {EPHE_PATH}", flush=True)
        READY = True
        print("[app] init: READY", flush=True)
    except Exception as e:
        INIT_ERROR = str(e)
        print("[app] init ERROR:", INIT_ERROR, flush=True)

threading.Thread(target=_bg_init, daemon=True).start()

# --- TZ utils ---
try:
    from zoneinfo import ZoneInfo
    def get_tz(tz_name_or_offset: str, lat: float=None, lon: float=None):
        s = tz_name_or_offset.strip()
        if s.startswith(("+","-")) and ":" in s:
            sign = 1 if s[0] == "+" else -1
            hh, mm = s[1:].split(":")
            return ("FIXED_OFFSET", sign*(int(hh)*60+int(mm)))
        return ZoneInfo(s)
except Exception:
    from pytz import timezone as PytzTZ
    def get_tz(tz_name_or_offset: str, lat: float=None, lon: float=None):
        s = tz_name_or_offset.strip()
        if s.startswith(("+","-")) and ":" in s:
            sign = 1 if s[0] == "+" else -1
            hh, mm = s[1:].split(":")
            return ("FIXED_OFFSET", sign*(int(hh)*60+int(mm)))
        return PytzTZ(s)

tf = TimezoneFinder()
def guess_iana_tz(lat: float, lon: float) -> str | None:
    try: return tf.timezone_at(lat=lat, lng=lon)
    except Exception: return None

def to_julday_utc(date_str: str, time_str: str, tz_obj, lat: float, lon: float) -> float:
    yyyy, mm, dd = [int(x) for x in date_str.split("-")]
    hh, mi = [int(x) for x in time_str.split(":")]
    from datetime import timedelta
    naive = datetime(yyyy, mm, dd, hh, mi)
    if isinstance(tz_obj, tuple) and tz_obj and tz_obj[0] == "FIXED_OFFSET":
        utc_dt = naive - timedelta(minutes=tz_obj[1])
    else:
        try:
            local_dt = naive.replace(tzinfo=tz_obj)
        except Exception:
            local_dt = tz_obj.localize(naive)  # type: ignore
        utc_dt = local_dt.astimezone(tz=None).replace(tzinfo=None)
    y, m, d = utc_dt.year, utc_dt.month, utc_dt.day
    h = utc_dt.hour + utc_dt.minute/60.0 + utc_dt.second/3600.0
    return swe.julday(y, m, d, h, swe.GREG_CAL)

def calc_planets(jd_ut: float) -> List[Dict[str, Any]]:
    bodies = [
        ("Sun", swe.SUN), ("Moon", swe.MOON), ("Mercury", swe.MERCURY),
        ("Venus", swe.VENUS), ("Mars", swe.MARS), ("Jupiter", swe.JUPITER),
        ("Saturn", swe.SATURN), ("Uranus", swe.URANUS), ("Neptune", swe.NEPTUNE),
        ("Pluto", swe.PLUTO), ("Node", swe.TRUE_NODE), ("Lilith", swe.MEAN_APOG),
    ]
    flags = swe.FLG_SWIEPH | swe.FLG_SPEED
    res = []
    for name, code in bodies:
        pos, ret = swe.calc_ut(jd_ut, code, flags)
        res.append({"body": name, "lon": pos[0], "lat": pos[1], "dist": pos[2], "speed": pos[3], "retrograde": pos[3] < 0})
    return res

def calc_houses(jd_ut: float, lat: float, lon: float, hsys: str="P") -> Dict[str, Any]:
    cusps, ascmc = swe.houses(jd_ut, lat, lon, hsys.encode("ascii"))
    return {
        "system": hsys,
        "cusps": {str(i+1): cusps[i] for i in range(12)},
        "angles": {"ASC": ascmc[0], "MC": ascmc[1], "ARMC": ascmc[2], "Vertex": ascmc[3]}
    }

@app.get("/healthz")
def healthz():
    # Railway healthcheck должен получить 200 даже во время инициализации
    return ("ok", 200)

@app.get("/status")
def status():
    return jsonify({"ready": READY, "error": INIT_ERROR, "ephe_path": EPHE_PATH})

@app.get("/")
def root():
    return jsonify({"name": "ИИ-Астролог API", "version": "1.0", "ready": READY})

@app.post("/calc")
def calc():
    if not READY:
        return jsonify({"error": "Ephemeris are not ready yet. Try again shortly.", "status": {"ready": READY, "error": INIT_ERROR}}), 503
    try:
        data = request.get_json(force=True) or {}
        date_str = data["date"]; time_str = data["time"]
        lat = float(data["lat"]); lon = float(data["lon"])
        hsys = data.get("hsys", "P")
        tz_in = data.get("tz")
        if tz_in:
            tz_obj = get_tz(tz_in, lat, lon)
        else:
            if data.get("guess_tz", True):
                tzname = guess_iana_tz(lat, lon)
                if not tzname: return jsonify({"error": "Cannot guess timezone, pass 'tz'."}), 400
                tz_obj = get_tz(tzname, lat, lon)
            else:
                return jsonify({"error": "Missing 'tz'."}), 400
        jd_ut = to_julday_utc(date_str, time_str, tz_obj, lat, lon)
        return jsonify({"input":{"date":date_str,"time":time_str,"lat":lat,"lon":lon,"tz":tz_in or "guessed","hsys":hsys},
                        "julday_ut": jd_ut, "planets": calc_planets(jd_ut), "houses": calc_houses(jd_ut, lat, lon, hsys)})
    except KeyError as ke:
        return jsonify({"error": f"Missing field: {str(ke)}"}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
