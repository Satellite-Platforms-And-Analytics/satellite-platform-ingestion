"""
sensor_select.py

Interactive prompts for choosing the analysis date, time window,
lookback years, and sensor at the start of a main.py run, plus
the registry of built-in sensor profiles.

TIME ZONE HANDLING: the time window prompt now asks for LOCAL time
at the sensor's location and converts it to Zulu (UTC) automatically.
This uses Python's zoneinfo module with the tzdata package as a
fallback so DST is handled correctly regardless of what time of year
the analysis date falls on.

SENSOR PROFILES: each built-in entry's azimuth/elevation field-of-
regard parameters are sourced from publicly documented, unclassified
specs for that real radar system (see the comment on each entry for
the source). Nothing here is guessed -- if you want to add another
real sensor, look up its actual published field-of-regard before
adding it, and note the source the same way.
"""

from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:
    _HAS_ZONEINFO = False


# =====================================================
# TIMEZONE HELPERS
# =====================================================

def _get_tz(tz_name):
    """
    Return a timezone object for tz_name (an IANA timezone string
    like 'America/Chicago'). Uses zoneinfo if available, otherwise
    falls back to a fixed UTC offset extracted from the name's
    known-offset table below -- which is less accurate (ignores DST)
    but at least gives a reasonable value when zoneinfo is missing.
    """
    if _HAS_ZONEINFO:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass

    # Fixed-offset fallback (no DST) when zoneinfo is unavailable.
    # These are standard (winter) offsets -- results may be off by
    # 1 hour during summer for locations that observe DST.
    _FALLBACK_OFFSETS = {
        "America/New_York":    -5,
        "America/Chicago":     -6,
        "America/Denver":      -7,
        "America/Los_Angeles": -8,
        "America/Anchorage":   -9,
        "Pacific/Honolulu":   -10,
        "Europe/London":        0,
        "Europe/Berlin":        1,
        "Asia/Tokyo":           9,
    }
    offset_hours = _FALLBACK_OFFSETS.get(tz_name, 0)
    return timezone(timedelta(hours=offset_hours))


def local_to_utc(date_str, local_hhmm, tz):
    """
    Convert a local HHMM time on date_str (YYYY-MM-DD) to UTC,
    returning a new HHMM string.

    tz may be a zoneinfo.ZoneInfo, a datetime.timezone fixed-offset
    object, or a raw UTC-offset integer (e.g. -5).
    """
    if isinstance(tz, (int, float)):
        tz = timezone(timedelta(hours=tz))

    h = int(local_hhmm[:2])
    m = int(local_hhmm[2:])
    year, month, day = [int(x) for x in date_str.split("-")]

    local_dt = datetime(year, month, day, h, m, tzinfo=tz)
    utc_dt   = local_dt.astimezone(timezone.utc)

    # Handle crossing midnight -- analysis is still for the chosen
    # date but a late local time may map to the next UTC day.
    return utc_dt.strftime("%H%M"), utc_dt


def utc_offset_label(date_str, tz):
    """
    Return a short human-readable timezone label for display,
    e.g. 'CDT (UTC-5)' or 'EST (UTC-5)', correctly reflecting
    whether DST is in effect on date_str.
    """
    if isinstance(tz, (int, float)):
        sign = "+" if tz >= 0 else "-"
        return f"UTC{sign}{abs(int(tz))}"

    year, month, day = [int(x) for x in date_str.split("-")]
    try:
        # Get the offset in effect on the analysis date
        sample = datetime(year, month, day, 12, 0, tzinfo=tz)
        offset = sample.utcoffset()
        abbr   = sample.strftime("%Z")
        total_hours = int(offset.total_seconds() / 3600)
        sign = "+" if total_hours >= 0 else "-"
        return f"{abbr} (UTC{sign}{abs(total_hours)})"
    except Exception:
        return str(tz)


# =====================================================
# SENSOR PROFILES
# =====================================================

