from __future__ import annotations
import os, sys, io, glob, hashlib, zipfile, tarfile, tempfile, shutil, re, urllib.parse
from typing import List, Tuple
from datetime import datetime

# file lock (безопасно на Railway Linux)
try:
    import fcntl
except Exception:
    fcntl = None

LOG_PREFIX = "[fetch_ephe]"
def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)

def _split_csv(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]

def _split_alternatives(s: str) -> List[str]:
    return [x.strip() for x in s.split("|") if x.strip()]

def _any_glob_matches(base: str, patterns: List[str]) -> Tuple[bool, int, List[str]]:
    total = 0
    matched_files: List[str] = []
    for p in patterns:
        files = glob.glob(os.path.join(base, p))
        total += len(files)
        matched_files.extend(files)
    return (total > 0, total, matched_files)

def _parse_required_groups(raw: str) -> List[List[str]]:
    groups: List[List[str]] = []
    for part in _split_csv(raw):
        groups.append(_split_alternatives(part))
    return groups

def _acquire_lock(lock_path: str):
    fh = open(lock_path, "a+")
    try:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        return fh
    except Exception:
        return fh

def _release_lock(fh):
    try:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        try: fh.close()
        except Exception: pass

def _check_required(ephe_path: str, groups: List[List[str]]) -> Tuple[bool, List[str]]:
    details = []
    all_ok = True
    for i, alts in enumerate(groups, start=1):
        ok, count, files = _any_glob_matches(ephe_path, alts)
        details.append(f"check group {i}: {' | '.join(alts)} -> {count} files")
        if not ok:
            all_ok = False
    return all_ok, details

# ---------- DOWNLOADER ----------

def _normalize_provider_url(url: str) -> str:
    """Fix common provider links (Dropbox preview -> direct)."""
    u = urllib.parse.urlparse(url)
    if "dropbox.com" in u.netloc:
        # force direct download
        q = urllib.parse.parse_qs(u.query)
        q["dl"] = ["1"]
        u = u._replace(query=urllib.parse.urlencode({k:v[0] for k,v in q.items()}))
        return urllib.parse.urlunparse(u)
    return url

def _build_opener():
    from urllib.request import build_opener, HTTPCookieProcessor
    from http.cookiejar import CookieJar
    cj = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cj))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"),
        ("Accept", "*/*"),
        ("Connection", "close"),
    ]
    return opener

def _download_generic(url: str) -> tuple[bytes, dict]:
    opener = _build_opener()
    resp = opener.open(url, timeout=int(os.environ.get("EPHE_HTTP_TIMEOUT", "600")))
    data = resp.read()
    meta = {
        "status": getattr(resp, "status", None),
        "content_type": resp.headers.get("Content-Type", ""),
        "content_disp": resp.headers.get("Content-Disposition", ""),
        "length_hdr": resp.headers.get("Content-Length", ""),
        "final_url": resp.geturl(),
    }
    return data, meta

def _gdrive_extract_file_id(url: str) -> str | None:
    u = urllib.parse.urlparse(url)
    if u.netloc.endswith("drive.google.com"):
        if u.path.startswith("/file/d/"):
            parts = u.path.split("/")
            if len(parts) >= 4:
                return parts[3]
        q = urllib.parse.parse_qs(u.query)
        if "id" in q and q["id"]:
            return q["id"][0]
    return None

