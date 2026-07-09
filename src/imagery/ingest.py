"""
Satellite Intelligence Platform — Ingestion Pipeline v1
========================================================
Processes Sentinel-2 .SAFE folders from staging/ through:
  1. Validation    — checks structure, bands, extracts metadata
  2. Conversion    — JP2 → GeoTIFF with LZW compression + tiling
  3. Indexing      — writes metadata to SQLite + CSV log
  4. Archiving     — moves .SAFE to raw/, cleans staging/

Usage:
    conda activate satellite-base
    python ingest.py                    # process all scenes in staging/
    python ingest.py --dry-run          # validate only, no conversion
    python ingest.py --scene SCENE_ID   # process specific scene
    python ingest.py --status           # show database summary

Environment: satellite-base
"""

import os
import sys
import csv
import glob
import json
import shutil
import signal
import sqlite3
import argparse
import logging
import subprocess
from pathlib import Path
from datetime import datetime, date
from typing import Optional

import rasterio
from rasterio.enums import Resampling

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

BASE_DIR        = Path("D:/SatelliteData")
STAGING_DIR     = BASE_DIR / "staging"
RAW_DIR         = BASE_DIR / "raw" / "sentinel-2"
PROCESSED_DIR   = BASE_DIR / "processed"
DB_PATH         = Path("D:/Databases/satellite_platform.db")
CSV_LOG_PATH    = Path("D:/Databases/ingestion_log.csv")

# GeoTIFF conversion settings
COMPRESS        = "lzw"
TILE_SIZE       = 512
BIGTIFF         = "IF_SAFER"

# QGIS gdal_translate — used for JP2 -> GeoTIFF conversion
# QGIS has a working JP2 driver; conda satellite-base DLL is broken
def _find_qgis_gdal():
    """Find QGIS gdal_translate and build its PROJ environment."""
    candidates = glob.glob(r"C:\Program Files\QGIS*\bin\gdal_translate.exe")
    if not candidates:
        return None, {}
    qgis_bin  = os.path.dirname(candidates[0])
    qgis_root = os.path.dirname(qgis_bin)
    env = os.environ.copy()
    env["PROJ_DATA"] = os.path.join(qgis_root, "share", "proj")
    env["PROJ_LIB"]  = os.path.join(qgis_root, "share", "proj")
    env["GDAL_DATA"] = os.path.join(qgis_root, "share", "gdal")
    env["PATH"]      = qgis_bin + os.pathsep + env.get("PATH", "")
    return candidates[0], env

QGIS_TRANSLATE, QGIS_ENV = _find_qgis_gdal()

# Sentinel-2 bands to convert (10m bands — highest value)
TARGET_BANDS = {
    "R10m": ["B02", "B03", "B04", "B08", "TCI", "AOT", "WVP"],
    "R20m": ["B05", "B06", "B07", "B8A", "B11", "B12", "SCL"],
}

# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING SETUP
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Interrupt flag
_interrupted = False

def _handle_interrupt(sig, frame):
    global _interrupted
    if not _interrupted:
        _interrupted = True
        log.warning("Ctrl+C — finishing current step then stopping gracefully...")
        log.warning("Press Ctrl+C again to force quit.")
    else:
        log.error("Force quit.")
        sys.exit(1)

signal.signal(signal.SIGINT, _handle_interrupt)


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE SETUP
# ══════════════════════════════════════════════════════════════════════════════

