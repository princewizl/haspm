"""Schema migration: per-property commission, unit naming, annual rent, attachments.

Idempotent — safe to run repeatedly and safe to run on a fresh database.

  python migrate_v2.py            # migrate the database the app is configured for
  python migrate_v2.py --dry-run  # report what would change, touch nothing

What it does:
  1. properties.commission_pct        (new, NULL = inherit the global default)
  2. properties.unit_naming_pattern   (new)
  3. tenancies.monthly_rent -> annual_rent   (rename; values are unchanged,
     they already held the yearly figure)
  4. unique index on units(property_id, unit_number), after reporting clashes
  5. attachments table

Run deploy/backup.sh first on anything with real data in it.
"""
import sys

from sqlalchemy import inspect, text

from app import app
from models import db

DRY = "--dry-run" in sys.argv


def _cols(insp, table):
    try:
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


def _say(msg, changed=False):
    print(("  [would] " if DRY and changed else "  ") + msg)


def migrate():
    with app.app_context():
        insp = inspect(db.engine)
        tables = set(insp.get_table_names())
        print(f"Database: {app.config['SQLALCHEMY_DATABASE_URI']}")
        if DRY:
            print("DRY RUN - nothing will be written\n")

        stmts = []

        # ── 1 & 2. property columns ──────────────────────────────────
        if "properties" in tables:
            pcols = _cols(insp, "properties")
            if "commission_pct" not in pcols:
                stmts.append("ALTER TABLE properties ADD COLUMN commission_pct FLOAT")
                _say("properties.commission_pct  -> add", True)
            else:
                _say("properties.commission_pct  already present")

            if "unit_naming_pattern" not in pcols:
                stmts.append(
                    "ALTER TABLE properties ADD COLUMN unit_naming_pattern VARCHAR(120)")
                _say("properties.unit_naming_pattern -> add", True)
            else:
                _say("properties.unit_naming_pattern already present")

        # ── 3. rent is annual ────────────────────────────────────────
        if "tenancies" in tables:
            tcols = _cols(insp, "tenancies")
            if "annual_rent" in tcols:
                _say("tenancies.annual_rent already present")
            elif "monthly_rent" in tcols:
                # SQLite has supported RENAME COLUMN since 3.25 (2018).
                stmts.append(
                    "ALTER TABLE tenancies RENAME COLUMN monthly_rent TO annual_rent")
                _say("tenancies.monthly_rent -> annual_rent (values unchanged)", True)

        # ── 4. unit names unique within a property ───────────────────
        if "units" in tables:
            existing = {i["name"] for i in insp.get_indexes("units")}
            if "uq_unit_per_property" in existing:
                _say("units unique index already present")
            else:
                dupes = db.session.execute(text("""
                    SELECT property_id, unit_number, COUNT(*) c
                    FROM units GROUP BY property_id, unit_number HAVING c > 1
                """)).fetchall()
                if dupes:
                    print("\n  ! Cannot add the unique index yet - duplicates exist:")
                    for pid, num, c in dupes:
                        print(f"      property {pid}: '{num}' x{c}")
                    print("    Rename the duplicates, then run this again.\n")
                else:
                    stmts.append("CREATE UNIQUE INDEX uq_unit_per_property "
                                 "ON units (property_id, unit_number)")
                    _say("units(property_id, unit_number) unique index -> add", True)

        # ── apply ────────────────────────────────────────────────────
        if stmts and not DRY:
            with db.engine.begin() as conn:
                for st in stmts:
                    conn.execute(text(st))
            print(f"\n  applied {len(stmts)} statement(s)")
        elif not stmts:
            print("\n  no column changes needed")

        # ── 5. anything new (attachments) ────────────────────────────
        if DRY:
            missing = {t.name for t in db.metadata.sorted_tables} - tables
            if missing:
                _say(f"create tables: {', '.join(sorted(missing))}", True)
        else:
            db.create_all()
            after = set(inspect(db.engine).get_table_names())
            created = after - tables
            if created:
                print(f"  created table(s): {', '.join(sorted(created))}")

        print("\nDone." if not DRY else "\nDry run complete.")


if __name__ == "__main__":
    migrate()
