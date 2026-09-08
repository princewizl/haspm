"""
HS Property Management — Department-Based Access Control (Admin RBAC)

Implements the four-department permission matrix specified by the client:

  Management        — full visibility, full editing, approvals, property & staff management
  Finance           — full financial access; limited tenant/landlord access (payment-related)
  Maintenance       — full maintenance access; limited tenant/landlord access (basic info)
  Customer Service  — full tenant/landlord contact access; status-only finance/maintenance

All enforcement is SERVER-SIDE. Template helpers (`has_perm`, `dept`) are only
used to hide UI elements; every protected action is re-checked in its route.

Admins with no department assigned are treated as Management (the original
system had a single super-admin; this preserves owner access and avoids
lockout). New staff accounts should always be given a department.
"""
from functools import wraps
from flask import session, flash, redirect, url_for, g


DEPARTMENTS = ["Management", "Finance", "Maintenance", "Customer Service"]

# ── Permission keys ───────────────────────────────────────────
#  view_*  = read access        edit_* / manage_* = write access
#
#  manage_properties   add/edit/remove properties & ownership       (Management only)
#  manage_staff        create/edit/delete users, assign roles/depts (Management only)
#  manage_settings     email / paystack / bank config, run jobs     (Management only)
#
#  view_finance_full   finance dashboards, ledgers, full amounts
#  view_finance_costs  maintenance-related costs only (Maintenance dept)
#  view_payment_status payment status without amounts (Customer Service)
#  edit_payments       record/confirm payments, mark invoices paid, adjust ledger
#  edit_expenses       create/update expense records & cost estimates
#  manage_payouts      update landlord payout status
#  export_financials   generate/export financial statements & reports
#
#  view_maintenance_full   full ticket details & job logs
#  view_maintenance_status ticket status only (Customer Service)
#  edit_maintenance        create/assign/update/close maintenance jobs
#
#  view_tenant_full    complete tenant records & documents
#  view_tenant_basic   name / unit / phone only (Maintenance dept)
#  view_landlord_full  complete landlord records
#  edit_contacts       update tenant/landlord contact details (CS + Management)
#  log_complaints      log & assign complaints, send notices/reminders
#  onboard_users       add tenant/landlord accounts, onboarding status

_FINANCE_PERMS = {
    "view_finance_full", "view_payment_status",
    "edit_payments", "edit_expenses", "manage_payouts", "export_financials",
    "view_tenant_payment", "view_landlord_payout",
}

_MAINTENANCE_PERMS = {
    "view_maintenance_full", "view_maintenance_status", "edit_maintenance",
    "view_finance_costs", "view_tenant_basic", "edit_expenses",
}

_CUSTOMER_SERVICE_PERMS = {
    "view_tenant_full", "view_landlord_full", "edit_contacts",
    "log_complaints", "onboard_users",
    "view_maintenance_status", "view_payment_status",
}

DEPT_PERMS = {
    "Management":       {"*"},          # every permission
    "Finance":          _FINANCE_PERMS,
    "Maintenance":      _MAINTENANCE_PERMS,
    "Customer Service": _CUSTOMER_SERVICE_PERMS,
}


def current_dept():
    """Department of the logged-in user, read from the DB (not the session)
    so a department change by Management takes effect immediately.
    Cached per-request on flask.g. Returns None for non-admins/anonymous."""
    if "user_id" not in session:
        return None
    if session.get("role") != "Admin":
        return None
    if not hasattr(g, "_dept_cache"):
        from models import User
        u = User.query.get(session["user_id"])
        # No department set → legacy super-admin → Management
        g._dept_cache = (u.department if u and u.department in DEPARTMENTS
                         else "Management")
    return g._dept_cache


def has_perm(perm):
    """True if the logged-in user is an Admin whose department grants `perm`."""
    dept = current_dept()
    if dept is None:
        return False
    perms = DEPT_PERMS.get(dept, set())
    return "*" in perms or perm in perms


def dept_required(*perms):
    """Route decorator: user must be a logged-in Admin whose department has
    at least ONE of the listed permissions. Management always passes."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "user_id" not in session:
                flash("Please sign in to continue.", "info")
                return redirect(url_for("login"))
            if session.get("role") != "Admin":
                flash("Access denied. This page is restricted to staff.", "danger")
                return redirect(url_for("dashboard"))
            if not any(has_perm(p) for p in perms):
                flash(f"Access denied. The {current_dept()} department cannot perform this action.", "danger")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return decorated
    return decorator


def register_permissions(app):
    """Expose helpers to every template."""
    @app.context_processor
    def inject_permissions():
        return {
            "has_perm": has_perm,
            "dept": current_dept(),
            "DEPARTMENTS": DEPARTMENTS,
        }
