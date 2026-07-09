"""
historical_accuracy.py

Scores each satellite's prediction confidence based on how stable
its orbit has been over a historical lookback window.

Run after main.py has generated visible_satellites.xlsx:

    python historical_accuracy.py

Queries Space-Track for historical orbital element data and writes
output/historical_accuracy_report_<tag>.xlsx.

Requires a Space-Track account. Set credentials in your .env file:
    SPACETRACK_USERNAME=you@example.com
    SPACETRACK_PASSWORD=your-password

Performance settings (HISTORICAL_BATCH_SIZE, HISTORICAL_CONCURRENT_REQUESTS,
HISTORICAL_QUERY_TIMEOUT_SEC) are auto-tuned at startup based on your
hardware and network latency. See config.py to override manually.
"""

import os
import sys
import time
import math
import socket
import platform
import threading
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from tqdm import tqdm

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

from config import (
    OUTPUT_FILE,
    SPACETRACK_USERNAME,
    SPACETRACK_PASSWORD,
    REQUEST_TIMEOUT_SEC,
    HISTORICAL_LOOKBACK_YEARS,
    HISTORICAL_MAX_SATELLITES,
    HISTORICAL_BATCH_SIZE,
    HISTORICAL_QUERY_TIMEOUT_SEC,
    HISTORICAL_CONCURRENT_REQUESTS,
    GCAT_CACHE_FILE,
    GCAT_CACHE_MAX_AGE_HOURS,
    TLE_HISTORY_CACHE_DB,
    SATCAT_CACHE_DB,
    SATCAT_CACHE_MAX_AGE_HOURS,
    API_REQUEST_LOG_DB,
    BASE_DIR,
    TLE_DATA_DIR,
    SATELLITE_CONFIDENCE_DB,
)
import tle_history_cache
import satcat_cache
import spacetrack_policy_check
import api_request_log
import satellite_confidence_db as _conf_db
from satellite_utils import (
    spacetrack_login,
    ensure_spacetrack_session,
    SpaceTrackRateLimiter,
    get_historical_orbital_elements_batch_with_retry,
    fetch_satcat_data,
    fetch_gcat_catalog,
)
from run_state import get_run_files


# =====================================================
# HARDWARE PROFILING (auto-tune performance settings)
# =====================================================

def _get_cpu_cores():
    cores = os.cpu_count()
    return cores if cores else 2


def _get_available_ram_gb():
    if _HAS_PSUTIL:
        try:
            return psutil.virtual_memory().available / (1024 ** 3)
        except Exception:
            pass
    if platform.system() == "Windows":
        try:
            import ctypes, ctypes.wintypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength",                ctypes.wintypes.DWORD),
                    ("dwMemoryLoad",            ctypes.wintypes.DWORD),
                    ("ullTotalPhys",            ctypes.c_uint64),
                    ("ullAvailPhys",            ctypes.c_uint64),
                    ("ullTotalPageFile",        ctypes.c_uint64),
                    ("ullAvailPageFile",        ctypes.c_uint64),
                    ("ullTotalVirtual",         ctypes.c_uint64),
                    ("ullAvailVirtual",         ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]
            mem = MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            return mem.ullAvailPhys / (1024 ** 3)
        except Exception:
            pass
    return 4.0


def _measure_latency_ms(host="www.space-track.org", port=443, attempts=3, timeout=5):
    timings = []
    for _ in range(attempts):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            start = time.perf_counter()
            sock.connect((host, port))
            timings.append((time.perf_counter() - start) * 1000)
            sock.close()
        except Exception:
            pass
        time.sleep(0.1)
    return min(timings) if timings else 9999.0


