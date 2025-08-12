# fetch_ephe.py — скачать ZIP с эфемеридами и распаковать в EPHE_PATH
import os
import sys
import time
import glob
import zipfile
import tempfile
import urllib.request
from urllib.error import URLError, HTTPError

EPHE_PATH = os.environ.get("EPHE_PATH", "/app/ephe")
EPHE_ZIP_URL = os.environ.get("EPHE_ZIP_URL", "").strip()

def have_ephe_files(path: str) -> bool:
    # ищем хотя бы несколько .se1 (обычно десятки)
    files = glob.glob(os.path.join(path, "**", "*.se1"), recursive=True)
    return len(files) >= 3

def flatten_if_nested(path: str) -> None:
    """Если .se1 лежат во вложенных папках — скопируем/ссылкой вытащим их в EPHE_PATH."""
    for src in glob.glob(os.path.join(path, "**", "*.se1"), recursive=True):
        base = os.path.basename(src)
        dst = os.path.join(EPHE_PATH, base)
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        try:
            # жёсткая ссылка (быстро и без копии)
            os.link(src, dst)
        except Exception:
            # fallback — копия
            import shutil
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                print(f"[fetch_ephe] WARN: cannot move {src} -> {dst}: {e}", file=sys.stderr)

def download_with_retries(url: str, dst: str, retries: int = 3, timeout: int = 180):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            print(f"[fetch_ephe] Download attempt {attempt}/{retries} …")
            urllib.request.urlretrieve(url, dst)  # следует редиректам сам
            return
        except (URLError, HTTPError) as e:
            last_err = e
            print(f"[fetch_ephe] Download error: {e}. Retry in 3s…", file=sys.stderr)
            time.sleep(3)
    raise last_err if last_err else RuntimeError("Unknown download error")

def main():
    os.makedirs(EPHE_PATH, exist_ok=True)

    if have_ephe_files(EPHE_PATH):
        print(f"[fetch_ephe] .se1 already present in {EPHE_PATH}. Skip download.")
        return

    if not EPHE_ZIP_URL:
        print("[fetch_ephe] ERROR: EPHE_ZIP_URL is not set.", file=sys.stderr)
        sys.exit(1)

    # качаем во временный файл
    fd, tmpzip = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        print(f"[fetch_ephe] Downloading ephemeris from: {EPHE_ZIP_URL}")
        download_with_retries(EPHE_ZIP_URL, tmpzip)

        # быстрая проверка, что это ZIP
        if not zipfile.is_zipfile(tmpzip):
            size = os.path.getsize(tmpzip)
            print(f"[fetch_ephe] ERROR: downloaded file is not a ZIP (size={size}).", file=sys.stderr)
            sys.exit(2)

        print(f"[fetch_ephe] Extracting to {EPHE_PATH} …")
        with zipfile.ZipFile(tmpzip, "r") as zf:
            zf.extractall(EPHE_PATH)

        # если файлы внутри во вложенной папке — вытаскиваем их в EPHE_PATH
        flatten_if_nested(EPHE_PATH)

        if not have_ephe_files(EPHE_PATH):
            print("[fetch_ephe] ERROR: no .se1 files found after extract.", file=sys.stderr)
            sys.exit(3)

        count = len(glob.glob(os.path.join(EPHE_PATH, "*.se1")))
        print(f"[fetch_ephe] OK: ephemeris ready in {EPHE_PATH}. Files: {count}")
    finally:
        try:
            os.remove(tmpzip)
        except Exception:
            pass

if __name__ == "__main__":
    main()
