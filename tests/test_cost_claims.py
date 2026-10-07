"""
`021_cost_claims.sql`'s constraints must be live in the database, not
merely written in the file.

WHY THIS IS A TEST AND NOT A COMMENT
====================================
021 carries controls that are silent when they work:

  - `hierarchy` is GENERATED from `subject_kind`, and a CHECK makes
    `'unknown'` unreachable. The failure it prevents: adding a kind to
    the CHECK list and forgetting the generated column, after which rows
    arrive filed under a third tree nobody designed (AD-101).
  - `is_forecast` is GENERATED from `as_of_date > published_date`, so a
    claim about the future is a forecast by construction rather than
    because someone remembered to set a flag.
  - `method IN ('derived','inferred')` requires a non-blank
    `derivation_basis`. An allocation without a basis is a made-up
    number with a decimal point (AD-102).
  - confidence and its basis are both-or-neither (AD-052).
  - The natural key keeps rival sources and refuses a source restating
    the same vintage — because the losing claim is the point (AD-077).

EVERY CONSTRAINT TEST CARRIES A POSITIVE CONTROL
================================================
On 2026-09-15 five tx_code assertions in `test_research_schema.py` stayed
green after the CHECK they named had been DROPPED, because a foreign key
refused the rows before the CHECK was evaluated. Five green assertions,
none of them touching the control they claimed to test.

So every test here inserts a row that MUST be accepted before asserting
that a variant is rejected. If the accepted row fails, the test says so
rather than crediting the rejection to the wrong constraint.

TO RUN IT AGAINST THE LIVE DATABASE
===================================
    PowerShell:   $env:REQUIRE_DB=1; pytest tests/test_cost_claims.py
    bash:         REQUIRE_DB=1 pytest tests/test_cost_claims.py

`$env:` persists for the rest of that PowerShell window. Clear it with
`Remove-Item Env:REQUIRE_DB` before working offline, or these turn from
skips into failures.
"""
from __future__ import annotations

import os
import uuid

import pytest

from src.env import bootstrap

bootstrap()

REQUIRE_DB = bool(os.environ.get("REQUIRE_DB"))

TABLES = ("cost_claims",)
VIEWS = ("cost_claims_resolved", "cost_claim_conflicts")


def _connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        msg = ("DATABASE_URL is not set in the environment and was not "
               "found in .env, so the live schema cannot be checked")
        if REQUIRE_DB:
            pytest.fail(msg + " — REQUIRE_DB is set.")
        pytest.skip(msg)
    from src.db.writer import get_engine
    return get_engine()


@pytest.fixture(scope="module")
def conn():
    from sqlalchemy import text
    engine = _connect()
    try:
        c = engine.connect()
    except Exception as exc:                                 # noqa: BLE001
        msg = f"database unreachable: {type(exc).__name__}"
        if REQUIRE_DB:
            pytest.fail(msg + " — REQUIRE_DB is set.")
        pytest.skip(msg)
    present = c.execute(text(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name = ANY(:t)"
    ), {"t": list(TABLES)}).scalar()
    if present != len(TABLES):
        c.close()
        msg = "021_cost_claims.sql is not applied (cost_claims is absent)"
        if REQUIRE_DB:
            pytest.fail(msg + " — REQUIRE_DB is set.")
        pytest.skip(msg)
    yield c
    c.close()


_INSERT = """
    INSERT INTO cost_claims (
        subject_kind, subject_id, subject_label, metric, value, currency,
        as_of_date, published_date, method, derivation_basis, source,
        confidence, confidence_basis)
    VALUES (
        :subject_kind, :subject_id, :subject_label, :metric, :value,
        :currency, :as_of_date, :published_date, :method,
        :derivation_basis, :source, :confidence, :confidence_basis)
    RETURNING id, hierarchy, is_forecast
"""


def _claim(**over):
    base = dict(
        subject_kind="contract",
        subject_id=f"test-{uuid.uuid4()}",
        subject_label="Test Award",
        metric="contract_obligated",
        value=1000000.00,
        currency="USD",
        as_of_date="2024-01-15",
        published_date="2024-02-01",
        method="published",
        derivation_basis=None,
        source="test",
        confidence=None,
        confidence_basis=None,
    )
    base.update(over)
    return base


def _insert(conn, **over):
    from sqlalchemy import text
    return conn.execute(text(_INSERT), _claim(**over)).one()


def _refuses(conn, **over) -> bool:
    """True if the insert is rejected. Always rolls back."""
    sp = conn.begin_nested()
    try:
        _insert(conn, **over)
        sp.rollback()
        return False
    except Exception:                                        # noqa: BLE001
        sp.rollback()
        return True


