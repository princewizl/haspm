# ─────────────────────────────────────────────
#  INVOICES  ·  app.py  — drop-in replacement
#  HS Property Management
# ─────────────────────────────────────────────
#
#  Routes:
#   GET  /invoices                       → list (role-filtered)
#   POST /invoices/create                → Admin / Landlord create invoice
#   POST /invoices/<id>/pay              → Tenant self-pay (Paystack initialize)
#   GET  /invoices/paystack/callback     → Paystack redirect after payment
#   POST /invoices/paystack/webhook      → Paystack server-side webhook
#   POST /invoices/<id>/mark-paid        → Admin manual mark-paid + Transaction record
#   POST /invoices/<id>/delete           → Admin delete (unpaid only)
#   GET  /api/invoices/tenants           → JSON — tenant list for create-modal select
#   GET  /api/invoices/<id>/detail       → JSON — invoice detail (for future fetch)
#   POST /invoices/bulk-overdue          → Admin cron-style: flip pending→overdue past due_date
#
# ─────────────────────────────────────────────

import hashlib
import hmac
from datetime import date, datetime
import uuid

import requests as http

from flask import (
    render_template, redirect, url_for,
    session, request, flash, jsonify, abort, current_app
)
from models import db, User, Unit, Invoice, Transaction, BankTransferClaim
from permissions import has_perm

PAYSTACK_API   = "https://api.paystack.co"
PAYSTACK_LIMIT = 10_000_000   # Paystack blocked above ₦10 million


# ── helpers ──────────────────────────────────

def _next_inv_number() -> str:
    """Generate the next sequential INV-XXXX number."""
    last = (
        Invoice.query
        .order_by(Invoice.id.desc())
        .first()
    )
    n = (last.id + 1) if last else 1
    return f"INV-{n:04d}"


def _next_txn_number() -> str:
    last = (
        Transaction.query
        .order_by(Transaction.id.desc())
        .first()
    )
    n = (last.id + 1) if last else 1
    return f"TXN-{n:04d}"


def _build_invoice_rows(inv_list):
    """
    Convert a list of Invoice ORM objects into plain dicts
    safe to pass to the template.
    """
    today = date.today()
    rows  = []
    for inv in inv_list:
        # Auto-flip to overdue if still pending and past due
        if inv.status == "pending" and inv.due_date < today:
            inv.status = "overdue"

        unit_label  = inv.unit.unit_number if inv.unit else "—"
        prop_name   = inv.unit.prop.name   if inv.unit and inv.unit.prop else "—"
        tenant_name = inv.tenant.name      if inv.tenant else "—"
        tenant_email = inv.tenant.email    if inv.tenant else ""

        rows.append({
            "id":           inv.id,
            "inv_number":   inv.inv_number,
            "type":         inv.type,
            "amount":       inv.amount,
            "due_date":     inv.due_date,
            "due":          inv.due_date.strftime("%b %d, %Y"),
            "status":       inv.status,
            "tenant":       tenant_name,
            "tenant_email": tenant_email,
            "property":     prop_name,
            "unit":         unit_label,
        })

    # Commit any status flips
    db.session.commit()
    return rows