def init_database(db_path: Path) -> sqlite3.Connection:
    """Create database and tables if they don't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS scenes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id        TEXT UNIQUE NOT NULL,
            filename        TEXT NOT NULL,
            sensor          TEXT NOT NULL,
            satellite       TEXT,
            date_acquired   DATE NOT NULL,
            date_ingested   DATETIME NOT NULL,
            tile            TEXT,
            crs             TEXT,
            bounds_west     REAL,
            bounds_east     REAL,
            bounds_south    REAL,
            bounds_north    REAL,
            cloud_cover_pct REAL,
            bands           TEXT,
            resolution_m    REAL,
            shape_rows      INTEGER,
            shape_cols      INTEGER,
            file_size_mb    REAL,
            raw_path        TEXT,
            processed_path  TEXT,
            status          TEXT DEFAULT 'staging',
            processing_log  TEXT,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS processing_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id    TEXT NOT NULL,
            step        TEXT NOT NULL,
            status      TEXT NOT NULL,
            message     TEXT,
            duration_s  REAL,
            timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_scenes_date   ON scenes(date_acquired);
        CREATE INDEX IF NOT EXISTS idx_scenes_tile   ON scenes(tile);
        CREATE INDEX IF NOT EXISTS idx_scenes_sensor ON scenes(sensor);
        CREATE INDEX IF NOT EXISTS idx_scenes_status ON scenes(status);
        CREATE INDEX IF NOT EXISTS idx_scenes_cloud  ON scenes(cloud_cover_pct);
    """)
    conn.commit()
    log.info(f"Database ready: {db_path}")
    return conn


def log_step(conn: sqlite3.Connection, csv_path: Path,
             scene_id: str, step: str, status: str,
             message: str = "", duration_s: float = 0.0):
    """Write a pipeline step result to SQLite and CSV."""
    now = datetime.now().isoformat()

    # SQLite
    conn.execute(
        "INSERT INTO processing_log (scene_id, step, status, message, duration_s, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (scene_id, step, status, message, round(duration_s, 3), now)
    )
    conn.commit()

    # CSV
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "scene_id", "step", "status",
                             "message", "duration_s"])
        writer.writerow([now, scene_id, step, status, message,
                        round(duration_s, 3)])


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1 — VALIDATOR
# ══════════════════════════════════════════════════════════════════════════════

def parse_scene_id(safe_name: str) -> dict:
    """Extract metadata from .SAFE folder name."""
    # e.g. S2C_MSIL2A_20260621T173901_N0512_R098_T13SED_20260621T223314.SAFE
    parts = safe_name.replace(".SAFE", "").split("_")
    meta = {
        "satellite":    parts[0] if len(parts) > 0 else "Unknown",  # S2C
        "level":        parts[1] if len(parts) > 1 else "Unknown",  # MSIL2A
        "date_str":     parts[2] if len(parts) > 2 else "Unknown",  # 20260621T173901
        "tile":         parts[5] if len(parts) > 5 else "Unknown",  # T13SED
    }

    # Parse date
    try:
        meta["date_acquired"] = datetime.strptime(
            meta["date_str"][:8], "%Y%m%d").date().isoformat()
    except ValueError:
        meta["date_acquired"] = "Unknown"

    # Sensor name
    sat_map = {"S2A": "Sentinel-2A", "S2B": "Sentinel-2B", "S2C": "Sentinel-2C"}
    meta["sensor"] = sat_map.get(meta["satellite"], meta["satellite"])

    # Scene ID (short form for database)
    meta["scene_id"] = (f"{meta['satellite']}_{meta['date_str'][:8]}"
                       f"_{meta['tile']}")

    return meta


def find_granule(safe_path: Path) -> Optional[Path]:
    """Find the GRANULE subfolder inside a .SAFE folder."""
    granule_dir = safe_path / "GRANULE"
    if not granule_dir.exists():
        return None
    granules = list(granule_dir.iterdir())
    return granules[0] if granules else None


def find_bands(granule_path: Path) -> dict:
    """Find all band files organized by resolution."""
    found = {}
    img_data = granule_path / "IMG_DATA"
    if not img_data.exists():
        return found

    for res_folder, band_list in TARGET_BANDS.items():
        res_path = img_data / res_folder
        if not res_path.exists():
            continue
        found[res_folder] = {}
        for jp2_file in res_path.glob("*.jp2"):
            for band in band_list:
                if band in jp2_file.name:
                    found[res_folder][band] = jp2_file

    return found


