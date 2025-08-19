from __future__ import annotations
import os, sys, io, glob, hashlib, zipfile, tempfile, shutil
from typing import List, Tuple
from datetime import datetime

# Для файлового лока на Linux
try:
    import fcntl  # type: ignore
except Exception:
    fcntl = None

LOG_PREFIX = "[fetch_ephe]"

def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)

def _split_csv(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]

def _split_alternatives(s: str) -> List[str]:
    # Поддержка ‘|’ внутри одной группы: seplm*.se1|sepl_*.se1|sepm*.se1
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
    """
    Возвращает список групп альтернатив.
    Пример EPHE_REQUIRED_GLOBS:
      seplm*.se1|sepl_*.se1|sepm*.se1,semo*.se1|semo_*.se1,seas*.se1|seas_*.se1
    """
    groups: List[List[str]] = []
    for part in _split_csv(raw):
        groups.append(_split_alternatives(part))
    return groups

def _sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def _atomic_extract_zip(zip_bytes: bytes, dest_dir: str) -> None:
    """
    Безопасная распаковка: защита от path traversal, атомарная распаковка во временную папку.
    """
    tmp_dir = tempfile.mkdtemp(prefix="ephe_extract_")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for zi in zf.infolist():
                # защита от ../ и абсолютных путей
                target = os.path.normpath(os.path.join(tmp_dir, zi.filename))
                if not target.startswith(os.path.normpath(tmp_dir) + os.sep) and target != os.path.normpath(tmp_dir):
                    raise RuntimeError(f"Illegal path in zip: {zi.filename}")
                if zi.is_dir():
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(zi, "r") as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        # Копируем поверх назначения
        for root, dirs, files in os.walk(tmp_dir):
            rel = os.path.relpath(root, tmp_dir)
            out_root = dest_dir if rel == "." else os.path.join(dest_dir, rel)
            os.makedirs(out_root, exist_ok=True)
            for name in files:
                shutil.move(os.path.join(root, name), os.path.join(out_root, name))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def _download(url: str) -> bytes:
    # Используем стандартный urllib, чтобы не тянуть зависимости
    from urllib.request import urlopen, Request
    req = Request(url, headers={"User-Agent": "ephe-fetcher/1.0"})
    with urlopen(req, timeout=180) as resp:
        return resp.read()

def _acquire_lock(lock_path: str):
    fh = open(lock_path, "a+")
    try:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        return fh
    except Exception:
        # если flock недоступен — просто держим дескриптор открытым
        return fh

def _release_lock(fh):
    try:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        try:
            fh.close()
        except Exception:
            pass

def _check_required(ephe_path: str, groups: List[List[str]]) -> Tuple[bool, List[str]]:
    """
    Возвращает (ok, details).
    ok = True если в каждой группе есть хотя бы одно совпадение.
    """
    details = []
    all_ok = True
    for i, alts in enumerate(groups, start=1):
        ok, count, files = _any_glob_matches(ephe_path, alts)
        details.append(f"group {i}: {' | '.join(alts)} -> {count} files")
        if not ok:
            all_ok = False
    return all_ok, details

def ensure_ephe() -> None:
    """
    Основная функция: гарантирует наличие эфемерид по маскам.
    Загружает и распаковывает архив при необходимости.
    Переменные окружения:
      EPHE_PATH                — каталог эфемерид (по умолчанию /app/ephe)
      EPHE_ZIP_URL             — обязательный URL архива (.zip), напр. Dropbox ?dl=1
      EPHE_SHA256              — опционально, контрольная сумма архива
      EPHE_REQUIRED_GLOBS      — CSV групп с альтернативами через |
                                 По умолчанию:
                                 seplm*.se1|sepl_*.se1|sepm*.se1,
                                 semo*.se1|semo_*.se1,
                                 seas*.se1|seas_*.se1
      EPHE_FORCE_DOWNLOAD=1    — принудительно скачать и распаковать архив
      FETCH_EPHE_STRICT=1      — если после распаковки маски не закрыты — бросить исключение
    """
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
        log(f"EPHE_ZIP_URL is NOT set (download will be skipped unless already present)")

    # Лок
    lock_path = os.path.join(ephe_path, ".ephe.lock")
    lock_fh = _acquire_lock(lock_path)
    try:
        ok, details = _check_required(ephe_path, groups)
        for d in details:
            log(d)

        need_download = force or (not ok)

        if need_download:
            if not zip_url:
                msg = "EPHE_ZIP_URL is empty, but required files are missing. Set EPHE_ZIP_URL (e.g., Dropbox link with ?dl=1)."
                log(msg)
                if strict:
                    raise RuntimeError(msg)
                else:
                    return

            log("Downloading ephemeris archive...")
            data = _download(zip_url)
            log(f"Downloaded {len(data)} bytes")

            got_sha = hashlib.sha256(data).hexdigest()
            log(f"Archive SHA256 = {got_sha}")

            if sha_env:
                if got_sha != sha_env:
                    raise RuntimeError(f"EPHE_SHA256 mismatch: expected {sha_env}, got {got_sha}")
                else:
                    log("EPHE_SHA256 OK")

            # Проверим, менялся ли архив
            last_sha_path = os.path.join(ephe_path, ".last_zip.sha256")
            last_sha = ""
            if os.path.exists(last_sha_path):
                try:
                    last_sha = open(last_sha_path, "r", encoding="utf-8").read().strip()
                except Exception:
                    last_sha = ""

            if last_sha == got_sha and not force:
                log("Archive unchanged compared to .last_zip.sha256 — re-extract anyway (to restore missing files).")

            log("Extracting archive...")
            _atomic_extract_zip(data, ephe_path)
            open(last_sha_path, "w", encoding="utf-8").write(got_sha)
            log("Extraction done.")

            # Повторная проверка
            ok, details = _check_required(ephe_path, groups)
            for d in details:
                log(d)

            # Покажем пример содержимого
            sample = sorted(glob.glob(os.path.join(ephe_path, "*.se1")))[:12]
            if sample:
                log("Sample of ephe content: " + ", ".join([os.path.basename(s) for s in sample]))
            else:
                log("No .se1 files found after extraction.")

            if not ok:
                msg = "Required ephemeris patterns still missing after extraction."
                if strict:
                    raise RuntimeError(msg)
                else:
                    log("WARNING: " + msg)
                    return
        else:
            log("Required ephemeris files already present — skip download/extract.")

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