SENSOR_PROFILES = {
    "1": {
        "name": "AN/FPS-85 (Eglin AFB, FL)",
        "lat": 30.57,
        "lon": -86.21,
        "elev_m": 40.0,
        "min_elevation_deg": 3.0,
        "max_elevation_deg": 90.0,
        "apply_field_of_regard": True,
        "boresight_azimuth_deg": 180.0,
        "azimuth_half_width_deg": 60.0,
        "timezone_name": "America/Chicago",
        # Eglin AFB is in Florida's panhandle, which observes
        # Central Time (not Eastern like the rest of Florida).
        # CDT (UTC-5) in summer, CST (UTC-6) in winter.
        # Source: publicly documented AN/FPS-85 fact sheets.
    },
    "2": {
        "name": "PAVE PAWS (Cape Cod AFS, MA)",
        "lat": 41.75222,
        "lon": -70.53806,
        "elev_m": 97.5,
        "min_elevation_deg": 3.0,
        "max_elevation_deg": 85.0,
        "apply_field_of_regard": True,
        "boresight_azimuth_deg": 107.0,
        "azimuth_half_width_deg": 120.0,
        "timezone_name": "America/New_York",
        # Cape Cod AFS is in Massachusetts, Eastern Time.
        # EDT (UTC-4) in summer, EST (UTC-5) in winter.
        # Source: publicly documented AN/FPS-115 PAVE PAWS specs.
    },
}


# =====================================================
# PROMPTS
# =====================================================

def prompt_for_date(default_date):
    """
    Ask for the analysis date (YYYY-MM-DD), looping until a valid
    date is entered or the default is accepted (just press Enter).
    Returns the date as a string in YYYY-MM-DD format.
    """
    while True:
        date_input = input(
            f"Enter the date for this data grab and analysis "
            f"(YYYY-MM-DD) [default: {default_date}]: "
        ).strip()

        if not date_input:
            return default_date

        try:
            datetime.strptime(date_input, "%Y-%m-%d")
            return date_input
        except ValueError:
            print("  Invalid format -- please enter as YYYY-MM-DD (e.g. 2026-08-05).")


def prompt_for_timeframe(analysis_date, sensor_profile, default_start="0600", default_end="1800"):
    """
    Ask for the analysis start and end times in LOCAL time at the
    sensor's location, then convert both to Zulu (UTC) and display
    the translation so the user can verify.

    analysis_date: YYYY-MM-DD string (needed to look up the correct
    DST offset for that specific date).

    sensor_profile: the chosen sensor dict (provides timezone_name
    and sensor name for the prompt).

    Returns:
      local_start_hhmm  -- what the user typed, e.g. "0600"
      local_end_hhmm    -- what the user typed, e.g. "1800"
      utc_start_hhmm    -- converted to UTC, e.g. "1100"
      utc_end_hhmm      -- converted to UTC, e.g. "2300"
      tz_label          -- display string, e.g. "CDT (UTC-5)"
    """

    tz_name   = sensor_profile.get("timezone_name", "UTC")
    tz        = _get_tz(tz_name)
    tz_label  = utc_offset_label(analysis_date, tz)
    sensor_name = sensor_profile.get("name", "selected sensor")

    def parse_hhmm(s):
        s = s.strip().replace(":", "")
        if len(s) != 4 or not s.isdigit():
            raise ValueError
        h, m = int(s[:2]), int(s[2:])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return h, m

    print()
    print(f"Enter the analysis time window in LOCAL time for {sensor_name}.")
    print(f"Timezone: {tz_label}")
    print("Press Enter to accept the default shown in brackets.")
    print("The tool will automatically convert your local times to Zulu (UTC).")

    while True:
        start_input = input(
            f"  Start time local (HHMM) [default: {default_start}]: "
        ).strip()
        if not start_input:
            start_input = default_start
        try:
            sh, sm = parse_hhmm(start_input)
            local_start = f"{sh:02d}{sm:02d}"
            break
        except ValueError:
            print("  Invalid -- enter time as HHMM, e.g. 0600 for 6:00am local.")

    while True:
        end_input = input(
            f"  End time   local (HHMM) [default: {default_end}]: "
        ).strip()
        if not end_input:
            end_input = default_end
        try:
            eh, em = parse_hhmm(end_input)
            local_end = f"{eh:02d}{em:02d}"
        except ValueError:
            print("  Invalid -- enter time as HHMM, e.g. 1800 for 6:00pm local.")
            continue

        start_mins = sh * 60 + sm
        end_mins   = eh * 60 + em

        if end_mins <= start_mins:
            print(f"  End time ({local_end}) must be after start time ({local_start}).")
            continue
        if end_mins - start_mins < 60:
            print(f"  Window must be at least 1 hour ({end_mins - start_mins} minutes entered).")
            continue

        break

    # Convert both times to UTC
    utc_start, utc_start_dt = local_to_utc(analysis_date, local_start, tz)
    utc_end,   utc_end_dt   = local_to_utc(analysis_date, local_end,   tz)

    # Display the translation clearly
    print()
    print(f"  Time conversion ({tz_label} → Zulu):")
    print(f"    {local_start} local  →  {utc_start}Z")
    print(f"    {local_end} local  →  {utc_end}Z")

    # Warn if the UTC window crosses midnight (unusual but possible)
    if utc_end_dt.date() > utc_start_dt.date():
        print()
        print("  Note: your end time crosses midnight in UTC.")
        print(f"  Analysis will run from {utc_start}Z on {analysis_date}")
        print(f"  to {utc_end}Z on {utc_end_dt.strftime('%Y-%m-%d')}.")

    print()
    return local_start, local_end, utc_start, utc_end, tz_label