def get_metadata_from_band(band_path: Path) -> dict:
    """Extract CRS, bounds, shape from a band file using rasterio."""
    try:
        with rasterio.open(band_path) as src:
            return {
                "crs":         str(src.crs),
                "bounds_west": src.bounds.left,
                "bounds_east": src.bounds.right,
                "bounds_south": src.bounds.bottom,
                "bounds_north": src.bounds.top,
                "resolution_m": src.res[0],
                "shape_rows":  src.height,
                "shape_cols":  src.width,
            }
    except Exception as e:
        log.warning(f"Could not read metadata from {band_path.name}: {e}")
        return {}


def validate_scene(safe_path: Path, conn: sqlite3.Connection,
                   csv_path: Path) -> tuple[bool, dict]:
    """Validate a .SAFE folder and extract metadata."""
    start = datetime.now()
    scene_meta = parse_scene_id(safe_path.name)
    scene_id = scene_meta["scene_id"]

    log.info(f"  [1/4] Validating: {scene_id}")

    # Check already processed
    existing = conn.execute(
        "SELECT status FROM scenes WHERE scene_id = ?", (scene_id,)
    ).fetchone()

    if existing and existing["status"] == "processed":
        log.warning(f"  ⏭  Already processed — skipping: {scene_id}")
        log_step(conn, csv_path, scene_id, "validate", "skipped",
                 "Already processed", 0)
        return False, scene_meta

    # Find granule
    granule = find_granule(safe_path)
    if not granule:
        msg = "GRANULE folder not found"
        log.error(f"  ❌ {msg}")
        log_step(conn, csv_path, scene_id, "validate", "failed", msg, 0)
        return False, scene_meta

    # Find bands
    bands = find_bands(granule)
    if not bands:
        msg = "No band files found in IMG_DATA"
        log.error(f"  ❌ {msg}")
        log_step(conn, csv_path, scene_id, "validate", "failed", msg, 0)
        return False, scene_meta

    # Get spatial metadata from first 10m band
    r10m_bands = bands.get("R10m", {})
    ref_band = r10m_bands.get("B04") or r10m_bands.get("TCI")
    spatial_meta = {}

    if ref_band:
        spatial_meta = get_metadata_from_band(ref_band)
        if spatial_meta:
            log.info(f"      CRS: {spatial_meta.get('crs')}")
            log.info(f"      Shape: {spatial_meta.get('shape_rows')} × "
                    f"{spatial_meta.get('shape_cols')}")
            log.info(f"      Resolution: {spatial_meta.get('resolution_m')}m")

    # Calculate file size
    total_size = sum(f.stat().st_size for f in safe_path.rglob("*")
                    if f.is_file())
    file_size_mb = round(total_size / (1024 * 1024), 1)

    # Compile all band names found
    all_bands = []
    for res, band_dict in bands.items():
        all_bands.extend(band_dict.keys())

    # Merge metadata
    full_meta = {
        **scene_meta,
        **spatial_meta,
        "filename":     safe_path.name,
        "file_size_mb": file_size_mb,
        "bands":        json.dumps(sorted(all_bands)),
        "bands_found":  bands,
        "granule_path": granule,
        "raw_path":     str(RAW_DIR / safe_path.name),
    }

    duration = (datetime.now() - start).total_seconds()
    band_count = len(all_bands)
    msg = f"Valid — {band_count} bands found, {file_size_mb}MB"
    log.info(f"  ✅ {msg}")
    log_step(conn, csv_path, scene_id, "validate", "success", msg, duration)

    return True, full_meta


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 2 — CONVERTER
# ══════════════════════════════════════════════════════════════════════════════

