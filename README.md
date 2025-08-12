# Astro TZ + Swiss Ephemeris API (v2, with Aspects & Extra Points)

Endpoints:
- `GET /timezone` — Historical-correct timezone offset using IANA tzdata.
- `GET /swisseph` — Swiss Ephemeris planets + houses **+ aspects** (with Nodes, Lilith, Chiron, Part of Fortune, ASC/MC, optional house cusps).

## Deploy (Railway)
Start command: `gunicorn app:app`

## /swisseph — Query Params
- `date` (YYYY-MM-DD) **required**
- `time` (HH:MM) **required**
- `lat`, `lng` (floats) **required**
- `tz` (IANA tzid) optional (if omitted, resolved from `lat,lng`)
- `hs` house system, default `P` (Placidus)
- `sidereal` `true`/`false` (default `false`)

Aspect controls (degrees):
- `orb_main` default `6`
- `orb_lum` default `8` (Sun/Moon)
- `orb_angle` default `4` (ASC/MC & cusps)
- `orb_node` default `3` (Nodes)
- `orb_chiron` default `3`
- `orb_lilith` default `3`

Include controls (booleans):
- `include_angles` default `true` (ASC, MC in aspects)
- `include_cusps` default `true` (house cusps 1..12 in aspects)

## Extra points included
- **Mean Node**, **True Node**
- **Lilith (Mean)**, **Lilith (Oscu)** (lunar apogee)
- **Chiron**
- **Part of Fortune** (day/night formula)
- **ASC**, **MC**
- **House cusps 1..12** (optional in aspects)

## Example
```
/swisseph?date=1994-05-17&time=04:40&lat=53.3478&lng=83.7769&tz=Asia/Barnaul&include_cusps=true
```
Response includes:
- `planets` (Sun..Pluto + Chiron, Nodes, Lilith variants),
- `houses` (cusps, asc, mc),
- `lots.part_of_fortune` (deg),
- `aspects` (list, sorted by tightness).
