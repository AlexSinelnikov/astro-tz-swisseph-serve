from __future__ import annotations
import os, math, traceback
from typing import Dict, Any, List
from datetime import datetime

from flask import Flask, request, jsonify
from timezonefinder import TimezoneFinder

import swisseph as swe

# === ЭТАП 1. Гарантируем эфемериды ===
# Важно: ensure_ephe() скачает/проверит архив и подготовит EPHE_PATH до set_ephe_path().
from fetch_ephe import ensure_ephe

EPHE_PATH = os.environ.get("EPHE_PATH", "/app/ephe")

def init_ephe():
    ensure_ephe()
    swe.set_ephe_path(EPHE_PATH)
    print(f"[app] Swiss Ephemeris path = {EPHE_PATH}", flush=True)

init_ephe()

# === ЭТАП 2. Flask-приложение ===
app = Flask(__name__)

# Вспом. TZ
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    def get_tz(tz_name_or_offset: str, lat: float = None, lon: float = None):
        # Если приходит "+06:00" — преобразуем в фиктивную зону с заданным смещением нельзя просто через ZoneInfo
        # поэтому лучше требовать IANA. Но дадим fallback на фиксированный offset в часах/HH:MM.
        s = tz_name_or_offset.strip()
        if s.startswith(("+", "-")) and ":" in s:
            sign = 1 if s[0] == "+" else -1
            hh, mm = s[1:].split(":")
            offset_minutes = sign * (int(hh) * 60 + int(mm))
            # Упрощённый режим: вернём None и будем считать время как UTC+offset при расчете юлианской даты.
            return ("FIXED_OFFSET", offset_minutes)
        else:
            return ZoneInfo(s)
except Exception:
    # Fallback на pytz если нужно
    from pytz import timezone as PytzTZ
    def get_tz(tz_name_or_offset: str, lat: float = None, lon: float = None):
        s = tz_name_or_offset.strip()
        if s.startswith(("+", "-")) and ":" in s:
            sign = 1 if s[0] == "+" else -1
            hh, mm = s[1:].split(":")
            offset_minutes = sign * (int(hh) * 60 + int(mm))
            return ("FIXED_OFFSET", offset_minutes)
        else:
            return PytzTZ(s)

tf = TimezoneFinder()

def guess_iana_tz(lat: float, lon: float) -> str | None:
    try:
        return tf.timezone_at(lat=lat, lng=lon)
    except Exception:
        return None

def to_julday_utc(date_str: str, time_str: str, tz_obj, lat: float, lon: float) -> float:
    """
    Принимает локальные date/time + tz, возвращает юлианскую дату в UTC.
    tz_obj может быть ZoneInfo или ("FIXED_OFFSET", minutes).
    """
    yyyy, mm, dd = [int(x) for x in date_str.split("-")]
    hh, mi = [int(x) for x in time_str.split(":")]
    naive = datetime(yyyy, mm, dd, hh, mi)

    if isinstance(tz_obj, tuple) and tz_obj and tz_obj[0] == "FIXED_OFFSET":
        # Конвертируем локальное время с фиксированным смещением в UTC вручную
        offset_min = tz_obj[1]
        # Локальное = UTC + offset => UTC = Локальное - offset
        from datetime import timedelta
        utc_dt = naive - timedelta(minutes=offset_min)
    else:
        try:
            local_dt = naive.replace(tzinfo=tz_obj)  # ZoneInfo ok
        except Exception:
            # pytz: localize
            local_dt = tz_obj.localize(naive)  # type: ignore
        utc_dt = local_dt.astimezone(datetime.utcfromtimestamp(0).tzinfo)  # tzinfo=None -> naive UTC
        # Приведём к naive UTC
        utc_dt = utc_dt.replace(tzinfo=None)

    # Получаем UTC компоненты
    y, m, d = utc_dt.year, utc_dt.month, utc_dt.day
    h = utc_dt.hour + utc_dt.minute/60.0 + utc_dt.second/3600.0
    # Юлианская дата:
    jd_ut = swe.julday(y, m, d, h, swe.GREG_CAL)
    return jd_ut