def convert_band(jp2_path: Path, output_dir: Path,
                 scene_id: str) -> Optional[Path]:
    """Convert a single JP2 band to GeoTIFF using QGIS gdal_translate."""
    out_name = jp2_path.stem + ".tif"
    out_path = output_dir / out_name

    if out_path.exists():
        log.info(f"      Already exists: {out_name}")
        return out_path

    if not QGIS_TRANSLATE:
        log.error("      QGIS gdal_translate not found")
        return None

    try:
        result = subprocess.run([
            QGIS_TRANSLATE,
            "-co", f"COMPRESS={COMPRESS.upper()}",
            "-co", "TILED=YES",
            "-co", f"BLOCKXSIZE={TILE_SIZE}",
            "-co", f"BLOCKYSIZE={TILE_SIZE}",
            str(jp2_path), str(out_path)
        ], capture_output=True, text=True, env=QGIS_ENV)

        if result.returncode == 0 and out_path.exists():
            log.info(f"      OK {out_name}")
            return out_path
        else:
            log.error(f"      Failed {out_name}: {result.stderr[:200]}")
            return None

    except Exception as e:
        log.error(f"      Failed {out_name}: {e}")
        return None

def convert_scene(meta: dict, conn: sqlite3.Connection,
                  csv_path: Path, dry_run: bool = False) -> Optional[Path]:
    """Convert all bands of a scene from JP2 to GeoTIFF."""
    start = datetime.now()
    scene_id = meta["scene_id"]
    log.info(f"  [2/4] Converting bands: {scene_id}")

    if dry_run:
        log.info("      DRY RUN — skipping conversion")
        return None

    # Output directory per scene
    scene_out_dir = PROCESSED_DIR / scene_id
    scene_out_dir.mkdir(parents=True, exist_ok=True)

    converted = []
    failed = []

    for res, band_dict in meta["bands_found"].items():
        res_dir = scene_out_dir / res
        res_dir.mkdir(exist_ok=True)

        for band_name, jp2_path in band_dict.items():
            if _interrupted:
                break
            result = convert_band(jp2_path, res_dir, scene_id)
            if result:
                converted.append(band_name)
            else:
                failed.append(band_name)

        if _interrupted:
            break

    duration = (datetime.now() - start).total_seconds()

    if failed:
        msg = f"Converted {len(converted)} bands, {len(failed)} failed: {failed}"
        status = "partial"
        log.warning(f"  ⚠️  {msg}")
    else:
        msg = f"Converted {len(converted)} bands in {duration:.1f}s"
        status = "success"
        log.info(f"  ✅ {msg}")

    log_step(conn, csv_path, scene_id, "convert", status, msg, duration)
    meta["processed_path"] = str(scene_out_dir)
    return scene_out_dir


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 3 — INDEXER
# ══════════════════════════════════════════════════════════════════════════════

