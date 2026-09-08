"""
Flask-Mail wrapper for HS Property Management.
All outbound emails go through this module.
"""
from flask_mail import Mail, Message
from flask import render_template

mail = Mail()


def _send(app, subject, recipients, template, **ctx):
    """Send an HTML email. Returns True on success, False on failure/suppression."""
    if not app.config.get("MAIL_SERVER"):
        app.logger.warning("[MAIL] MAIL_SERVER not configured — email suppressed.")
        return False
    try:
        sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME", "noreply@hsproperty.com")
        with app.app_context():
            html = render_template(template, **ctx)
            msg  = Message(subject=subject, recipients=recipients, html=html, sender=sender)
            mail.send(msg)
        return True
    except Exception as exc:
        app.logger.error(f"[MAIL ERROR] {exc}")
        return False


def send_invoice_email(app, tenant_email, tenant_name, invoice, property_name):
    """New rent invoice notification."""
    return _send(
        app,
        subject=f"New Invoice {invoice.inv_number} — HS Property Management",
        recipients=[tenant_email],
        template="email/invoice_generated.html",
        tenant_name=tenant_name,
        invoice=invoice,
        property_name=property_name,
    )


def send_lease_expiry_email(app, tenant_email, tenant_name, tenancy, days_left):
    """30-day lease expiry warning."""
    return _send(
        app,
        subject=f"Lease Expiry Notice — {days_left} Days Remaining",
        recipients=[tenant_email],
        template="email/lease_expiry.html",
        tenant_name=tenant_name,
        tenancy=tenancy,
        days_left=days_left,
    )


def send_overdue_email(app, tenant_email, tenant_name, invoices):
    """Overdue payment reminder (weekly)."""
    return _send(
        app,
        subject="Payment Overdue Reminder — HS Property Management",
        recipients=[tenant_email],
        template="email/overdue_reminder.html",
        tenant_name=tenant_name,
        invoices=invoices,
    )


def send_test_email(app, to_email):
    """Admin test — verify SMTP is working."""
    return _send(
        app,
        subject="HS Property — Email Configuration Test",
        recipients=[to_email],
        template="email/test.html",
    )
