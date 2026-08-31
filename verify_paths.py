"""
Post-reorganization path check (2026-08-30 folder restructure).

    python verify_paths.py

Read-only - creates nothing, writes nothing. Distinguishes a BROKEN PATH
(what this script is for) from a MISSING DEPENDENCY (an environment issue,
reported separately so it can't be mistaken for a bad path).
"""
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
    _env = "loaded .env"
except ImportError:
    _env = "python-dotenv not installed - reading real environment only"

problems, notes = [], []

def check(label, path):
    p = Path(path)
    if p.exists():
        print(f"  [ OK ] {label:30s} {p}")
    else:
        print(f"  [MISS] {label:30s} {p}")
        problems.append(label)

print(f"\nEnvironment: {_env}")

# ── 1. Paths, derived exactly as the modules derive them ─────────────────────
print("\n1. Data locations")
DATA_DIR = Path(os.environ.get("SATELLITE_DATA_DIR", r"D:\SatelliteData"))
DB_DIR   = Path(os.environ.get("SATELLITE_DB_DIR",   r"D:\Databases\satellite"))
WIT_BASE = Path(os.environ.get("WIT_BASE_DIR",       r"D:\Databases\wit"))
WIT_PATH = Path(os.environ.get("WIT_PATH",           r"D:\Projects\WIT"))

check("SATELLITE_DATA_DIR", DATA_DIR)
check("  raw/sentinel-2",   DATA_DIR / "raw" / "sentinel-2")
check("  processed",        DATA_DIR / "processed")
check("SATELLITE_DB_DIR",   DB_DIR)
check("  satellite_platform.db", DB_DIR / "satellite_platform.db")
check("  ingestion_log.csv",     DB_DIR / "ingestion_log.csv")
check("WIT_BASE_DIR",       WIT_BASE)
check("  wit.db",           WIT_BASE / "wit.db")
check("WIT_PATH",           WIT_PATH)

# ── 2. Optional: imagery module (needs rasterio) ─────────────────────────────
print("\n2. Imagery pipeline config")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from src.imagery import ingest
    agree = ingest.DB_PATH == DB_DIR / "satellite_platform.db"
    print(f"  [{' OK ' if agree else 'FAIL'}] ingest.py resolves to {ingest.DB_PATH}")
    if not agree:
        problems.append("ingest.py path mismatch")
except ModuleNotFoundError as e:
    print(f"  [SKIP] dependency not installed ({e.name}) - not a path problem")
    notes.append(f"install {e.name} to check the imagery pipeline")
except Exception as e:
    print(f"  [FAIL] {type(e).__name__}: {e}")
    problems.append("ingest.py import")

# ── 3. WIT package + bridge ──────────────────────────────────────────────────
print("\n3. WIT package and satellite bridge")
try:
    from wit import config as wit_config
    agree = Path(wit_config.BASE_DIR) == WIT_BASE
    print(f"  [{' OK ' if agree else 'FAIL'}] wit.config.BASE_DIR -> {wit_config.BASE_DIR}")
    if not agree:
        problems.append("wit BASE_DIR mismatch")
except ModuleNotFoundError:
    print("  [SKIP] 'wit' not importable - run:  pip install -e " + str(WIT_PATH))
    notes.append("pip install -e " + str(WIT_PATH))
except Exception as e:
    print(f"  [FAIL] {type(e).__name__}: {e}")
    problems.append("wit import")

try:
    from src.resources import SatelliteResources
    with SatelliteResources() as res:
        counts = {
            "tle_sources":        len(res.tle_sources()),
            "tracking_databases": len(res.tracking_databases()),
            "launch_providers":   len(res.launch_providers()),
            "regulatory_sources": len(res.regulatory_sources()),
        }
    print("  [ OK ] bridge live -> " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if sum(counts.values()) == 0:
        notes.append("bridge returned 0 sites everywhere - check WIT_BASE_DIR")
except ModuleNotFoundError as e:
    print(f"  [SKIP] dependency not installed ({e.name})")
    notes.append(f"install {e.name} to check the bridge")
except Exception as e:
    print(f"  [FAIL] SatelliteResources: {type(e).__name__}: {e}")
    problems.append("satellite bridge")

# ── Summary ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if problems:
    print("PATH PROBLEMS FOUND:")
    for p in problems:
        print("  -", p)
else:
    print("ALL PATHS RESOLVE")
for n in notes:
    print("  note:", n)
sys.exit(1 if problems else 0)