def index_scene(meta: dict, conn: sqlite3.Connection,
                csv_path: Path, dry_run: bool = False):
    """Write scene metadata to SQLite database."""
    start = datetime.now()
    scene_id = meta["scene_id"]
    log.info(f"  [3/4] Indexing metadata: {scene_id}")

    if dry_run:
        log.info("      DRY RUN — skipping database write")
        return

    now = datetime.now().isoformat()

    try:
        conn.execute("""
            INSERT INTO scenes (
                scene_id, filename, sensor, satellite,
                date_acquired, date_ingested, tile,
                crs, bounds_west, bounds_east, bounds_south, bounds_north,
                cloud_cover_pct, bands, resolution_m,
                shape_rows, shape_cols, file_size_mb,
                raw_path, processed_path, status
            ) VALUES (
                :scene_id, :filename, :sensor, :satellite,
                :date_acquired, :date_ingested, :tile,
                :crs, :bounds_west, :bounds_east, :bounds_south, :bounds_north,
                :cloud_cover_pct, :bands, :resolution_m,
                :shape_rows, :shape_cols, :file_size_mb,
                :raw_path, :processed_path, :status
            )
            ON CONFLICT(scene_id) DO UPDATE SET
                processed_path = excluded.processed_path,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
        """, {
            "scene_id":       scene_id,
            "filename":       meta.get("filename", ""),
            "sensor":         meta.get("sensor", ""),
            "satellite":      meta.get("satellite", ""),
            "date_acquired":  meta.get("date_acquired", ""),
            "date_ingested":  now,
            "tile":           meta.get("tile", ""),
            "crs":            meta.get("crs", ""),
            "bounds_west":    meta.get("bounds_west"),
            "bounds_east":    meta.get("bounds_east"),
            "bounds_south":   meta.get("bounds_south"),
            "bounds_north":   meta.get("bounds_north"),
            "cloud_cover_pct": meta.get("cloud_cover_pct"),
            "bands":          meta.get("bands", "[]"),
            "resolution_m":   meta.get("resolution_m"),
            "shape_rows":     meta.get("shape_rows"),
            "shape_cols":     meta.get("shape_cols"),
            "file_size_mb":   meta.get("file_size_mb"),
            "raw_path":       meta.get("raw_path", ""),
            "processed_path": meta.get("processed_path", ""),
            "status":         "processed",
        })
        conn.commit()

        duration = (datetime.now() - start).total_seconds()
        msg = f"Metadata indexed to SQLite and CSV"
        log.info(f"  ✅ {msg}")
        log_step(conn, csv_path, scene_id, "index", "success", msg, duration)

    except Exception as e:
        duration = (datetime.now() - start).total_seconds()
        msg = f"Database error: {e}"
        log.error(f"  ❌ {msg}")
        log_step(conn, csv_path, scene_id, "index", "failed", msg, duration)


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 4 — ARCHIVER
# ══════════════════════════════════════════════════════════════════════════════

def archive_scene(safe_path: Path, meta: dict,
                  conn: sqlite3.Connection, csv_path: Path,
                  dry_run: bool = False):
    """Move .SAFE folder from staging/ to raw/sentinel-2/."""
    start = datetime.now()
    scene_id = meta["scene_id"]
    log.info(f"  [4/4] Archiving to raw/: {scene_id}")

    if dry_run:
        log.info("      DRY RUN — skipping move")
        return

    dest = RAW_DIR / safe_path.name

    try:
        if dest.exists():
            log.info(f"      ⏭  Already in raw/: {safe_path.name}")
        else:
            shutil.move(str(safe_path), str(dest))
            log.info(f"      ✓ Moved to: {dest}")

        duration = (datetime.now() - start).total_seconds()
        msg = f"Archived to {dest}"
        log.info(f"  ✅ {msg}")
        log_step(conn, csv_path, scene_id, "archive", "success", msg, duration)

    except Exception as e:
        duration = (datetime.now() - start).total_seconds()
        msg = f"Archive failed: {e}"
        log.error(f"  ❌ {msg}")
        log_step(conn, csv_path, scene_id, "archive", "failed", msg, duration)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def process_scene(safe_path: Path, conn: sqlite3.Connection,
                  csv_path: Path, dry_run: bool = False) -> bool:
    """Run the full 4-step pipeline on a single .SAFE folder."""
    print(f"\n{'═'*60}")
    log.info(f"Processing: {safe_path.name}")
    print(f"{'═'*60}")

    # Step 1 — Validate
    valid, meta = validate_scene(safe_path, conn, csv_path)
    if not valid:
        return False
    if _interrupted:
        return False

    # Step 2 — Convert
    processed_dir = convert_scene(meta, conn, csv_path, dry_run)
    if _interrupted:
        return False

    # Step 3 — Index
    index_scene(meta, conn, csv_path, dry_run)
    if _interrupted:
        return False

    # Step 4 — Archive
    archive_scene(safe_path, meta, conn, csv_path, dry_run)

    print(f"{'═'*60}")
    log.info(f"✅ Complete: {meta['scene_id']}")
    print(f"{'═'*60}\n")
    return True


