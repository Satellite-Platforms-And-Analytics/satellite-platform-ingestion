"""
confidence_tabs_report.py

Cross-references the visibility results (output/visible_satellites.xlsx)
with the historical confidence scores (output/historical_accuracy_report.xlsx)
and produces a workbook with one TAB PER CONFIDENCE CATEGORY, instead
of one sheet with color-coded rows (that's grouped_report.py).

Each tab uses the same grouped/merged-cell layout (Date / Design
Point / Time (Zulu) merged across each group's row span) as
grouped_report.py, but with NO color coding -- a tab's name already
tells you the confidence category, so a fill color would be
redundant here.

Design Point numbers are shared with grouped_report.py (same
underlying lookup, computed once across the full dataset), so a
given Design Point number means the same time-group no matter which
file or tab you're looking at.

Tabs are created only for confidence categories that actually have
at least one satellite in your results; empty categories are
skipped rather than producing a blank tab. A final Legend sheet
explains each confidence category, its underlying 0-100 confidence-
score range, and what it means (no color coding here, since the
data tabs themselves have none).

Run:
    python confidence_tabs_report.py

Requires both main.py and historical_accuracy.py to have been run
first -- needs both output files to exist.
"""

import os

from openpyxl import Workbook

from config import OUTPUT_FILE
from report_utils import load_merged_data, write_grouped_sheet, write_legend_sheet, CONFIDENCE_ORDER
from run_state import get_run_files


def main(output_file=None, accuracy_file=None, report_file=None):
    """
    output_file, accuracy_file, report_file: when called from
    main.py these are the timestamped paths for this run. When
    called standalone they default to None, and the most recent
    run's paths are read from last_run.json (written by main.py).
    """

    if output_file is None or accuracy_file is None or report_file is None:
        run_files     = get_run_files()
        output_file   = output_file  or run_files["visible_satellites"]
        accuracy_file = accuracy_file or run_files["accuracy_report"]
        report_file   = report_file  or run_files["tabs_report"]

    merged, design_point_lookup = load_merged_data(output_file, accuracy_file)

    wb = Workbook()
    wb.remove(wb.active)  # drop the default blank sheet, we add our own below

    total_rows = 0
    sheets_created = 0

    for category in CONFIDENCE_ORDER:

        subset = merged[merged["Confidence Category"] == category]

        if len(subset) == 0:
            continue

        ws = wb.create_sheet(category)
        num_rows, num_groups = write_grouped_sheet(
            ws, subset, design_point_lookup, apply_color=False
        )

        total_rows += num_rows
        sheets_created += 1

        print(f"  {category}: {num_rows} satellites across {num_groups} time groups")

    if sheets_created == 0:
        print("No satellites found in any confidence category -- nothing to save.")
        return

    write_legend_sheet(wb, apply_color=False)

    os.makedirs(os.path.dirname(report_file), exist_ok=True)
    wb.save(report_file)

    print(f"\nSaved {total_rows} satellite rows across {sheets_created} tabs")
    print(f"Output: {report_file}")


if __name__ == "__main__":
    main()