@pytest.fixture
def sandbox(conn):
    """A savepoint per test. Nothing these tests write survives."""
    sp = conn.begin_nested()
    yield conn
    sp.rollback()


# ── the two hierarchies (AD-101) ──────────────────────────────────────

@pytest.mark.parametrize("kind", [
    "system_of_systems", "system", "subsystem", "assembly", "part"])
def test_technical_kinds_derive_the_technical_tree(sandbox, kind):
    row = _insert(sandbox, subject_kind=kind)
    assert row.hierarchy == "technical", f"{kind} filed as {row.hierarchy!r}"


@pytest.mark.parametrize("kind", [
    "enterprise", "business_area", "programme", "contract", "line_item"])
def test_business_kinds_derive_the_business_tree(sandbox, kind):
    row = _insert(sandbox, subject_kind=kind)
    assert row.hierarchy == "business", f"{kind} filed as {row.hierarchy!r}"


def test_an_unknown_kind_is_refused_not_filed_as_unknown(sandbox):
    assert not _refuses(sandbox), "the positive control was refused"
    assert _refuses(sandbox, subject_kind="galaxy_brain")


# ── vintage ───────────────────────────────────────────────────────────

def test_a_claim_about_the_future_is_a_forecast_without_being_told(sandbox):
    past = _insert(sandbox, as_of_date="2024-01-01",
                   published_date="2024-06-01")
    assert past.is_forecast is False
    fwd = _insert(sandbox, as_of_date="2030-01-01",
                  published_date="2026-10-05")
    assert fwd.is_forecast is True


def test_currency_must_be_a_currency(sandbox):
    assert not _refuses(sandbox, currency="USD"), "positive control refused"
    assert _refuses(sandbox, currency="usd")
    assert _refuses(sandbox, currency="US")


# ── method and basis (AD-102) ─────────────────────────────────────────

def test_an_inference_without_a_basis_is_refused(sandbox):
    assert not _refuses(sandbox, method="inferred",
                        derivation_basis="mass fraction of the bus"), \
        "positive control refused — the basis path itself is broken"
    assert _refuses(sandbox, method="inferred", derivation_basis=None)
    assert _refuses(sandbox, method="derived", derivation_basis="   ")
    assert not _refuses(sandbox, method="published", derivation_basis=None)


def test_confidence_and_basis_are_both_or_neither(sandbox):
    assert not _refuses(sandbox, confidence=0.8,
                        confidence_basis="two independent sources agree")
    assert not _refuses(sandbox, confidence=None, confidence_basis=None)
    assert _refuses(sandbox, confidence=0.8, confidence_basis=None)
    assert _refuses(sandbox, confidence=None, confidence_basis="vibes")
    assert _refuses(sandbox, confidence=1.5, confidence_basis="over-confident")


# ── the natural key (AD-077) ──────────────────────────────────────────

def test_rival_sources_coexist_but_a_restatement_updates(sandbox):
    sid = f"test-{uuid.uuid4()}"
    _insert(sandbox, subject_id=sid, source="usaspending", value=100)
    _insert(sandbox, subject_id=sid, source="press", value=180)
    assert _refuses(sandbox, subject_id=sid, source="press", value=175)
    assert not _refuses(sandbox, subject_id=sid, source="press",
                        value=175, as_of_date="2025-01-15")


# ── resolution ────────────────────────────────────────────────────────

def test_resolution_prefers_published_over_inferred(sandbox):
    from sqlalchemy import text
    sid = f"test-{uuid.uuid4()}"
    _insert(sandbox, subject_id=sid, source="inferred-one", value=500,
            method="inferred", derivation_basis="contract divided by units",
            confidence=0.9, confidence_basis="clean division")
    _insert(sandbox, subject_id=sid, source="filing", value=640,
            method="published", confidence=0.9,
            confidence_basis="audited filing")
    got = sandbox.execute(text(
        "SELECT value, method FROM cost_claims_resolved "
        "WHERE subject_id = :sid AND metric = 'contract_obligated'"
    ), {"sid": sid}).one()
    assert got.method == "published", (
        "an inference outranked a published figure at equal confidence")
    assert float(got.value) == 640.0


def test_resolution_returns_exactly_one_row_per_subject_metric(sandbox):
    from sqlalchemy import text
    sid = f"test-{uuid.uuid4()}"
    for i, src in enumerate(("a", "b", "c")):
        _insert(sandbox, subject_id=sid, source=src, value=100 + i)
    n = sandbox.execute(text(
        "SELECT count(*) FROM cost_claims_resolved WHERE subject_id = :sid"
    ), {"sid": sid}).scalar_one()
    assert n == 1, f"resolver returned {n} rows for one subject+metric"


