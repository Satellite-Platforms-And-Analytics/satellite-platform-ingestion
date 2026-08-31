"""
Post-reorganization path check. Run on Windows with your real Python:

    python verify_paths.py

Confirms every path the platform depends on resolves after the
2026-08-30 folder reorganization. Read-only - creates nothing.
"""
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ok = True

def check(label, path, must_exist=True):
    global ok
    p = Path(path)
    hit = p.exists()
    if must_exist and not hit:
        ok = False
    print(f"  [{'OK ' if hit or not must_exist else 'MISS'}] {label:32s} {p}")

print("\n1. Data locations")
check("satellite db",   os.environ.get("SATELLITE_DB_DIR", r"D:\Databases\satellite"))
check("satellite imagery root", os.environ.get("SATELLITE_DATA_DIR", r"D:\SatelliteData"))
check("WIT data (WIT_BASE_DIR)", os.environ.get("WIT_BASE_DIR", r"D:\Databases\wit"))
check("WIT package (WIT_PATH)",  os.environ.get("WIT_PATH", r"D:\Projects\WIT"))

print("\n2. Satellite ingestion config")
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from src.imagery import ingest
    check("ingest.DB_PATH",      ingest.DB_PATH)
    check("ingest.CSV_LOG_PATH", ingest.CSV_LOG_PATH)
    check("ingest.RAW_DIR",      ingest.RAW_DIR)
except Exception as e:
    ok = False
    print(f"  [FAIL] could not import src.imagery.ingest: {e}")

print("\n3. WIT import + database")
try:
    from wit import config as wit_config
    check("wit.config.BASE_DIR", wit_config.BASE_DIR)
    check("wit.config.DB_PATH",  wit_config.DB_PATH)
except Exception as e:
    ok = False
    print(f"  [FAIL] could not import wit: {e}")
    print("         -> run 'pip install -e D:\\Projects\\WIT'")

print("\n4. Satellite <-> WIT bridge")
try:
    from src.resources import SatelliteResources
    with SatelliteResources() as res:
        n = len(res.tle_sources())
    print(f"  [OK ] bridge live - {n} TLE sources readable")
except Exception as e:
    ok = False
    print(f"  [FAIL] SatelliteResources: {e}")

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED - see above"))
sys.exit(0 if ok else 1)