# ── Orbit-type specific lookback year recommendations ─────────────
# Longer history helps HEO (variable orbits, maneuvers) more than
# GEO/MEO/LEO (stable or well-understood decay patterns).
# Orbit-type minimum lookback floors used by smart adaptive mode.
# These are the minimum years needed before the scoring model gives
# a reliable result, regardless of how stable the satellite appears.
ORBIT_LOOKBACK_MINIMUMS = {
    "LEO": 3,   # Need at least 3 years to distinguish stable from slow-decaying
    "MEO": 3,   # GPS/nav satellites are stable; 3 years confirms it
    "GEO": 3,   # Station-kept continuously; 3 years is more than enough
    "HEO": 5,   # Variable orbits; 5 years needed to distinguish stable vs erratic
}

# Scan window for the initial pass in smart adaptive mode.
# Short enough to run quickly across all satellites; long enough
# to reliably detect maneuvers and recent behavior changes.
SMART_SCAN_YEARS = 2


def prompt_for_lookback_style(default_years=5):
    """
    Ask whether to use a single lookback window for all satellites
    or the smart adaptive mode that finds the optimal years per satellite.

    Returns either:
      ("single",        years)              -- same years for all
      ("smart_adaptive", {"max_years": N})  -- per-satellite optimal
    """
    print()
    print("─" * 60)
    print("Historical Lookback Window")
    print("─" * 60)
    print(
        "Choose how many years of orbital history to pull from\n"
        "Space-Track for the confidence scoring step.\n"
    )
    print(
        "  1. Single window for all satellites\n"
        "     One number applies to every satellite regardless of\n"
        "     orbit type or behavior. Simpler, predictable runtime.\n"
    )
    print(
        "  2. Smart adaptive per satellite (recommended)\n"
        "     Initial 2-year scan detects each satellite\'s orbital\n"
        "     behavior. Satellites showing instability are then\n"
        "     re-evaluated with a deeper history window sized to\n"
        "     capture the full extent of their behavioral pattern --\n"
        "     up to 10 years if needed. Stable satellites keep the\n"
        "     2-year result. Every satellite\'s output includes a\n"
        "     \'Lookback Used\' column showing exactly how many years\n"
        "     were used for its evaluation.\n"
        "\n"
        "     Orbit-type minimums (floor regardless of behavior):\n"
        + "\n".join(
            f"       {ot:<6}: {yrs} year(s)"
            for ot, yrs in ORBIT_LOOKBACK_MINIMUMS.items()
        )
    )

    while True:
        choice = input("\n  Select option (1 or 2) [default: 2]: ").strip()
        if not choice:
            choice = "2"

        if choice == "1":
            years = prompt_for_lookback_years(default_years)
            return "single", years

        elif choice == "2":
            print()
            val = input(
                "  Maximum lookback years (1-10) [default: 10]: "
            ).strip()
            max_years = 10
            if val:
                try:
                    max_years = max(2, min(int(val), 10))
                except ValueError:
                    pass
            print(
                f"\n  Smart adaptive: 2-year scan for all → optimal depth\n"
                f"  per satellite (up to {max_years} years) for less stable orbits.\n"
                f"  Output will include \'Lookback Used (years)\' per satellite.\n"
            )
            return "smart_adaptive", {"max_years": max_years}

        else:
            print("  Please enter 1 or 2.")