# ─────────────────────────────────────────────
#  GET /invoices
# ─────────────────────────────────────────────
def invoices_view(app):
    @app.route("/invoices")
    def invoices():
        from functools import wraps  # already imported globally; here for clarity

        role    = session.get("role")
        user_id = session.get("user_id")

        if not user_id:
            flash("Please sign in to continue.", "info")
            return redirect(url_for("login"))

        # Department gate (staff): Management/Finance see amounts;
        # Customer Service sees payment status only (amounts hidden);
        # Maintenance has no invoice access.
        hide_amounts = False
        if role == "Admin" and not has_perm("view_finance_full"):
            if has_perm("view_payment_status"):
                hide_amounts = True
            else:
                flash("Access denied. Your department cannot view invoices.", "danger")
                return redirect(url_for("dashboard"))

        # ── Fetch raw invoice objects ──────────────
        if role == "Tenant":
            inv_objs = (
                Invoice.query
                .filter_by(tenant_id=user_id)
                .order_by(Invoice.due_date.desc())
                .all()
            )
        else:
            inv_objs = (
                Invoice.query
                .order_by(Invoice.due_date.desc())
                .all()
            )

        # ── Build display rows (also auto-flips overdue) ──
        invoices_data = _build_invoice_rows(inv_objs)

        # ── Summary stats ──────────────────────────
        total       = sum(i["amount"] for i in invoices_data)
        paid        = sum(i["amount"] for i in invoices_data if i["status"] == "paid")
        outstanding = total - paid
        overdue_amt = sum(i["amount"] for i in invoices_data if i["status"] == "overdue")
        pending_cnt = sum(1 for i in invoices_data if i["status"] == "pending")
        overdue_cnt = sum(1 for i in invoices_data if i["status"] == "overdue")
        paid_cnt    = sum(1 for i in invoices_data if i["status"] == "paid")

        # ── Tenant list for Admin/Landlord create-modal ──
        tenants = []
        if role in ("Admin", "Landlord"):
            tenants = (
                User.query
                .filter_by(role="Tenant", is_active=True)
                .order_by(User.name)
                .all()
            )

        # ── Bank transfer: flag invoices that have pending claims ──
        if role == "Tenant":
            _pending_ids = {c.invoice_id for c in
                            BankTransferClaim.query.filter_by(
                                tenant_id=user_id, status="pending").all()}
        else:
            _pending_ids = {c.invoice_id for c in
                            BankTransferClaim.query.filter_by(status="pending").all()}
        for row in invoices_data:
            row["has_pending_transfer"] = row["id"] in _pending_ids

        # ── Pending transfer claims for Admin review panel ──
        pending_transfers = []
        if role == "Admin":
            pending_transfers = (BankTransferClaim.query
                                 .filter_by(status="pending")
                                 .order_by(BankTransferClaim.submitted_at.asc())
                                 .all())

        bank_cfg = {
            "BANK_NAME":           current_app.config.get("BANK_NAME", ""),
            "BANK_ACCOUNT_NUMBER": current_app.config.get("BANK_ACCOUNT_NUMBER", ""),
            "BANK_ACCOUNT_NAME":   current_app.config.get("BANK_ACCOUNT_NAME", ""),
            "BANK_TRANSFER_NOTE":  current_app.config.get("BANK_TRANSFER_NOTE", ""),
        }

        return render_template(
            "invoices.html",
            invoices             = invoices_data,
            total                = total,
            paid                 = paid,
            outstanding          = outstanding,
            overdue_amt          = overdue_amt,
            pending_count        = pending_cnt,
            overdue_count        = overdue_cnt,
            paid_count           = paid_cnt,
            role                 = role,
            tenants              = tenants,
            paystack_configured  = bool(current_app.config.get("PAYSTACK_SECRET_KEY")),
            pending_transfers    = pending_transfers,
            bank_cfg             = bank_cfg,
            PAYSTACK_LIMIT       = PAYSTACK_LIMIT,
            hide_amounts         = hide_amounts,
        )


