# fetch_ephe.py — загрузка эфемерид из Dropbox/HTTP, распаковка zip,
# рекурсивная проверка *.se1, настройка мульти‑пути для Swiss Ephemeris.
# Идempotent: если всё уже есть — ничего не делает (если не указан --force).

import os, sys, io, time, zipfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import argparse

# ===== Env / Defaults =====
EPHE_PATH = os.environ.get("EPHE_PATH", "/app/ephe").rstrip("/")
EPHE_ZIP_URLS = [u.strip() for u in os.environ.get("EPHE_ZIP_URL", "").split(",") if u.strip()]
EPHE_REQUIRED_GLOBS = [g.strip() for g in os.environ.get("EPHE_REQUIRED_GLOBS", "sepl_*.se1,semo_*.se1,seas_*.se1").split(",") if g.strip()]

def log(msg: str): print(f"[fetch_ephe] {msg}", flush=True)
def err(msg: str): print(f"[fetch_ephe][ERROR] {msg}", file=sys.stderr, flush=True)

# ===== FS / Checks =====
def ensure_dir(p: str) -> Path:
    d = Path(p); d.mkdir(parents=True, exist_ok=True); return d

def rglob_exists(root: str, pattern: str) -> bool:
    return any(Path(root).rglob(pattern))

def have_required(root: str) -> bool:
    if not Path(root).exists(): return False
    if not EPHE_REQUIRED_GLOBS: return True
    return all(rglob_exists(root, pat) for pat in EPHE_REQUIRED_GLOBS)

def list_se1(root: str, limit: int = 200):
    return [str(p) for p in Path(root).rglob("*.se1")][:limit]

def build_swisseph_search_path(root: str) -> str:
    dirs = set()
    r = Path(root)
    if not r.exists(): return root
    for f in r.rglob("*.se1"):
        if f.is_file(): dirs.add(str(f.parent))
    dirs.add(root)
    return os.pathsep.join(sorted(dirs))

def set_swisseph_path(root: str):
    try:
        import swisseph as swe
    except Exception:
        import pyswisseph as swe  # type: ignore
    multi = build_swisseph_search_path(root)
    swe.set_ephe_path(multi)
    log(f"swisseph search path set to: {multi}")

# ===== Network / Download =====
def download_zip(url: str, timeout: int) -> bytes:
    log(f"Downloading: {url}")
    req = Request(url, headers={"User-Agent": "fetch_ephe/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    log(f"Downloaded {len(data)} bytes")
    return data

def unzip_bytes(blob: bytes, dest_dir: str):
    ensure_dir(dest_dir)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(dest_dir)
    log(f"Unzipped into {dest_dir}")

# ===== Main ensure =====
def ensure_ephe(force: bool, tries: int, timeout: int, set_path: bool, allow_missing: bool) -> int:
    ensure_dir(EPHE_PATH)

    if have_required(EPHE_PATH) and not force:
        log("Required files already present — skip download.")
        if set_path: set_swisseph_path(EPHE_PATH)
        log(f"Sample files: {list_se1(EPHE_PATH, 20)}")
        return 0

    if not EPHE_ZIP_URLS:
        msg = "EPHE_ZIP_URL is empty and required files are missing."
        if allow_missing:
            log(msg + " Continuing because --allow-missing is set.")
            if set_path: set_swisseph_path(EPHE_PATH)
            return 0
        err(msg); return 4

    last_err = None
    for url in EPHE_ZIP_URLS:
        for attempt in range(1, tries + 1):
            try:
                blob = download_zip(url, timeout)
                unzip_bytes(blob, EPHE_PATH)
                if have_required(EPHE_PATH):
                    log("Ephemeris ready.")
                    if set_path: set_swisseph_path(EPHE_PATH)
                    log(f"Sample files: {list_se1(EPHE_PATH, 20)}")
                    return 0
                else:
                    msg = f"After unzip, required files not found (patterns={EPHE_REQUIRED_GLOBS})."
                    if allow_missing:
                        log(msg + " Continuing because --allow-missing is set.")
                        if set_path: set_swisseph_path(EPHE_PATH)
                        return 0
                    last_err = msg
                    break
            except (URLError, HTTPError, zipfile.BadZipFile) as e:
                last_err = f"{type(e).__name__}: {e}"
                log(f"Attempt {attempt}/{tries} failed: {last_err}")
                time.sleep(min(5 * attempt, 20))
        if last_err:
            log(f"Next URL fallback (if any)…")

    if last_err:
        err(last_err)
    return 4

# ===== CLI =====
def main():
    ap = argparse.ArgumentParser(description="Ensure Swiss Ephemeris files in EPHE_PATH.")
    ap.add_argument("--force", action="store_true", help="Force re-download even if files exist.")
    ap.add_argument("--tries", type=int, default=3, help="Retry count per URL.")
    ap.add_argument("--timeout", type=int, default=60, help="HTTP timeout, seconds.")
    ap.add_argument("--set-path", action="store_true", help="Set swisseph ephe path after ensuring files.")
    ap.add_argument("--allow-missing", action="store_true",
                    help="Do not exit with error if required files missing; just set path and continue.")
    args = ap.parse_args()

    rc = ensure_ephe(force=args.force, tries=args.tries, timeout=args.timeout,
                     set_path=args.set_path, allow_missing=args.allow_missing)
    sys.exit(rc)

if __name__ == "__main__":
    main()
