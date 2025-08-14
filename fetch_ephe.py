# fetch_ephe.py — Dropbox-only, проверка по шаблонам (sepl_*.se1, semo_*.se1, seas_*.se1)
import os, sys, time, zipfile, tempfile, urllib.request, shutil, fnmatch
from pathlib import Path
from urllib.error import URLError, HTTPError

EPHE_PATH = os.environ.get("EPHE_PATH", "/app/ephe").rstrip("/")
# Несколько URL можно через запятую
EPHE_ZIP_URLS = [u.strip() for u in os.environ.get("EPHE_ZIP_URL", "").split(",") if u.strip()]

# Шаблоны «обязательных» групп (через запятую). По каждому шаблону нужен хотя бы один файл.
# По умолчанию требуем по одному из: sepl_*.se1, semo_*.se1, seas_*.se1
EPHE_REQUIRED_GLOBS = [g.strip() for g in os.environ.get(
    "EPHE_REQUIRED_GLOBS", "sepl_*.se1,semo_*.se1,seas_*.se1"
).split(",") if g.strip()]

CHUNK = 1024 * 256
MAX_RETRIES = 4
TIMEOUT = 60

def log(m): print(f"[fetch_ephe] {m}", flush=True)
def err(m): print(f"[fetch_ephe] ERROR: {m}", file=sys.stderr, flush=True)

def ensure_dir(p: str): Path(p).mkdir(parents=True, exist_ok=True)

def list_se1(dirpath: str):
    """Собираем все *.se1 (регистронезависимо), возвращаем реальные имена (с их регистром)."""
    found = []
    for root, _, files in os.walk(dirpath):
        for name in files:
            if name.lower().endswith(".se1"):
                found.append(os.path.join(root, name))
    return found

def groups_ok(dirpath: str):
    """Проверяем, что по каждому шаблону есть >=1 совпадение."""
    all_files = [os.path.basename(p) for p in list_se1(dirpath)]
    all_lower = [n.lower() for n in all_files]
    missing = []
    for pat in EPHE_REQUIRED_GLOBS:
        pat_l = pat.lower()
        if not any(fnmatch.fnmatch(n, pat_l) for n in all_lower):
            missing.append(pat)
    return (len(missing) == 0, all_files, missing)

def _download_stream(url: str, dst: str):
    req = urllib.request.Request(url, headers={"User-Agent": "fetch-ephe/4"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dst, "wb") as f:
        while True:
            chunk = r.read(CHUNK)
            if not chunk: break
            f.write(chunk)

def download_with_retries(url: str, dst: str):
    last = None
    for attempt in range(1, MAX_RETRIES+1):
        try:
            log(f"Скачиваю ({attempt}/{MAX_RETRIES}): {url}")
            _download_stream(url, dst)
            return True
        except (URLError, HTTPError, TimeoutError, OSError) as e:
            last = e
            wait = min(10, 2**attempt)
            err(f"Сбой скачивания: {e} — повтор через {wait}с")
            time.sleep(wait)
    if last: raise last
    return False

def safe_extract_zip(zip_path: str, dest_dir: str):
    """Распаковываем только *.se1 в EPHE_PATH (безопасно, без traversal)."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir(): continue
            name = info.filename
            if not name.lower().endswith(".se1"):
                continue
            base = os.path.basename(name)
            target = os.path.join(dest_dir, base)
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, length=CHUNK)

def flatten_if_nested(ephe_dir: str):
    """Если внутри ZIP .se1 лежат во вложенных папках — вытащим их в корень EPHE_PATH."""
    for root, _, files in os.walk(ephe_dir):
        for name in files:
            if not name.lower().endswith(".se1"): continue
            src = os.path.join(root, name)
            dst = os.path.join(ephe_dir, name)
            if os.path.abspath(src) == os.path.abspath(dst): continue
            try: os.link(src, dst)
            except Exception:
                try: shutil.copy2(src, dst)
                except Exception as e: err(f"Не удалось положить {src} -> {dst}: {e}")

def main():
    ensure_dir(EPHE_PATH)
    log(f"EPHE_PATH = {EPHE_PATH}")

    ok0, present0, miss0 = groups_ok(EPHE_PATH)
    if ok0:
        log(f".se1 уже на месте ({len(present0)} файлов). Пропускаю загрузку.")
        return

    if not EPHE_ZIP_URLS:
        err("EPHE_ZIP_URL не задан. Укажи прямую Dropbox‑ссылку на ZIP (с ?dl=1).")
        sys.exit(1)

    fd, tmpzip = tempfile.mkstemp(suffix=".zip"); os.close(fd)
    try:
        downloaded = False
        last_err = None
        for url in EPHE_ZIP_URLS:
            try:
                if download_with_retries(url, tmpzip):
                    downloaded = True
                    break
            except Exception as e:
                last_err = e
                err(f"Не удалось скачать из {url}: {e}")

        if not downloaded:
            if last_err: err(f"Скачивание не удалось ни с одного URL: {last_err}")
            sys.exit(2)

        if not zipfile.is_zipfile(tmpzip):
            size = os.path.getsize(tmpzip)
            err(f"Файл не похож на ZIP (size={size}). Проверь Dropbox‑ссылку (?dl=1).")
            sys.exit(3)

        log("Распаковываю ZIP…")
        safe_extract_zip(tmpzip, EPHE_PATH)
        flatten_if_nested(EPHE_PATH)

        ok, present, missing = groups_ok(EPHE_PATH)
        if not ok:
            log(f"Найдены .se1: {sorted(present)}")
            err("После распаковки не нашли нужные группы (EPHE_REQUIRED_GLOBS):")
            for m in missing:
                err(f"  - {m}")
            err("Либо поправь ZIP (положи нужные серии), либо задай точные шаблоны в EPHE_REQUIRED_GLOBS.")
            sys.exit(4)

        log(f"Готово: файлов .se1 = {len(present)}")
    finally:
        try: os.remove(tmpzip)
        except Exception: pass

if __name__ == "__main__":
    import time
    start = time.time()
    main()
    log(f"Done in {time.time()-start:.1f}s")
