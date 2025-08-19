from __future__ import annotations
import os, sys, io, glob, hashlib, zipfile, tarfile, tempfile, shutil
from typing import List, Tuple
from datetime import datetime

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
        details.append(f"group {i}: {' | '.join(alts)} -> {count} files")
        if not ok:
            all_ok = False
    return all_ok, details

def _download(url: str) -> tuple[bytes, dict]:
    # более «человеческий» заголовок
    from urllib.request import urlopen, Request
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
        "Accept": "*/*",
        "Connection": "close",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=int(os.environ.get("EPHE_HTTP_TIMEOUT", "300"))) as resp:
        data = resp.read()
        meta = {
            "status": getattr(resp, "status", None),
            "content_type": resp.headers.get("Content-Type", ""),
            "content_disp": resp.headers.get("Content-Disposition", ""),
            "length_hdr": resp.headers.get("Content-Length", ""),
        }
        return data, meta

def _save_debug_payload(ephe_path: str, data: bytes, meta: dict) -> None:
    try:
        os.makedirs(ephe_path, exist_ok=True)
        open(os.path.join(ephe_path, ".last_download.bin"), "wb").write(data)
        info = "\n".join([f"{k}: {v}" for k,v in meta.items()])
        open(os.path.join(ephe_path, ".last_download.info"), "w", encoding="utf-8").write(info)
    except Exception:
        pass

def _is_zip(data: bytes) -> bool:
    return len(data) >= 4 and data[:2] == b"PK"

def _is_targz(data: bytes) -> bool:
    # gzip сигнатура
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
        # перемещаем в dest
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
        # копия в dest
        for root, dirs, files in os.walk(tmp_dir):
            rel = os.path.relpath(root, tmp_dir)
            out_root = dest_dir if rel == "." else os.path.join(dest_dir, rel)
            os.makedirs(out_root, exist_ok=True)
            for name in files:
                shutil.move(os.path.join(root, name), os.path.join(out_root, name))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def _flatten_if_needed(dest_dir: str) -> None:
    # если *.se1 не в корне, но есть единственная подпапка с *.se1 — поднимем содержимое
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

    default_required = "seplm*.se1|sepl_*.se1|sepm*.se1,semo*.se1|semo_*.se1,seas*.se1|seas_*.se1"
    required_raw = os.environ.get("EPHE_REQUIRED_GLOBS", default_required)
    groups = _parse_required_groups(required_raw)

    zip_url = os.environ.get("EPHE_ZIP_URL", "").strip()
    sha_env = os.environ.get("EPHE_SHA256", "").strip().lower()
    force = os.environ.get("EPHE_FORCE_DOWNLOAD", "0") == "1"
    strict = os.environ.get("FETCH_EPHE_STRICT", "1") == "1"

    log(f"EPHE_PATH = {ephe_path}")
    log(f"EPHE_REQUIRED_GLOBS = {required_raw}")
    if zip_url:
        log(f"EPHE_ZIP_URL is set")
    else:
        log(f"EPHE_ZIP_URL is NOT set")

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
                log("EPHE_SHA256 OK")

        # Определим формат и распакуем
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
                "Check EPHE_ZIP_URL (use dl.dropboxusercontent.com and /download for folders). "
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
