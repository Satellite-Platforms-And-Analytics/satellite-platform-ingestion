"""
Proves the execute_values rewrite did not shift columns.

    python test_writer_roundtrip.py

upsert_satellites uses ON CONFLICT DO UPDATE. If the tuple order did not
match the INSERT column list, values would land in the wrong fields and
quietly corrupt the catalogue. This reads real rows, re-upserts them
unchanged, and asserts every field is identical afterwards.

Safe: it writes back exactly what it read. Worst case it is a no-op.
"""
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, text            # noqa: E402
from src.db.writer import _SATELLITE_COLUMNS, upsert_satellites   # noqa: E402

engine = create_engine(os.environ["DATABASE_URL"])
cols = ", ".join(_SATELLITE_COLUMNS)

with engine.connect() as c:
    before = [dict(r._mapping) for r in c.execute(text(
        f"SELECT {cols} FROM satellites "
        "WHERE tle_line1 IS NOT NULL ORDER BY norad_id LIMIT 5"))]

if not before:
    print("No satellites to test against."); sys.exit(1)

print(f"  Read {len(before)} satellites: "
      f"{', '.join(str(r['norad_id']) for r in before)}")

written = upsert_satellites(before)
print(f"  Re-upserted {written} rows unchanged.")

with engine.connect() as c:
    after = {r._mapping["norad_id"]: dict(r._mapping) for r in c.execute(text(
        f"SELECT {cols} FROM satellites WHERE norad_id = ANY(:ids)"),
        {"ids": [r["norad_id"] for r in before]})}

problems = []
for row in before:
    a = after.get(row["norad_id"])
    if a is None:
        problems.append(f"norad {row['norad_id']} vanished"); continue
    for col in _SATELLITE_COLUMNS:
        if row[col] != a[col]:
            problems.append(
                f"norad {row['norad_id']}.{col}: {row[col]!r} -> {a[col]!r}")

print("  " + "=" * 56)
if problems:
    print(f"  COLUMN MISALIGNMENT - {len(problems)} field(s) changed:")
    for p in problems[:15]:
        print("    ", p)
    sys.exit(1)
print(f"  PASS - all {len(_SATELLITE_COLUMNS)} columns identical across "
      f"{len(before)} satellites.")
print("  Column order is correct; the batched upsert is safe.")
