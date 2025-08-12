# fetch_ephe.py — только Dropbox ZIP, без внешних догрузок
import os, sys, time, glob, zipfile, tempfile, urllib.request, shutil
from urllib.error import URLError, HTTPError
from pathlib import Path

EPHE_PATH = os.environ.get("EPHE_PATH", "/app/ephe").rstrip("/")
# Можно указать несколько ссылок через запятую — попробуем по порядку
EPHE_ZIP_URLS = [u.strip() for u in os.environ.get("EPHE_ZIP_URL", "").split(",") if u.strip()]

# Минимальный набор, без которого Swiss Ephemeris будет ругаться (включая Chiron)
# При желании расширь этот список в переменной окружения EPHE_REQUIRED
EPHE_REQUIRED = [f.strip() for f in os.environ.get(
    "EPHE_REQUIRED",
    "sepl_18.se1,semo_18.se1,seas_18.se1"
).split(",") if f.strip()]

CHUNK = 1024 * 256
MAX_RETRIES = 4
TIMEOUT = 60  # сек

def log(msg): print(f"[fetch_ephe] {msg}", flush=True)
def err(msg): print(f"[fetch_ephe] ERROR: {msg}", file=sys.stderr, flush=True)

def ensure_dir(p: str): Path(p).mkdir(parents=True, exist_ok=True)

def present_se1(dirpath: str):
    return {os.path.basename(p) for p in glob.glob(os.path.join(dirpath, "*.se1"))}

def have_required(dirpath: str):
    present = present_se1(dirpath)
    missing = [name for name in EPHE_REQUIRED if name not in present]
    return (len(missing) == 0, present, missing)

def _download_stream(url: str, dst: str):
    # Поддерживает редиректы Dropbox, ?dl=1 и др.
    req = urllib.request.Request(url, headers={"User-Agent": "fetch-ephe/3"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dst, "wb") as f:
        while True:
            chunk = r.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)

def download_with_retries(url: str, dst: str):
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log(f"Скачиваю ({attempt}/{MAX_RETRIES}): {url}")
            _download_stream(url, dst)
            return True
        except (URLError, HTTPError, TimeoutError, OSError) as e:
            last = e
            wait = min(10, 2 ** attempt)
            err(f"Сбой скачивания: {e} — повтор через {wait}с")
            time.sleep(wait)
    if last:
        raise last
    return False

def safe_extract_zip(zip_path: str, dest_dir: str):
    """Распаковка только *.se1 в корень EPHE_PATH; защита от path traversal."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if not name.lower().endswith(".se1"):
                # игнорируем мусор, README и т.п.
                continue
            basename = os.path.basename(name)
            target = os.path.join(dest_dir, basename)
            # защита от traversal (у нас и так basename, но оставим проверку)
            target_path = Path(target).resolve()
            if not str(target_path).startswith(str(Path(dest_dir).resolve())):
                err(f"Пропускаю подозрительный путь в ZIP: {name}")
                continue
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, length=CHUNK)

def flatten_if_nested(ephe_dir: str):
    """Если автор ZIP сложил .se1 во вложенные папки — вытаскиваем в EPHE_PATH."""
    for src in glob.glob(os.path.join(ephe_dir, "**", "*.se1"), recursive=True):
        base = os.path.basename(src)
        dst = os.path.join(ephe_dir, base)
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        try:
            os.link(src, dst)
        except Exception:
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                err(f"Не удалось вытащить {src} -> {dst}: {e}")

def main():
    ensure_dir(EPHE_PATH)
    log(f"EPHE_PATH = {EPHE_PATH}")
    ok, present, missing = have_required(EPHE_PATH)
    if ok:
        log(f".se1 уже на месте ({len(present)} файлов). Пропускаю загрузку.")
        return

    if not EPHE_ZIP_URLS:
        err("EPHE_ZIP_URL не задан. Укажи прямую Dropbox‑ссылку на ZIP (с ?dl=1).")
        sys.exit(1)

    fd, tmpzip = tempfile.mkstemp(suffix=".zip"); os.close(fd)
    try:
        downloaded = False
        last_error = None
        for url in EPHE_ZIP_URLS:
            try:
                if download_with_retries(url, tmpzip):
                    downloaded = True
                    break
            except Exception as e:
                last_error = e
                err(f"Не удалось скачать из {url}: {e}")

        if not downloaded:
            if last_error:
                err(f"Скачивание не удалось ни с одного URL: {last_error}")
            sys.exit(2)

        if not zipfile.is_zipfile(tmpzip):
            size = os.path.getsize(tmpzip)
            err(f"Файл не похож на ZIP (size={size}). Проверь Dropbox‑ссылку (?dl=1).")
            sys.exit(3)

        log("Распаковываю ZIP…")
        safe_extract_zip(tmpzip, EPHE_PATH)
        flatten_if_nested(EPHE_PATH)

        ok, present, missing = have_required(EPHE_PATH)
        if not ok:
            err("После распаковки отсутствуют обязательные файлы:")
            for m in missing:
                err(f"  - {m}")
            err("Исправь содержимое ZIP в Dropbox (добавь недостающие .se1) и задеплой заново.")
            sys.exit(4)

        log(f"Готово: в {EPHE_PATH} файлов .se1 = {len(present_se1(EPHE_PATH))}")
    finally:
        try:
            os.remove(tmpzip)
        except Exception:
            pass

if __name__ == "__main__":
    main()
