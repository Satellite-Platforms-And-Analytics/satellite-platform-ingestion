"""
grouped_report.py

Cross-references the visibility results (output/visible_satellites.xlsx)
with the historical confidence scores (output/historical_accuracy_report.xlsx)
to produce a grouped, color-coded tasking-style report.

LAYOUT: each distinct exact (Date, Time (Zulu)) match defines one
group, auto-numbered as Design Point 1, 2, 3... in chronological
order. The Date / Design Point / Time (Zulu) cells are genuinely
MERGED (a real spanning Excel cell, not just blanked-out repeats)
across every row in that group, with a thin divider line above each
new group. Every satellite is still its own row underneath.

COLOR CODING: the satellite-specific columns (Target Name / Orbit /
NORAD) are filled with a background color based on that satellite's
historical confidence category -- green/stable through red/unstable,
with distinct colors for "insufficient data" and "query failed" so
those aren't confused with an actual low-stability finding. The
merged Date/Design Point/Time block gets a neutral header style
instead, since a group can contain satellites with different
confidence categories. This color coding is applied ONLY in this
file, not in the original visible_satellites.xlsx.

A Legend sheet explains each confidence category, its underlying
0-100 confidence-score range, and what it means.

See also: confidence_tabs_report.py, which splits satellites onto
separate sheets by confidence category instead of color-coding them
on one sheet.

Run:
    python grouped_report.py

Requires both main.py and historical_accuracy.py to have been run
first -- needs both output files to exist.
"""

import os

from openpyxl import Workbook

from config import OUTPUT_FILE
from report_utils import load_merged_data, write_grouped_sheet, write_legend_sheet
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
        report_file   = report_file  or run_files["grouped_report"]

    merged, design_point_lookup = load_merged_data(output_file, accuracy_file)

    wb = Workbook()
    ws = wb.active
    ws.title = "Grouped Report"

    num_rows, num_groups = write_grouped_sheet(
        ws, merged, design_point_lookup, apply_color=True
    )

    write_legend_sheet(wb, apply_color=True)

    os.makedirs(os.path.dirname(report_file), exist_ok=True)
    wb.save(report_file)

    print(f"Saved {num_rows} satellite rows across {num_groups} time groups")
    print(f"Output: {report_file}")


if __name__ == "__main__":
    main()
