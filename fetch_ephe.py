# fetch_ephe.py
# Prod-утилита для подготовки эфемерид на Railway/Render/Heroku.
# Делает:
#   - проверяет наличие требуемых файлов по EPHE_REQUIRED_GLOBS
#   - при отсутствии скачивает ZIP по EPHE_ZIP_URL (с ретраями/таймаутом)
#   - валидирует ZIP, проверяет EPHE_SHA256 (если задан)
#   - извлекает ТОЛЬКО *.se1 (и опц. *.txt) в EPHE_PATH (flatten)
#   - пишет кеш-хэш .last_zip.sha256
#   - никогда не пропускает распаковку, если *.se1 ещё нет (даже при совпадении SHA)
#
# Переменные окружения:
#   EPHE_PATH=/app/ephe
#   EPHE_ZIP_URL=<dropbox ?dl=1 или любой прямой URL>
#   EPHE_REQUIRED_GLOBS=sepl_*.se1,semo_*.se1,seas_*.se1
#   EPHE_TRIES=3
#   EPHE_TIMEOUT=60
#   EPHE_KEEP_TXT=0|1
#   EPHE_ALLOW_MISSING=0|1
#   EPHE_SHA256=<64-символов sha256 архива> (опционально)
#
# Ключи:
#   --set-path         : просто печатает путь (для логов стартовой команды)
#   --force-refetch    : принудительно распаковать даже при совпадении SHA
#   --tries N / --timeout S / --allow-missing
#
from __future__ import annotations

import argparse
import glob
import hashlib
import os
import random
import shutil
import tempfile
import time
import zipfile
from typing import List, Tuple, Dict, Optional
from urllib.request import Request, urlopen

LOGP = "[fetch_ephe]"

def log(*a: object) -> None:
    print(LOGP, *a, flush=True)

# ===== env helpers =====
def env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) and v is not None else v

def env_int(name: str, default: int) -> int:
    v = env_str(name)
    try:
        return int(v) if v is not None else default
    except Exception:
        return default

def env_bool(name: str, default: bool = False) -> bool:
    v = env_str(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "y", "on")

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

# ===== lock (atomic) =====
def acquire_lock(dir_path: str, name: str = ".ephe.lock", timeout_s: int = 25) -> Optional[str]:
    ensure_dir(dir_path)
    lock_path = os.path.join(dir_path, name)
    start = time.time()
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return lock_path
        except FileExistsError:
            if time.time() - start > timeout_s:
                log("Не удалось получить лок (таймаут). Продолжаю без лока.")
                return None
            time.sleep(0.3 + random.random() / 5)

def release_lock(lock_path: Optional[str]) -> None:
    if lock_path and os.path.exists(lock_path):
        try:
            os.remove(lock_path)
        except Exception:
            pass

# ===== core helpers =====
def have_required(root: str, patterns_csv: str) -> Tuple[bool, Dict[str, List[str]]]:
    """
    Проверяем маски:
      - в корне root
      - если не нашли, то рекурсивно (**/)
    """
    res: Dict[str, List[str]] = {}
    ok_all = True
    pats = [p.strip() for p in patterns_csv.split(",") if p.strip()]
    for pat in pats:
        matches = glob.glob(os.path.join(root, pat))
        if not matches and ("*" in pat or "?" in pat) and "/" not in pat and "\\" not in pat:
            matches = glob.glob(os.path.join(root, "**", pat), recursive=True)
        ok = len(matches) > 0
        res[pat] = matches
        ok_all &= ok
        log(f"Проверка '{pat}': {'OK' if ok else 'NOT FOUND'} (найдено {len(matches)})")
    return ok_all, res

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def download_with_retries(urls: List[str], dst_zip: str, timeout: int, tries: int) -> bool:
    ensure_dir(os.path.dirname(dst_zip))
    for url in [u.strip() for u in urls if u.strip()]:
        for attempt in range(1, tries + 1):
            try:
                log(f"Скачиваю ({attempt}/{tries}): {url}")
                t0 = time.time()
                req = Request(url, headers={"User-Agent": "railway-ephe/1.0"})
                with urlopen(req, timeout=timeout) as r, open(dst_zip, "wb") as out:
                    while True:
                        chunk = r.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                size = os.path.getsize(dst_zip)
                if size == 0:
                    raise IOError("Пустой файл после скачивания")
                log(f"OK, {size} байт, {time.time()-t0:.2f}s → {dst_zip}")
                return True
            except Exception as e:
                log(f"Ошибка: {type(e).__name__}: {e}")
                if attempt < tries:
                    time.sleep(min(15, 2 ** (attempt - 1)) + random.random())
        log(f"Провал по URL: {url}")
    return False

