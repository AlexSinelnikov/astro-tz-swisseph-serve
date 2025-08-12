# Astro TZ + Swiss Ephemeris API (No-code Deploy)

This server exposes two endpoints:
- `GET /timezone` — Historical-correct timezone offset using IANA tzdata (via `timezonefinder` + `zoneinfo`).
- `GET /swisseph` — Basic Swiss Ephemeris computations (planets, houses).

## How to deploy without coding

### Option A: Railway (UI only)
1. Create a Railway account.
2. Click "New Project" → "Deploy from GitHub" OR "Deploy from Repo". If you don't have a repo, use "Deploy from Template" and upload this ZIP.
3. Set build command (auto) and start command: `gunicorn app:app`.
4. Expose port `8000` (Railway auto-detects from `PORT`).
5. Deploy.

### Option B: Render
1. Create an account on Render.
2. Create a new Web Service → "Deploy from Git" or Upload.
3. Set "Start Command": `gunicorn app:app`.
4. Environment: `Python 3.11` (or 3.10).
5. Deploy.

## Endpoints

### 1) /health
```
GET /health
→ { "ok": true, "version": "1.0.0" }
```

### 2) /timezone
Query:
- `lat` (float), `lng` (float) — required
- `date` (YYYY-MM-DD) — optional, default today
- `time` (HH:MM) — optional, default 12:00
- `tz` (IANA tzid) — optional (if given, skips tz lookup)
```
GET /timezone?lat=55.7558&lng=37.6173&date=1994-05-17&time=04:40
→ {
  "zoneName": "Europe/Moscow",
  "gmtOffsetSeconds": 10800,
  "utcOffsetString": "+03:00",
  "dstSeconds": 0,
  "atLocal": "1994-05-17T04:40:00+03:00",
  "atUTC": "1994-05-17T01:40:00Z"
}
```

### 3) /swisseph
Query:
- `date` (YYYY-MM-DD), `time` (HH:MM), `lat`, `lng` — required
- `tz` (IANA tzid) — optional, discovered by `lat,lng` if missing
- `hs` (house system, default `P` = Placidus)
- `sidereal` (`true`/`false`) — default `false`
```
GET /swisseph?date=1994-05-17&time=04:40&lat=53.3478&lng=83.7769&tz=Asia/Barnaul
→ {
  "utc": "1994-05-17T01:40:00Z",
  "jd_ut": <...>,
  "planets": { "Sun": { "lon": ... }, ... },
  "houses": { "cusps": { "1": ..., ... }, "asc": ..., "mc": ... }
}
```

> Note: by default, Swiss Ephemeris runs in tropical; set `sidereal=true` to enable sidereal (Lahiri).

## Notes
- No database required.
- If you have Swiss Ephemeris ephemeris files, you can mount them via `EPHE_PATH` env var to improve precision/performance. Otherwise the built-in data works for online mode.
