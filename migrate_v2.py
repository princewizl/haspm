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
  6. tickets: maintenance classification columns, existing tickets mapped
     from their old priority (urgent/high -> Urgent, otherwise Routine)
  7. properties.avg_rent recalculated as the mean of the units' rents
  8. units.unit_number widened to 60 characters (Postgres only; SQLite
     does not enforce lengths)
  9. tenancies.lease_expiry (new): filled from the old lease end if one was
     typed, otherwise one year after move-in. Also lists tenancies that
     look like they were ended by mistake.

Run deploy/backup.sh first on anything with real data in it.
"""
import sys

from sqlalchemy import inspect, text

from app import app
from models import db

DRY = "--dry-run" in sys.argv

TICKET_COLUMNS = [
    ("tenant_category",       "VARCHAR(30)"),
    ("classification",        "VARCHAR(30)"),
    ("classified_by_id",      "INTEGER"),
    ("classified_at",         "TIMESTAMP"),
    ("response_due_at",       "TIMESTAMP"),
    ("scheduled_for",         "DATE"),
    ("charge_estimate",       "FLOAT"),
    ("charge_reason",         "VARCHAR(300)"),
    ("approval_status",       "VARCHAR(20)"),
    ("approval_note",         "TEXT"),
    ("approval_requested_at", "TIMESTAMP"),
    ("approval_decided_at",   "TIMESTAMP"),
]

_PRIORITY_CASE = ("CASE WHEN priority IN ('urgent', 'high') THEN 'urgent' "
                  "ELSE 'routine' END")


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

        # ── 8. unit names up to 60 characters ────────────────────────
        # SQLite ignores VARCHAR lengths; only a real server database needs it.
        if "units" in tables and db.engine.dialect.name == "postgresql":
            stmts.append("ALTER TABLE units ALTER COLUMN unit_number TYPE VARCHAR(60)")
            _say("units.unit_number -> VARCHAR(60)", True)

        # ── 9. lease expiry, separate from lease end ─────────────────
        backfill_expiry = False
        if "tenancies" in tables and "lease_expiry" not in _cols(insp, "tenancies"):
            stmts.append("ALTER TABLE tenancies ADD COLUMN lease_expiry DATE")
            _say("tenancies.lease_expiry -> add", True)
            backfill_expiry = True

        # ── 6. maintenance request classification ────────────────────
        backfill = False
        if "tickets" in tables:
            kcols = _cols(insp, "tickets")
            for name, ddl in TICKET_COLUMNS:
                if name not in kcols:
                    stmts.append(f"ALTER TABLE tickets ADD COLUMN {name} {ddl}")
                    _say(f"tickets.{name} -> add", True)
            backfill = "classification" not in kcols

        # ── apply ────────────────────────────────────────────────────
        if stmts and not DRY:
            with db.engine.begin() as conn:
                for st in stmts:
                    conn.execute(text(st))
            print(f"\n  applied {len(stmts)} statement(s)")
        elif not stmts:
            print("\n  no column changes needed")

        # Existing tickets only had a priority. Map it onto both the tenant's
        # category and the staff classification, and set the response window.
        if backfill and not DRY:
            with db.engine.begin() as conn:
                n = conn.execute(text(f"""
                    UPDATE tickets SET
                      tenant_category = {_PRIORITY_CASE},
                      classification  = {_PRIORITY_CASE}
                    WHERE classification IS NULL
                """)).rowcount
            from models import Ticket
            for t in Ticket.query.filter(Ticket.response_due_at.is_(None)).all():
                t.apply_classification(t.classification or "routine")
            db.session.commit()
            print(f"  classified {n} existing ticket(s) from their old priority")
        elif backfill:
            _say("existing tickets -> classify from priority", True)

        # ── 9 (cont). fill lease_expiry, report suspect closures ─────
        if "tenancies" in tables and not DRY:
            from models import Tenancy
            if backfill_expiry:
                # A lease end typed in the old form was the expiry; otherwise
                # one year after move-in. Lease end itself is left untouched.
                n = 0
                for t in Tenancy.query.filter(Tenancy.lease_expiry.is_(None)).all():
                    if t.start_date:
                        t.lease_expiry = t.end_date or Tenancy.default_expiry(t.start_date)
                        n += 1
                db.session.commit()
                print(f"  set lease_expiry on {n} tenancy(ies)")
                ending = Tenancy.query.filter(Tenancy.is_active.is_(True),
                                              Tenancy.end_date.isnot(None)).count()
                if ending:
                    print(f"  ! {ending} active tenancy(ies) still have a Lease End date, so they"
                          " stop renewing then. Clear it on the tenant's form if they renew yearly.")
        if "tenancies" in tables:
            # Before this release, saving a tenant's profile form ended their
            # tenancy without setting an end date. List likely cases.
            rows = db.session.execute(text("""
                SELECT t.id, u.name, un.unit_number, p.name
                FROM tenancies t
                JOIN users u       ON u.id = t.tenant_id
                LEFT JOIN units un ON un.id = t.unit_id
                LEFT JOIN properties p ON p.id = un.property_id
                WHERE t.is_active = :no AND t.end_date IS NULL AND u.role = 'Tenant'
                  AND NOT EXISTS (SELECT 1 FROM tenancies a
                                  WHERE a.tenant_id = t.tenant_id AND a.is_active = :yes)
            """), {"no": False, "yes": True}).fetchall()
            if rows:
                print("\n  ! Tenancies ended with no end date (possibly by the old profile-form"
                      " bug) - check and re-assign if wrong:")
                for tid, who, unit, prop in rows:
                    print(f"      tenancy {tid}: {who} - {prop or '?'} / {unit or '?'}")

        # ── 7. average rent is now derived from the units' rents ─────
        # Only where some unit has a rent; properties whose units were all
        # created at 0 keep the figure that was typed in by hand.
        if "properties" in tables and "units" in tables:
            from models import Property
            changed = []
            for p in Property.query.all():
                rents = [u.rent_amount or 0 for u in p.units]
                if rents and any(rents):
                    avg = round(sum(rents) / len(rents), 2)
                    if abs((p.avg_rent or 0) - avg) > 0.005:
                        changed.append((p, avg))
            for p, avg in changed:
                _say(f"properties[{p.id}] {p.name}: avg_rent {p.avg_rent or 0:,.0f} -> {avg:,.0f}", True)
                if not DRY:
                    p.avg_rent = avg
            if changed and not DRY:
                db.session.commit()

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