def _download_gdrive_large(url: str) -> tuple[bytes, dict]:
    from urllib.request import build_opener, HTTPCookieProcessor
    from http.cookiejar import CookieJar

    file_id = _gdrive_extract_file_id(url)
    if not file_id:
        return _download_generic(url)

    base = "https://drive.google.com/uc?export=download&id=" + file_id

    cj = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cj))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"),
        ("Accept", "*/*"),
        ("Connection", "close"),
    ]

    r1 = opener.open(base, timeout=int(os.environ.get("EPHE_HTTP_TIMEOUT", "600")))
    d1 = r1.read()
    ct1 = r1.headers.get("Content-Type", "")

    # если сразу отдали бинарь — отлично
    if ct1.startswith("application/zip") or ct1.startswith("application/octet-stream"):
        meta = {"status": getattr(r1, "status", None), "content_type": ct1,
                "length_hdr": r1.headers.get("Content-Length",""), "final_url": r1.geturl()}
        return d1, meta

    # иначе ищем confirm-токен
    html = d1.decode("utf-8", "ignore")
    m = re.search(r'confirm=([0-9A-Za-z_-]+)', html) or re.search(r'name="confirm"\s+value="([0-9A-Za-z_-]+)"', html)
    token = m.group(1) if m else None
    if not token:
        meta = {"status": getattr(r1, "status", None), "content_type": ct1,
                "length_hdr": r1.headers.get("Content-Length",""), "final_url": r1.geturl()}
        return d1, meta

    url2 = f"https://drive.google.com/uc?export=download&confirm={token}&id={file_id}"
    r2 = opener.open(url2, timeout=int(os.environ.get("EPHE_HTTP_TIMEOUT", "600")))
    d2 = r2.read()
    meta = {"status": getattr(r2, "status", None), "content_type": r2.headers.get("Content-Type",""),
            "length_hdr": r2.headers.get("Content-Length",""), "final_url": r2.geturl()}
    return d2, meta

def _download(url: str) -> tuple[bytes, dict]:
    url = _normalize_provider_url(url)
    if "drive.google.com" in url:
        return _download_gdrive_large(url)
    return _download_generic(url)

def _save_debug_payload(ephe_path: str, data: bytes, meta: dict) -> None:
    try:
        os.makedirs(ephe_path, exist_ok=True)
        open(os.path.join(ephe_path, ".last_download.bin"), "wb").write(data)
        info = "\n".join([f"{k}: {v}" for k, v in meta.items()])
        open(os.path.join(ephe_path, ".last_download.info"), "w", encoding="utf-8").write(info)
    except Exception:
        pass

def _is_zip(data: bytes) -> bool:
    return len(data) >= 4 and data[:2] == b"PK"

def _is_targz(data: bytes) -> bool:
    return len(data) >= 2 and data[:2] == b"\x1f\x8b"