def validate_zip(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            if not zf.namelist():
                log("Архив пуст.")
                return False
            bad = zf.testzip()
            if bad:
                log(f"Повреждённый ZIP, проблемный файл: {bad}")
                return False
        return True
    except Exception as e:
        log(f"Bad ZIP: {e}")
        return False

def extract_needed(zip_path: str, dst_root: str, keep_txt: bool) -> int:
    """
    Извлекаем ТОЛЬКО *.se1 (и опц. *.txt) прямо в корень dst_root (flatten).
    """
    ensure_dir(dst_root)
    extracted = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            base = os.path.basename(info.filename.replace("\\", "/"))
            if not base:
                continue
            low = base.lower()
            if low.endswith(".se1") or (keep_txt and low.endswith(".txt")):
                target = os.path.join(dst_root, base)
                # перезаписываем, чтобы исправить ситуации с пустым каталогом, но совпавшим SHA
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
                extracted += 1
    log(f"Извлечено файлов: {extracted}")
    return extracted

# ===== main =====
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-path", action="store_true")
    parser.add_argument("--tries", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--force-refetch", action="store_true")
    args = parser.parse_args()

    ephe_path = env_str("EPHE_PATH", "/app/ephe")
    urls_raw = env_str("EPHE_ZIP_URL", "") or ""
    required = env_str("EPHE_REQUIRED_GLOBS", "sepl_*.se1,semo_*.se1,seas_*.se1")
    tries = args.tries if args.tries is not None else env_int("EPHE_TRIES", 3)
    timeout = args.timeout if args.timeout is not None else env_int("EPHE_TIMEOUT", 60)
    keep_txt = env_bool("EPHE_KEEP_TXT", False)
    allow_missing = args.allow_missing or env_bool("EPHE_ALLOW_MISSING", False)
    expected_sha = env_str("EPHE_SHA256", None)

    ensure_dir(ephe_path)

    # 0) Быстрый выход, если всё уже есть и рефетч не просят
    ok_now, _ = have_required(ephe_path, required)
    if ok_now and not args.force_refetch:
        if args.set_path:
            log(f"swisseph search path set to: {ephe_path}")
        log("Требуемые файлы уже на месте — загрузка пропущена.")
        return

    urls = [u for u in (s.strip() for s in urls_raw.split(",")) if u]
    if not urls:
        log("EPHE_ZIP_URL не задан и локальных файлов недостаточно.")
        if allow_missing:
            if args.set_path:
                log(f"swisseph search path set to: {ephe_path}")
            return
        raise SystemExit(5)

    # Лок на каталог
    lock_path = acquire_lock(ephe_path)

    try:
        # Повторная проверка под локом — вдруг другой процесс уже всё сделал
        ok_now, _ = have_required(ephe_path, required)
        if ok_now and not args.force_refetch:
            if args.set_path:
                log(f"swisseph search path set to: {ephe_path}")
            log("(после лока) Всё уже на месте — выходим.")
            return

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "ephe.zip")
            t0 = time.time()
            if not download_with_retries(urls, zip_path, timeout=timeout, tries=tries):
                if allow_missing:
                    if args.set_path:
                        log(f"swisseph search path set to: {ephe_path}")
                    log("Не удалось скачать ZIP, но allow-missing=1 — продолжаю.")
                    return
                raise SystemExit(5)
            log(f"Загрузка ZIP заняла {time.time() - t0:.2f}s")

            # Проверка sha256 (если задан)
            if expected_sha:
                got = sha256_file(zip_path)
                if got.lower() != expected_sha.lower():
                    log(f"SHA256 mismatch. expected={expected_sha} got={got}")
                    if not allow_missing:
                        raise SystemExit(5)
                    log("allow-missing=1 — продолжаю несмотря на mismatch.")

            # Проверка целостности ZIP
            if not validate_zip(zip_path):
                if allow_missing:
                    if args.set_path:
                        log(f"swisseph search path set to: {ephe_path}")
                    log("Некорректный ZIP, но allow-missing=1 — продолжаю.")
                    return
                raise SystemExit(5)

            # Решение: распаковывать или нет
            cache_sha_path = os.path.join(ephe_path, ".last_zip.sha256")
            prev_sha = None
            try:
                if os.path.exists(cache_sha_path):
                    with open(cache_sha_path, "r") as f:
                        prev_sha = f.read().strip()
            except Exception:
                pass

            cur_sha = sha256_file(zip_path)

            # Никогда не пропускаем распаковку, если нужных *.se1 нет.
            need_extract = True
            if prev_sha and prev_sha == cur_sha and not args.force_refetch:
                ok_after, _ = have_required(ephe_path, required)
                if ok_after:
                    log("Архив не менялся (sha256 совпал) и файлы уже на месте — распаковка пропущена.")
                    need_extract = False
                else:
                    log("Архив не менялся, но *.se1 отсутствуют — распаковываю принудительно.")

            if need_extract:
                extracted = extract_needed(zip_path, ephe_path, keep_txt=keep_txt)
                try:
                    with open(cache_sha_path, "w") as f:
                        f.write(cur_sha)
                except Exception:
                    pass
                if extracted == 0:
                    log("В архиве не найдено новых *.se1 (возможно, все уже присутствовали).")

        # Финальная проверка наличия
        ok, _ = have_required(ephe_path, required)
        if args.set_path:
            log(f"swisseph search path set to: {ephe_path}")
        if ok:
            log("have_required: true")
            return
        else:
            log("have_required: false")
            if allow_missing:
                log("EPHE_ALLOW_MISSING=1 — старт без фатальной ошибки.")
                return
            raise SystemExit(4)
    finally:
        release_lock(lock_path)

if __name__ == "__main__":
    main()
