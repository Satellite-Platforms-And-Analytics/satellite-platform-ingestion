"""
The prune must be subordinate to the archive.

Since 2026-09-10 this database is the serving layer and D: is the
warehouse. The prunes run in GitHub Actions on a fixed clock; the archive
runs on a workstation that might be off. Fixed-clock prune plus
intermittent archive loses data silently, and orbital_positions gives a
two-day margin before it does.

These tests pin the three outcomes that arrangement has to produce:

  archive current   clock wins, behaviour identical to before
  archive behind    watermark wins, the table grows, nothing is lost
  archive stalled   the backstop deletes unarchived rows, loudly

and the two refusals that are safer than guessing: no watermark row at
all, and a watermark that is still NULL.

No database. _tx is replaced with a fake connection, so every case here
runs in CI where there is no Supabase.
"""
from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta, timezone

import pytest

from src.db import writer
from archive_to_local import contiguous_through

NOW = datetime.now(timezone.utc)


class FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    """Answers the watermark SELECT; records anything else."""

    def __init__(self, row):
        self.row = row
        self.executed = []

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return FakeResult(self.row)


@pytest.fixture
def watermark_row(monkeypatch):
    """
    Install a watermark row and capture what the code does with it.

    Yields a setter; call it with (archived_through, max_retention_days)
    or with None to mean "no row in the table at all".
    """
    holder = {"row": None, "conns": []}

    @contextlib.contextmanager
    def fake_tx():
        conn = FakeConn(holder["row"])
        holder["conns"].append(conn)
        yield conn

    monkeypatch.setattr(writer, "_tx", fake_tx)

    def setter(archived_through, max_days):
        # A row that exists. (None, None) means the row is there with a
        # NULL watermark and no backstop - which is tle_history's shipped
        # state and a different case from the row being absent.
        holder["row"] = (archived_through, max_days)
        return holder

    def absent():
        """No row at all: 007 not applied, or the row was deleted."""
        holder["row"] = None
        return holder

    setter.absent = absent
    setter.holder = holder
    return setter


# ----------------------------------------------------------- the three paths

def test_clock_wins_when_the_archive_is_current(watermark_row):
    """Watermark ahead of the retention cutoff: behaves as it always did."""
    watermark_row(NOW - timedelta(hours=1), 7)
    soft = NOW - timedelta(days=3)
    cutoff, reason = writer._archive_cutoff("visibility_windows", soft)
    assert cutoff == soft
    assert reason == "clock"


def test_watermark_wins_when_the_archive_is_behind(watermark_row):
    """
    The archive has not run for a day. Less is deleted, the table grows,
    nothing is lost. This is the failure mode the design wants.
    """
    behind = NOW - timedelta(days=4)
    watermark_row(behind, 7)
    soft = NOW - timedelta(days=3)
    cutoff, reason = writer._archive_cutoff("visibility_windows", soft)
    assert cutoff == behind
    assert cutoff < soft, "must delete less than the clock would"
    assert reason == "archive watermark"


def test_backstop_fires_when_the_archive_has_stalled(watermark_row, caplog):
    """
    Past max_retention_days unarchived rows are destroyed, because an
    unbounded table exhausts the tier and stops every pipeline. It must
    be loud: ERROR in the log and a catalog_events row.
    """
    stalled = NOW - timedelta(days=30)
    watermark_row(stalled, 7)
    soft = NOW - timedelta(days=3)

    with caplog.at_level("ERROR"):
        cutoff, reason = writer._archive_cutoff("visibility_windows", soft)

    assert "BACKSTOP" in reason
    assert cutoff > stalled, "the backstop deletes past the watermark"
    assert any("ARCHIVE BACKSTOP" in r.message for r in caplog.records), \
        "the one path that destroys unarchived data must log at ERROR"

    inserts = [sql for conn in watermark_row.holder["conns"]
               for sql, _ in conn.executed if "catalog_events" in sql]
    assert inserts, "the backstop must reach the monitor's digest"


# ------------------------------------------------------------- the refusals

def test_missing_watermark_row_refuses_rather_than_falling_back(watermark_row):
    """
    No row means 007 was not applied. Falling back to the clock would
    silently restore the unsafe behaviour, so prune nothing instead: a
    table that grows for a day is recoverable, an element set is not.
    """
    watermark_row.absent()
    cutoff, reason = writer._archive_cutoff("tle_history",
                                            NOW - timedelta(days=14))
    assert cutoff is None
    assert "refused" in reason


def test_never_archived_without_backstop_prunes_nothing(watermark_row):
    """tle_history: NULL backstop means grow rather than ever delete."""
    watermark_row(None, None)
    cutoff, reason = writer._archive_cutoff("tle_history",
                                            NOW - timedelta(days=14))
    assert cutoff is None


def test_never_archived_with_backstop_still_deletes(watermark_row, caplog):
    """A regenerable table with a backstop does not grow forever."""
    watermark_row(None, 7)
    with caplog.at_level("ERROR"):
        cutoff, reason = writer._archive_cutoff("orbital_positions",
                                                NOW - timedelta(hours=48))
    assert cutoff is not None
    assert "BACKSTOP" in reason


def test_unmanaged_table_is_untouched(watermark_row):
    """
    A table outside the archive scheme prunes on the clock. This function
    must not be able to break a table that was never part of it.
    """
    soft = NOW - timedelta(days=1)
    cutoff, reason = writer._archive_cutoff("ingestion_log", soft)
    assert cutoff == soft
    assert "not archived" in reason


def test_tle_history_has_no_backstop_in_the_shipped_migration():
    """
    007 seeds tle_history with max_retention_days NULL on purpose:
    CelesTrak serves current elements only and GP_History is one request
    per lifetime, so a pruned element set is gone. Better a full tier
    than a permanent hole - see the 2026-09-02 gap.
    """
    import pathlib
    sql = None
    for base in (pathlib.Path("_infrastructure/schema"),
                 pathlib.Path("../satellite-platform-infrastructure/schema")):
        p = base / "007_archive_watermark.sql"
        if p.exists():
            sql = p.read_text(encoding="utf-8")
            break
    if sql is None:
        pytest.skip("007_archive_watermark.sql not checked out here")
    assert "('tle_history',        NULL, NULL)" in sql


# --------------------------------------------------------- contiguous_through

def test_contiguous_through_stops_at_the_first_hole():
    """
    max(archived) would be wrong. If 09-08 is missing, telling the prune
    that 09-09 is archived authorises deleting a day that exists nowhere.
    """
    present = [date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9)]
    archived = {date(2026, 9, 7), date(2026, 9, 9)}      # 09-08 failed
    assert contiguous_through(archived, present) == date(2026, 9, 7)


def test_contiguous_through_with_no_holes():
    present = [date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9)]
    assert contiguous_through(set(present), present) == date(2026, 9, 9)


def test_contiguous_through_when_the_oldest_day_is_missing():
    present = [date(2026, 9, 7), date(2026, 9, 8)]
    assert contiguous_through({date(2026, 9, 8)}, present) is None


def test_contiguous_through_ignores_archive_days_not_in_the_database():
    """Old partitions already pruned from the cloud must not block it."""
    present = [date(2026, 9, 8), date(2026, 9, 9)]
    archived = {date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 9)}
    assert contiguous_through(archived, present) == date(2026, 9, 9)


def test_contiguous_through_empty():
    assert contiguous_through(set(), []) is None
