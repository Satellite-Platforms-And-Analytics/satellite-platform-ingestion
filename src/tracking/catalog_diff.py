"""
catalog_diff.py

Compares a freshly downloaded TLE catalog against the snapshot from
before that download, to detect:
  - satellites newly added to the catalog
  - satellites removed from the catalog (decayed / deorbited)
  - satellites whose orbital elements changed enough to suggest a
    real orbital change (maneuver) since the last snapshot, as
    opposed to ordinary day-to-day drag decay / measurement noise

...then cross-references that against historical_accuracy_report.xlsx
(if it exists) to recommend whether re-running historical_accuracy.py
is actually worthwhile, instead of asking blindly every time.
"""

import os
import shutil

import pandas as pd

from satellite_utils import extract_tle_records, parse_tle_orbital_elements
from config import (
    CATALOG_ALTITUDE_CHANGE_THRESHOLD_KM,
    CATALOG_INCLINATION_CHANGE_THRESHOLD_DEG,
)


def backup_current_catalog(tle_file):
    """
    Copy the current TLE catalog file to a sibling ".previous" file
    BEFORE a new download overwrites it, so compare_and_recommend()
    has a prior snapshot to diff against.

    Returns the backup path, or None if there was no existing
    catalog file to back up (e.g. the very first run ever).
    """

    if not os.path.exists(tle_file):
        return None

    backup_path = tle_file + ".previous"
    shutil.copyfile(tle_file, backup_path)
    return backup_path


def compare_catalogs(old_file, new_file):
    """
    Compare two TLE catalog snapshots.

    Returns a dict:
      new_ids:     NORAD IDs present in new_file but not old_file
      removed_ids: NORAD IDs present in old_file but not new_file
      changed_ids: NORAD IDs present in both, but with altitude or
                   inclination different enough (see thresholds in
                   config.py) to suggest a real orbital change
    """

    old_records = extract_tle_records(old_file)
    new_records = extract_tle_records(new_file)

    old_ids = set(old_records)
    new_ids_set = set(new_records)

    new_ids = new_ids_set - old_ids
    removed_ids = old_ids - new_ids_set
    common_ids = old_ids & new_ids_set

    changed_ids = set()

    for norad_id in common_ids:
        try:
            old_elements = parse_tle_orbital_elements(*old_records[norad_id])
            new_elements = parse_tle_orbital_elements(*new_records[norad_id])
        except Exception:
            continue

        alt_delta = abs(new_elements["altitude_km"] - old_elements["altitude_km"])
        incl_delta = abs(new_elements["inclination_deg"] - old_elements["inclination_deg"])

        if (
            alt_delta > CATALOG_ALTITUDE_CHANGE_THRESHOLD_KM
            or incl_delta > CATALOG_INCLINATION_CHANGE_THRESHOLD_DEG
        ):
            changed_ids.add(norad_id)

    return {
        "new_ids": new_ids,
        "removed_ids": removed_ids,
        "changed_ids": changed_ids,
    }


def compare_and_recommend(old_file, new_file, accuracy_file):
    """
    Run compare_catalogs(), print a human-readable summary, and
    return True if re-running historical_accuracy.py is recommended,
    False otherwise.
    """

    diff = compare_catalogs(old_file, new_file)

    new_ids = diff["new_ids"]
    removed_ids = diff["removed_ids"]
    changed_ids = diff["changed_ids"]

    previously_evaluated_ids = set()
    accuracy_report_exists = os.path.exists(accuracy_file)

    if accuracy_report_exists:
        try:
            acc_df = pd.read_excel(accuracy_file)
            previously_evaluated_ids = set(acc_df["Target NORAD"].dropna().astype(int))
        except Exception:
            accuracy_report_exists = False

    # Changed satellites that were already evaluated: their existing
    # confidence score may now be stale. New satellites that have
    # never been evaluated at all: need a first-time evaluation.
    stale_ids = changed_ids & previously_evaluated_ids
    never_evaluated_new_ids = new_ids - previously_evaluated_ids

    print("\n=== Catalog Change Summary (vs. previous snapshot) ===")
    print(f"New satellites added to catalog: {len(new_ids)}")
    print(f"Satellites removed from catalog (decayed/deorbited): {len(removed_ids)}")
    print(
        f"Satellites with significant orbital changes (possible maneuver): "
        f"{len(changed_ids)}"
    )

    if accuracy_report_exists:
        print(f"  - Of those changed, {len(stale_ids)} were previously evaluated and may now be stale")
        print(f"  - Of the new satellites, {len(never_evaluated_new_ids)} have never been evaluated")
    else:
        print(
            "  (No historical_accuracy_report.xlsx found yet -- nothing to "
            "compare evaluation status against.)"
        )

    needs_attention = len(stale_ids) + len(never_evaluated_new_ids)

    if not accuracy_report_exists:
        recommend = False
        print(
            "\nRecommendation: No prior historical accuracy report exists yet, "
            "so there's nothing to compare staleness against. Run "
            "historical_accuracy.py whenever you're ready for an initial "
            "evaluation."
        )
    elif needs_attention == 0:
        recommend = False
        print(
            "\nRecommendation: No re-run needed -- no new satellites and no "
            "significant orbital changes detected for previously-evaluated "
            "satellites since the last historical accuracy run."
        )
    else:
        recommend = True
        print(
            f"\nRecommendation: Re-running historical_accuracy.py is "
            f"recommended ({needs_attention} satellite(s) are new or may "
            f"have stale confidence scores)."
        )

    return recommend
