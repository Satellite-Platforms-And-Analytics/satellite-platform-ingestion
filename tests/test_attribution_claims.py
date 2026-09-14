"""
Per-field provenance: what each source claims, kept beside what the others
claim (012_satellite_attribution.sql).

004 named the condition that would make row-level provenance insufficient
and left this for when it happened. It happened three times:

  2026-09-11  GCAT's pass took ownership of the provenance columns for
              17,486 rows whose values came from SATCAT.
  2026-09-14  66 launch dates where the two catalogues disagree, with a
              write path that lets the source rated 0.95 overrule the one
              rated 1.0.
  2026-09-14  24 operator disagreements plus 1,944 objects where GCAT
              holds several records under one catalogue number.

Each was handled by choosing a winner and discarding the loser. These
tests are about not discarding it.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.db import writer


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def row(**kw):
    base = {"norad_id": 25544, "data_source": "gcat", "match_method": "norad_id",
            "source_confidence": 0.95, "matched_at": NOW}
    base.update(kw)
    return base


def claims(rows, fields=None, monkeypatch=None):
    """Capture what would be written, without a database."""
    captured = {}

    def fake_bulk(sql, prepared, template=None, *a, **k):
        captured["sql"] = sql
        captured["rows"] = prepared
        return len(prepared)

    monkeypatch.setattr(writer, "_bulk_upsert", fake_bulk)
    n = writer.record_attribution_claims(rows, fields=fields)
    return n, captured


def test_a_claim_is_recorded_per_field_not_per_row(monkeypatch):
    n, cap = claims([row(operator="SpaceX", orbit_type="LEO")],
                    fields=["operator", "orbit_type"], monkeypatch=monkeypatch)
    assert n == 2
    fields = {r[1] for r in cap["rows"]}
    assert fields == {"operator", "orbit_type"}


def test_nulls_are_not_recorded(monkeypatch):
    """
    A missing row already means "this source said nothing". Writing NULLs
    would roughly triple the table to express the same thing.
    """
    n, cap = claims([row(operator="SpaceX", orbit_type=None)],
                    fields=["operator", "orbit_type"], monkeypatch=monkeypatch)
    assert n == 1 and cap["rows"][0][1] == "operator"


def test_dates_are_normalised_to_iso(monkeypatch):
    """
    Comparison in this table is textual, so two sources agreeing on a date
    and formatting it differently would read as a conflict. Normalising at
    the single write point is what stops that being a per-source decision.
    """
    n, cap = claims([row(launch_date=date(2023, 11, 3))],
                    fields=["launch_date"], monkeypatch=monkeypatch)
    assert cap["rows"][0][3] == "2023-11-03"


def test_the_claimant_is_required(monkeypatch):
    with pytest.raises(ValueError, match="no data_source"):
        claims([row(operator="X", data_source=None)],
               fields=["operator"], monkeypatch=monkeypatch)


def test_a_field_that_is_not_a_real_column_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="non-descriptive"):
        claims([row()], fields=["operatorr"], monkeypatch=monkeypatch)


def test_the_default_field_list_is_every_descriptive_column():
    """
    So a new enrichment column starts being evidenced the moment it is
    added, rather than when somebody remembers to list it here.
    """
    import inspect
    src = inspect.getsource(writer.record_attribution_claims)
    assert "_ATTRIBUTION_DESCRIPTIVE" in src


def test_untracked_objects_are_skipped_not_fatal():
    """
    GCAT offers 70,324 objects against 17,487 tracked. Relying on the
    foreign key would abort the whole batch over rows never in scope, so
    the statement filters instead.
    """
    assert "WHERE EXISTS" in writer._UPSERT_CLAIM_SQL
    assert "FROM satellites s WHERE s.norad_id = v.norad_id" \
        in writer._UPSERT_CLAIM_SQL


def test_reimport_updates_rather_than_duplicating():
    assert "ON CONFLICT (norad_id, field, source) DO UPDATE" \
        in writer._UPSERT_CLAIM_SQL


def test_the_claim_writer_counts_what_was_written_not_what_was_sent():
    """
    The statement filters on WHERE EXISTS, so submitted != written. The
    default _bulk_upsert return is the submitted count, and using it here
    reported 454,074 GCAT claims when ~113,000 were stored -- four times
    the truth, in the direction of looking more successful.

    _bulk_upsert's own docstring calls this "the absence made to look like
    success, which is this project's most expensive recurring bug", three
    lines above the parameter that prevents it.
    """
    import inspect
    src = inspect.getsource(writer.record_attribution_claims)
    assert "count_affected=True" in src, (
        "record_attribution_claims must count rows written, not submitted")


def test_any_filtering_statement_must_count_affected():
    """
    Generalised: a writer whose SQL can match nothing cannot report the
    submitted count. This catches the next one rather than this one.
    """
    import inspect
    for name in ("record_attribution_claims", "upsert_satellite_attribution"):
        fn = getattr(writer, name)
        src = inspect.getsource(fn)
        sql_name = [n for n in ("_UPSERT_CLAIM_SQL", "_UPDATE_ATTRIBUTION_SQL",
                                "sql") if n in src]
        assert "count_affected=True" in src, (
            f"{name} sends rows through a statement that can match nothing "
            f"({sql_name}); it must count affected rows")
