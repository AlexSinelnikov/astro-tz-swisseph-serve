from __future__ import annotations
import os, glob, shutil
from typing import List, Tuple

LOG_PREFIX = "[fetch_ephe]"
def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)

def _any_glob_matches(base: str, patterns: List[str]) -> Tuple[bool, int, List[str]]:
    total = 0
    matched_files: List[str] = []
    for p in patterns:
        files = glob.glob(os.path.join(base, p))
        total += len(files)
        matched_files.extend(files)
    return (total > 0, total, matched_files)

def _check_required(ephe_path: str) -> Tuple[bool, List[str]]:
    # Поддерживаем варианты имён (с/без подчёркивания)
    groups = [
        ["seplm*.se1", "sepl_*.se1", "sepm*.se1"],  # планеты
        ["semo*.se1", "semo_*.se1"],               # луна
        ["seas*.se1", "seas_*.se1"],               # астероиды/хирон
    ]
    details = []
    all_ok = True
    for i, alts in enumerate(groups, start=1):
        ok, count, _ = _any_glob_matches(ephe_path, alts)
        details.append(f"group {i}: {' | '.join(alts)} -> {count} files")
        if not ok:
            all_ok = False
    return all_ok, details

def ensure_ephe() -> None:
    ephe_path = os.environ.get("EPHE_PATH", "/app/ephe")
    bundled_path = os.path.join(os.path.dirname(__file__), "ephe")  # ./ephe внутри репо
    os.makedirs(ephe_path, exist_ok=True)

    log(f"EPHE_PATH = {ephe_path}")
    has_any = bool(glob.glob(os.path.join(ephe_path, "*.se1")))

    if not has_any and os.path.isdir(bundled_path):
        log(f"No .se1 in EPHE_PATH, found bundled ./ephe -> copying...")
        for f in glob.glob(os.path.join(bundled_path, "*.se1")):
            shutil.copy2(f, os.path.join(ephe_path, os.path.basename(f)))

    ok, details = _check_required(ephe_path)
    for d in details: log(d)

    sample = sorted(glob.glob(os.path.join(ephe_path, "*.se1")))[:12]
    if sample:
        log("Sample of ephe content: " + ", ".join([os.path.basename(s) for s in sample]))
    else:
        log("No .se1 files found.")

    if not ok:
        raise RuntimeError(
            "Swiss Ephemeris files are missing. "
            "Put *.se1 into ./ephe/ (repo) or directly into EPHE_PATH. "
            f"Checked path: {ephe_path}"
        )

if __name__ == "__main__":
    try:
        ensure_ephe()
        log("OK")
    except Exception as e:
        log(f"ERROR: {e}")
        raise
