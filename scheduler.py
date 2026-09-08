"""
Background scheduler — HS Property Management.
Three daily jobs run at 08:00 WAT (Africa/Lagos):
  1. mark_overdue       — flip pending→overdue, email tenant weekly
  2. monthly_invoices   — generate rent invoice if none exists this month
  3. lease_expiry       — 30-day warning + final invoice when lease ends soon
"""
import os
import logging
from datetime import date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from models import db, User, Tenancy, Invoice, NotificationLog
from mailer import send_invoice_email, send_lease_expiry_email, send_overdue_email

log = logging.getLogger("hs.scheduler")


# ── shared helpers ────────────────────────────────────────────────────

def _next_inv_number():
    last = Invoice.query.order_by(Invoice.id.desc()).first()
    n = (last.id + 1) if last else 1
    return f"INV-{n:04d}"


def _already_notified(tenant_id, notif_type, ref_id=None, window_days=1):
    """True if this notification was already sent within window_days."""
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    q = (NotificationLog.query
         .filter_by(tenant_id=tenant_id, notif_type=notif_type, status="sent")
         .filter(NotificationLog.sent_at >= cutoff))
    if ref_id is not None:
        q = q.filter_by(ref_id=ref_id)
    return q.first() is not None


def _log(tenant_id, notif_type, ref_id=None, recipient="", status="sent"):
    entry = NotificationLog(
        notif_type=notif_type, tenant_id=tenant_id,
        ref_id=ref_id, recipient=recipient, status=status,
    )
    db.session.add(entry)


