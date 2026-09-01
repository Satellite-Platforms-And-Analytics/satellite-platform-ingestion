import os
from dotenv import load_dotenv; load_dotenv()
from sqlalchemy import create_engine, text
e = create_engine(os.environ["DATABASE_URL"])
with e.connect() as c:
    print("  step        status   records   duration_s   rows/sec")
    for r in c.execute(text(
        "SELECT step, status, records_processed, duration_s FROM ingestion_log "
        "WHERE duration_s IS NOT NULL AND records_processed > 0 "
        "ORDER BY created_at DESC LIMIT 8")):
        rate = (r[2]/r[3]) if r[3] else 0
        print(f"  {str(r[0])[:10]:<10}  {str(r[1])[:7]:<7} {r[2]:>8,} {r[3]:>11.1f} {rate:>10.1f}")