def prompt_for_lookback_years(default_years=5):
    """
    Ask how many years of historical TLE data to pull from Space-Track
    for the confidence scoring step, explaining the time/accuracy
    tradeoff so the user can make an informed choice.

    Returns an integer number of years (1-10).
    """
    print()
    print("─" * 60)
    print("Historical Lookback Window")
    print("─" * 60)
    print(
        "The confidence scoring step pulls each satellite's orbital\n"
        "history from Space-Track to measure how stable its orbit\n"
        "has been. More years = more data = more accurate scores,\n"
        "but also more data to download and process.\n"
    )
    print("Guidance by orbit type:")
    print(
        "  LEO (Low Earth Orbit, < 2,000 km)\n"
        "    Orbits decay due to atmospheric drag and are sometimes\n"
        "    actively raised or lowered. 3-5 years captures meaningful\n"
        "    stability history; going back further mainly adds old data\n"
        "    from before the satellite settled into its current regime.\n"
        "    Recommended: 3 years\n"
    )
    print(
        "  MEO (Medium Earth Orbit, 2,000-35,000 km)\n"
        "    GPS and navigation satellites. Orbits are very stable;\n"
        "    even 2-3 years of history is enough to confirm stability.\n"
        "    Recommended: 3 years\n"
    )
    print(
        "  GEO (Geostationary, ~35,786 km)\n"
        "    Communications and weather satellites under tight station-\n"
        "    keeping. Extremely stable -- 2-3 years is plenty to score\n"
        "    with high confidence. Going back 10 years rarely changes\n"
        "    the result.\n"
        "    Recommended: 3 years\n"
    )
    print(
        "  HEO (Highly Elliptical Orbit)\n"
        "    Variable -- some are very stable, others maneuver\n"
        "    frequently. Longer history (5+ years) is more useful here\n"
        "    to distinguish genuinely stable from occasionally erratic.\n"
        "    Recommended: 5 years\n"
    )
    print(
        "  Mixed catalog (all orbit types, default)\n"
        "    5 years balances accuracy and speed for a full catalog.\n"
        "    Recommended: 5 years\n"
    )

    print("Estimated run times (for a ~19,000 satellite catalog):")
    estimates = [
        (1,  "~5-10 min",   "minimal -- only catches recent maneuvers"),
        (3,  "~15-25 min",  "good balance for most use cases"),
        (5,  "~25-40 min",  "default -- recommended for mixed catalogs"),
        (7,  "~35-55 min",  "deeper history, diminishing returns for LEO/GEO"),
        (10, "~50-80 min",  "maximum depth -- mainly useful for HEO analysis"),
    ]
    for yrs, time_est, note in estimates:
        marker = " <-- default" if yrs == default_years else ""
        print(f"  {yrs:>2} year{'s' if yrs > 1 else ' '}: {time_est:<18} {note}{marker}")

    print()

    while True:
        val = input(
            f"  Enter number of years (1-10) [default: {default_years}]: "
        ).strip()

        if not val:
            print(f"  Using default: {default_years} years.\n")
            return default_years

        try:
            years = int(val)
            if 1 <= years <= 10:
                print()
                return years
            else:
                print("  Please enter a number between 1 and 10.")
        except ValueError:
            print("  Please enter a whole number, e.g. 5.")