def _month_window(d=None):
    """Return (month_start, month_end) for date d (default today)."""
    d = d or date.today()
    m_start = d.replace(day=1)
    if d.month == 12:
        m_end = d.replace(year=d.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        m_end = d.replace(month=d.month + 1, day=1) - timedelta(days=1)
    return m_start, m_end


# ── job 1: mark overdue + weekly reminder ─────────────────────────────

def mark_overdue_invoices(app):
    with app.app_context():
        today = date.today()
        overdue = (Invoice.query
                   .filter_by(status="pending")
                   .filter(Invoice.due_date < today)
                   .all())

        by_tenant = {}
        for inv in overdue:
            inv.status = "overdue"
            by_tenant.setdefault(inv.tenant_id, []).append(inv)
        db.session.commit()

        for tid, invs in by_tenant.items():
            if _already_notified(tid, "overdue", window_days=7):
                continue
            tenant = User.query.get(tid)
            if not tenant:
                continue
            ok = send_overdue_email(app, tenant.email, tenant.name, invs)
            _log(tid, "overdue", recipient=tenant.email, status="sent" if ok else "failed")
        db.session.commit()
        log.info(f"[mark_overdue] {len(overdue)} invoice(s) flipped overdue.")


# ── job 2: monthly rent invoice generation ────────────────────────────

def generate_monthly_rent_invoices(app):
    """
    Idempotent: creates a Rent invoice for each active tenancy
    if none already exists for the current calendar month.
    """
    with app.app_context():
        today = date.today()
        m_start, m_end = _month_window(today)
        due_date = today + timedelta(days=7)

        active = Tenancy.query.filter_by(is_active=True).all()
        created = 0

        for t in active:
            # Skip if lease ends before the due date (lease_expiry job handles these)
            if t.end_date and t.end_date < due_date:
                continue

            existing = (Invoice.query
                        .filter_by(tenant_id=t.tenant_id, type="Rent")
                        .filter(Invoice.due_date >= m_start,
                                Invoice.due_date <= m_end)
                        .first())
            if existing:
                continue

            inv = Invoice(
                inv_number=_next_inv_number(),
                type="Rent",
                amount=t.monthly_rent,
                due_date=due_date,
                status="pending",
                tenant_id=t.tenant_id,
                unit_id=t.unit_id,
            )
            db.session.add(inv)
            db.session.flush()

            tenant    = t.tenant
            prop_name = (t.unit.prop.name if t.unit and t.unit.prop else "Your Property")
            if not _already_notified(tenant.id, "rent_invoice", window_days=25):
                ok = send_invoice_email(app, tenant.email, tenant.name, inv, prop_name)
                _log(tenant.id, "rent_invoice", ref_id=inv.id,
                     recipient=tenant.email, status="sent" if ok else "failed")
            created += 1

        db.session.commit()
        log.info(f"[monthly_invoices] {created} invoice(s) created.")


# ── job 3: lease expiry warning (30 days before end) ──────────────────

def check_lease_expiry(app):
    """
    Fires once per tenancy in the 28–35 day window before lease end.
    Sends warning email + generates final month's invoice.
    """
    with app.app_context():
        today        = date.today()
        window_start = today + timedelta(days=28)
        window_end   = today + timedelta(days=35)

        expiring = Tenancy.query.filter(
            Tenancy.is_active == True,
            Tenancy.end_date  >= window_start,
            Tenancy.end_date  <= window_end,
        ).all()

        for t in expiring:
            tenant    = t.tenant
            days_left = (t.end_date - today).days

            # Email warning (once per tenancy)
            if not _already_notified(tenant.id, "lease_expiry", ref_id=t.id, window_days=30):
                ok = send_lease_expiry_email(app, tenant.email, tenant.name, t, days_left)
                _log(tenant.id, "lease_expiry", ref_id=t.id,
                     recipient=tenant.email, status="sent" if ok else "failed")

            # Generate final month invoice if not yet issued
            if t.end_date.month == 12:
                next_m = date(t.end_date.year + 1, 1, 1)
            else:
                next_m = date(t.end_date.year, t.end_date.month, 1)

            final_exists = (Invoice.query
                            .filter_by(tenant_id=t.tenant_id, type="Rent")
                            .filter(Invoice.due_date >= next_m)
                            .first())
            if not final_exists:
                inv = Invoice(
                    inv_number=_next_inv_number(),
                    type="Rent",
                    amount=t.monthly_rent,
                    due_date=t.end_date,
                    status="pending",
                    tenant_id=t.tenant_id,
                    unit_id=t.unit_id,
                )
                db.session.add(inv)
                db.session.flush()
                prop_name = (t.unit.prop.name if t.unit and t.unit.prop else "Your Property")
                ok2 = send_invoice_email(app, tenant.email, tenant.name, inv, prop_name)
                _log(tenant.id, "rent_invoice", ref_id=inv.id,
                     recipient=tenant.email, status="sent" if ok2 else "failed")

        db.session.commit()
        log.info(f"[lease_expiry] Processed {len(expiring)} expiring tenancy/ies.")


# ── manual run (called from admin UI) ─────────────────────────────────

def run_all_jobs(app):
    """Trigger all three jobs immediately (admin manual run)."""
    mark_overdue_invoices(app)
    generate_monthly_rent_invoices(app)
    check_lease_expiry(app)


# ── init ──────────────────────────────────────────────────────────────

def init_scheduler(app):
    try:
        tz = "Africa/Lagos"
        scheduler = BackgroundScheduler(timezone=tz)

        scheduler.add_job(
            func=lambda: mark_overdue_invoices(app),
            trigger=CronTrigger(hour=8, minute=0, timezone=tz),
            id="mark_overdue", replace_existing=True,
        )
        scheduler.add_job(
            func=lambda: generate_monthly_rent_invoices(app),
            trigger=CronTrigger(hour=8, minute=5, timezone=tz),
            id="monthly_invoices", replace_existing=True,
        )
        scheduler.add_job(
            func=lambda: check_lease_expiry(app),
            trigger=CronTrigger(hour=8, minute=10, timezone=tz),
            id="lease_expiry", replace_existing=True,
        )

        scheduler.start()
        log.info("[scheduler] Started — 3 daily jobs at 08:00 WAT.")
        return scheduler
    except Exception as exc:
        log.error(f"[scheduler] Failed to start: {exc}")
        return None