def _auto_configure():
    """
    Measure CPU cores, available RAM, and network latency to
    Space-Track, then derive the three performance settings that
    most affect how long the historical evaluation takes.
    Returns a dict of recommended values and prints the reasoning.
    """
    print("  Detecting hardware and network conditions...")
    cpu   = _get_cpu_cores()
    ram   = _get_available_ram_gb()
    print("  Measuring network latency to Space-Track", end="", flush=True)
    lat   = _measure_latency_ms()
    print(f"... {lat:.0f} ms")

    # Concurrent requests: bounded by CPU and latency
    if   lat < 100:   lat_rec, lat_why = 5,  f"low latency ({lat:.0f} ms)"
    elif lat < 200:   lat_rec, lat_why = 8,  f"moderate latency ({lat:.0f} ms)"
    elif lat < 400:   lat_rec, lat_why = 10, f"above-average latency ({lat:.0f} ms)"
    elif lat < 9999:  lat_rec, lat_why = 12, f"high latency ({lat:.0f} ms)"
    else:             lat_rec, lat_why = 4,  "Space-Track unreachable during profiling"
    cpu_rec    = min(cpu * 2, 14)

    # Space-Track's published rate limit (28/min, enforced by
    # SpaceTrackRateLimiter) is the real ceiling on throughput for
    # this workload, since each batch is exactly one request. CPU and
    # latency-based concurrency recommendations above were previously
    # computed in isolation from that limit, so on fast/low-latency
    # connections the tool would recommend e.g. 5+ concurrent workers
    # when individual gp_history queries for large multi-year batches
    # routinely take 60-200+ seconds to return -- well past what the
    # limiter alone would suggest is needed, since those workers
    # spend almost all their time waiting on Space-Track's response,
    # not on the limiter. Concurrency much above ~rate_limit/typical
    # batches_per_minute buys nothing once batches are individually
    # slow: extra threads just pile up making the SAME 28-per-minute
    # budget look busier without moving more data, and the in-flight
    # display in past runs showed batches climbing past 2-3 minutes
    # each as more workers queued up. A soft cap keeps the
    # recommendation honest about what the limiter can actually
    # sustain rather than recommending workers that just sit blocked.
    rate_limit_per_min = 28
    # Assume a single batch typically takes at least ~20s round-trip
    # even for small/fast responses; more than ~rate_limit/3 workers
    # rarely has headroom to all be usefully in-flight at once given
    # that budget, so soft-cap there rather than letting CPU/latency
    # alone push concurrency arbitrarily high.
    rate_limiter_cap = max(3, rate_limit_per_min // 3)
    concurrent = max(3, min(lat_rec, cpu_rec, rate_limiter_cap))

    # Batch size: keep working set under 10% of available RAM
    bytes_per_sat = 547 * 1024
    raw_batch = int((ram * 0.10 * 1024**3) / (bytes_per_sat * concurrent))
    batch_size = max(25, min(raw_batch, 200))

    # Timeout: transfer + processing time × 3 safety margin
    bw = min(10.0, 1000.0 / max(lat, 50))
    transfer = (batch_size * bytes_per_sat / (1024**2)) / bw
    processing = batch_size * 0.2
    timeout = int(max(60, min((transfer + processing) * 3, 300)))

    print()
    print("  ┌─────────────────────────────────────────────────────────┐")
    print("  │  Hardware Profile                                       │")
    print("  ├─────────────────────────────────────────────────────────┤")
    print(f"  │  CPU logical cores   : {cpu:<36}│")
    print(f"  │  Available RAM       : {ram:.1f} GB{'':<32}│")
    lat_str = f"{lat:.0f} ms" if lat < 9999 else "unreachable during profiling"
    print(f"  │  Space-Track latency : {lat_str:<36}│")
    print("  ├─────────────────────────────────────────────────────────┤")
    print("  │  Recommended Settings                                   │")
    print("  ├─────────────────────────────────────────────────────────┤")
    print(f"  │  CONCURRENT_REQUESTS : {concurrent:<36}│")
    print(f"  │  BATCH_SIZE          : {batch_size:<36}│")
    print(f"  │  QUERY_TIMEOUT_SEC   : {timeout:<36}│")
    print("  └─────────────────────────────────────────────────────────┘")
    print()
    print(f"  CONCURRENT_REQUESTS: {concurrent}")
    print(f"    CPU limit {cpu_rec} (from {cpu} cores); network limit {lat_rec} ({lat_why}); "
          f"rate-limit ceiling {rate_limiter_cap} ({rate_limit_per_min}/min Space-Track budget)")
    print(f"  BATCH_SIZE: {batch_size} satellites")
    print(f"    {ram:.1f} GB RAM × 10% ÷ {concurrent} concurrent ÷ ~547 KB/sat = {raw_batch} → clamped to [25,200]")
    print(f"  QUERY_TIMEOUT_SEC: {timeout}s")
    print(f"    transfer ~{transfer:.0f}s + processing ~{processing:.0f}s × 3 safety margin")
    print()
    print("  These settings apply for this run only.")
    print("  To make them permanent, update config.py manually.")
    print()

    return {
        "HISTORICAL_CONCURRENT_REQUESTS": concurrent,
        "HISTORICAL_BATCH_SIZE":          batch_size,
        "HISTORICAL_QUERY_TIMEOUT_SEC":   timeout,
    }


# =====================================================
# CONFIDENCE SCORING MODEL
# =====================================================

def _score_orbital_stability(elements, lookback_years):
    """
    Score how stable a satellite's orbit has been over a historical
    lookback window.  Returns a dict with confidence_score (0-100),
    category label, and the underlying statistics.

    Score = coverage component (0-40, rewards data spanning the
    full lookback window) + stability component (0-60, penalizes
    altitude and inclination variability).

    CAVEAT: this is a heuristic, not a calibrated probability model.
    Use it as a triage signal -- which predictions deserve scrutiny --
    not as a precise accuracy guarantee.
    """
    if len(elements) < 2:
        return {
            "confidence_score": 0.0,
            "category": "Insufficient historical data",
            "data_points": len(elements),
            "years_covered": 0.0,
            "altitude_cv": None,
            "inclination_std_deg": None,
            "note": (
                "Fewer than 2 historical TLE records found. Typical for "
                "recently launched objects. Treat the visibility prediction "
                "as model-only, not history-backed."
            ),
        }

    alts  = [e["altitude_km"]    for e in elements]
    incls = [e["inclination_deg"] for e in elements]

    mean_alt  = sum(alts)  / len(alts)
    std_alt   = math.sqrt(sum((a - mean_alt)**2  for a in alts)  / len(alts))
    altitude_cv = (std_alt / mean_alt) if mean_alt else 0.0

    mean_incl = sum(incls) / len(incls)
    inclination_std_deg = math.sqrt(
        sum((i - mean_incl)**2 for i in incls) / len(incls)
    )

    years_covered = (elements[-1]["epoch"] - elements[0]["epoch"]).days / 365.25
    coverage  = 40.0 * min(1.0, years_covered / lookback_years) if lookback_years else 0.0
    stability = 60.0 * math.exp(-(40.0 * altitude_cv) - (8.0 * inclination_std_deg))
    score     = coverage + stability

    if   score >= 80: category = "High"
    elif score >= 50: category = "Moderate"
    elif score >= 20: category = "Low"
    else:             category = "Very low"

    return {
        "confidence_score":    round(score, 1),
        "category":            category,
        "data_points":         len(elements),
        "years_covered":       round(years_covered, 2),
        "altitude_cv":         round(altitude_cv, 5),
        "inclination_std_deg": round(inclination_std_deg, 4),
        "note":                None,
    }


# =====================================================
# BATCH PROCESSING
# =====================================================


# =====================================================
# BATCH PROGRESS DISPLAY
# =====================================================

import shutil as _shutil

# ANSI colour codes -- gracefully disabled when the terminal does
# not support them (e.g. redirected output, very old Windows).
def _ansi_ok():
    import sys, os
    if not hasattr(sys.stderr, "isatty"):
        return False
    if not sys.stderr.isatty():
        return False
    if os.name == "nt":
        # Enable VT processing on Windows 10+
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            # Get current mode on stderr handle (STD_ERROR_HANDLE = -12)
            handle = kernel.GetStdHandle(-12)
            mode   = ctypes.c_ulong()
            kernel.GetConsoleMode(handle, ctypes.byref(mode))
            kernel.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass
    return True

_USE_ANSI = _ansi_ok()

class _C:
    """Terminal colour/style helpers.  All no-ops when ANSI is off."""
    R  = "\033[0m"   if _USE_ANSI else ""   # reset
    B  = "\033[1m"   if _USE_ANSI else ""   # bold
    DIM= "\033[2m"   if _USE_ANSI else ""   # dim
    GN = "\033[32m"  if _USE_ANSI else ""   # green
    YL = "\033[33m"  if _USE_ANSI else ""   # yellow
    RD = "\033[31m"  if _USE_ANSI else ""   # red
    CY = "\033[36m"  if _USE_ANSI else ""   # cyan
    BL = "\033[34m"  if _USE_ANSI else ""   # blue
    MG = "\033[35m"  if _USE_ANSI else ""   # magenta
    WH = "\033[97m"  if _USE_ANSI else ""   # bright white
    BG_DARK = "\033[48;5;235m" if _USE_ANSI else ""  # near-black bg


class _BatchDisplay:
    """
    Self-contained terminal display for historical confidence evaluation.

    Draws a live panel on sys.stderr that shows:
      • Header   -- run date, lookback style, total satellites
      • Overall  -- batch progress bar + time stats
      • Scoring  -- current satellite being scored (updates in place)
      • In-Flight-- one bar per concurrent Space-Track request,
                    showing elapsed wait time with a slow-query warning
      • Completed-- rolling log of the last 8 finished batches

    All output goes to sys.stderr so it doesn't interfere with the
    log_utils _Tee on sys.stdout.  Key events are also printed to
    stdout (via the normal print()) so they appear in the log file.

    Callers:
      d = _BatchDisplay(total_batches, total_sats, run_tag, lookback_desc)
      d.start()
      d.batch_fetching(bn)            # network request started
      d.batch_done(bn, n_sats, elapsed_sec, n_failed, total_scored)
      d.scoring_update(norad, name)   # called per-satellite during scoring
      d.stop()
    """

    _SLOW_WARN_SEC  = 90    # flag in-flight batch as slow after this long
    _BAR_WIDTH      = 28    # chars for the filled bar section
    _MAX_COMPLETED  = 8     # rows to show in the completed table

    def __init__(self, total_batches, total_sats, run_tag="", lookback_desc=""):
        self._total_batches  = total_batches
        self._total_sats     = total_sats
        self._run_tag        = run_tag
        self._lookback_desc  = lookback_desc

        self._done_batches   = 0
        self._scored_sats    = 0
        self._start_time     = None
        self._batch_times    = []   # elapsed secs for completed batches
        self._in_flight      = {}   # bn -> start_time
        self._completed      = []   # list of dicts for the table
        self._scoring_info   = ""   # current satellite being scored
        self._lock           = threading.Lock()
        self._stop_event     = threading.Event()
        self._draw_thread    = None
        self._lines_drawn    = 0    # how many lines the last draw wrote
        self._last_fingerprint = None  # used to skip redundant non-ANSI repaints

    # ── Public API ─────────────────────────────────────────────────

    def start(self):
        self._start_time = time.time()
        self._draw_thread = threading.Thread(
            target=self._draw_loop, daemon=True
        )
        self._draw_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._draw_thread:
            self._draw_thread.join(timeout=2)
        self._clear_panel()

    def batch_fetching(self, bn):
        with self._lock:
            self._in_flight[bn] = time.time()

    def batch_done(self, bn, n_sats, elapsed_sec, n_failed, total_scored):
        with self._lock:
            self._in_flight.pop(bn, None)
            self._done_batches += 1
            self._scored_sats   = total_scored
            self._batch_times.append(elapsed_sec)
            self._completed.insert(0, {
                "bn":      bn,
                "sats":    n_sats,
                "elapsed": elapsed_sec,
                "failed":  n_failed,
                "scored":  total_scored,
            })
            if len(self._completed) > self._MAX_COMPLETED:
                self._completed.pop()

    def scoring_update(self, norad, name=""):
        with self._lock:
            label = f"{norad}"
            if name:
                label = f"{name[:20]} ({norad})"
            self._scoring_info = label

    # ── Internal drawing ───────────────────────────────────────────

    def _draw_loop(self):
        # When stderr isn't a real interactive terminal (redirected to
        # a file, piped through `tee`, captured by a wrapper script,
        # etc.) there is no cursor-based in-place redraw available --
        # _USE_ANSI is False and _clear_panel() becomes a no-op, so
        # every redraw prints a brand-new static block instead of
        # overwriting the last one. Left at the interactive 0.5s
        # cadence that adds up to thousands of near-duplicate blocks
        # over a multi-minute run and bloats any captured/piped log
        # file for no benefit (nobody is watching it redraw live).
        # Slow the cadence way down in that case so the non-interactive
        # output stays readable and small; keep it snappy when there's
        # a real terminal to animate.
        interval = 0.5 if _USE_ANSI else 15.0
        while not self._stop_event.is_set():
            self._redraw()
            self._stop_event.wait(interval)

    def _clear_panel(self):
        if self._lines_drawn and _USE_ANSI:
            sys.stderr.write(
                f"\033[{self._lines_drawn}A"   # move up N lines
                f"\033[J"                       # clear from cursor to end
            )
            sys.stderr.flush()
        self._lines_drawn = 0

    def _bar(self, fraction, width=None, full_char="█", empty_char="░"):
        width = width or self._BAR_WIDTH
        filled = max(0, min(width, int(width * fraction)))
        return full_char * filled + empty_char * (width - filled)

    def _fmt_time(self, seconds):
        if seconds < 60:
            return f"{seconds:.0f}s"
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m{s:02d}s"

    def _redraw(self):
        now   = time.time()
        lines = []
        W     = min(80, _shutil.get_terminal_size().columns - 2)

        with self._lock:
            done      = self._done_batches
            total     = self._total_batches
            scored    = self._scored_sats
            elapsed   = now - self._start_time if self._start_time else 0
            avg_sec   = (sum(self._batch_times) / len(self._batch_times)) if self._batch_times else 0
            remaining = max(0, (total - done) * avg_sec) if avg_sec else 0
            in_flight = dict(self._in_flight)
            completed = list(self._completed)
            scoring   = self._scoring_info

        # In non-interactive mode each redraw is a brand-new printed
        # block (there's no cursor to overwrite), so skip the repaint
        # entirely when nothing meaningful has changed since the last
        # one -- otherwise the elapsed-time clock alone would still
        # produce a new block every interval even with zero progress.
        if not _USE_ANSI:
            fingerprint = (done, scored, tuple(sorted(in_flight)), scoring)
            if fingerprint == self._last_fingerprint:
                return
            self._last_fingerprint = fingerprint

        # ── Header ─────────────────────────────────────────────────
        title = "Historical Confidence Evaluation"
        lines.append(f"{_C.WH}{_C.B} {title}{_C.R}")
        meta = f" {self._run_tag}"
        if self._lookback_desc:
            meta += f"  ·  {self._lookback_desc}"
        lines.append(f"{_C.DIM}{meta}{_C.R}")
        lines.append(f"{_C.DIM}{'─' * W}{_C.R}")

        # ── Overall bar ────────────────────────────────────────────
        frac  = done / total if total else 0
        pct   = frac * 100
        color = _C.GN if frac >= 0.9 else (_C.CY if frac >= 0.5 else _C.BL)
        bar   = self._bar(frac)
        lines.append(
            f" {_C.B}Overall{_C.R}  "
            f"{color}[{bar}]{_C.R} "
            f"{_C.B}{done}/{total}{_C.R} batches  "
            f"({pct:.0f}%)"
        )
        time_str = (
            f" {_C.DIM}{self._fmt_time(elapsed)} elapsed"
            + (f"  ·  ~{self._fmt_time(remaining)} remaining" if avg_sec else "")
            + (f"  ·  {avg_sec:.1f}s/batch avg" if avg_sec else "")
            + f"  ·  {scored:,} satellites scored"
            + f"{_C.R}"
        )
        lines.append(f"          {time_str}")

        if scoring:
            lines.append(
                f"          {_C.DIM}Scoring: {scoring}{_C.R}"
            )

        # ── In-flight ──────────────────────────────────────────────
        lines.append("")
        lines.append(
            f" {_C.B}In-Flight{_C.R}  "
            f"{_C.DIM}{'─' * (W - 11)}{_C.R}"
        )
        if in_flight:
            sorted_if = sorted(in_flight.items())
            for bn, t0 in sorted_if:
                wait     = now - t0
                # Bar shows what fraction of the auto-timeout has been used.
                # Hard-code 120s reference; actual timeout may vary.
                frac_if  = min(1.0, wait / 120)
                slow     = wait > self._SLOW_WARN_SEC
                if_color = _C.RD if slow else (_C.YL if frac_if > 0.5 else _C.CY)
                if_bar   = self._bar(frac_if, width=22,
                                     full_char="▓", empty_char="░")
                flag     = f"  {_C.RD}⚠ slow{_C.R}" if slow else ""
                lines.append(
                    f"   Batch {bn:>3}  "
                    f"{if_color}[{if_bar}]{_C.R}  "
                    f"{_C.B}{self._fmt_time(wait):>6}{_C.R}{flag}"
                )
        else:
            lines.append(f"   {_C.DIM}(idle){_C.R}")

        # ── Completed log ──────────────────────────────────────────
        lines.append("")
        hdr = (
            f" {_C.B}Completed{_C.R}  "
            f"{_C.DIM}── Batch ── Sats ── Network ─ Failed ─ Running Total ──{_C.R}"
        )
        lines.append(hdr)
        for row in completed:
            fail_str = (
                f"{_C.RD}{row['failed']:>4}{_C.R}"
                if row["failed"]
                else f"{_C.GN}{row['failed']:>4}{_C.R}"
            )
            net_color = _C.YL if row["elapsed"] > 60 else _C.GN
            lines.append(
                f"   {_C.GN}✓{_C.R}  "
                f"Batch {row['bn']:>4}/{total}   "
                f"{row['sats']:>5}   "
                f"{net_color}{row['elapsed']:>7.1f}s{_C.R}  "
                f"{fail_str}   "
                f"{_C.B}{row['scored']:>8,}{_C.R}"
            )
        if not completed:
            lines.append(f"   {_C.DIM}(waiting for first batch to complete){_C.R}")

        lines.append(f"{_C.DIM}{'─' * W}{_C.R}")

        # ── Render ─────────────────────────────────────────────────
        self._clear_panel()
        output = "\n".join(lines) + "\n"
        sys.__stderr__.write(output)
        sys.__stderr__.flush()
        self._lines_drawn = len(lines)



# =====================================================
# ORBIT BEHAVIOR DETECTION
# =====================================================

# Thresholds for orbit behavior classification
_MANEUVER_ALT_JUMP_KM   = 5.0    # altitude change in <= 3 days = maneuver
_MANEUVER_WINDOW_DAYS   = 7.0    # maneuver may take several days to appear in TLE stream
_RAPID_DECAY_KM_DAY     = -0.5   # very fast altitude loss -- reentry risk
_NORMAL_DECAY_KM_DAY    = -0.03  # typical LEO atmospheric drag (~10 km/year loss)
_SIGNIFICANT_CHANGE_KM  = 20.0   # historical mean shift worth flagging
_RECENT_WINDOW_DAYS     = 90     # "recent" analysis window

# Display colors (hex, no #) per behavior flag -- used in Excel reports
BEHAVIOR_STYLE = {
    "Stable":             {"fill": "FFFFFF", "font": "000000"},
    "Decaying":           {"fill": "FFF2CC", "font": "7F6000"},
    "Rapid Decay":        {"fill": "F4B942", "font": "7F3F00"},
    "Maneuvering":        {"fill": "DDEBF7", "font": "1F4E79"},
    "Recently Changed":   {"fill": "E8D5F5", "font": "4B0082"},
    "Actively Managed":   {"fill": "B4A7D6", "font": "20124D"},
    "Insufficient Data":  {"fill": "D9D9D9", "font": "404040"},
}


def _detect_orbit_behavior(elements):
    """
    Analyze a satellite's TLE history and classify its orbital
    behavior to flag satellites whose orbits are changing or likely
    to change.

    Detection logic:
      1. Maneuver events -- a sudden altitude jump (> 5 km within
         3 days) that cannot be explained by atmospheric drag alone
         indicates a thruster burn. Count how many occurred in the
         last 90 days.
      2. Decay rate -- sustained altitude loss over the recent 90-day
         window. Rapid decay (> 0.5 km/day) suggests reentry risk.
         Normal drag decay is expected for uncontrolled LEO objects.
      3. Historical mean shift -- if the satellite's recent mean
         altitude differs significantly from its older mean, it has
         been repositioned even if no individual maneuver was large
         enough to cross the jump threshold.

    Returns a dict with:
      behavior_flag      -- category string
      behavior_detail    -- plain-English explanation for the report
      maneuvers_90d      -- maneuver count in last 90 days
      alt_change_90d_km  -- altitude change over last 90 days (km)
    """
    from datetime import timedelta

    if len(elements) < 3:
        return {
            "behavior_flag":     "Insufficient Data",
            "behavior_detail":   "Too few historical records to analyse behavior.",
            "maneuvers_90d":     None,
            "alt_change_90d_km": None,
        }

    elements  = sorted(elements, key=lambda e: e["epoch"])
    altitudes = [e["altitude_km"] for e in elements]
    epochs    = [e["epoch"]       for e in elements]

    latest    = epochs[-1]
    cut90     = latest - timedelta(days=_RECENT_WINDOW_DAYS)

    recent    = [e for e in elements if e["epoch"] >= cut90]
    older     = [e for e in elements if e["epoch"] <  cut90]

    # ── Detect maneuvers ────────────────────────────────────────
    # A maneuver is a sudden altitude jump between two consecutive
    # TLEs that are close in time, where the change is large enough
    # that atmospheric drag alone cannot explain it.
    maneuver_events = []
    for i in range(1, len(elements)):
        dt_days   = (epochs[i] - epochs[i-1]).total_seconds() / 86400
        alt_delta = altitudes[i] - altitudes[i-1]
        if dt_days <= _MANEUVER_WINDOW_DAYS and abs(alt_delta) >= _MANEUVER_ALT_JUMP_KM:
            maneuver_events.append({"epoch": epochs[i], "delta_km": alt_delta})

    maneuvers_90d = sum(1 for m in maneuver_events if m["epoch"] >= cut90)
    maneuvers_all = len(maneuver_events)

    # ── Altitude change over last 90 days ───────────────────────
    if len(recent) >= 2:
        alt_change_90d = recent[-1]["altitude_km"] - recent[0]["altitude_km"]
        span_days      = max(
            (recent[-1]["epoch"] - recent[0]["epoch"]).total_seconds() / 86400, 1
        )
        decay_rate     = alt_change_90d / span_days
    elif len(elements) >= 2:
        alt_change_90d = altitudes[-1] - altitudes[-2]
        decay_rate     = 0.0
    else:
        alt_change_90d = 0.0
        decay_rate     = 0.0

    # ── Historical mean shift ────────────────────────────────────
    recent_mean = (sum(e["altitude_km"] for e in recent) / len(recent)) if recent else None
    older_mean  = (sum(e["altitude_km"] for e in older)  / len(older))  if older  else None

    # ── Classify ─────────────────────────────────────────────────
    if maneuvers_90d >= 3:
        flag   = "Actively Managed"
        detail = (
            f"{maneuvers_90d} maneuver(s) in last 90 days "
            f"(net altitude change: {alt_change_90d:+.1f} km). "
            "This satellite is under active orbital management -- "
            "orbit may change at any time and prediction confidence "
            "will decrease after each burn."
        )
    elif maneuvers_90d >= 1:
        flag   = "Maneuvering"
        detail = (
            f"{maneuvers_90d} maneuver(s) detected in last 90 days "
            f"(net altitude change: {alt_change_90d:+.1f} km). "
            "Recent burns reduce prediction reliability until the orbit "
            "has had time to stabilise and be refit by ground stations."
        )
    elif maneuvers_all >= 2 and recent_mean and older_mean:
        shift = recent_mean - older_mean
        if abs(shift) >= _SIGNIFICANT_CHANGE_KM:
            flag   = "Recently Changed"
            direction = "raised" if shift > 0 else "lowered"
            detail = (
                f"Mean altitude {direction} by {abs(shift):.0f} km compared to "
                f"earlier history (no maneuvers in last 90 days -- orbit now stable "
                f"at new altitude). Verify predictions use recent elements."
            )
        else:
            flag   = "Stable"
            detail = (
                f"No significant changes. 90-day altitude change: {alt_change_90d:+.1f} km."
            )
    elif decay_rate <= _RAPID_DECAY_KM_DAY:
        flag   = "Rapid Decay"
        detail = (
            f"Altitude declining at {decay_rate:.2f} km/day "
            f"(90-day total: {alt_change_90d:.1f} km). "
            "Object may reenter within months. Predictions degrade "
            "rapidly -- use the most recent available TLE."
        )
    elif decay_rate <= _NORMAL_DECAY_KM_DAY:
        flag   = "Decaying"
        detail = (
            f"Normal drag decay: {abs(decay_rate):.3f} km/day "
            f"(90-day total: {alt_change_90d:.1f} km). Expected for "
            "an uncontrolled LEO object. Predictions remain valid but "
            "degrade faster than for higher/stable orbits."
        )
    else:
        flag   = "Stable"
        detail = (
            f"No significant changes detected. "
            f"90-day altitude change: {alt_change_90d:+.1f} km."
        )

    return {
        "behavior_flag":     flag,
        "behavior_detail":   detail,
        "maneuvers_90d":     maneuvers_90d,
        "alt_change_90d_km": round(alt_change_90d, 2),
    }




# ── Per-satellite optimal lookback computation ────────────────────────────

# Orbit-type floor minimums -- even a stable satellite needs this many
# years before the confidence score is statistically meaningful.
_ORBIT_LOOKBACK_MINIMUMS = {"LEO": 3, "MEO": 3, "GEO": 3, "HEO": 5}


def _find_change_onset_days(elements):
    """
    Search a satellite's element history for the earliest significant
    change -- the first maneuver event or the point where a sustained
    trend (decay acceleration, mean altitude shift) began.

    Returns the approximate number of days before the latest epoch
    that the change started, or None if no clear onset is found.

    Knowing the onset lets _determine_optimal_lookback ask for just
    enough history to see the satellite's pre-change behaviour as a
    baseline, without pulling decades of irrelevant data.
    """
    from datetime import timedelta

    if len(elements) < 5:
        return None

    elements = sorted(elements, key=lambda e: e["epoch"])
    altitudes = [e["altitude_km"] for e in elements]
    epochs    = [e["epoch"]       for e in elements]
    latest    = epochs[-1]

    # Check for the earliest maneuver jump
    for i in range(1, len(elements)):
        dt   = (epochs[i] - epochs[i - 1]).total_seconds() / 86400
        delta = abs(altitudes[i] - altitudes[i - 1])
        if dt <= _MANEUVER_WINDOW_DAYS and delta >= _MANEUVER_ALT_JUMP_KM:
            return (latest - epochs[i]).days

    # Check for a significant mean altitude shift between the first
    # and second halves of the history (gradual repositioning / decay
    # onset that didn't produce a detectable single-step jump).
    mid = len(elements) // 2
    early_mean = sum(altitudes[:mid]) / mid
    late_mean  = sum(altitudes[mid:]) / (len(elements) - mid)
    if abs(late_mean - early_mean) >= _SIGNIFICANT_CHANGE_KM:
        return (latest - epochs[mid]).days

    return None


def _determine_optimal_lookback(elements, behavior_flag, orbit_type, max_years=10,
                                 scan_years=2):
    """
    Compute the optimal number of years to query for one satellite,
    given its scan-window data and detected behavior flag.

    Logic:
      Stable / Decaying     → orbit-type minimum (no benefit from more).
                              Decaying means ordinary atmospheric drag --
                              expected for essentially every uncontrolled
                              LEO object -- not an anomaly that needs a
                              deeper look-back baseline. It is grouped
                              with Stable here, not just defaulted to
                              orbit_min as a fallback further down,
                              because routine drag over a 2-year scan
                              window routinely shifts mean altitude by
                              more than the 20 km "significant change"
                              threshold on its own. Left ungrouped, that
                              would send _find_change_onset_days down
                              the mean-shift branch and escalate almost
                              every LEO satellite to a multi-year
                              re-query for an answer that works out to
                              the same orbit_min value as Stable anyway
                              -- i.e. a full second Space-Track query
                              pass bought nothing. (Confirmed against a
                              real run: 13,180 of 13,196 satellites
                              were escalated to a 3-year re-query this
                              way, when the vast majority were ordinary
                              drag with no actual anomaly.)
      Insufficient Data     → only extend if the earliest record found
                              is near the edge of the scan window (i.e.
                              the satellite likely predates the scan and
                              older records exist to find). If the
                              earliest record is recent, the satellite
                              was simply launched recently -- there is
                              no additional history further back to
                              find, and re-querying at max_years would
                              just re-fetch the same sparse data at a
                              much higher cost for zero new information.
      Actively Managed      → max_years (need full operational pattern)
      Other flagged flags   → orbit_min + enough years before first change
                              so the scoring model has a stable baseline
                              to compare against (Rapid Decay,
                              Maneuvering, Recently Changed -- these are
                              genuine anomalies, unlike routine drag)

    The return value is clamped to [orbit_minimum, max_years].
    """
    from datetime import timedelta

    orbit_min = _ORBIT_LOOKBACK_MINIMUMS.get(orbit_type, 3)

    if behavior_flag in ("Stable", "Decaying"):
        return orbit_min

    if behavior_flag == "Insufficient Data":
        # Only worth extending if the earliest record we found is
        # close to the start of the scan window -- that's the signal
        # the satellite existed before the scan started and there may
        # be more history to find further back. If the earliest
        # record is well inside the window (i.e. recently launched),
        # there's nothing older to find; keep the scan-window result.
        if not elements:
            return orbit_min
        elements_sorted = sorted(elements, key=lambda e: e["epoch"])
        earliest = elements_sorted[0]["epoch"]
        latest   = elements_sorted[-1]["epoch"]
        scan_start = latest - timedelta(days=scan_years * 365.25)
        # "Near the edge" = earliest record within 90 days of when the
        # scan window started looking -- i.e. data exists right up to
        # the boundary, suggesting older data likely exists too.
        near_edge = (earliest - scan_start).days <= 90
        return max_years if near_edge else orbit_min

    if behavior_flag == "Actively Managed":
        return max_years

    # For all other flags: find when the change started and add a
    # 2-year buffer before it so the scoring model sees stable history
    # as well as the anomaly period.
    onset_days = _find_change_onset_days(elements)

    if onset_days is not None:
        years_needed = math.ceil((onset_days / 365.25) + 2.0)
    else:
        # No clear onset -- use behavior-based defaults.
        # "Decaying" is intentionally absent here: it's handled by the
        # early return above alongside "Stable", since routine drag
        # decay shouldn't reach this onset-detection path at all.
        _BEHAVIOR_DEFAULTS = {
            "Rapid Decay":      5,
            "Maneuvering":      7,
            "Recently Changed": 5,
        }
        years_needed = _BEHAVIOR_DEFAULTS.get(behavior_flag, max_years)

    # Per-flag floors: certain behavior types need a minimum depth
    # regardless of when the onset was detected in the scan window.
    _FLAG_FLOORS = {
        "Rapid Decay":      5,  # need full decay arc, not just recent acceleration
        "Maneuvering":      5,  # understand whether burns are a new pattern or ongoing
        "Recently Changed": 5,  # capture the before-and-after clearly
    }
    flag_floor = _FLAG_FLOORS.get(behavior_flag, orbit_min)

    return max(max(orbit_min, flag_floor), min(years_needed, max_years))

def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _score_batch(batch, elements_by_norad, failed_norad_ids, lookback_years, orbit_lookback_map=None, lookback_used_map=None, batch_label="", display=None):
    """
    Score one completed batch's satellites and return report rows.
    Shows a per-satellite inner progress bar (position=2).

    lookback_years is passed explicitly -- it is NOT a global so
    that this function works correctly regardless of how main() is
    invoked (standalone, chained from main.py, or tested directly).
    """
    failed_set = set(failed_norad_ids)
    rows = []

    import sys as _sys
    import sys as _sys
    inner_bar = tqdm(
        batch,
        desc=f"  Scoring {batch_label}",
        unit="sat",
        total=len(batch),
        position=2,
        leave=False,
        file=_sys.stderr,
        dynamic_ncols=True,
        bar_format=(
            "{desc}: {n_fmt}/{total_fmt} "
            "[{elapsed}<{remaining}, {rate_fmt}] | norad={postfix}"
        ),
    )

    for row in inner_bar:
        norad_id   = int(row["Target NORAD"])
        name       = row["Target Name"]
        orbit_type = row["Target Orbit"]
        inner_bar.set_postfix(norad=norad_id)
        if display:
            display.scoring_update(norad_id, name)

        if norad_id in failed_set:
            failed_sat_lookback = (
                lookback_used_map.get(norad_id) if lookback_used_map else lookback_years
            )
            rows.append({
                "Target NORAD": norad_id, "Target Name": name,
                "Target Orbit": orbit_type,
                "Confidence Score (0-100)": None,
                "Confidence Category": "Query failed",
                "Orbit Behavior":           "Insufficient Data",
                "Behavior Detail":          "Query failed -- no history available.",
                "Maneuvers (last 90 days)": None,
                "Altitude Change 90d (km)": None,
                "Lookback Used (years)":    failed_sat_lookback,
                "Historical Data Points": None, "Years of History Found": None,
                "Altitude Variability (CV)": None, "Inclination Std Dev (deg)": None,
                "Note": (
                    "Space-Track query failed/timed out even after retry. "
                    "Re-run to retry -- this is a transient network issue, "
                    "not a finding about the satellite."
                ),
            })
            continue

        elements = elements_by_norad.get(norad_id, [])
        # Determine the scoring window for this satellite:
        # lookback_used_map holds the per-satellite optimal years
        # computed after the initial scan. Falling back to
        # orbit_lookback_map, then the global lookback_years.
        if lookback_used_map and norad_id in lookback_used_map:
            sat_lookback = lookback_used_map[norad_id]
        elif orbit_lookback_map:
            sat_lookback = orbit_lookback_map.get(orbit_type, lookback_years)
        else:
            sat_lookback = lookback_years
        score    = _score_orbital_stability(elements, sat_lookback)
        behavior = _detect_orbit_behavior(elements)
        rows.append({
            "Target NORAD":               norad_id,
            "Target Name":                name,
            "Target Orbit":               orbit_type,
            "Confidence Score (0-100)":   score["confidence_score"],
            "Confidence Category":        score["category"],
            "Orbit Behavior":             behavior["behavior_flag"],
            "Behavior Detail":            behavior["behavior_detail"],
            "Maneuvers (last 90 days)":   behavior["maneuvers_90d"],
            "Altitude Change 90d (km)":   behavior["alt_change_90d_km"],
            "Lookback Used (years)":      sat_lookback,
            "Historical Data Points":     score["data_points"],
            "Years of History Found":     score["years_covered"],
            "Altitude Variability (CV)":  score["altitude_cv"],
            "Inclination Std Dev (deg)":  score["inclination_std_deg"],
            "Note":                       score["note"] or "",
        })

    inner_bar.close()
    return rows


def _cached_fetch(session, norad_ids, start_date, end_date, timeout_sec,
                   rate_limiter):
    """
    Cache-aware wrapper around get_historical_orbital_elements_batch_with_retry.

    Consults the persistent local TLE history cache (tle_history_cache.py)
    first: satellites whose requested [start_date, end_date] window is
    already fully covered by previously-fetched data are read straight
    from disk with NO Space-Track query at all. Satellites that are
    partially covered (e.g. cached through an earlier run's "now"
    boundary) only have the missing incremental slice fetched, not
    the whole range again. Only genuinely new/uncached satellites get
    a full-range query.

    This is the single shared implementation used by both Pass 1 and
    the extended-lookback (3yr/5yr/etc. year-group) passes in main(),
    so the "don't re-query what's already cached" behavior is applied
    consistently everywhere a gp_history fetch happens -- duplicating
    this logic separately in each call site would risk one of them
    drifting out of sync and silently bypassing the cache.

    Returns (elements_by_norad, failed_norad_ids), same shape as
    get_historical_orbital_elements_batch_with_retry.
    """
    fully_cached, needs_fetch = tle_history_cache.split_cached_vs_needed(
        TLE_HISTORY_CACHE_DB, norad_ids, start_date, end_date
    )

    elements_by_norad = {}
    failed_ids = []

    if fully_cached:
        elements_by_norad.update(
            tle_history_cache.load_cached_elements(
                TLE_HISTORY_CACHE_DB, fully_cached
            )
        )

    if needs_fetch:
        # Group satellites needing the SAME fetch range so they can
        # still be sent as one batched request each (Space-Track's
        # own guidance: combine objects into one comma-delimited
        # query rather than one request per satellite).
        range_groups = {}
        for nid, (f_start, f_end) in needs_fetch.items():
            range_groups.setdefault((f_start, f_end), []).append(nid)

        for (f_start, f_end), group_ids in range_groups.items():
            fetched, group_failed = get_historical_orbital_elements_batch_with_retry(
                session, group_ids, f_start, f_end,
                timeout_sec=timeout_sec, rate_limiter=rate_limiter,
            )
            successfully_queried = [
                nid for nid in group_ids if nid not in set(group_failed)
            ]
            if successfully_queried:
                tle_history_cache.store_elements(
                    TLE_HISTORY_CACHE_DB, successfully_queried, fetched,
                    f_start, f_end,
                )
            elements_by_norad.update(fetched)
            failed_ids.extend(group_failed)

        # Merge previously-cached history back in for satellites whose
        # fetch only covered a NEW slice (because part of the window
        # was already cached), so the caller sees the complete picture.
        needs_merge_with_cache = [
            nid for nid in needs_fetch if nid not in set(failed_ids)
        ]
        if needs_merge_with_cache:
            merged = tle_history_cache.load_cached_elements(
                TLE_HISTORY_CACHE_DB, needs_merge_with_cache
            )
            for nid in needs_merge_with_cache:
                if nid in merged:
                    elements_by_norad[nid] = merged[nid]

    return elements_by_norad, failed_ids


# =====================================================
# MAIN
# =====================================================

def main(output_file=None, accuracy_file=None, lookback_years=None, lookback_style="single",
         offline_mode=False):
    """
    output_file, accuracy_file: when called from main.py these are
    the timestamped paths for this run. When called standalone they
    default to None, in which case last_run.json is read.

    lookback_years: years of history to pull per satellite. When
    called from main.py this is the user's prompt choice. When
    standalone it falls back to last_run.json then config.py.

    offline_mode: if True, skip Space-Track entirely (no login, no
    network calls of any kind) and score every satellite using ONLY
    whatever historical TLE data is already in the local cache
    (data/tle_history_cache.sqlite3). This is the backup path for
    when Space-Track itself is unavailable -- down, your account is
    suspended/under review, or you simply don't want to make any
    Space-Track requests right now. It uses ONLY data this tool has
    already legitimately fetched and stored locally in past runs
    (which Space-Track's own usage policy explicitly tells you to
    do: "you need to store it on your own servers"), so there is no
    policy concern with running this mode as often as you like.

    Coverage is necessarily limited to whatever has been cached by
    previous online runs -- a satellite with no prior cached history
    will be reported as "Insufficient historical data" the same as
    today, just because there's genuinely nothing local to score it
    from (not because a query failed). Run normally (online) first
    to build up cache coverage, then offline_mode becomes a
    genuinely useful fallback for situations where you need a result
    NOW and can't reach Space-Track.
    """

    print("─" * 60)
    print("Historical Confidence Evaluation")
    print("─" * 60)

    if offline_mode:
        print(
            "OFFLINE MODE -- skipping Space-Track entirely. Scoring\n"
            "satellites using ONLY data already in the local TLE\n"
            "history cache (data/tle_history_cache.sqlite3) from\n"
            "previous runs. No network calls to Space-Track will be\n"
            "made. Satellites with no cached history will show as\n"
            "'Insufficient historical data' -- run normally (online)\n"
            "first to build up coverage for them.\n"
        )
    else:
        print(
            "Queries Space-Track for orbital history per satellite\n"
            "and scores how stable each orbit has been.\n"
            "Checkpoints are saved after each batch so progress is\n"
            "not lost if the run is interrupted.\n"
        )

    if not offline_mode and (not SPACETRACK_USERNAME or not SPACETRACK_PASSWORD):
        raise RuntimeError(
            "SPACETRACK_USERNAME / SPACETRACK_PASSWORD not set. "
            "Add them to your .env file. (Or pass offline_mode=True "
            "to score satellites from the local cache only, without "
            "needing Space-Track credentials at all.)"
        )

    # Resolve file paths
    if output_file is None or accuracy_file is None:
        run_files     = get_run_files()
        output_file   = output_file   or run_files["visible_satellites"]
        accuracy_file = accuracy_file or run_files["accuracy_report"]
        _tag          = run_files["tag"]
        if lookback_years is None:
            lookback_years = run_files.get("lookback_years", HISTORICAL_LOOKBACK_YEARS)
    else:
        _tag = (
            os.path.basename(accuracy_file)
            .replace("historical_accuracy_report_", "")
            .replace(".xlsx", "")
        )
        if lookback_years is None:
            lookback_years = HISTORICAL_LOOKBACK_YEARS

    if _tag and _tag != "unknown":
        print(f"Run tag : {_tag}")
    if lookback_style == "smart_adaptive" and isinstance(lookback_years, dict):
        max_yrs = lookback_years.get("max_years", 10)
        # Mirrors the scan_years calculation in the smart_adaptive
        # branch below -- kept as a separate min() call here rather
        # than restructuring this early preview to share state with
        # that later block, since this print only needs the number,
        # not the rest of the strategy setup.
        preview_scan_years = min(_ORBIT_LOOKBACK_MINIMUMS.values())
        print(
            f"Lookback: Smart Adaptive ({preview_scan_years}-year scan → "
            f"up to {max_yrs}-year depth)\n"
        )
    else:
        print(f"Lookback: {lookback_years} year(s)\n")

    if not os.path.exists(output_file):
        raise RuntimeError(
            f"{output_file} not found. Run main.py first."
        )

    df = pd.read_excel(output_file)
    unique_sats = df[["Target NORAD", "Target Name", "Target Orbit"]].drop_duplicates(
        subset="Target NORAD"
    )
    total_unique = len(unique_sats)
    if HISTORICAL_MAX_SATELLITES is not None:
        unique_sats = unique_sats.head(HISTORICAL_MAX_SATELLITES)

    # ── Skip satellites already evaluated in a previous run ──────
    # Load existing results and carry them forward so we only query
    # Space-Track for satellites that are genuinely new or previously
    # failed. "Query failed" satellites are always retried since the
    # failure was transient, not a finding about the satellite.
    existing_results         = {}
    existing_missing_behavior = set()   # NORADs with no behavior data

    # ── Load from permanent confidence DB first ───────────────────
    # The permanent DB stores results for every satellite ever scored
    # across ALL runs, regardless of date/time window. This means a
    # satellite scored in last week's run is carried forward today
    # without re-evaluation, even if the run tag differs.
    try:
        db_existing = _conf_db.load_all_results(SATELLITE_CONFIDENCE_DB)
        if db_existing:
            # Re-evaluate "Insufficient historical data" satellites if the
            # TLE cache now has data for them (delta sync may have filled gaps)
            insufficient_to_recheck = {
                norad for norad, row in db_existing.items()
                if row.get("Confidence Category") == "Insufficient historical data"
                and _conf_db.needs_recheck(
                    SATELLITE_CONFIDENCE_DB, norad, TLE_HISTORY_CACHE_DB
                )
            }
            if insufficient_to_recheck:
                print(
                    f"  {len(insufficient_to_recheck):,} 'Insufficient' satellites "
                    f"now have TLE cache data -- will re-evaluate.",
                    flush=True,
                )
                for norad in insufficient_to_recheck:
                    del db_existing[norad]

            print(
                f"  Permanent confidence DB: {len(db_existing):,} satellites "
                f"already scored -- carried forward, zero re-evaluation needed.",
                flush=True,
            )
            existing_results.update(db_existing)
    except Exception as _e:
        print(f"  (Could not read permanent confidence DB: {_e})", flush=True)

    # ── Also read the timestamped per-run file (for behavior data) ─
    # The per-run file may have behavior columns not yet in the DB
    # (e.g. from this very run tag's earlier checkpoint). Merge it in.
    if os.path.exists(accuracy_file):
        try:
            ex_df = pd.read_excel(accuracy_file)
            file_has_behavior = (
                "Orbit Behavior" in ex_df.columns
                and ex_df["Orbit Behavior"].notna().any()
            )
            if not file_has_behavior:
                print(
                    "  Existing accuracy file has no Orbit Behavior data "
                    "(generated by an earlier version). All satellites "
                    "will be re-evaluated to add behavior detection."
                )

            for _, ex_row in ex_df.iterrows():
                norad    = int(ex_row["Target NORAD"])
                category = str(ex_row.get("Confidence Category", ""))
                if category and category != "Query failed":
                    # Per-run file wins over DB for this run's satellites
                    # (it has fresher behavior data from this specific run)
                    existing_results[norad] = ex_row.to_dict()
                    if not file_has_behavior:
                        existing_missing_behavior.add(norad)
                    else:
                        behavior = ex_row.get("Orbit Behavior", "")
                        if not behavior or str(behavior) in ("nan", "Unknown", ""):
                            existing_missing_behavior.add(norad)

            if existing_results:
                skipped_preview = len(existing_results) - len(existing_missing_behavior)
                print(
                    f"Found existing results for {len(existing_results)} satellites. "
                    f"{skipped_preview} will be skipped; "
                    f"{len(existing_missing_behavior)} will be re-evaluated for behavior data."
                )
        except Exception as e:
            print(f"  Could not read existing accuracy file ({e}); will evaluate all satellites.")

    all_sat_rows = unique_sats.to_dict("records")
    # Evaluate: (a) satellites not in existing file at all, OR
    #            (b) satellites missing behavior detection data
    sat_rows     = [
        r for r in all_sat_rows
        if int(r["Target NORAD"]) not in existing_results
        or int(r["Target NORAD"]) in existing_missing_behavior
    ]

    skipped_count = len(all_sat_rows) - len(sat_rows)
    if skipped_count:
        print(
            f"Skipping {skipped_count} already-evaluated satellites. "
            f"Evaluating {len(sat_rows)} new / previously-failed satellites."
        )
    else:
        sat_rows = all_sat_rows

    # ── Determine lookback strategy ──────────────────────────────
    # smart_adaptive: initial scan for all satellites, then compute
    # the optimal years per satellite from the scan data, group by
    # year, and re-query only satellites that actually need more
    # than the scan window found.
    if lookback_style == "smart_adaptive":
        cfg         = lookback_years if isinstance(lookback_years, dict) else {}
        max_years   = cfg.get("max_years", 10)

        # The initial scan window MUST be at least as deep as the
        # smallest orbit-type minimum in _ORBIT_LOOKBACK_MINIMUMS
        # (LEO/MEO/GEO=3, HEO=5). A hardcoded 2-year scan was always
        # shorter than every orbit type's floor, which meant
        # _determine_optimal_lookback's `opt_yrs > scan_years` check
        # was True for every single satellite regardless of its
        # behavior flag -- including satellites correctly classified
        # "Stable". The entire fleet was unconditionally pushed into
        # a second, full re-query pass every run; the behavior-based
        # escalation logic never got a chance to actually skip
        # anyone. Confirmed against a real run: 13,180 of 13,196
        # satellites were forced into a 3-year re-query this way --
        # essentially the whole catalog, even though the scoring
        # model's own minimums show 3 years was already enough for
        # the great majority of them. Scanning at the true minimum
        # depth up front means satellites that turn out to be stable
        # (the common case) finish in the first pass as intended, and
        # only satellites whose behavior genuinely warrants more than
        # their orbit-type floor go through the second pass.
        scan_years  = min(_ORBIT_LOOKBACK_MINIMUMS.values())
        orbit_lookback_map = None
        global_lookback    = scan_years
        print(
            f"Smart adaptive: {scan_years}-year initial scan for all "
            f"(orbit-type minimum), then optimal depth (up to "
            f"{max_years} years) only for satellites whose behavior "
            f"warrants it.\n"
        )
    else:
        # Single window for all satellites
        if isinstance(lookback_years, dict):
            lookback_years = lookback_years.get("max_years",
                             max(lookback_years.values()) if lookback_years else HISTORICAL_LOOKBACK_YEARS)
        orbit_lookback_map = None
        global_lookback    = lookback_years if lookback_years else HISTORICAL_LOOKBACK_YEARS
        scan_years  = None
        max_years   = None
        print(f"Lookback: {global_lookback} year(s)\n")

    # ── Offline mode: score from the local cache only, zero network ──
    # Completely separate, much simpler code path from the online run
    # below it -- deliberately NOT threading offline_mode through the
    # online pipeline's login/Pass-1/extended-pass/SATCAT/GCAT
    # machinery (600+ lines), since that risks accidentally leaving a
    # network call reachable in some branch. This path instead reuses
    # the same scoring functions (_score_batch, _detect_orbit_behavior
    # via that) directly against whatever's already in the local TLE
    # history cache, and writes the output file itself.
    if offline_mode:
        end_date_off   = date.today()
        start_date_off = date(end_date_off.year - global_lookback,
                               end_date_off.month, end_date_off.day)

        all_norads_off = [int(r["Target NORAD"]) for r in sat_rows]
        cached_elements = tle_history_cache.load_cached_elements(
            TLE_HISTORY_CACHE_DB, all_norads_off
        )
        # load_cached_elements returns [] (not a missing key) for any
        # NORAD ID with no cached rows, so no satellite is silently
        # dropped -- it just scores as "Insufficient historical data"
        # the same as a genuinely new satellite would, which is the
        # honest answer when there's truly nothing local to score it
        # from yet.
        have_data_count = sum(1 for v in cached_elements.values() if v)
        print(
            f"Offline scoring: {len(all_norads_off):,} satellites "
            f"requested, {have_data_count:,} have cached historical "
            f"data available ({100.0 * have_data_count / max(1, len(all_norads_off)):.1f}%). "
            f"The remainder will show as 'Insufficient historical data' "
            f"-- run online to build up their cache coverage.\n",
            flush=True,
        )

        offline_results = _score_batch(
            sat_rows, cached_elements, failed_norad_ids=[],
            lookback_years=global_lookback,
            orbit_lookback_map=orbit_lookback_map,
            batch_label="offline (cache only)",
            display=None,
        )

        all_rows_off = list(existing_results.values()) + offline_results
        out_df_off = pd.DataFrame(all_rows_off).sort_values(
            "Confidence Score (0-100)", ascending=False
        )
        out_df_off.to_excel(accuracy_file, index=False)
        print(
            f"Saved {len(out_df_off)} total rows "
            f"({len(offline_results)} scored offline, "
            f"{len(existing_results)} carried from previous run)"
        )
        print(f"  → {accuracy_file}")
        print(
            "\nNote: offline mode does not fetch SATCAT or GCAT launch "
            "metadata (those also require Space-Track/network access). "
            "Launch Date / Country / etc. columns will be blank for "
            "any satellite that didn't already have that data carried "
            "forward from a previous online run.\n"
        )
        return

    # ── Pre-flight check against Space-Track's API usage policy ──
    # Runs BEFORE any network call (including _auto_configure's
    # latency probe and login itself) -- computes exactly how many
    # gp_history/SATCAT requests this run actually needs (after
    # subtracting what the local caches already cover) and refuses
    # to proceed if the plan looks like it would violate documented
    # policy. See spacetrack_policy_check.py for the full rationale;
    # this is a safety net on top of the caching fix (tle_history_cache.py
    # / satcat_cache.py), which is what makes compliance possible in
    # the first place -- this just verifies the resulting plan looks
    # right before spending a single request on it.
    try:
        spacetrack_policy_check.run_preflight_check(
            norad_ids=[int(r["Target NORAD"]) for r in sat_rows],
            lookback_years=global_lookback,
            tle_cache_db=TLE_HISTORY_CACHE_DB,
            satcat_cache_db=SATCAT_CACHE_DB,
            satcat_max_age_hours=SATCAT_CACHE_MAX_AGE_HOURS,
            batch_size=HISTORICAL_BATCH_SIZE,
            api_log_db=API_REQUEST_LOG_DB,
            interactive=sys.stdin.isatty(),
        )
    except spacetrack_policy_check.PolicyCheckFailed as e:
        raise RuntimeError(str(e)) from e

    # Auto-tune performance settings for this machine
    print("Profiling hardware and network conditions...")
    hw = _auto_configure()
    _batch_size    = hw.get("HISTORICAL_BATCH_SIZE",          HISTORICAL_BATCH_SIZE)
    _concurrent    = hw.get("HISTORICAL_CONCURRENT_REQUESTS", HISTORICAL_CONCURRENT_REQUESTS)
    _query_timeout = hw.get("HISTORICAL_QUERY_TIMEOUT_SEC",   HISTORICAL_QUERY_TIMEOUT_SEC)

    batches = list(_chunk(sat_rows, _batch_size))
    print(
        f"Evaluating {len(sat_rows)} satellites "
        f"in {len(batches)} batch(es) of up to {_batch_size}, "
        f"{_concurrent} concurrent..."
    )
    if HISTORICAL_MAX_SATELLITES and total_unique > HISTORICAL_MAX_SATELLITES:
        print(
            f"  Note: {total_unique} unique satellites found; evaluating "
            f"only the first {HISTORICAL_MAX_SATELLITES} "
            f"(HISTORICAL_MAX_SATELLITES in config.py)."
        )

    end_date   = date.today()
    start_date = date(end_date.year - global_lookback, end_date.month, end_date.day)

    # ── Single shared session + rate limiter for the ENTIRE run ──
    # Space-Track enforces its own rate limit on the login endpoint
    # separately from data queries, and tracks combined request
    # volume across endpoints rather than per-phase. Previously this
    # function created a NEW session (and a new, independent rate
    # limiter that knew nothing about the others' usage) for pass 1,
    # for each extended-lookback year group, AND for the SATCAT
    # fetch -- meaning the real combined request rate sent to
    # Space-Track was never actually throttled by any single limiter,
    # even though each one believed it was compliant. This pattern
    # is what triggered Space-Track's API abuse detection.
    #
    # One session + one rate limiter, created here and reused for
    # every phase (pass 1, every extended group, and SATCAT), fixes
    # this: every request across the whole run shares the same
    # 28/min, 290/hour budget, and login itself respects it too.
    #
    # spacetrack_login() now verifies the account with a real query
    # immediately after login (verify_account=True by default) -- this
    # is the single most important place that matters: it catches an
    # inactive/suspended/restricted account right here, before any of
    # the expensive multi-minute scoring work begins, instead of only
    # discovering it after a full run completes with every satellite
    # silently mislabeled "Insufficient historical data" (the failure
    # mode this whole call chain exists to prevent -- see
    # SpaceTrackMalformedResponseError for the matching fix on the
    # data-fetch side).
    session      = spacetrack_login(SPACETRACK_USERNAME, SPACETRACK_PASSWORD,
                                    timeout_sec=REQUEST_TIMEOUT_SEC,
                                    rate_limiter=None)   # first login, nothing to wait on yet
    # Log the login request itself to the persistent cross-run log,
    # then wire that same log into the rate limiter as a callback so
    # every subsequent gp_history / SATCAT request is also persisted
    # automatically the moment its slot is granted. This is what lets
    # the pre-flight check on the NEXT run see what THIS run did.
    api_request_log.log_request(
        API_REQUEST_LOG_DB, api_request_log.CLASS_LOGIN, norad_count=0
    )
    rate_limiter = SpaceTrackRateLimiter(
        log_callback=lambda cls, n: api_request_log.log_request(
            API_REQUEST_LOG_DB, cls, norad_count=n
        )
    )
    results      = []
    run_start    = time.time()

    # ── Three stacked progress bars ─────────────────────────────
    #
    #  pos 0  Overall progress  (persists the whole run)
    #         Shows: batches completed / total, elapsed, ETA, rate.
    #         ETA uses smoothing=0.2 (weighted moving average) so it
    #         stabilises quickly without being thrown off by the first
    #         slow batch. bar_format is explicit so the ETA column is
    #         always visible -- tqdm's default can show '?' while it
    #         gathers enough samples on irregular concurrent workloads.
    #
    #  pos 1  In-flight monitor  (persists, updated every second by a
    #         background thread)
    #         Shows: how many batches are currently waiting on a
    #         Space-Track response and how long each has been waiting.
    #
    #  pos 2  Per-batch scoring  (brief inner bar, leave=False so it
    #         disappears cleanly after each batch)
    #         Shows: per-satellite scoring progress within each
    #         returned batch, with its own elapsed/ETA.

    # _BatchDisplay manages all terminal output for this pass.
    # It draws to sys.stderr (bypasses the log_utils _Tee on stdout)
    # while key summary lines are also printed to stdout for the log.
    import sys as _sys
    _tqdm_file = _sys.stderr   # kept for the inner scoring bar (still tqdm)

    # Nothing to evaluate -- all visible satellites already have
    # valid scores in the existing accuracy file.
    if not sat_rows:
        print(
            f"\nAll {len(existing_results)} satellites already have valid confidence "
            f"scores -- nothing new to evaluate.\n"
            "If you want to force a full re-evaluation, delete the existing\n"
            f"accuracy file and re-run:\n"
            f"  {os.path.basename(accuracy_file)}",
            flush=True
        )
        # Skip straight to the final save using the existing results.
        results = []
        all_rows = list(existing_results.values())
        if all_rows:
            out_df = pd.DataFrame(all_rows).sort_values(
                "Confidence Score (0-100)", ascending=False
            )
            out_df.to_excel(accuracy_file, index=False)
            print(f"\nSaved {len(out_df)} existing rows → {accuracy_file}")
        return

    lookback_desc = (
        f"Smart Adaptive  2→{max_years}yr"
        if lookback_style == "smart_adaptive"
        else f"Single  {global_lookback}yr"
    )
    display = _BatchDisplay(
        total_batches  = len(batches),
        total_sats     = len(sat_rows),
        run_tag        = _tag if "_tag" in dir() else "",
        lookback_desc  = lookback_desc,
    )

    # Show how much of this run's work the local cache already covers
    # BEFORE starting -- see tle_history_cache.py for why this exists:
    # Space-Track's documented policy is gp_history = 1 query per
    # object per lifetime, so any satellite already fully covered by
    # a previous run's cached data costs zero additional queries here.
    print(tle_history_cache.cache_stats(TLE_HISTORY_CACHE_DB), flush=True)

    display.start()

    inflight_lock     = threading.Lock()
    batch_start_times = {}

    def _fetch(batch_num, norad_ids):
        t0 = time.time()
        batch_start_times[batch_num] = t0
        display.batch_fetching(batch_num)
        return _cached_fetch(
            session, norad_ids, start_date, end_date,
            timeout_sec=_query_timeout, rate_limiter=rate_limiter,
        )

    # pass1_elements accumulates Space-Track data across all batches
    # so we can compute optimal lookback after the pass completes.
    pass1_elements = {}   # norad_id -> elements list

    try:
        with ThreadPoolExecutor(max_workers=_concurrent) as executor:
            futures = {
                executor.submit(_fetch, bn, [int(r["Target NORAD"]) for r in batch]): (bn, batch)
                for bn, batch in enumerate(batches, start=1)
            }
            for future in as_completed(futures):
                bn, batch = futures[future]
                fetch_elapsed = time.time() - batch_start_times.get(bn, time.time())
                try:
                    elements_by_norad, failed_ids = future.result()
                except Exception as e:
                    print(f"  Batch {bn} failed: {e}", flush=True)
                    elements_by_norad, failed_ids = {}, [int(r["Target NORAD"]) for r in batch]

                pass1_elements.update(elements_by_norad)

                results.extend(
                    _score_batch(batch, elements_by_norad, failed_ids,
                                 global_lookback,
                                 orbit_lookback_map=orbit_lookback_map,
                                 batch_label=f"Batch {bn}/{len(batches)}",
                                 display=display)
                )

                failed_count = len(set(failed_ids))
                display.batch_done(bn, len(batch), fetch_elapsed,
                                   failed_count, len(results))

                # Log line to stdout (captured in log file)
                print(
                    f"  Batch {bn:>3}/{len(batches)} done | "
                    f"{len(batch):>4} sats | "
                    f"network: {fetch_elapsed:.1f}s | "
                    f"failed: {failed_count} | "
                    f"total scored: {len(results)}",
                    flush=True
                )

                all_rows = list(existing_results.values()) + results
                pd.DataFrame(all_rows).sort_values(
                    "Confidence Score (0-100)", ascending=False
                ).to_excel(accuracy_file, index=False)

    finally:
        display.stop()
        # Session stays OPEN -- reused by the extended pass and the
        # SATCAT fetch below. Closed once at the very end of main().

    pass1_elapsed = time.time() - run_start
    print(f"\nPass 1 complete: {pass1_elapsed/60:.1f} minutes, {len(results)} satellites scored.")

    # ── Smart adaptive: compute optimal lookback per satellite ───
    # Now that we have pass-1 behavior data, determine how many
    # years each satellite really needs, group by that number to
    # minimise Space-Track queries, and re-query only the groups
    # that need more than the initial scan window.
    if lookback_style == "smart_adaptive" and scan_years and max_years:
        from collections import defaultdict

        # Build a behaviour lookup from pass-1 results
        p1_behavior = {int(r["Target NORAD"]): r.get("Orbit Behavior", "Stable")
                       for r in results}
        p1_orbit    = {int(r["Target NORAD"]): r.get("Target Orbit", "LEO")
                       for r in results}

        # Compute the optimal year count per satellite
        optimal_by_norad = {}
        for row in sat_rows:
            norad   = int(row["Target NORAD"])
            flag    = p1_behavior.get(norad, "Stable")
            orbit   = p1_orbit.get(norad, row.get("Target Orbit", "LEO"))
            els     = pass1_elements.get(norad, [])
            optimal = _determine_optimal_lookback(els, flag, orbit, max_years, scan_years)
            optimal_by_norad[norad] = optimal

        # Group satellites that need MORE than the scan window, by year value
        year_groups = defaultdict(list)
        for row in sat_rows:
            norad    = int(row["Target NORAD"])
            opt_yrs  = optimal_by_norad.get(norad, scan_years)
            if opt_yrs > scan_years:
                year_groups[opt_yrs].append(row)

        if year_groups:
            distinct_windows = sorted(year_groups.keys())
            total_to_extend  = sum(len(v) for v in year_groups.values())
            print(
                f"\n{total_to_extend} satellites need extended analysis across "
                f"{len(distinct_windows)} year-window(s): "
                + ", ".join(f"{y}yr×{len(year_groups[y])}" for y in distinct_windows)
            )

            for opt_yrs in distinct_windows:
                group = year_groups[opt_yrs]

                # ── Scale settings for this year window ──────────────
                # Longer lookback = more data per satellite per batch.
                # Rough data volume scales linearly with years, so we
                # shrink the batch size and concurrency and lengthen the
                # timeout proportionally to avoid overwhelming Space-Track
                # and timing out on the much larger responses.
                year_scale   = opt_yrs / max(scan_years, 1)
                ext_batch_sz = max(10, int(_batch_size / year_scale))
                ext_timeout  = min(600, int(_query_timeout * year_scale))
                ext_conc     = max(2, int(_concurrent / max(1, year_scale ** 0.5)))

                ext_start   = date(end_date.year - opt_yrs, end_date.month, end_date.day)
                ext_batches = list(_chunk(group, ext_batch_sz))

                # Upfront estimate so the user knows how long to expect
                # and can decide whether to wait or interrupt.
                est_min_per_batch = (ext_timeout * 0.4) / 60  # ~40% of timeout
                est_total_min     = est_min_per_batch * len(ext_batches) / ext_conc
                print(
                    f"\n  → {opt_yrs}-year window: {len(group)} satellites",
                    flush=True
                )
                print(
                    f"     Batch size   : {ext_batch_sz} satellites  "
                    f"(reduced from {_batch_size} for {opt_yrs}-year data volume)",
                    flush=True
                )
                print(
                    f"     Concurrency  : {ext_conc} concurrent  "
                    f"(reduced from {_concurrent})",
                    flush=True
                )
                print(
                    f"     Timeout/batch: {ext_timeout}s  "
                    f"(scaled from {_query_timeout}s)",
                    flush=True
                )
                print(
                    f"     Estimated    : {est_total_min:.0f}-{est_total_min*2:.0f} minutes  "
                    f"({len(ext_batches)} batches × ~{est_min_per_batch:.1f} min each)",
                    flush=True
                )
                print(
                    "     Press Ctrl+C at any time to stop and save current results.",
                    flush=True
                )

                ext_results   = []
                ext_timings   = {}
                ext_interrupted = False

                # Reuse the single shared session + rate limiter for
                # the entire run. Do NOT create a new login per year
                # group -- repeated logins are themselves rate-limited
                # by Space-Track and were part of what triggered abuse
                # detection previously.
                #
                # ensure_spacetrack_session() is now a no-op pass-
                # through (see its docstring) -- an earlier version
                # made a real "is this session still valid" probe
                # query before every phase, which itself violated
                # Space-Track's per-class rate limits. If the session
                # genuinely has expired by this point in a long run,
                # the actual gp_history fetch below will fail and
                # surface that clearly via SpaceTrackMalformedResponseError
                # / get_historical_orbital_elements_batch_with_retry's
                # error handling, at zero extra request cost in the
                # (overwhelmingly common) case where the session is
                # still fine.
                session = ensure_spacetrack_session(
                    session, SPACETRACK_USERNAME, SPACETRACK_PASSWORD,
                    rate_limiter=rate_limiter,
                )
                ext_session = session
                ext_limiter = rate_limiter

                ext_display = _BatchDisplay(
                    total_batches = len(ext_batches),
                    total_sats    = len(group),
                    run_tag       = "",
                    lookback_desc = f"{opt_yrs}-year deep analysis",
                )
                ext_display.start()

                def _ext_fetch(bn, norad_ids,
                               _session=ext_session, _limiter=ext_limiter,
                               _start=ext_start, _tim=ext_timings,
                               _timeout=ext_timeout,
                               _disp=ext_display):
                    t0 = time.time()
                    _tim[bn] = t0
                    _disp.batch_fetching(bn)   # notifies display → shows in In-Flight
                    return _cached_fetch(
                        _session, norad_ids, _start, end_date,
                        timeout_sec=_timeout, rate_limiter=_limiter,
                    )

                try:
                    with ThreadPoolExecutor(max_workers=ext_conc) as executor:
                        ef = {
                            executor.submit(
                                _ext_fetch, bn,
                                [int(r["Target NORAD"]) for r in batch]
                            ): (bn, batch)
                            for bn, batch in enumerate(ext_batches, start=1)
                        }
                        for future in as_completed(ef):
                            bn, batch = ef[future]
                            fetch_elapsed = time.time() - ext_timings.get(bn, time.time())
                            try:
                                els_by_norad, failed_ids = future.result()
                            except Exception as e:
                                print(
                                    f"    {opt_yrs}-yr batch {bn} failed "
                                    f"({fetch_elapsed:.0f}s): {e}",
                                    flush=True
                                )
                                els_by_norad, failed_ids = {}, [int(r["Target NORAD"]) for r in batch]

                            lkb_map = {int(r["Target NORAD"]): optimal_by_norad.get(
                                           int(r["Target NORAD"]), opt_yrs)
                                       for r in batch}

                            ext_results.extend(
                                _score_batch(batch, els_by_norad, failed_ids,
                                             opt_yrs,
                                             lookback_used_map=lkb_map,
                                             batch_label=f"{opt_yrs}yr {bn}/{len(ext_batches)}",
                                             display=ext_display)
                            )
                            ext_display.batch_done(
                                bn, len(batch), fetch_elapsed,
                                len(set(failed_ids)), len(ext_results)
                            )
                            print(
                                f"    {opt_yrs}yr batch {bn:>3}/{len(ext_batches)} done | "
                                f"{len(batch):>4} sats | "
                                f"network: {fetch_elapsed:.1f}s",
                                flush=True
                            )

                            # Checkpoint after every extended batch so a
                            # Ctrl+C or crash doesn't lose completed work.
                            _interim = {int(r["Target NORAD"]): r for r in ext_results}
                            interim_results = [
                                _interim.get(int(r["Target NORAD"]), r) for r in results
                            ]
                            all_interim = list(existing_results.values()) + interim_results
                            pd.DataFrame(all_interim).sort_values(
                                "Confidence Score (0-100)", ascending=False
                            ).to_excel(accuracy_file, index=False)

                except KeyboardInterrupt:
                    ext_interrupted = True
                    print(
                        f"\n  Interrupted during {opt_yrs}-year pass after "
                        f"{len(ext_results)} satellites. "
                        f"Results saved to checkpoint.",
                        flush=True
                    )

                finally:
                    ext_display.stop()
                    # ext_session IS the shared session -- do not close
                    # it here, it's reused by subsequent year groups and
                    # the SATCAT fetch.

                # Merge extended results into main results list
                ext_by_norad = {int(r["Target NORAD"]): r for r in ext_results}
                results = [ext_by_norad.get(int(r["Target NORAD"]), r) for r in results]

                if ext_interrupted:
                    pct = len(ext_results) / len(group) * 100
                    print(
                        f"  {opt_yrs}-yr pass interrupted: {len(ext_results)}/{len(group)} "
                        f"satellites updated ({pct:.0f}%). Re-run to complete remaining "
                        f"{len(group)-len(ext_results)} satellites -- already-scored "
                        f"satellites will be skipped.",
                        flush=True
                    )
                    # Skip remaining year groups
                    break
                else:
                    print(
                        f"  {len(ext_results)} satellites updated with {opt_yrs}-year history.",
                        flush=True
                    )

        else:
            print("\nAll satellites are stable -- no extended analysis needed.")

        # Stamp Lookback Used for satellites that kept their scan result
        for r in results:
            if "Lookback Used (years)" not in r or r["Lookback Used (years)"] is None:
                r["Lookback Used (years)"] = optimal_by_norad.get(
                    int(r["Target NORAD"]), scan_years
                )

    total_elapsed = (time.time() - run_start) / 60
    print(f"\nTotal run time: {total_elapsed:.1f} minutes")

    if not results:
        print("No results to save.")
        return

    # ── Fetch launch metadata from Space-Track SATCAT ────────────
    # This is a single lightweight pass: one SATCAT record per
    # satellite, batched in groups of 500. Takes a few seconds, not
    # minutes, and gives us launch date, international designator,
    # country, launch site, object type, and size class.
    #
    # Launch DATE is available for all catalogued objects.
    # Launch TIME is not in any public catalog database -- it exists
    # only in mission-specific press releases and cannot be fetched
    # programmatically for the full catalog.
    print("\nFetching launch metadata from Space-Track SATCAT...")
    all_norads  = list({int(r["Target NORAD"]) for r in results})
    satcat_limiter = rate_limiter

    print(satcat_cache.cache_stats(SATCAT_CACHE_DB, SATCAT_CACHE_MAX_AGE_HOURS), flush=True)

    # Only query Space-Track for satellites whose cached SATCAT record
    # is missing or older than SATCAT_CACHE_MAX_AGE_HOURS (default
    # 24h) -- see satcat_cache.py docstring for why: Space-Track's
    # policy documents SATCAT as 1 query/day, and re-fetching the
    # full catalog on every run (a normal usage pattern is multiple
    # runs per day for an updated observation window) would violate
    # that every time.
    satcat_data, norads_needing_fetch = satcat_cache.split_cached_vs_needed(
        SATCAT_CACHE_DB, all_norads, SATCAT_CACHE_MAX_AGE_HOURS
    )

    if not norads_needing_fetch:
        print(
            f"  All {len(all_norads)} satellites already have fresh "
            f"cached SATCAT data (within {SATCAT_CACHE_MAX_AGE_HOURS}h) "
            f"-- no Space-Track query needed this run.",
            flush=True,
        )
        satcat_session = None
    else:
        # The historical-confidence pass above can run 10-15+ minutes.
        # Space-Track sessions can expire from inactivity over that
        # span, and reusing an expired session here previously meant
        # every SATCAT batch silently 401'd in sequence. SATCAT
        # enrichment is supplementary -- not worth losing the already-
        # computed historical-confidence results over. If
        # re-authentication itself genuinely fails here (bad
        # credentials, account locked, Space-Track down), skip SATCAT
        # enrichment entirely rather than letting the exception
        # propagate and crash the run after 10-15+ minutes of
        # otherwise-successful work.
        try:
            satcat_session = ensure_spacetrack_session(
                session, SPACETRACK_USERNAME, SPACETRACK_PASSWORD,
                rate_limiter=satcat_limiter,
            )
        except Exception as e:
            print(
                f"  Could not establish a working Space-Track session for "
                f"SATCAT enrichment ({e}) -- skipping launch metadata for "
                f"{len(norads_needing_fetch)} satellites this run "
                f"(already-cached satellites are unaffected). Results "
                f"and reports are otherwise unaffected; re-run later "
                f"once Space-Track access is restored if launch "
                f"metadata is needed.",
                flush=True,
            )
            satcat_session = None

    satcat_batches = (
        list(_chunk(norads_needing_fetch, 500)) if satcat_session else []
    )
    consecutive_failures = 0
    _SATCAT_FAIL_CIRCUIT_BREAKER = 3  # abort after this many in a row

    for i, batch_norads in enumerate(satcat_batches, 1):
        print(f"  SATCAT batch {i}/{len(satcat_batches)} ({len(batch_norads)} satellites)...",
              end=" ", flush=True)
        batch_data = fetch_satcat_data(
            satcat_session, batch_norads,
            timeout_sec=60, rate_limiter=satcat_limiter,
            username=SPACETRACK_USERNAME, password=SPACETRACK_PASSWORD,
        )
        satcat_data.update(batch_data)
        satcat_cache.store_records(SATCAT_CACHE_DB, batch_data)
        print(f"{len(batch_data)} records received.")

        if batch_data:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if consecutive_failures >= _SATCAT_FAIL_CIRCUIT_BREAKER:
                print(
                    f"  {consecutive_failures} consecutive SATCAT batches "
                    f"returned no data -- stopping SATCAT enrichment early "
                    f"rather than repeating the same failure across all "
                    f"{len(satcat_batches)} batches. Remaining satellites "
                    f"will have blank launch metadata; results and reports "
                    f"are otherwise unaffected. Check Space-Track "
                    f"credentials/availability and re-run if launch "
                    f"metadata is needed.",
                    flush=True,
                )
                break

    # Now that every phase is done, close the single shared session
    # (it may be None if re-authentication failed, or if every
    # satellite already had fresh cached SATCAT data and no session
    # was needed at all).
    if satcat_session is not None:
        satcat_session.close()

    # ── Supplementary cross-reference: GCAT ──────────────────────
    # Jonathan McDowell's GCAT is independent of Space-Track and
    # sometimes has launch date or owner data that SATCAT is missing
    # (e.g. very old or unusual objects), plus an Owner/operator field
    # that's often more specific than SATCAT's two-letter Country code.
    # This is a SUPPLEMENT, not a replacement: SATCAT values are used
    # first wherever present, GCAT only fills genuine gaps and adds
    # the new "GCAT Owner" column. Cached to disk (~24h) since the
    # full catalog is large and only updated roughly monthly.
    print("\nCross-referencing GCAT (General Catalog of Space Objects)...")
    gcat_data = fetch_gcat_catalog(
        GCAT_CACHE_FILE, max_cache_age_hours=GCAT_CACHE_MAX_AGE_HOURS
    )
    if gcat_data:
        print(f"  GCAT catalog loaded: {len(gcat_data):,} objects.")
    else:
        print("  GCAT unavailable this run -- continuing with SATCAT data only.")

    # Stamp SATCAT metadata onto every scored satellite, with GCAT
    # filling any gaps SATCAT left blank.
    for row in results:
        norad = int(row["Target NORAD"])
        meta  = satcat_data.get(norad, {})
        gmeta = gcat_data.get(norad, {})
        row["Launch Date"]            = meta.get("launch_date",     "") or gmeta.get("ldate", "")
        row["Intl Designator"]        = meta.get("intl_designator", "")
        row["Object Type"]            = meta.get("object_type",     "")
        row["Country"]                = meta.get("country",         "") or gmeta.get("state", "")
        row["Launch Site"]            = meta.get("launch_site",     "")
        row["Size Class"]             = meta.get("size_class",      "")
        row["Decay Date"]             = meta.get("decay_date",      "")
        row["GCAT Owner"]             = gmeta.get("owner", "")
        row["GCAT Status"]            = gmeta.get("status", "")

    # Also backfill existing_results that are missing launch data
    for norad, row in existing_results.items():
        gmeta = gcat_data.get(norad, {})
        if not row.get("Launch Date"):
            meta = satcat_data.get(norad, {})
            row["Launch Date"]     = meta.get("launch_date",     "") or gmeta.get("ldate", "")
            row["Intl Designator"] = meta.get("intl_designator", "")
            row["Object Type"]     = meta.get("object_type",     "")
            row["Country"]         = meta.get("country",         "") or gmeta.get("state", "")
            row["Launch Site"]     = meta.get("launch_site",     "")
            row["Size Class"]      = meta.get("size_class",      "")
            row["Decay Date"]      = meta.get("decay_date",      "")
        if not row.get("GCAT Owner"):
            row["GCAT Owner"]  = gmeta.get("owner", "")
            row["GCAT Status"] = gmeta.get("status", "")

    print(f"Launch metadata added for {len(satcat_data)} satellites (SATCAT) "
          f"+ {len(gcat_data)} cross-referenced (GCAT).")

    all_rows = list(existing_results.values()) + results
    out_df   = pd.DataFrame(all_rows).sort_values(
        "Confidence Score (0-100)", ascending=False
    )
    out_df.to_excel(accuracy_file, index=False)
    newly   = len(results)
    carried = len(existing_results)
    print(f"Saved {len(out_df)} total rows ({newly} new, {carried} carried from previous run)")
    print(f"  → {accuracy_file}")

    # ── Save new results to permanent confidence DB ───────────────
    # Store newly-scored satellites in the permanent DB so they are
    # carried forward automatically on ALL future runs, regardless of
    # what date/time window is used. Only new results (not carried-
    # forward existing ones) are written -- existing DB rows are only
    # replaced when the satellite has genuinely been re-scored.
    if results:
        try:
            _conf_db.save_results(
                SATELLITE_CONFIDENCE_DB,
                results,
                evaluation_tag=_tag,
            )
            print(
                f"  Permanent confidence DB: {newly:,} new results saved → "
                f"{SATELLITE_CONFIDENCE_DB}",
                flush=True,
            )
        except Exception as _e:
            print(f"  (Could not save to permanent confidence DB: {_e})", flush=True)

    # ── Auto-snapshot: add today's full GP catalog to the archive ────
    # Every successful online run captures a daily GP snapshot silently
    # -- one additional Space-Track request (class/gp, not gp_history)
    # that covers every currently-tracked satellite at once. Over time
    # this accumulates into a multi-year TLE history in the local cache
    # and zip archives, complementing the one-time historical seed from
    # seed_tle_history.py --seed. If today's snapshot already exists
    # (e.g. the tool was run twice today), this is a no-op.
    try:
        import tle_bulk_seeder as _tbs
        import spacetrack_client as _stc

        # If the spacetrack library is installed, snapshot_daily_gp will
        # handle its own authentication internally for streaming mode and
        # we don't need a separate session login. Otherwise, open a fresh
        # session the same way as before.
        if _stc._lib_available:
            _snap_records, _snap_skipped = _tbs.snapshot_daily_gp(
                session=None,
                cache_db_path=TLE_HISTORY_CACHE_DB,
                tle_data_dir=TLE_DATA_DIR,
                rate_limiter=rate_limiter,
                request_log_db=API_REQUEST_LOG_DB,
                username=SPACETRACK_USERNAME,
                password=SPACETRACK_PASSWORD,
            )
        else:
            # Use a fresh session for the snapshot -- the main session may
            # have been closed by the SATCAT phase above.
            _snap_session = spacetrack_login(
                SPACETRACK_USERNAME, SPACETRACK_PASSWORD,
                rate_limiter=rate_limiter, verify_account=False,
            )
            _snap_records, _snap_skipped = _tbs.snapshot_daily_gp(
                session=_snap_session,
                cache_db_path=TLE_HISTORY_CACHE_DB,
                tle_data_dir=TLE_DATA_DIR,
                rate_limiter=rate_limiter,
                request_log_db=API_REQUEST_LOG_DB,
                username=SPACETRACK_USERNAME,
                password=SPACETRACK_PASSWORD,
            )
            _snap_session.close()

        if not _snap_skipped and _snap_records > 0:
            print(
                f"  Daily GP snapshot: {_snap_records:,} records archived "
                f"(see {TLE_DATA_DIR} for zip files)."
            )
    except Exception as _e:
        # Never let snapshot failure affect the main run's output
        print(f"  (Daily snapshot skipped: {_e})")


if __name__ == "__main__":
    import argparse
    _parser = argparse.ArgumentParser(
        description="Historical confidence evaluation for satellite visibility predictions."
    )
    _parser.add_argument(
        "--offline", action="store_true",
        help=(
            "Score satellites using ONLY the local TLE history cache "
            "(data/tle_history_cache.sqlite3) -- no Space-Track login, "
            "no network calls of any kind. Use this when Space-Track "
            "is unavailable. Coverage is limited to whatever a "
            "previous online run already cached."
        ),
    )
    _args = _parser.parse_args()
    main(offline_mode=_args.offline)
