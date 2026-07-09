"""
run_state.py

Reads the last_run.json file that main.py writes after each run,
returning the paths to the timestamped output files for that run.

Used by historical_accuracy.py, grouped_report.py, and
confidence_tabs_report.py so they always work with the correct
timestamped files -- whether they're launched standalone or
chained through main.py -- without needing the date/time to be
re-entered.

If last_run.json is missing (e.g. on a very first run where
main.py hasn't been run yet, or the file was deleted), these
scripts fall back to the un-timestamped defaults from config.py
and print a clear message explaining what they're doing.
"""

import os
import json

from config import OUTPUT_FILE, BASE_DIR


_LAST_RUN_FILE = os.path.join(os.path.dirname(OUTPUT_FILE), "last_run.json")


def load_last_run():
    """
    Load and return the last_run.json state dict, or None if it
    doesn't exist yet.
    """
    if not os.path.exists(_LAST_RUN_FILE):
        return None
    try:
        with open(_LAST_RUN_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def get_run_files(run=None):
    """
    Return a dict of {role: filepath} for the files belonging to
    a specific run. If run is None, loads the most recent run from
    last_run.json. If no state file exists, returns the untimstamped
    config.py defaults with a printed warning.

    Keys returned:
      visible_satellites  -- main visibility results
      accuracy_report     -- historical confidence scores
      grouped_report      -- grouped/color-coded report
      tabs_report         -- per-confidence-tab report
      tag                 -- the date+time label (e.g. 2026-08-05_0600-1800Z)
    """

    if run is None:
        run = load_last_run()

    if run is not None:
        return {
            "visible_satellites": run["visible_satellites"],
            "accuracy_report":    run["accuracy_report"],
            "grouped_report":     run["grouped_report"],
            "tabs_report":        run["tabs_report"],
            "tag":                run["tag"],
            "lookback_years":     run.get("lookback_years", 5),
            "_raw":               run,   # full dict for callers that need other fields
        }

    # No state file -- fall back to config.py defaults.
    out_dir = os.path.dirname(OUTPUT_FILE)
    print(
        "  Note: last_run.json not found. Using default (un-timestamped)\n"
        "  filenames from config.py. Run main.py first to generate\n"
        "  timestamped output files.\n"
    )
    return {
        "visible_satellites": OUTPUT_FILE,
        "accuracy_report":    os.path.join(out_dir, "historical_accuracy_report.xlsx"),
        "grouped_report":     os.path.join(out_dir, "grouped_confidence_report.xlsx"),
        "tabs_report":        os.path.join(out_dir, "Final_Report_With_Confidence_Tabs.xlsx"),
        "tag":                "unknown",
    }
