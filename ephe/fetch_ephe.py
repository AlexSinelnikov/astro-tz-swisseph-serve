# fetch_ephe.py — скачивает ZIP/папку с эфемеридами и распаковывает в EPHE_PATH
import os, sys, urllib.request, zipfile, tempfile, glob

EPHE_PATH = os.environ.get("EPHE_PATH", "/app/ephe")
EPHE_ZIP_URL = os.environ.get("EPHE_ZIP_URL")  # прямая ссылка на ZIP/папку (Dropbox с ?dl=1)

def have_ephe_files(path):
    # ищем *.se1 рекурсивно (на случай если в ZIP файлы лежат во вложенной папке)
    return len(glob.glob(os.path.join(path, "**", "*.se1"), recursive=True)) >= 3

def flatten_if_nested(path):
    # если файлы *.se1 лежат во вложенной папке, перенесём их в EPHE_PATH/корень
    for src in glob.glob(os.path.join(path, "**", "*.se1"), recursive=True):
        base = os.path.basename(src)
        dst = os.path.join(EPHE_PATH, base)
        if os.path.abspath(src) != os.path.abspath(dst):
            try:
                os.link(src, dst)
            except Exception:
                try:
                    import shutil; shutil.copy2(src, dst)
                except Exception:
                    pass

def main():
    os.makedirs(EPHE_PATH, exist_ok=True)

    if have_ephe_files(EPHE_PATH):
        print(f"[fetch_ephe] Found .se1 files in {EPHE_PATH}, skip download.")
        return

    if not EPHE_ZIP_URL:
        print("[fetch_ephe] EPHE_ZIP_URL is not set; cannot download ephemeris.", file=sys.stderr)
        sys.exit(1)

    print(f"[fetch_ephe] Downloading ephemeris from {EPHE_ZIP_URL} ...")
    fd, tmpzip = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        urllib.request.urlretrieve(EPHE_ZIP_URL, tmpzip)
        print(f"[fetch_ephe] Downloaded to {tmpzip}, extracting to {EPHE_PATH} ...")
        with zipfile.ZipFile(tmpzip, "r") as zf:
            zf.extractall(EPHE_PATH)
        flatten_if_nested(EPHE_PATH)
        if not have_ephe_files(EPHE_PATH):
            print("[fetch_ephe] No .se1 files found after extract — check ZIP structure.", file=sys.stderr)
            sys.exit(2)
        print("[fetch_ephe] OK: ephemeris ready.")
    finally:
        try:
            os.remove(tmpzip)
        except Exception:
            pass

if __name__ == "__main__":
    main()
