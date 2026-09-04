"""
Tests for insert_tle_history's epoch handling in src/db/writer.py.

Split out of tests/test_visibility.py, where these started life for
convenience and did not belong.

THE REGRESSION THESE EXIST FOR
==============================
On 2026-09-01 a guard was added to skip element sets epoched more than a
day in the future. It compared the parsed epoch against an aware UTC
horizon. CelesTrak's OMM JSON writes EPOCH without a timezone designator
('2026-09-04T11:00:00.000000'), so the parsed value was naive and every
comparison raised:

    TypeError: can't compare offset-naive and offset-aware datetimes

Every TLE write failed from 09-01 to 09-04. The `fetch` step kept logging
success - only `write_db` failed - so satellites.last_updated stayed
current while tle_history sat frozen at 346,171 rows for three days.

The test that should have caught it existed and used
'2026-08-25T15:13:35.376672Z'. The trailing Z was an assumption about the
feed, not an observation of it, and it made the test agree with the bug.
Every test below uses the format CelesTrak actually sends.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.db.writer import _as_datetime, _dedupe_key

#: Exactly what CelesTrak's OMM JSON puts in EPOCH - no Z, no offset.
CELESTRAK_EPOCH = "2026-09-04T11:00:00.000000"


# ─────────────────────────────────────────────────────────────────────────────
#  Parsing
# ─────────────────────────────────────────────────────────────────────────────

def test_celestrak_epoch_parses_to_an_aware_datetime():
    """The regression, stated directly."""
    out = _as_datetime(CELESTRAK_EPOCH)
    assert isinstance(out, datetime)
    assert out.tzinfo is not None, (
        "a naive epoch cannot be compared against an aware horizon - this "
        "is the exact failure that broke writes from 2026-09-01 to 09-04"
    )
    assert out.utcoffset() == timedelta(0)


def test_a_naive_epoch_is_comparable_with_an_aware_horizon():
    """
    The operation that actually raised. Assert on the comparison itself,
    not just on the tzinfo attribute, because it is the comparison that
    the pipeline performs.
    """
    horizon = datetime.now(timezone.utc) + timedelta(days=1)
    assert _as_datetime(CELESTRAK_EPOCH) < horizon


def test_an_explicit_z_is_still_accepted():
    """Space-Track and some mirrors do send a designator."""
    out = _as_datetime("2026-08-25T15:13:35.376672Z")
    assert out.tzinfo is not None
    assert out.microsecond == 376672        # full precision preserved


def test_an_explicit_offset_is_preserved_not_overwritten():
    out = _as_datetime("2026-08-25T15:13:35+02:00")
    assert out.utcoffset() == timedelta(hours=2)


def test_an_already_aware_datetime_passes_through_unchanged():
    aware = datetime(2026, 8, 25, 15, 13, 35, tzinfo=timezone.utc)
    assert _as_datetime(aware) is aware


def test_unparseable_epoch_is_passed_through_not_swallowed():
    """Let the server reject it loudly rather than inventing a value."""
    assert _as_datetime("not a timestamp") == "not a timestamp"


# ─────────────────────────────────────────────────────────────────────────────
#  Deduplication
# ─────────────────────────────────────────────────────────────────────────────

def test_the_two_rows_observed_on_object_69235_collapse():
    """
    864 microseconds apart: the same element set arriving in two CelesTrak
    groups within one fetch. An earlier attempt rounded the stored epoch to
    the millisecond and did NOT fix this - the two values straddle a
    millisecond boundary. Keep this pointed at the real observed values.
    """
    a = datetime(2026, 8, 25, 15, 13, 35, 376672, tzinfo=timezone.utc)
    b = datetime(2026, 8, 25, 15, 13, 35, 375808, tzinfo=timezone.utc)
    assert _dedupe_key(25544, a) == _dedupe_key(25544, b)


def test_real_element_sets_hours_apart_stay_distinct():
    a = datetime(2026, 8, 25, 15, 13, 35, 376672, tzinfo=timezone.utc)
    later = datetime(2026, 8, 25, 18, 13, 35, 376672, tzinfo=timezone.utc)
    assert _dedupe_key(25544, a) != _dedupe_key(25544, later)


def test_the_same_epoch_for_different_satellites_stays_distinct():
    e = datetime(2026, 8, 25, 15, 13, 35, tzinfo=timezone.utc)
    assert _dedupe_key(25544, e) != _dedupe_key(25545, e)


# ─────────────────────────────────────────────────────────────────────────────
#  The whole write path, with the real feed format
# ─────────────────────────────────────────────────────────────────────────────

def _capture(monkeypatch):
    """Intercept the bulk upsert so no database is needed."""
    import src.db.writer as writer

    captured: dict = {}

    def fake(sql, rows, **kwargs):
        captured["rows"] = rows
        return len(rows)

    monkeypatch.setattr(writer, "_bulk_upsert", fake)
    return captured


def test_insert_tle_history_accepts_the_feeds_naive_epochs(monkeypatch):
    """
    End to end with strings shaped like the real feed. This is the test
    whose absence let a three-day outage through: every unit test used
    datetime objects or a Z-suffixed string, so nothing exercised what
    CelesTrak actually sends.
    """
    from src.db.writer import insert_tle_history

    captured = _capture(monkeypatch)
    now = datetime.now(timezone.utc)

    records = [
        {"norad_id": 25544, "line1": "1 ...", "line2": "2 ...",
         "epoch": now.replace(tzinfo=None).isoformat(), "source": "celestrak"},
        {"norad_id": 25545, "line1": "1 ...", "line2": "2 ...",
         "epoch": (now - timedelta(hours=3)).replace(tzinfo=None).isoformat(),
         "source": "celestrak"},
    ]

    assert insert_tle_history(records) == 2
    assert len(captured["rows"]) == 2
    for row in captured["rows"]:
        assert row[3].tzinfo is not None, "naive epoch reached the database"


def test_future_dated_element_sets_are_still_skipped(monkeypatch):
    """The guard that caused the regression must still do its job."""
    from src.db.writer import insert_tle_history

    captured = _capture(monkeypatch)
    now = datetime.now(timezone.utc)

    records = [
        {"norad_id": 25544, "line1": "1 ...", "line2": "2 ...",
         "epoch": now.replace(tzinfo=None).isoformat()},
        {"norad_id": 99999, "line1": "1 ...", "line2": "2 ...",
         "epoch": (now + timedelta(days=6)).replace(tzinfo=None).isoformat()},
    ]

    assert insert_tle_history(records) == 1
    assert all(r[0] != 99999 for r in captured["rows"])


def test_duplicates_within_one_batch_collapse(monkeypatch):
    from src.db.writer import insert_tle_history

    captured = _capture(monkeypatch)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    records = [
        {"norad_id": 69235, "line1": "1 ...", "line2": "2 ...",
         "epoch": now.replace(microsecond=376672).isoformat()},
        {"norad_id": 69235, "line1": "1 ...", "line2": "2 ...",
         "epoch": now.replace(microsecond=375808).isoformat()},
    ]

    assert insert_tle_history(records) == 1
    assert captured["rows"][0][3].microsecond == 376672   # full precision
