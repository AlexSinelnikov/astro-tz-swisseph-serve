# fetch_ephe.py
from __future__ import annotations
import os, sys, io, time, zipfile, shutil, argparse, glob, tempfile, hashlib, random
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

LOGP = "[fetch_ephe]"
def log(*a): print(LOGP, *a, flush=True)

# ---------- env helpers ----------
def env_str(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) and v is not None else v

def env_int(name: str, default: int) -> int:
    v = env_str(name)
    try: return int(v) if v is not None else default
    except: return default

def env_bool(name: str, default: bool = False) -> bool:
    v = env_str(name)
    if v is None: return default
    return v.lower() in ("1","true","yes","y","on")

# ---------- core ----------
def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def have_required(root: str, patterns_csv: str) -> tuple[bool, dict[str, list[str]]]:
    """
    Быстрая проверка наличия нужных файлов:
      1) ищем в корне
      2) если пусто — рекурсивно по всему EPHE_PATH
    """
    res: dict[str, list[str]] = {}
    all_ok = True
    pats = [p.strip() for p in patterns_csv.split(",") if p.strip()]
    for pat in pats:
        matches = glob.glob(os.path.join(root, pat))
        if not matches and ("*" in pat or "?" in pat) and "/" not in pat and "\\" not in pat:
            # безопасная эскалация в рекурсивный поиск
            matches = glob.glob(os.path.join(root, "**", pat), recursive=True)
        ok = len(matches) > 0
        res[pat] = matches
        all_ok = all_ok and ok
        log(f"Проверка шаблона '{pat}': {'OK' if ok else 'NOT FOUND'} (найдено {len(matches)})")
    return all_ok, res

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def download_with_retries(urls: list[str], dst_zip: str, timeout: int, tries: int) -> bool:
    """
    Стриминговая загрузка в файл, без хранения в памяти.
    """
    ensure_dir(os.path.dirname(dst_zip))
    for url in [u.strip() for u in urls if u.strip()]:
        for attempt in range(1, tries + 1):
            try:
                log(f"Скачиваю ({attempt}/{tries}): {url}")
                req = Request(url, headers={"User-Agent": "railway-ephe/1.0"})
                with urlopen(req, timeout=timeout) as r, open(dst_zip, "wb") as out:
                    while True:
                        chunk = r.read(1024 * 1024)
                        if not chunk: break
                        out.write(chunk)
                size = os.path.getsize(dst_zip)
                if size == 0:
                    raise IOError("Пустой файл после скачивания")
                log(f"OK, получено {size} байт → {dst_zip}")
                return True
            except Exception as e:
                log(f"Ошибка загрузки: {type(e).__name__}: {e}")
                if attempt < tries:
                    # экспоненциальный бэк‑офф с мелким джиттером
                    sleep_s = min(15, (2 ** (attempt - 1))) + random.random()
                    time.sleep(sleep_s)
        log(f"Не удалось скачать по адресу: {url}")
    return False