# ── conflicts ─────────────────────────────────────────────────────────

def test_conflicts_needs_two_sources_and_two_values(sandbox):
    """Two sources agreeing must NOT appear. A conflicts view that fires
    on agreement is the false alarm that gets a conflicts view ignored."""
    from sqlalchemy import text
    agree = f"test-{uuid.uuid4()}"
    _insert(sandbox, subject_id=agree, source="a", value=100)
    _insert(sandbox, subject_id=agree, source="b", value=100)

    differ = f"test-{uuid.uuid4()}"
    _insert(sandbox, subject_id=differ, source="a", value=100)
    _insert(sandbox, subject_id=differ, source="b", value=250)

    rows = {r.subject_id: r for r in sandbox.execute(text(
        "SELECT * FROM cost_claim_conflicts WHERE subject_id IN (:a, :d)"
    ), {"a": agree, "d": differ})}

    assert agree not in rows, "agreement was reported as a conflict"
    assert differ in rows, "a real disagreement was not surfaced"
    assert float(rows[differ].spread) == 150.0
    assert float(rows[differ].spread_ratio) == 1.5


# ── privilege and visibility ──────────────────────────────────────────

def test_cost_claims_has_rls_enabled_and_a_pipeline_policy(conn):
    """`relkind='r'` matters: on 09-15 the sibling assertion flagged six
    VIEWS for lacking policies a view cannot have."""
    from sqlalchemy import text
    enabled = conn.execute(text(
        "SELECT relrowsecurity FROM pg_class "
        "WHERE relname = 'cost_claims' AND relkind = 'r'")).scalar_one()
    assert enabled, ("RLS is off — a GRANT without a policy leaves INSERTs "
                     "failing and UPDATEs matching zero rows, which is not "
                     "an error")
    policies = conn.execute(text(
        "SELECT polname FROM pg_policy "
        "WHERE polrelid = 'cost_claims'::regclass")).scalars().all()
    assert any("pipeline" in p for p in policies), policies


def test_pipeline_can_write_but_never_delete(conn):
    from sqlalchemy import text
    for priv in ("SELECT", "INSERT", "UPDATE"):
        assert conn.execute(text(
            "SELECT has_table_privilege('pipeline','cost_claims',:p)"
        ), {"p": priv}).scalar_one(), f"pipeline lost {priv}"
    assert not conn.execute(text(
        "SELECT has_table_privilege('pipeline','cost_claims','DELETE')"
    )).scalar_one(), "nothing leaves this table by DELETE (AD-077)"


@pytest.mark.parametrize("view", VIEWS)
def test_views_declare_security_invoker(conn, view):
    """A view runs with its OWNER's privileges by default, so RLS on the
    tables beneath it does not apply to what it returns (AD-070)."""
    from sqlalchemy import text
    opts = conn.execute(text(
        "SELECT reloptions FROM pg_class WHERE relname = :v"
    ), {"v": view}).scalar_one()
    assert opts and any("security_invoker=true" in o for o in opts), opts


def test_public_read_is_granted_not_merely_policied(conn):
    """A policy without a grant is inert, and it fails silently.

    021 created "Public read cost_claims" FOR SELECT USING (true) and
    granted SELECT only to pipeline, so anon got "permission denied for
    table cost_claims" and never reached the policy. 022 adds the grant.

    This asserts the GRANT rather than the policy, because the policy was
    already there and already proved nothing. RLS and GRANT are separate
    layers; a test that checks one and reports on the other is how
    AD-088's defect survived its own test.
    """
    from sqlalchemy import text
    missing = [
        (role, obj)
        for role in ("anon", "authenticated")
        for obj in ("cost_claims", "cost_claims_resolved",
                    "cost_claim_conflicts")
        if not conn.execute(text(
            "SELECT has_table_privilege(:r, :o, 'SELECT')"
        ), {"r": role, "o": obj}).scalar_one()
    ]
    assert not missing, (
        f"no SELECT grant for {missing} — 022 is not applied, and the "
        '"Public read cost_claims" policy is decorative until it is')


def test_public_read_does_not_leak_writes(conn):
    """The positive control for the test above: proving anon can READ is
    only reassuring if anon still cannot WRITE. A grant typo widening
    this to ALL would otherwise pass the assertion above and look fine."""
    from sqlalchemy import text
    for role in ("anon", "authenticated"):
        for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not conn.execute(text(
                "SELECT has_table_privilege(:r, 'cost_claims', :p)"
            ), {"r": role, "p": priv}).scalar_one(), \
                f"{role} holds {priv} on cost_claims — 022 granted too much"