def _prompt_float(prompt_text):
    while True:
        try:
            return float(input(prompt_text).strip())
        except ValueError:
            print("  Please enter a numeric value.")


def _prompt_custom_sensor():
    """
    Build a sensor profile from manual user input, including
    the sensor's local timezone.
    """
    print("\n--- Custom sensor setup ---")

    lat   = _prompt_float("  Latitude (deg, e.g. 30.57): ")
    lon   = _prompt_float("  Longitude (deg, e.g. -86.21): ")
    elev_m = _prompt_float("  Elevation above sea level (m): ")

    print()
    print("  Enter the sensor's local timezone.")
    print("  Examples:  -5  (UTC-5 / Eastern Standard Time)")
    print("             -6  (UTC-6 / Central Standard Time)")
    print("             America/Chicago  (handles DST automatically)")
    print("             UTC  (if the sensor is already in Zulu time)")

    while True:
        tz_input = input("  Timezone (UTC offset or IANA name) [default: UTC]: ").strip()
        if not tz_input:
            tz_input = "UTC"
        # Try as numeric offset first
        try:
            tz_offset = float(tz_input.lstrip("+"))
            tz_name   = f"UTC{'+' if tz_offset >= 0 else ''}{int(tz_offset)}"
            _tz       = timezone(timedelta(hours=tz_offset))
            break
        except ValueError:
            pass
        # Try as IANA name
        if _HAS_ZONEINFO:
            try:
                _tz     = ZoneInfo(tz_input)
                tz_name = tz_input
                break
            except Exception:
                print(f"  '{tz_input}' is not a recognized timezone. Try a UTC offset like -5.")
        else:
            tz_name = tz_input
            break

    for_choice = input(
        "\n  Apply a directional field-of-regard restriction (fixed-facing "
        "radar), or use generic full-sky horizon visibility? "
        "[fixed/full, default: full]: "
    ).strip().lower()

    if for_choice == "fixed":
        boresight  = _prompt_float("  Boresight azimuth (deg, 180 = due south): ")
        half_width = _prompt_float("  Azimuth half-width (deg, e.g. 60 for 120 deg total): ")
        min_elev   = _prompt_float("  Minimum elevation (deg, e.g. 3): ")

        return {
            "name": f"Custom sensor ({lat}, {lon})",
            "lat": lat,
            "lon": lon,
            "elev_m": elev_m,
            "min_elevation_deg": min_elev,
            "max_elevation_deg": 90.0,
            "apply_field_of_regard": True,
            "boresight_azimuth_deg": boresight,
            "azimuth_half_width_deg": half_width,
            "timezone_name": tz_name,
        }

    return {
        "name": f"Custom sensor ({lat}, {lon}, full-sky)",
        "lat": lat,
        "lon": lon,
        "elev_m": elev_m,
        "min_elevation_deg": 0.0,
        "max_elevation_deg": 90.0,
        "apply_field_of_regard": False,
        "boresight_azimuth_deg": 180.0,
        "azimuth_half_width_deg": 180.0,
        "timezone_name": tz_name,
    }


def prompt_for_sensor():
    """
    Display the built-in sensor registry plus a custom option, and
    return the chosen sensor profile dict.
    """
    print("\nAvailable sensors:")
    for key, profile in SENSOR_PROFILES.items():
        tz_name = profile.get("timezone_name", "UTC")
        print(f"  {key}. {profile['name']}  [{tz_name}]")

    custom_key  = str(len(SENSOR_PROFILES) + 1)
    print(f"  {custom_key}. Custom (enter your own location/parameters)")

    default_key = "1"

    while True:
        choice = input(
            f"\nSelect a sensor [default: {default_key} - "
            f"{SENSOR_PROFILES[default_key]['name']}]: "
        ).strip()

        if not choice:
            choice = default_key

        if choice in SENSOR_PROFILES:
            return SENSOR_PROFILES[choice]
        elif choice == custom_key:
            return _prompt_custom_sensor()
        else:
            print("  Invalid choice, try again.")
