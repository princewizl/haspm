"""
Background scheduler — HS Property Management.
Three daily jobs run at 08:00 WAT (Africa/Lagos):
  1. mark_overdue       — flip pending→overdue, email tenant weekly
  2. annual_invoices    — generate the year's rent invoice on the lease anniversary
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


# ── job 2: annual rent invoice generation ─────────────────────────────

def _lease_year_window(tenancy, today):
    """The lease term containing `today`, as (term_start, term_expiry).

    The first term runs from move-in to the lease expiry entered on the
    tenant's form; every later term starts on a renewal date (the day after
    an expiry) and runs a year. A tenancy that moved in 2026-01-15 with expiry
    2026-12-31 is billed for 2026-01-15..2026-12-31, then 2027-01-01..
    2027-12-31, and so on.
    """
    term = tenancy.term_for(today)
    return term if term else (None, None)


def generate_annual_rent_invoices(app):
    """
    Idempotent: creates one Rent invoice per active tenancy per lease year.

    Rent is paid annually, so a tenant is billed at the start of each lease
    term: on move-in, then on each renewal date. Invoices are raised
    ADVANCE_DAYS before the renewal date so the tenant has notice.
    """
    ADVANCE_DAYS = 30

    with app.app_context():
        today = date.today()
        active = Tenancy.query.filter_by(is_active=True).all()
        created = 0

        for t in active:
            if not t.start_date:
                continue

            period_start, period_end = _lease_year_window(t, today + timedelta(days=ADVANCE_DAYS))
            if period_start is None:
                continue

            # Do not bill a year that starts after the lease has ended.
            if t.end_date and period_start > t.end_date:
                continue

            # One Rent invoice per lease term: due on the day the term starts.
            existing = (Invoice.query
                        .filter_by(tenant_id=t.tenant_id, unit_id=t.unit_id, type="Rent")
                        .filter(Invoice.due_date >= period_start,
                                Invoice.due_date <= period_end)
                        .first())
            if existing:
                continue

            inv = Invoice(
                inv_number=_next_inv_number(),
                type="Rent",
                amount=t.annual_rent,
                due_date=period_start,
                status="pending",
                tenant_id=t.tenant_id,
                unit_id=t.unit_id,
            )
            db.session.add(inv)
            db.session.flush()

            tenant    = t.tenant
            prop_name = (t.unit.prop.name if t.unit and t.unit.prop else "Your Property")
            # One notification per lease year, not per run.
            if not _already_notified(tenant.id, "rent_invoice", ref_id=inv.id, window_days=300):
                ok = send_invoice_email(app, tenant.email, tenant.name, inv, prop_name)
                _log(tenant.id, "rent_invoice", ref_id=inv.id,
                     recipient=tenant.email, status="sent" if ok else "failed")
            created += 1

        db.session.commit()
        log.info(f"[annual_invoices] {created} rent invoice(s) created.")


# ── job 3: lease expiry warning (30 days before end) ──────────────────

def check_lease_expiry(app):
    """
    Fires once per lease term in the 28-35 day window before the lease
    expiry. Leases renew yearly, so this is a renewal reminder every year;
    when a lease end date is set it warns before the tenant moves out.
    """
    with app.app_context():
        today = date.today()
        sent  = 0

        for t in Tenancy.query.filter_by(is_active=True).all():
            expiry = t.current_expiry
            if not expiry:
                continue
            days_left = (expiry - today).days
            if not 28 <= days_left <= 35:
                continue

            tenant = t.tenant
            # Once per term: the 30-day window cannot span two expiries.
            if not _already_notified(tenant.id, "lease_expiry", ref_id=t.id, window_days=30):
                ok = send_lease_expiry_email(app, tenant.email, tenant.name, t, days_left)
                _log(tenant.id, "lease_expiry", ref_id=t.id,
                     recipient=tenant.email, status="sent" if ok else "failed")
                sent += 1

            # No final invoice here: with annual rent the next term's invoice is
            # raised ahead of the renewal date by generate_annual_rent_invoices.

        db.session.commit()
        log.info(f"[lease_expiry] {sent} expiry reminder(s) sent.")


# ── manual run (called from admin UI) ─────────────────────────────────

def run_all_jobs(app):
    """Trigger all three jobs immediately (admin manual run)."""
    mark_overdue_invoices(app)
    generate_annual_rent_invoices(app)
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
            func=lambda: generate_annual_rent_invoices(app),
            trigger=CronTrigger(hour=8, minute=5, timezone=tz),
            id="annual_invoices", replace_existing=True,
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