def calc_planets(jd_ut: float) -> List[Dict[str, Any]]:
    """
    Рассчитываем основные тела. Возвращаем градусы элонгаций в эклиптической системе (тропический зодиак).
    """
    # Планеты и точки
    bodies = [
        ("Sun", swe.SUN),
        ("Moon", swe.MOON),
        ("Mercury", swe.MERCURY),
        ("Venus", swe.VENUS),
        ("Mars", swe.MARS),
        ("Jupiter", swe.JUPITER),
        ("Saturn", swe.SATURN),
        ("Uranus", swe.URANUS),
        ("Neptune", swe.NEPTUNE),
        ("Pluto", swe.PLUTO),
        ("Node", swe.TRUE_NODE),      # Лунный узел (истинный)
        ("Lilith", swe.MEAN_APOG),    # Чёрная Луна (средний апогей)
    ]
    flags = swe.FLG_SWIEPH | swe.FLG_SPEED  # Swiss ephemeris + скорость
    res = []
    for name, code in bodies:
        pos, ret = swe.calc_ut(jd_ut, code, flags)
        # pos[0] — долгота, pos[1] — широта, pos[2] — расстояние (AU), pos[3] — скорость долготы
        res.append({
            "body": name,
            "lon": pos[0],
            "lat": pos[1],
            "dist": pos[2],
            "speed": pos[3],
            "retrograde": pos[3] < 0.0
        })
    return res

def calc_houses(jd_ut: float, lat: float, lon: float, hsys: str = "P") -> Dict[str, Any]:
    """
    Возвращает куспиды домов и углы. hsys: 'P' (Placidus) по умолчанию.
    """
    # swe.houses(jd_ut, lat, lon) — lon восточной долготы положительная
    # В большинстве источников долгота восточная — положительная. Если у вас западная положительная — инвертируйте.
    # Здесь предполагаем стандарт: восток +, запад -.
    cusps, ascmc = swe.houses(jd_ut, lat, lon, hsys.encode("ascii"))
    # cusps — 1..12, ascmc: 0=Asc, 1=MC, 2=ARMC, 3=Vertex, 4=Equatorial Asc, 5=Co-Asc1, 6=Co-Asc2, 7=Polar Asc
    return {
        "system": hsys,
        "cusps": {str(i+1): cusps[i] for i in range(12)},
        "angles": {
            "ASC": ascmc[0],
            "MC": ascmc[1],
            "ARMC": ascmc[2],
            "Vertex": ascmc[3]
        }
    }

@app.get("/healthz")
def healthz():
    return ("ok", 200)

@app.get("/")
def root():
    return jsonify({
        "name": "ИИ-Астролог API",
        "version": "1.0",
        "status": "ready"
    })

@app.post("/calc")
def calc():
    """
    Вход (JSON):
      {
        "date": "YYYY-MM-DD",
        "time": "HH:MM",
        "lat": float,
        "lon": float,
        "tz": "Europe/Moscow" | "+06:00" (опционально),
        "hsys": "P" (опционально, Placidus по умолчанию),
        "guess_tz": true|false (если tz не передан, угадать по координатам)
      }
    """
    try:
        data = request.get_json(force=True, silent=False) or {}
        date_str = data["date"]
        time_str = data["time"]
        lat = float(data["lat"])
        lon = float(data["lon"])
        hsys = data.get("hsys", "P")

        tz_in = data.get("tz")
        if tz_in:
            tz_obj = get_tz(tz_in, lat, lon)
        else:
            if data.get("guess_tz", True):
                tzname = guess_iana_tz(lat, lon)
                if not tzname:
                    return jsonify({"error": "Cannot guess timezone from coordinates; pass 'tz' explicitly."}), 400
                tz_obj = get_tz(tzname, lat, lon)
            else:
                return jsonify({"error": "Missing 'tz'. Provide IANA name (e.g., 'Europe/Rome') or '+HH:MM' offset."}), 400

        jd_ut = to_julday_utc(date_str, time_str, tz_obj, lat, lon)
        planets = calc_planets(jd_ut)
        houses = calc_houses(jd_ut, lat, lon, hsys=hsys)

        return jsonify({
            "input": {
                "date": date_str,
                "time": time_str,
                "lat": lat,
                "lon": lon,
                "tz": tz_in or "guessed",
                "hsys": hsys
            },
            "julday_ut": jd_ut,
            "planets": planets,
            "houses": houses
        })
    except KeyError as ke:
        return jsonify({"error": f"Missing field: {str(ke)}"}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# Локальный запуск (на Railway используйте Gunicorn через Procfile)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