# ─────────────────────────────────────────────
#  POST /invoices/create   (Admin / Landlord)
# ─────────────────────────────────────────────
def invoices_create(app):
    @app.route("/invoices/create", methods=["POST"])
    def create_invoice():
        role = session.get("role")
        if role not in ("Admin", "Landlord"):
            flash("Access denied.", "danger")
            return redirect(url_for("invoices"))
        if role == "Admin" and not has_perm("edit_payments"):
            flash("Access denied. Only Finance or Management can create invoices.", "danger")
            return redirect(url_for("invoices"))

        tenant_id  = request.form.get("tenant_id", "").strip()
        inv_type   = request.form.get("type", "Rent").strip()
        amount_raw = request.form.get("amount", "").strip()
        due_raw    = request.form.get("due", "").strip()
        unit_id    = request.form.get("unit_id", "").strip() or None

        # ── Validation ────────────────────────────
        errors = []
        if not tenant_id:
            errors.append("Please select a tenant.")
        if not amount_raw:
            errors.append("Amount is required.")
        if not due_raw:
            errors.append("Due date is required.")

        try:
            amount = float(amount_raw)
            if amount <= 0:
                raise ValueError
        except (ValueError, TypeError):
            errors.append("Amount must be a positive number.")
            amount = 0

        try:
            due_date = date.fromisoformat(due_raw)
        except (ValueError, TypeError):
            errors.append("Invalid due date format.")
            due_date = date.today()

        tenant = User.query.get(int(tenant_id)) if tenant_id.isdigit() else None
        if not tenant:
            errors.append("Selected tenant not found.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return redirect(url_for("invoices"))

        # ── Resolve unit from tenant's active tenancy if not provided ──
        resolved_unit_id = None
        if unit_id and unit_id.isdigit():
            resolved_unit_id = int(unit_id)
        else:
            from models import Tenancy
            active = (
                Tenancy.query
                .filter_by(tenant_id=tenant.id, is_active=True)
                .first()
            )
            if active:
                resolved_unit_id = active.unit_id

        new_inv = Invoice(
            inv_number  = _next_inv_number(),
            type        = inv_type,
            amount      = amount,
            due_date    = due_date,
            status      = "pending",
            tenant_id   = tenant.id,
            unit_id     = resolved_unit_id,
        )
        db.session.add(new_inv)
        db.session.commit()

        flash(
            f"Invoice {new_inv.inv_number} created for {tenant.name} "
            f"(₦{amount:,.0f} due {due_date.strftime('%b %d, %Y')}).",
            "success",
        )
        return redirect(url_for("invoices"))


# ─────────────────────────────────────────────
#  POST /invoices/<id>/pay   (Tenant self-pay)
#  Initialises a real Paystack transaction and
#  redirects the tenant to the Paystack checkout.
# ─────────────────────────────────────────────
def invoices_pay(app):
    @app.route("/invoices/<int:inv_id>/pay", methods=["POST"])
    def pay_invoice(inv_id):
        user_id = session.get("user_id")
        role    = session.get("role")

        if not user_id:
            flash("Please sign in to continue.", "info")
            return redirect(url_for("login"))

        inv = Invoice.query.get_or_404(inv_id)

        if role == "Tenant" and inv.tenant_id != user_id:
            flash("You can only pay your own invoices.", "danger")
            return redirect(url_for("invoices"))

        if inv.status == "paid":
            flash(f"{inv.inv_number} has already been paid.", "info")
            return redirect(url_for("invoices"))

        secret_key = current_app.config.get("PAYSTACK_SECRET_KEY", "")
        if not secret_key:
            if role == "Admin":
                flash("Paystack secret key is not configured. Enter it in Settings → Paystack Integration.", "warning")
                return redirect(url_for("settings"))
            flash("Online payment is not available right now. Please contact the admin.", "danger")
            return redirect(url_for("invoices"))

        tenant = User.query.get(inv.tenant_id)
        callback_url = url_for("paystack_callback", _external=True)

        try:
            resp = http.post(
                f"{PAYSTACK_API}/transaction/initialize",
                headers={"Authorization": f"Bearer {secret_key}"},
                json={
                    "email":        tenant.email,
                    "amount":       int(inv.amount * 100),   # kobo
                    "callback_url": callback_url,
                    "metadata": {
                        "invoice_id":  inv.id,
                        "inv_number":  inv.inv_number,
                        "tenant_id":   tenant.id,
                        "tenant_name": tenant.name,
                    },
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            current_app.logger.error(f"[paystack] initialize error: {exc}")
            flash("Could not reach payment gateway. Please try again.", "danger")
            return redirect(url_for("invoices"))

        if not data.get("status"):
            flash(f"Payment error: {data.get('message', 'unknown')}", "danger")
            return redirect(url_for("invoices"))

        # Store reference so the callback can look up the invoice
        inv.paystack_ref = data["data"]["reference"]
        inv.status = "pending"
        db.session.commit()

        return redirect(data["data"]["authorization_url"])


# ─────────────────────────────────────────────
#  GET /invoices/paystack/callback
#  Paystack redirects here after the user pays.
#  We verify the transaction before marking paid.
# ─────────────────────────────────────────────
def paystack_callback_view(app):
    @app.route("/invoices/paystack/callback", methods=["GET"])
    def paystack_callback():
        reference = request.args.get("reference") or request.args.get("trxref")
        if not reference:
            flash("Invalid payment callback.", "danger")
            return redirect(url_for("invoices"))

        secret_key = current_app.config.get("PAYSTACK_SECRET_KEY", "")
        try:
            resp = http.get(
                f"{PAYSTACK_API}/transaction/verify/{reference}",
                headers={"Authorization": f"Bearer {secret_key}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            current_app.logger.error(f"[paystack] verify error: {exc}")
            flash("Could not verify payment. Please contact support.", "danger")
            return redirect(url_for("invoices"))

        pdata = data.get("data", {})
        if not data.get("status") or pdata.get("status") != "success":
            flash(f"Payment not confirmed (status: {pdata.get('status', 'unknown')}).", "warning")
            return redirect(url_for("invoices"))

        inv = Invoice.query.filter_by(paystack_ref=reference).first()
        if not inv:
            flash("Invoice not found for this payment reference.", "danger")
            return redirect(url_for("invoices"))

        if inv.status != "paid":
            inv.status = "paid"
            txn = Transaction(
                txn_number   = _next_txn_number(),
                paystack_ref = reference,
                amount       = pdata["amount"] / 100,
                method       = "Paystack Card",
                status       = "confirmed",
                date         = datetime.utcnow(),
                tenant_id    = inv.tenant_id,
                invoice_id   = inv.id,
            )
            db.session.add(txn)
            db.session.commit()

        flash(
            f"Payment confirmed for {inv.inv_number}! Reference: {reference}",
            "success",
        )
        return redirect(url_for("invoices"))


# ─────────────────────────────────────────────
#  POST /invoices/paystack/webhook
#  Paystack calls this server-side for every
#  charge event. Validates HMAC-SHA512 signature
#  before acting on the payload.
# ─────────────────────────────────────────────
def paystack_webhook_view(app):
    @app.route("/invoices/paystack/webhook", methods=["POST"])
    def paystack_webhook():
        secret_key = current_app.config.get("PAYSTACK_SECRET_KEY", "").encode()

        sig = request.headers.get("x-paystack-signature", "")
        expected = hmac.new(secret_key, request.data, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(sig, expected):
            abort(400)

        event = request.get_json(silent=True) or {}
        if event.get("event") != "charge.success":
            return jsonify({"status": "ignored"}), 200

        pdata    = event.get("data", {})
        reference = pdata.get("reference")
        if not reference:
            return jsonify({"status": "no reference"}), 200

        with app.app_context():
            inv = Invoice.query.filter_by(paystack_ref=reference).first()
            if inv and inv.status != "paid":
                inv.status = "paid"
                txn = Transaction(
                    txn_number   = _next_txn_number(),
                    paystack_ref = reference,
                    amount       = pdata.get("amount", 0) / 100,
                    method       = "Paystack Card",
                    status       = "confirmed",
                    date         = datetime.utcnow(),
                    tenant_id    = inv.tenant_id,
                    invoice_id   = inv.id,
                )
                db.session.add(txn)
                db.session.commit()
                current_app.logger.info(f"[paystack webhook] {inv.inv_number} marked paid via {reference}")

        return jsonify({"status": "ok"}), 200


# ─────────────────────────────────────────────
#  POST /invoices/<id>/mark-paid   (Admin only)
# ─────────────────────────────────────────────
def invoices_mark_paid(app):
    @app.route("/invoices/<int:inv_id>/mark-paid", methods=["POST"])
    def mark_invoice_paid(inv_id):
        if session.get("role") != "Admin" or not has_perm("edit_payments"):
            flash("Only Finance or Management staff can manually mark invoices as paid.", "danger")
            return redirect(url_for("invoices"))

        inv = Invoice.query.get_or_404(inv_id)

        if inv.status == "paid":
            flash(f"{inv.inv_number} is already marked as paid.", "info")
            return redirect(url_for("invoices"))

        manual_ref = f"MANUAL-{uuid.uuid4().hex[:8].upper()}"
        inv.status       = "paid"
        inv.paystack_ref = manual_ref

        # Create a matching Transaction so Finance is consistent
        txn = Transaction(
            txn_number  = _next_txn_number(),
            paystack_ref= manual_ref,
            amount      = inv.amount,
            method      = "Manual (Admin)",
            status      = "confirmed",
            date        = datetime.utcnow(),
            tenant_id   = inv.tenant_id,
            invoice_id  = inv.id,
        )
        db.session.add(txn)
        db.session.commit()

        flash(f"{inv.inv_number} marked as paid and transaction recorded.", "success")
        return redirect(url_for("invoices"))


# ─────────────────────────────────────────────
#  POST /invoices/<id>/delete   (Admin only)
# ─────────────────────────────────────────────
def invoices_delete(app):
    @app.route("/invoices/<int:inv_id>/delete", methods=["POST"])
    def delete_invoice(inv_id):
        if session.get("role") != "Admin" or not has_perm("edit_payments"):
            flash("Only Finance or Management staff can delete invoices.", "danger")
            return redirect(url_for("invoices"))

        inv = Invoice.query.get_or_404(inv_id)

        if inv.status == "paid":
            flash(
                f"Cannot delete {inv.inv_number} — it has already been paid. "
                "Void it instead or contact your database administrator.",
                "warning",
            )
            return redirect(url_for("invoices"))

        inv_number = inv.inv_number
        db.session.delete(inv)
        db.session.commit()
        flash(f"Invoice {inv_number} deleted.", "success")
        return redirect(url_for("invoices"))


# ─────────────────────────────────────────────
#  GET /api/invoices/tenants
#  Returns JSON list of active tenants for the
#  create-modal dropdown + unit pre-fill.
# ─────────────────────────────────────────────
def api_invoice_tenants(app):
    @app.route("/api/invoices/tenants")
    def api_tenants_for_invoice():
        if session.get("role") not in ("Admin", "Landlord"):
            return jsonify({"error": "Forbidden"}), 403

        from models import Tenancy
        tenants = (
            User.query
            .filter_by(role="Tenant", is_active=True)
            .order_by(User.name)
            .all()
        )

        data = []
        for t in tenants:
            active = (
                Tenancy.query
                .filter_by(tenant_id=t.id, is_active=True)
                .first()
            )
            data.append({
                "id":           t.id,
                "name":         t.name,
                "email":        t.email,
                "unit_id":      active.unit_id      if active else None,
                "unit_number":  active.unit.unit_number if active and active.unit else None,
                "monthly_rent": active.monthly_rent if active else 0,
                "property":     active.unit.prop.name if active and active.unit and active.unit.prop else None,
            })
        return jsonify(data)


# ─────────────────────────────────────────────
#  POST /invoices/bulk-overdue
#  Cron-style — call manually or via a scheduler.
#  Flips all "pending" invoices whose due_date has
#  passed into "overdue".
# ─────────────────────────────────────────────
def invoices_bulk_overdue(app):
    @app.route("/invoices/bulk-overdue", methods=["POST"])
    def bulk_overdue():
        if session.get("role") != "Admin" or not has_perm("edit_payments"):
            return jsonify({"error": "Forbidden"}), 403

        today   = date.today()
        updated = (
            Invoice.query
            .filter(Invoice.status == "pending", Invoice.due_date < today)
            .all()
        )
        count = len(updated)
        for inv in updated:
            inv.status = "overdue"
        db.session.commit()

        flash(f"{count} invoice(s) flipped to overdue.", "info")
        return redirect(url_for("invoices"))


# ─────────────────────────────────────────────
#  POST /invoices/<id>/bank-transfer
#  Tenant submits proof of a bank transfer.
# ─────────────────────────────────────────────
def bank_transfer_submit_view(app):
    @app.route("/invoices/<int:inv_id>/bank-transfer", methods=["POST"])
    def submit_bank_transfer(inv_id):
        user_id = session.get("user_id")
        role    = session.get("role")
        if not user_id:
            return redirect(url_for("login"))

        inv = Invoice.query.get_or_404(inv_id)

        if role == "Tenant" and inv.tenant_id != user_id:
            flash("You can only pay your own invoices.", "danger")
            return redirect(url_for("invoices"))
        if inv.status == "paid":
            flash(f"{inv.inv_number} is already paid.", "info")
            return redirect(url_for("invoices"))

        if BankTransferClaim.query.filter_by(invoice_id=inv_id, status="pending").first():
            flash("A transfer claim for this invoice is already awaiting admin review.", "info")
            return redirect(url_for("invoices"))

        bank_ref = request.form.get("bank_ref", "").strip()
        if not bank_ref:
            flash("Please provide your bank transaction reference / teller number.", "danger")
            return redirect(url_for("invoices"))

        raw_date = request.form.get("transfer_date", "").strip()
        try:
            t_date = date.fromisoformat(raw_date) if raw_date else None
        except ValueError:
            t_date = None

        claim = BankTransferClaim(
            invoice_id    = inv.id,
            tenant_id     = user_id,
            amount        = inv.amount,
            bank_ref      = bank_ref,
            transfer_date = t_date,
            note          = request.form.get("note", "").strip(),
            status        = "pending",
        )
        db.session.add(claim)
        db.session.commit()

        flash(
            f"Transfer proof for {inv.inv_number} submitted — admin will confirm shortly.",
            "success",
        )
        return redirect(url_for("invoices"))


# ─────────────────────────────────────────────
#  POST /invoices/transfers/<claim_id>/confirm
#  Admin confirms a pending bank transfer.
# ─────────────────────────────────────────────
def confirm_transfer_view(app):
    @app.route("/invoices/transfers/<int:claim_id>/confirm", methods=["POST"])
    def confirm_transfer(claim_id):
        if session.get("role") != "Admin" or not has_perm("edit_payments"):
            flash("Only Finance or Management staff can confirm transfers.", "danger")
            return redirect(url_for("invoices"))

        claim = BankTransferClaim.query.get_or_404(claim_id)
        if claim.status != "pending":
            flash("This claim has already been reviewed.", "info")
            return redirect(url_for("invoices"))

        inv = Invoice.query.get(claim.invoice_id)
        if inv and inv.status != "paid":
            ref = f"BT-{uuid.uuid4().hex[:10].upper()}"
            inv.status       = "paid"
            inv.paystack_ref = ref
            txn = Transaction(
                txn_number   = _next_txn_number(),
                paystack_ref = ref,
                amount       = claim.amount,
                method       = f"Bank Transfer ({claim.bank_ref})",
                status       = "confirmed",
                date         = datetime.utcnow(),
                tenant_id    = inv.tenant_id,
                invoice_id   = inv.id,
            )
            db.session.add(txn)

        claim.status      = "confirmed"
        claim.reviewed_at = datetime.utcnow()
        claim.reviewed_by = session.get("user_id")
        db.session.commit()

        flash(
            f"{inv.inv_number if inv else 'Invoice'} confirmed as paid via bank transfer.",
            "success",
        )
        return redirect(url_for("invoices"))


# ─────────────────────────────────────────────
#  POST /invoices/transfers/<claim_id>/reject
#  Admin rejects a pending bank transfer claim.
# ─────────────────────────────────────────────
def reject_transfer_view(app):
    @app.route("/invoices/transfers/<int:claim_id>/reject", methods=["POST"])
    def reject_transfer(claim_id):
        if session.get("role") != "Admin" or not has_perm("edit_payments"):
            flash("Only Finance or Management staff can reject transfer claims.", "danger")
            return redirect(url_for("invoices"))

        claim = BankTransferClaim.query.get_or_404(claim_id)
        if claim.status != "pending":
            flash("This claim has already been reviewed.", "info")
            return redirect(url_for("invoices"))

        claim.status      = "rejected"
        claim.reviewed_at = datetime.utcnow()
        claim.reviewed_by = session.get("user_id")
        db.session.commit()

        flash(
            f"Transfer claim rejected — {claim.tenant.name}'s invoice remains unpaid.",
            "warning",
        )
        return redirect(url_for("invoices"))


# ─────────────────────────────────────────────
#  REGISTRATION  — call this from app.py
# ─────────────────────────────────────────────
def register_invoice_routes(app):
    invoices_view(app)
    invoices_create(app)
    invoices_pay(app)
    paystack_callback_view(app)
    paystack_webhook_view(app)
    bank_transfer_submit_view(app)
    confirm_transfer_view(app)
    reject_transfer_view(app)
    invoices_mark_paid(app)
    invoices_delete(app)
    api_invoice_tenants(app)
    invoices_bulk_overdue(app)