def validate_zip(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                log(f"Архив повреждён, проблемный файл: {bad}")
                return False
            # минимальная sanity‑проверка: есть ли вообще элементы
            if not zf.namelist():
                log("Архив пуст.")
                return False
        return True
    except zipfile.BadZipFile:
        log("Некорректный ZIP (BadZipFile).")
        return False
    except Exception as e:
        log(f"Ошибка при проверке ZIP: {e}")
        return False

def extract_needed_fast(zip_path: str, dst_root: str, keep_txt: bool) -> int:
    """
    Извлекаем только *.se1 (и опционально *.txt) в КОРЕНЬ dst_root.
    Любые подпапки внутри архива игнорируем — сразу 'сплющиваем'.
    Возвращаем число извлечённых файлов.
    """
    ensure_dir(dst_root)
    extracted = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            base = os.path.basename(name)
            if not base:  # папка
                continue
            lower = base.lower()
            if lower.endswith(".se1") or (keep_txt and lower.endswith(".txt")):
                target = os.path.join(dst_root, base)
                if not os.path.exists(target):
                    with zf.open(info) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out, length=1024 * 1024)
                    extracted += 1
    log(f"Извлечено файлов: {extracted}")
    return extracted

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-path", action="store_true", help="Вывести строку search path в логи")
    parser.add_argument("--tries", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--force-refetch", action="store_true", help="Игнорировать локальные файлы и перекачать архив")
    args = parser.parse_args()

    ephe_path = env_str("EPHE_PATH", "/app/ephe")
    zip_urls_raw = env_str("EPHE_ZIP_URL", "") or ""
    required_csv = env_str("EPHE_REQUIRED_GLOBS", "sepl_*.se1,semo_*.se1,seas_*.se1")
    allow_missing = args.allow_missin g or env_bool("EPHE_ALLOW_MISSING", False)
    tries = args.tries if args.tries is not None else env_int("EPHE_TRIES", 3)
    timeout = args.timeout if args.timeout is not None else env_int("EPHE_TIMEOUT", 60)
    keep_txt = env_bool("EPHE_KEEP_TXT", True)
    expected_sha = env_str("EPHE_SHA256", None)

    ensure_dir(ephe_path)

    # 1) Быстрая проверка: если всё уже есть и refetch не просили — пропускаем загрузку
    ok_now, _ = have_required(ephe_path, required_csv)
    if ok_now and not args.force_refetch:
        log("Требуемые файлы уже присутствуют — загрузка пропущена.")
        if args.set_path:
            log(f"swisseph search path set to: {ephe_path}")
        sys.exit(0)

    urls = [u.strip() for u in zip_urls_raw.split(",") if u.strip()]
    if not urls:
        log("EPHE_ZIP_URL не задан и локальных файлов недостаточно.")
        if allow_missing:
            log("EPHE_ALLOW_MISSING=1 — продолжаю старт без эфемерид.")
            if args.set_path:
                log(f"swisseph search path set to: {ephe_path}")
            sys.exit(0)
        else:
            sys.exit(5)

    # 2) Качаем ZIP во временный файл
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "ephe.zip")
        if not download_with_retries(urls, zip_path, timeout=timeout, tries=tries):
            log("Не удалось скачать ни один ZIP.")
            if allow_missing:
                log("EPHE_ALLOW_MISSING=1 — продолжаю старт без эфемерид.")
                if args.set_path:
                    log(f"swisseph search path set to: {ephe_path}")
                sys.exit(0)
            else:
                sys.exit(5)

        # 3) Проверка хэша (если задан)
        if expected_sha:
            got = sha256_file(zip_path)
            if got.lower() != expected_sha.lower():
                log(f"SHA256 не совпадает. Ожидали {expected_sha}, получили {got}")
                if not allow_missing:
                    sys.exit(5)
                else:
                    log("EPHE_ALLOW_MISSING=1 — продолжаю несмотря на расхождение SHA.")

        # 4) Валидация ZIP
        if not validate_zip(zip_path):
            if allow_missing:
                log("EPHE_ALLOW_MISSING=1 — продолжаю старт без эфемерид.")
                if args.set_path:
                    log(f"swisseph search path set to: {ephe_path}")
                sys.exit(0)
            else:
                sys.exit(5)

        # 5) Извлекаем только нужное прямо в корень EPHE_PATH
        extracted = extract_needed_fast(zip_path, ephe_path, keep_txt=keep_txt)
        if extracted == 0:
            log("В архиве не найдено ни одного *.se1 (или уже все присутствовали).")

    # 6) Финальная проверка required
    ok, _details = have_required(ephe_path, required_csv)

    if args.set_path:
        log(f"swisseph search path set to: {ephe_path}")

    if ok:
        log("have_required: true")
        sys.exit(0)
    else:
        log("have_required: false")
        if allow_missing:
            log("EPHE_ALLOW_MISSING=1 — продолжаю старт без фатальной ошибки.")
            sys.exit(0)
        else:
            log("Добавьте корректный архив или ослабьте проверку через EPHE_ALLOW_MISSING=1.")
            sys.exit(4)

if __name__ == "__main__":
    main()
