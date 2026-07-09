"""
log_utils.py

Sets up run logging so that everything printed to the terminal
during a run is also written to a timestamped log file in the
output/logs/ folder.

This lets you:
  - Review what happened during any past run
  - Share a run log when asking for help diagnosing a problem
  - See exactly what settings were used and what output was produced

Log files are named by the actual wall-clock time the run STARTED,
not the analysis date, so you can always distinguish between two
runs made on the same machine for the same analysis date.

Example log filename:
  output/logs/run_2026-08-05_143022.log

The logger captures:
  - All print() output from the analysis
  - Any Python exceptions and their full tracebacks
  - A run summary at the end (total time, files produced)
"""

import os
import sys
import logging
import traceback
from datetime import datetime


# =====================================================
# TEE: WRITE TO BOTH CONSOLE AND LOG FILE
# =====================================================

class _Tee:
    """
    Replaces sys.stdout so every print() call goes to both the
    terminal and the log file simultaneously. This is the simplest
    way to capture all output without requiring every print()
    call in the codebase to be changed to logging.info().
    """

    def __init__(self, log_file_path, terminal_stream=None, mode="w"):
        self._terminal = terminal_stream or sys.stdout
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        self._logfile = open(log_file_path, mode, encoding="utf-8", buffering=1)

    def write(self, message):
        self._terminal.write(message)
        self._logfile.write(message)

    def flush(self):
        self._terminal.flush()
        self._logfile.flush()

    def close(self):
        self._logfile.flush()
        self._logfile.close()

    # Needed for tqdm and other tools that check sys.stdout attributes.
    def isatty(self):
        return self._terminal.isatty()

    def fileno(self):
        return self._terminal.fileno()


# =====================================================
# PUBLIC API
# =====================================================

_tee = None
_log_path = None
_run_start = None


def start_logging(output_dir, run_tag=None):
    """
    Begin capturing all terminal output to a log file.

    output_dir: the output/ folder path (log goes to output/logs/).
    run_tag:    optional string appended to the log filename, e.g.
                the analysis date+time tag so the log is easy to
                match to its output files.

    Returns the path of the log file being written to.
    """
    global _tee, _log_path, _run_start

    _run_start = datetime.now()
    timestamp  = _run_start.strftime("%Y-%m-%d_%H%M%S")

    if run_tag:
        filename = f"run_{timestamp}_{run_tag}.log"
    else:
        filename = f"run_{timestamp}.log"

    logs_dir  = os.path.join(output_dir, "logs")
    _log_path = os.path.join(logs_dir, filename)

    _tee = _Tee(_log_path)
    sys.stdout = _tee

    # Use sys.excepthook to capture unhandled crash tracebacks into the
    # log rather than redirecting all of sys.stderr. Redirecting stderr
    # also captures every tqdm progress bar frame and every _BatchDisplay
    # ANSI redraw -- this bloated log files to 100,000+ lines that were
    # impossible to read. excepthook fires only on an unhandled exception
    # right before Python exits, which is the only stderr case we need.
    import traceback as _tb
    _original_hook = sys.excepthook
    def _logging_excepthook(exc_type, exc_value, exc_tb):
        msg = "".join(_tb.format_exception(exc_type, exc_value, exc_tb))
        print(f"\n*** UNHANDLED EXCEPTION ***\n{msg}", flush=True)
        _original_hook(exc_type, exc_value, exc_tb)
    sys.excepthook = _logging_excepthook

    print(f"{'=' * 60}")
    print(f"  Satellite Visibility Tool -- Run Log")
    print(f"  Started : {_run_start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Log file: {_log_path}")
    print(f"{'=' * 60}\n")

    return _log_path


def log_run_settings(settings_dict):
    """
    Write a structured block of run settings to the log so the
    log is self-contained and can be read without cross-referencing
    config.py or memory of what was chosen at the prompts.
    """
    print("\n--- Run Settings ---")
    for key, value in settings_dict.items():
        print(f"  {key:<30}: {value}")
    print()


def log_exception(context=""):
    """
    Write the current exception's full traceback to the log.
    Call this inside an except block to capture the error.
    """
    msg = traceback.format_exc()
    if context:
        print(f"\n*** ERROR in {context} ***")
    else:
        print("\n*** ERROR ***")
    print(msg)


def finish_logging(output_files=None):
    """
    Write a run summary and close the log file.

    output_files: optional dict of {label: filepath} for files
    produced during this run, included in the summary so the log
    is a complete record of what was generated.
    """
    global _tee, _run_start

    if _run_start:
        elapsed = datetime.now() - _run_start
        total_min = elapsed.total_seconds() / 60

        print(f"\n{'=' * 60}")
        print(f"  Run complete")
        print(f"  Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Elapsed : {total_min:.1f} minutes")

        if output_files:
            print(f"  Output files:")
            for label, path in output_files.items():
                exists = os.path.exists(path) if path else False
                status = "OK" if exists else "NOT CREATED"
                print(f"    [{status:<11}] {os.path.basename(path) if path else 'n/a'}  ({label})")

        print(f"{'=' * 60}\n")

    if _tee:
        sys.stdout = _tee._terminal
        _tee.close()
        _tee = None

    if _log_path:
        print(f"Log saved: {_log_path}")


def get_log_path():
    """Return the current log file path, or None if logging hasn't started."""
    return _log_path