def _atomic_extract_zip(zip_bytes: bytes, dest_dir: str) -> None:
    tmp_dir = tempfile.mkdtemp(prefix="ephe_extract_")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for zi in zf.infolist():
                target = os.path.normpath(os.path.join(tmp_dir, zi.filename))
                if not target.startswith(os.path.normpath(tmp_dir) + os.sep) and target != os.path.normpath(tmp_dir):
                    raise RuntimeError(f"Illegal path in zip: {zi.filename}")
                if zi.is_dir():
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(zi, "r") as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        # move out
        for root, dirs, files in os.walk(tmp_dir):
            rel = os.path.relpath(root, tmp_dir)
            out_root = dest_dir if rel == "." else os.path.join(dest_dir, rel)
            os.makedirs(out_root, exist_ok=True)
            for name in files:
                shutil.move(os.path.join(root, name), os.path.join(out_root, name))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def _atomic_extract_targz(tgz_bytes: bytes, dest_dir: str) -> None:
    tmp_dir = tempfile.mkdtemp(prefix="ephe_extract_")
    try:
        with tarfile.open(fileobj=io.BytesIO(tgz_bytes), mode="r:gz") as tf:
            safe_root = os.path.normpath(tmp_dir)
            for member in tf.getmembers():
                target = os.path.normpath(os.path.join(tmp_dir, member.name))
                if not target.startswith(safe_root + os.sep) and target != safe_root:
                    raise RuntimeError(f"Illegal path in tar: {member.name}")
            tf.extractall(tmp_dir)
        for root, dirs, files in os.walk(tmp_dir):
            rel = os.path.relpath(root, tmp_dir)
            out_root = dest_dir if rel == "." else os.path.join(dest_dir, rel)
            os.makedirs(out_root, exist_ok=True)
            for name in files:
                shutil.move(os.path.join(root, name), os.path.join(out_root, name))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def _flatten_if_needed(dest_dir: str) -> None:
    # если .se1 уже в корне — ничего не делаем
    if glob.glob(os.path.join(dest_dir, "*.se1")):
        return
    subdirs = [d for d in os.listdir(dest_dir) if os.path.isdir(os.path.join(dest_dir, d)) and not d.startswith(".")]
    if len(subdirs) != 1:
        return
    inner = os.path.join(dest_dir, subdirs[0])
    se1 = glob.glob(os.path.join(inner, "**", "*.se1"), recursive=True)
    if not se1:
        return
    for f in se1:
        rel = os.path.relpath(f, inner)
        out = os.path.join(dest_dir, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.move(f, out)

def ensure_ephe() -> None:
    ephe_path = os.environ.get("EPHE_PATH", "/app/ephe")
    os.makedirs(ephe_path, exist_ok=True)

    # минимум: планеты/луна/астероиды; учтены варианты именования (с/без "_")
    default_required = "seplm*.se1|sepl_*.se1|sepm*.se1,semo*.se1|semo_*.se1,seas*.se1|seas_*.se1"
    required_raw = os.environ.get("EPHE_REQUIRED_GLOBS", default_required)
    groups = _parse_required_groups(required_raw)

    zip_url = (os.environ.get("EPHE_ZIP_URL", "")).strip()
    sha_env = (os.environ.get("EPHE_SHA256", "")).strip().lower()
    force = os.environ.get("EPHE_FORCE_DOWNLOAD", "0") == "1"
    strict = os.environ.get("FETCH_EPHE_STRICT", "1") == "1"

    log(f"EPHE_PATH = {ephe_path}")
    log(f"EPHE_REQUIRED_GLOBS = {required_raw}")
    log("EPHE_ZIP_URL is set" if zip_url else "EPHE_ZIP_URL is NOT set")

    lock_path = os.path.join(ephe_path, ".ephe.lock")
    lock_fh = _acquire_lock(lock_path)
    try:
        ok, details = _check_required(ephe_path, groups)
        for d in details: log(d)

        if not (force or not ok):
            log("Required ephemeris files already present — skip download/extract.")
            log("Swiss Ephemeris files are ready.")
            return

        if not zip_url:
            msg = "EPHE_ZIP_URL is empty, but required files are missing."
            log(msg)
            if strict: raise RuntimeError(msg)
            else: return

        log("Downloading ephemeris archive...")
        data, meta = _download(zip_url)
        _save_debug_payload(ephe_path, data, meta)
        log(f"HTTP={meta.get('status')} Content-Type={meta.get('content_type')} Length={len(data)}")

        got_sha = hashlib.sha256(data).hexdigest()
        log(f"Archive SHA256 = {got_sha}")
        if sha_env:
            if got_sha != sha_env:
                raise RuntimeError(f"EPHE_SHA256 mismatch: expected {sha_env}, got {got_sha}")
            else:
                log("EPHE_SHA256 OK (match)")

        if _is_zip(data):
            log("Extracting ZIP...")
            _atomic_extract_zip(data, ephe_path)
        elif _is_targz(data):
            log("Extracting TAR.GZ...")
            _atomic_extract_targz(data, ephe_path)
        else:
            snippet = data[:200].decode("utf-8", "ignore")
            raise RuntimeError(
                "URL did not return a ZIP/TAR.GZ. "
                f"Content-Type={meta.get('content_type')}, bytes={len(data)}. "
                f"First bytes preview: {snippet[:120]!r}"
            )

        log("Extraction done.")
        _flatten_if_needed(ephe_path)

        ok, details = _check_required(ephe_path, groups)
        for d in details: log(d)

        sample = sorted(glob.glob(os.path.join(ephe_path, "*.se1")))[:12]
        if sample:
            log("Sample of ephe content: " + ", ".join([os.path.basename(s) for s in sample]))
        else:
            log("No .se1 files found after extraction.")

        if not ok:
            msg = "Required ephemeris patterns still missing after extraction."
            if strict: raise RuntimeError(msg)
            else: log("WARNING: " + msg)
        else:
            log("Swiss Ephemeris files are ready.")

    finally:
        _release_lock(lock_fh)

if __name__ == "__main__":
    try:
        ensure_ephe()
        log("OK")
        sys.exit(0)
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