def show_status(conn: sqlite3.Connection):
    """Print a summary of all scenes in the database."""
    print(f"\n{'═'*60}")
    print("  Satellite Platform — Scene Catalog")
    print(f"{'═'*60}")

    rows = conn.execute("""
        SELECT scene_id, sensor, date_acquired, tile,
               resolution_m, file_size_mb, status
        FROM scenes
        ORDER BY date_acquired DESC
    """).fetchall()

    if not rows:
        print("  No scenes indexed yet.")
    else:
        print(f"  {'Scene ID':<30} {'Date':<12} {'Tile':<8} "
              f"{'Res':>5} {'MB':>8} {'Status'}")
        print(f"  {'-'*30} {'-'*12} {'-'*8} {'-'*5} {'-'*8} {'-'*10}")
        for r in rows:
            print(f"  {r['scene_id']:<30} {r['date_acquired']:<12} "
                  f"{r['tile'] or '':<8} {r['resolution_m'] or 0:>4.0f}m "
                  f"{r['file_size_mb'] or 0:>7.0f} {r['status']}")

    total = conn.execute("SELECT COUNT(*) FROM scenes").fetchone()[0]
    processed = conn.execute(
        "SELECT COUNT(*) FROM scenes WHERE status='processed'"
    ).fetchone()[0]
    print(f"\n  Total: {total} scenes | Processed: {processed}")
    print(f"  DB: {DB_PATH}")
    print(f"  Log: {CSV_LOG_PATH}")
    print(f"{'═'*60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Satellite Intelligence Platform — Ingestion Pipeline v1"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate only — no conversion or file moves")
    parser.add_argument("--scene", type=str,
                        help="Process a specific scene by name")
    parser.add_argument("--status", action="store_true",
                        help="Show database summary and exit")
    args = parser.parse_args()

    # Ensure directories exist
    for d in [STAGING_DIR, RAW_DIR, PROCESSED_DIR,
              DB_PATH.parent, CSV_LOG_PATH.parent]:
        Path(d).mkdir(parents=True, exist_ok=True)

    # Init database
    conn = init_database(DB_PATH)

    # Status mode
    if args.status:
        show_status(conn)
        conn.close()
        return

    # Find scenes to process
    if args.scene:
        scene_path = STAGING_DIR / args.scene
        if not scene_path.exists():
            log.error(f"Scene not found in staging/: {args.scene}")
            conn.close()
            return
        scenes = [scene_path]
    else:
        scenes = [p for p in STAGING_DIR.iterdir()
                  if p.is_dir() and p.suffix == ".SAFE"]

    if not scenes:
        log.info("No .SAFE folders found in staging/")
        log.info(f"Drop Sentinel-2 .SAFE folders into: {STAGING_DIR}")
        conn.close()
        return

    print(f"\n{'═'*60}")
    print(f"  Ingestion Pipeline v1")
    print(f"  Found {len(scenes)} scene(s) to process")
    if args.dry_run:
        print(f"  MODE: DRY RUN — no files will be modified")
    print(f"{'═'*60}")

    # Process each scene
    success_count = 0
    fail_count = 0

    for safe_path in sorted(scenes):
        if _interrupted:
            log.warning("Pipeline interrupted — stopping after current scene")
            break

        success = process_scene(safe_path, conn, CSV_LOG_PATH, args.dry_run)
        if success:
            success_count += 1
        else:
            fail_count += 1

    # Final summary
    print(f"\n{'═'*60}")
    print(f"  Pipeline Complete")
    print(f"  ✅ Processed: {success_count}")
    print(f"  ❌ Failed:    {fail_count}")
    print(f"  📊 Database:  {DB_PATH}")
    print(f"  📋 CSV Log:   {CSV_LOG_PATH}")
    print(f"{'═'*60}\n")

    show_status(conn)
    conn.close()


if __name__ == "__main__":
    main()
