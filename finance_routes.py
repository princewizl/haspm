# ─────────────────────────────────────────────
#  FINANCE MODULE  ·  HS Property Management
#  Covers: all three roles (Admin / Landlord / Tenant)
#
#  Routes registered:
#   GET  /finance                        → main page (role-aware)
#   POST /finance/confirm/<id>           → Admin: confirm transaction
#   GET  /finance/tenant/<id>/history    → AJAX: tenant payment timeline
#   GET  /finance/ledger/<id>            → AJAX: tenant detailed ledger
#   POST /finance/ledger/adjust          → Admin: manual ledger entry
#   GET  /finance/export/csv             → CSV download
#   GET  /finance/export/pdf             → PDF download
#   POST /finance/expenses/add           → Admin: add expense
#   POST /finance/expenses/<id>/edit     → Admin: edit expense
#   POST /finance/expenses/<id>/delete   → Admin: delete expense
#   GET  /finance/expenses/<id>/receipt  → download receipt file
#   POST /finance/payouts/request        → Landlord: request payout
#   POST /finance/payouts/<id>/complete  → Admin: approve payout
# ─────────────────────────────────────────────

import os
import csv
import io
import uuid
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    render_template, redirect, url_for,
    session, request, flash, jsonify,
    Response, abort, send_file,
)
from werkzeug.utils import secure_filename
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

from models import (
    db, User, Property, Unit, Tenancy,
    Invoice, Transaction,
    RentLedger, Expense, LandlordPayout,
)
from permissions import has_perm

# ── constants ──────────────────────────────────
EXPENSE_CATEGORIES = [
    "Maintenance", "Utilities", "Repairs", "Cleaning",
    "Insurance", "Management Fee", "Legal", "Other",
]
RECEIPT_ALLOWED = {"pdf", "jpg", "jpeg", "png"}


# ── pure helpers ───────────────────────────────
def _fmt(amount):
    return f"₦{amount:,.2f}"


def _next_exp_number():
    last = Expense.query.order_by(Expense.id.desc()).first()
    n = (last.id + 1) if last else 1
    return f"EXP-{n:04d}"


def _next_payout_number():
    last = LandlordPayout.query.order_by(LandlordPayout.id.desc()).first()
    n = (last.id + 1) if last else 1
    return f"PAY-{n:04d}"


def _running_balance(tenant_id):
    """Positive = credit/prepaid. Negative = owes money."""
    charged = sum(i.amount for i in Invoice.query.filter_by(tenant_id=tenant_id).all())
    paid    = sum(t.amount for t in Transaction.query.filter_by(
                  tenant_id=tenant_id, status="confirmed").all())
    adj_cr  = (db.session.query(db.func.sum(RentLedger.amount))
               .filter_by(tenant_id=tenant_id, entry_type="credit").scalar() or 0)
    adj_ch  = (db.session.query(db.func.sum(RentLedger.amount))
               .filter_by(tenant_id=tenant_id, entry_type="charge").scalar() or 0)
    return paid + adj_cr - charged - adj_ch


def _landlord_earnings(landlord_id):
    props    = Property.query.filter_by(landlord_id=landlord_id).all()
    prop_ids = [p.id for p in props]
    if not prop_ids:
        return dict(total_income=0, total_expenses=0, net=0,
                    pending_payouts=0, withdrawable=0)

    unit_ids = [u.id for p in props for u in p.units.all()]
    inv_ids  = [i.id for i in Invoice.query.filter(Invoice.unit_id.in_(unit_ids)).all()]

    total_income = (db.session.query(db.func.sum(Transaction.amount))
                   .filter(Transaction.invoice_id.in_(inv_ids),
                           Transaction.status == "confirmed").scalar() or 0)
    total_exp    = (db.session.query(db.func.sum(Expense.amount))
                   .filter(Expense.property_id.in_(prop_ids),
                           Expense.status == "paid").scalar() or 0)
    pending_p    = (db.session.query(db.func.sum(LandlordPayout.amount))
                   .filter_by(landlord_id=landlord_id, status="pending").scalar() or 0)
    net          = total_income - total_exp
    withdrawable = max(0.0, net - pending_p)

    return dict(total_income=total_income, total_expenses=total_exp,
                net=net, pending_payouts=pending_p, withdrawable=withdrawable)


def _cashflow_forecast(prop_ids=None, months=12):
    today = date.today()
    if prop_ids is not None:
        unit_ids  = [u.id for pid in prop_ids
                     for u in Unit.query.filter_by(property_id=pid).all()]
        tenancies = Tenancy.query.filter(
            Tenancy.unit_id.in_(unit_ids), Tenancy.is_active == True).all()
    else:
        tenancies = Tenancy.query.filter_by(is_active=True).all()

    result = []
    for i in range(months):
        yr  = today.year  + (today.month + i - 1) // 12
        mo  = (today.month + i - 1) % 12 + 1
        m_start = date(yr, mo, 1)
        amount  = sum(t.monthly_rent for t in tenancies
                      if t.end_date is None or t.end_date >= m_start)
        result.append({
            "month":  m_start.strftime("%Y-%m"),
            "label":  m_start.strftime("%b %Y"),
            "amount": amount,
        })
    return result


def _collection_perf(invoices):
    on_time = late = unpaid = 0
    for inv in invoices:
        if inv.status == "paid" and inv.transaction:
            paid_d = inv.transaction.date.date() if inv.transaction.date else None
            if paid_d and paid_d <= inv.due_date:
                on_time += 1
            else:
                late += 1
        elif inv.status in ("pending", "overdue"):
            unpaid += 1
    return {"on_time": on_time, "late": late, "unpaid": unpaid}


# ══════════════════════════════════════════════
#  REGISTRATION
# ══════════════════════════════════════════════
def register_finance_routes(app):
    RECEIPTS_FOLDER = os.path.join(os.path.dirname(__file__), "uploads", "receipts")
    os.makedirs(RECEIPTS_FOLDER, exist_ok=True)
    app.config["RECEIPTS_FOLDER"] = RECEIPTS_FOLDER

    # local auth decorators (mirror app.py)
    def login_required(f):
        @wraps(f)
        def d(*a, **kw):
            if "user_id" not in session:
                flash("Please sign in to continue.", "info")
                return redirect(url_for("login"))
            return f(*a, **kw)
        return d

    def role_required(*roles):
        def decorator(f):
            @wraps(f)
            def d(*a, **kw):
                if "user_id" not in session:
                    flash("Please sign in to continue.", "info")
                    return redirect(url_for("login"))
                if session.get("role") not in roles:
                    flash("Access denied.", "danger")
                    return redirect(url_for("dashboard"))
                return f(*a, **kw)
            return d
        return decorator

    # ────────────────────────────────────────────
    # MAIN FINANCE PAGE
    # ────────────────────────────────────────────
    @app.route("/finance")
    @login_required
    def finance():
        role    = session.get("role")
        user_id = session.get("user_id")
        today   = date.today()

        # Department gate (staff): Management/Finance see the full page.
        # Maintenance sees expense costs only. Customer Service has no
        # financial-amounts access (payment status is visible on Invoices).
        if role == "Admin" and not has_perm("view_finance_full"):
            if has_perm("view_finance_costs"):
                expenses   = Expense.query.order_by(Expense.expense_date.desc()).all()
                total_all  = sum(e.amount or 0 for e in expenses)
                total_paid = sum(e.amount or 0 for e in expenses if e.status == "paid")
                return render_template("expenses_view.html",
                    role=role,
                    expenses=expenses,
                    expense_categories=EXPENSE_CATEGORIES,
                    total_all=total_all,
                    total_paid=total_paid,
                    total_unpaid=total_all - total_paid,
                    all_properties=Property.query.order_by(Property.name).all(),
                    today_str=today.strftime("%Y-%m-%d"),
                    fmt=_fmt)
            flash("Access denied. Your department cannot view financial records.", "danger")
            return redirect(url_for("dashboard"))

        # ── TENANT VIEW ──────────────────────────────────────────
        if role == "Tenant":
            tenancy  = Tenancy.query.filter_by(tenant_id=user_id, is_active=True).first()
            invoices = (Invoice.query.filter_by(tenant_id=user_id)
                        .order_by(Invoice.due_date.desc()).all())
            txns     = (Transaction.query.filter_by(tenant_id=user_id)
                        .order_by(Transaction.date.desc()).all())

            # auto-flip overdue
            for inv in invoices:
                if inv.status == "pending" and inv.due_date < today:
                    inv.status = "overdue"
            db.session.commit()

            total_charged = sum(i.amount for i in invoices)
            total_paid    = sum(t.amount for t in txns if t.status == "confirmed")
            adj_cr = (db.session.query(db.func.sum(RentLedger.amount))
                      .filter_by(tenant_id=user_id, entry_type="credit").scalar() or 0)
            adj_ch = (db.session.query(db.func.sum(RentLedger.amount))
                      .filter_by(tenant_id=user_id, entry_type="charge").scalar() or 0)
            balance     = total_paid + adj_cr - total_charged - adj_ch
            outstanding = max(0, -balance)
            prepaid     = max(0,  balance)

            out_invoices = [i for i in invoices if i.status in ("pending", "overdue")]
            my_expenses  = (Expense.query.filter_by(paid_by_tenant_id=user_id)
                            .order_by(Expense.expense_date.desc()).all())

            formatted_txns = [{
                "ref":     t.txn_number,
                "invoice": t.invoice.inv_number if t.invoice else "N/A",
                "amount":  t.amount,
                "date":    t.date.strftime("%d %b %Y") if t.date else "",
                "method":  t.method,
                "status":  t.status,
            } for t in txns]

            return render_template("finance.html",
                role=role,
                tenancy=tenancy,
                invoices=invoices,
                transactions=formatted_txns,
                total_charged=total_charged,
                total_paid=total_paid,
                balance=balance,
                outstanding=outstanding,
                prepaid=prepaid,
                out_invoices=out_invoices,
                my_expenses=my_expenses,
                fmt=_fmt,
            )

        # ── ADMIN / LANDLORD VIEW ─────────────────────────────────
        if role == "Admin":
            props_q    = Property.query
            all_invs   = Invoice.query.all()
            all_txns   = Transaction.query.order_by(Transaction.date.desc()).all()
            expenses   = Expense.query.order_by(Expense.expense_date.desc()).all()
            landlord_property_count = props_q.count()

            landlords = User.query.filter_by(role="Landlord", is_active=True).all()
            ll_earnings = []
            for ll in landlords:
                e = _landlord_earnings(ll.id)
                ll_earnings.append({"name": ll.name, "email": ll.email, "id": ll.id, **e})

            forecast     = _cashflow_forecast(prop_ids=None, months=12)
            all_props    = props_q.all()
            all_tenants  = User.query.filter_by(role="Tenant", is_active=True).all()

            active_tenancies = Tenancy.query.filter_by(is_active=True).all()
            all_pending_payouts = LandlordPayout.query.filter_by(status="pending").all()

        else:  # Landlord
            props_q  = Property.query.filter_by(landlord_id=user_id)
            props    = props_q.all()
            prop_ids = [p.id for p in props]
            unit_ids = [u.id for p in props for u in p.units.all()]
            landlord_property_count = len(prop_ids)

            inv_q    = Invoice.query.filter(Invoice.unit_id.in_(unit_ids)) if unit_ids else Invoice.query.filter(False)
            all_invs = inv_q.all()
            inv_ids_l = [i.id for i in all_invs]

            all_txns = (Transaction.query
                        .filter(Transaction.invoice_id.in_(inv_ids_l))
                        .order_by(Transaction.date.desc()).all()) if inv_ids_l else []

            expenses = (Expense.query.filter(Expense.property_id.in_(prop_ids))
                        .order_by(Expense.expense_date.desc()).all()) if prop_ids else []

            own_e = _landlord_earnings(user_id)
            ll_earnings = [{"name": "My Properties", "email": session.get("email", ""),
                             "id": user_id, **own_e}]

            forecast     = _cashflow_forecast(prop_ids=prop_ids, months=12)
            pending_txns = []
            all_props    = props
            all_tenants  = []

            unit_ids_all = [u.id for p in props for u in p.units.all()]
            active_tenancies = (Tenancy.query.filter(
                Tenancy.unit_id.in_(unit_ids_all), Tenancy.is_active == True).all()
                if unit_ids_all else [])
            all_pending_payouts = (LandlordPayout.query
                                   .filter_by(landlord_id=user_id, status="pending").all())

        # ── KPI cards ─────────────────────────────────────────────
        first_this  = today.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        first_last  = last_month_end.replace(day=1)

        total_rev    = sum(t.amount for t in all_txns if t.status == "confirmed")
        pending_amt  = sum(t.amount for t in all_txns if t.status == "pending")
        total_exp_p  = sum(e.amount for e in expenses if e.status == "paid")
        net_profit   = total_rev - total_exp_p

        rev_this = sum(t.amount for t in all_txns if t.status == "confirmed"
                       and t.date and t.date.date() >= first_this)
        rev_last = sum(t.amount for t in all_txns if t.status == "confirmed"
                       and t.date and first_last <= t.date.date() <= last_month_end)
        rev_delta = rev_this - rev_last
        delta_str = f"₦{abs(rev_delta/1000):.1f}k" if rev_delta else ""

        kpi_cards = [
            ("Total Revenue",        _fmt(total_rev),   "graph-up-arrow",        "cr", delta_str, rev_delta >= 0),
            ("Confirmed Payments",   _fmt(total_rev),   "check-circle",          "gr", "",        True),
            ("Pending Confirmation", _fmt(pending_amt), "hourglass-split",       "wn", "",        True),
            ("Net Profit",           _fmt(net_profit),  "arrow-up-right-circle", "nv", "",        True),
        ]

        # ── 6-month chart data ────────────────────────────────────
        chart_months = {}
        for i in range(5, -1, -1):
            yr = today.year  + (today.month - i - 1) // 12
            mo = (today.month - i - 1) % 12 + 1
            lbl = date(yr, mo, 1).strftime("%b")
            chart_months[lbl] = {"month": lbl, "rev": 0.0, "exp": 0.0}

        for t in all_txns:
            if t.status == "confirmed" and t.date:
                m = t.date.strftime("%b")
                if m in chart_months:
                    chart_months[m]["rev"] += t.amount

        for e in expenses:
            if e.status == "paid" and e.expense_date:
                m = e.expense_date.strftime("%b")
                if m in chart_months:
                    chart_months[m]["exp"] += e.amount

        rev_data = list(chart_months.values())

        # ── Invoice status donut ──────────────────────────────────
        status_counts = {"total": len(all_invs), "paid": 0, "pending": 0, "overdue": 0}
        for inv in all_invs:
            if inv.status in status_counts:
                status_counts[inv.status] += 1
        donut_data = [status_counts["paid"], status_counts["pending"], status_counts["overdue"]]

        # ── Aging report ──────────────────────────────────────────
        aging_summary = {"current": 0, "d30": 0, "d60": 0, "d90": 0, "over90": 0}
        aging_rows = []
        for inv in all_invs:
            if inv.status == "paid":
                continue
            days = (today - inv.due_date).days
            if days <= 0:
                bucket = "current"; aging_summary["current"] += 1; days = 0
            elif days <= 30:
                bucket = "1-30";  aging_summary["d30"] += 1
            elif days <= 60:
                bucket = "31-60"; aging_summary["d60"] += 1
            elif days <= 90:
                bucket = "61-90"; aging_summary["d90"] += 1
            else:
                bucket = "90+";   aging_summary["over90"] += 1
            u = inv.unit
            aging_rows.append({
                "tenant":       inv.tenant.name  if inv.tenant else "Unknown",
                "tenant_id":    inv.tenant_id,
                "email":        inv.tenant.email if inv.tenant else "",
                "property":     u.prop.name      if u and u.prop else "N/A",
                "unit":         u.unit_number    if u else "N/A",
                "inv_number":   inv.inv_number,
                "amount":       inv.amount,
                "due_date":     inv.due_date.strftime("%Y-%m-%d"),
                "days_overdue": days,
                "bucket":       bucket,
            })
        aging_rows.sort(key=lambda x: x["days_overdue"], reverse=True)

        # ── Collection rate by property ───────────────────────────
        all_props_list = props_q.all()
        coll_by_prop   = []
        hbar_data      = []
        tot_exp_all    = tot_col_all = 0

        for prop in all_props_list:
            pu_ids   = [u.id for u in prop.units.all()]
            p_invs   = [i for i in all_invs if i.unit_id in pu_ids]
            expected = sum(i.amount for i in p_invs)
            collected = sum(i.amount for i in p_invs if i.status == "paid")
            tot_exp_all += expected; tot_col_all += collected
            rate = int(collected / expected * 100) if expected else 0
            col_units = len(set(i.unit_id for i in p_invs if i.status == "paid"))
            coll_by_prop.append({
                "name": prop.name, "total_units": prop.total_units,
                "collected_units": col_units, "expected": expected,
                "collected": collected, "rate": rate,
            })
            hbar_data.append({"name": prop.name, "rate": rate})

        overall_rate = int(tot_col_all / tot_exp_all * 100) if tot_exp_all else 0
        margin       = int(net_profit  / total_rev    * 100) if total_rev    else 0

        # ── Formatted transactions ────────────────────────────────
        fmt_txns = [{
            "id":        t.id,
            "ref":       t.txn_number,
            "tenant":    t.tenant.name if t.tenant else "Unknown",
            "tenant_id": t.tenant_id,
            "invoice":   t.invoice.inv_number if t.invoice else "N/A",
            "amount":    t.amount,
            "date":      t.date.strftime("%Y-%m-%d") if t.date else "",
            "method":    t.method,
            "status":    t.status,
        } for t in all_txns]

        # Pending tab needs the same formatted shape (ref/tenant/tenant_id…),
        # not raw ORM objects
        pending_txns = [t for t in fmt_txns if t["status"] == "pending"]

        # ── Rent Ledger: per-tenant running balance ───────────────
        ledger_rows = []
        for ten in active_tenancies:
            tenant = ten.tenant
            unit   = ten.unit
            prop   = unit.prop if unit else None

            t_invs = Invoice.query.filter_by(tenant_id=tenant.id).all()
            t_txns = Transaction.query.filter_by(tenant_id=tenant.id, status="confirmed").all()
            t_cr   = (db.session.query(db.func.sum(RentLedger.amount))
                      .filter_by(tenant_id=tenant.id, entry_type="credit").scalar() or 0)
            t_ch   = (db.session.query(db.func.sum(RentLedger.amount))
                      .filter_by(tenant_id=tenant.id, entry_type="charge").scalar() or 0)

            total_ch = sum(i.amount for i in t_invs)
            total_pd = sum(tx.amount for tx in t_txns)
            bal      = total_pd + t_cr - total_ch - t_ch
            outstd   = max(0, -bal)

            ledger_rows.append({
                "tenant_id":    tenant.id,
                "tenant":       tenant.name,
                "email":        tenant.email,
                "unit":         unit.unit_number if unit else "—",
                "property":     prop.name if prop else "—",
                "monthly_rent": ten.monthly_rent,
                "total_charged": total_ch,
                "total_paid":   total_pd,
                "balance":      bal,
                "outstanding":  outstd,
                "bal_status":   "overpaid" if bal > 0 else ("clear" if bal == 0 else "outstanding"),
            })

        # ── Expense stats ─────────────────────────────────────────
        exp_by_cat   = {}
        for e in expenses:
            exp_by_cat[e.category] = exp_by_cat.get(e.category, 0) + e.amount

        total_exp_all  = sum(e.amount for e in expenses)
        total_exp_paid = sum(e.amount for e in expenses if e.status == "paid")
        total_exp_unp  = sum(e.amount for e in expenses if e.status == "unpaid")

        fmt_expenses = [{
            "id":           e.id,
            "exp_number":   e.exp_number,
            "property":     e.prop.name if e.prop else "—",
            "property_id":  e.property_id,
            "unit":         e.unit.unit_number if e.unit else "—",
            "unit_id":      e.unit_id,
            "category":     e.category,
            "description":  e.description,
            "amount":       e.amount,
            "vendor":       e.vendor or "—",
            "expense_date": e.expense_date.strftime("%Y-%m-%d") if e.expense_date else "",
            "status":       e.status,
            "paid_by":      e.paid_by or "—",
            "tenant_payer": e.paid_by_tenant.name if e.paid_by_tenant else None,
            "tenant_payer_id": e.paid_by_tenant_id,
            "has_receipt":  bool(e.receipt_filename),
            "notes":        e.notes or "",
        } for e in expenses]

        # ── Collection performance ────────────────────────────────
        perf = _collection_perf(all_invs)

        return render_template("finance.html",
            role=role,
            today_str=today.strftime("%Y-%m-%d"),
            landlord_property_count=landlord_property_count,
            kpi_cards=kpi_cards,
            rev_data=rev_data,
            donut_data=donut_data,
            hbar_data=hbar_data,
            invoice_status_counts=status_counts,
            aging_summary=aging_summary,
            aging_rows=aging_rows,
            transactions=fmt_txns,
            net=net_profit,
            total_rev=total_rev,
            total_exp=total_exp_p,
            margin=margin,
            overall_collection_rate=overall_rate,
            collection_by_property=coll_by_prop,
            # Finance module extras
            ledger_rows=ledger_rows,
            expenses=fmt_expenses,
            expense_categories=EXPENSE_CATEGORIES,
            exp_by_cat=exp_by_cat,
            total_exp_all=total_exp_all,
            total_exp_paid=total_exp_paid,
            total_exp_unp=total_exp_unp,
            ll_earnings=ll_earnings,
            forecast=forecast,
            perf=perf,
            pending_txns=pending_txns if role == "Admin" else [],
            pending_payouts=all_pending_payouts,
            all_properties=all_props,
            all_tenants=all_tenants,
            fmt=_fmt,
        )

    # ────────────────────────────────────────────
    # CONFIRM TRANSACTION (Admin)
    # ────────────────────────────────────────────
    @app.route("/finance/confirm/<int:txn_id>", methods=["POST"])
    @role_required("Admin")
    def confirm_txn(txn_id):
        if not has_perm("edit_payments"):
            flash("Access denied. Only Finance or Management can confirm payments.", "danger")
            return redirect(url_for("finance"))
        txn = Transaction.query.get_or_404(txn_id)
        txn.status = "confirmed"
        if txn.invoice:
            txn.invoice.status = "paid"
        db.session.commit()
        flash(f"Transaction {txn.txn_number} confirmed — invoice marked Paid.", "success")
        return redirect(url_for("finance"))

    # ────────────────────────────────────────────
    # TENANT PAYMENT HISTORY  (AJAX)
    # ────────────────────────────────────────────
    @app.route("/finance/tenant/<int:tenant_id>/history")
    @login_required
    def tenant_history(tenant_id):
        role    = session.get("role")
        user_id = session.get("user_id")

        # Payment amounts: Management/Finance only among staff
        if role == "Admin" and not has_perm("view_finance_full"):
            return jsonify({"error": "Unauthorized"}), 403

        if role == "Landlord":
            props    = Property.query.filter_by(landlord_id=user_id).all()
            unit_ids = [u.id for p in props for u in p.units.all()]
            ok       = Tenancy.query.filter_by(tenant_id=tenant_id).filter(
                Tenancy.unit_id.in_(unit_ids)).first()
            if not ok:
                return jsonify({"error": "Unauthorized"}), 403
        elif role == "Tenant" and user_id != tenant_id:
            return jsonify({"error": "Unauthorized"}), 403

        txns       = (Transaction.query.filter_by(tenant_id=tenant_id)
                      .order_by(Transaction.date.desc()).all())
        total_paid = sum(t.amount for t in txns if t.status == "confirmed")

        return jsonify({
            "stats": {
                "total_paid":   _fmt(total_paid),
                "count":        len(txns),
                "last_payment": txns[0].date.strftime("%b %d, %Y") if txns and txns[0].date else "Never",
            },
            "transactions": [{
                "ref":     t.txn_number,
                "invoice": t.invoice.inv_number if t.invoice else "N/A",
                "method":  t.method,
                "amount":  _fmt(t.amount),
                "date":    t.date.strftime("%d %b %Y") if t.date else "",
                "status":  t.status,
            } for t in txns],
        })

    # ────────────────────────────────────────────
    # TENANT LEDGER DETAIL  (AJAX)
    # ────────────────────────────────────────────
    @app.route("/finance/ledger/<int:tenant_id>")
    @role_required("Admin", "Landlord")
    def finance_ledger_detail(tenant_id):
        role    = session.get("role")
        user_id = session.get("user_id")

        if role == "Admin" and not has_perm("view_finance_full"):
            return jsonify({"error": "Unauthorized"}), 403

        if role == "Landlord":
            props    = Property.query.filter_by(landlord_id=user_id).all()
            unit_ids = [u.id for p in props for u in p.units.all()]
            ok       = Tenancy.query.filter_by(tenant_id=tenant_id).filter(
                Tenancy.unit_id.in_(unit_ids)).first()
            if not ok:
                return jsonify({"error": "Unauthorized"}), 403

        tenant   = User.query.get_or_404(tenant_id)
        invoices = (Invoice.query.filter_by(tenant_id=tenant_id)
                    .order_by(Invoice.due_date.asc()).all())
        txns     = (Transaction.query.filter_by(tenant_id=tenant_id)
                    .order_by(Transaction.date.asc()).all())
        adj      = (RentLedger.query.filter_by(tenant_id=tenant_id)
                    .order_by(RentLedger.entry_date.asc()).all())

        # Build chronological ledger with running balance
        entries = []
        for inv in invoices:
            entries.append({
                "date":        inv.due_date.isoformat(),
                "type":        "charge",
                "description": f"{inv.type} — {inv.inv_number}",
                "debit":       inv.amount,
                "credit":      0,
            })
        for t in txns:
            entries.append({
                "date":        t.date.date().isoformat() if t.date else "",
                "type":        "payment",
                "description": f"Payment — {t.txn_number} ({t.method})",
                "debit":       0,
                "credit":      t.amount,
            })
        for a in adj:
            entries.append({
                "date":        a.entry_date.isoformat(),
                "type":        a.entry_type,
                "description": a.description or f"Adjustment — {a.reference or ''}",
                "debit":       a.amount if a.entry_type == "charge" else 0,
                "credit":      a.amount if a.entry_type == "credit" else 0,
            })

        entries.sort(key=lambda x: x["date"])
        running = 0.0
        for e in entries:
            running += e["credit"] - e["debit"]
            e["balance"]  = running
            e["debit"]    = _fmt(e["debit"])   if e["debit"]  else ""
            e["credit"]   = _fmt(e["credit"])  if e["credit"] else ""
            e["balance_f"] = _fmt(abs(running))
            e["bal_sign"]  = "+" if running >= 0 else "-"

        return jsonify({
            "tenant": tenant.name,
            "balance": _fmt(abs(running)),
            "bal_sign": "+" if running >= 0 else "-",
            "entries": entries,
        })

    # ────────────────────────────────────────────
    # MANUAL LEDGER ADJUSTMENT  (Admin)
    # ────────────────────────────────────────────
    @app.route("/finance/ledger/adjust", methods=["POST"])
    @role_required("Admin")
    def finance_ledger_adjust():
        if not has_perm("edit_payments"):
            flash("Access denied. Only Finance or Management can adjust the ledger.", "danger")
            return redirect(url_for("finance"))
        tenant_id  = request.form.get("tenant_id", type=int)
        entry_type = request.form.get("entry_type")   # charge | credit
        amount     = request.form.get("amount",     type=float)
        description = request.form.get("description", "").strip()
        entry_date = request.form.get("entry_date")

        if not all([tenant_id, entry_type, amount, entry_date]):
            flash("All fields required for ledger adjustment.", "danger")
            return redirect(url_for("finance") + "#ledger")

        tenant = User.query.get_or_404(tenant_id)
        t      = Tenancy.query.filter_by(tenant_id=tenant_id, is_active=True).first()
        if not t:
            flash("Tenant has no active tenancy.", "danger")
            return redirect(url_for("finance") + "#ledger")

        last_adj = (RentLedger.query.order_by(RentLedger.id.desc()).first())
        ref_num  = f"ADJ-{(last_adj.id + 1) if last_adj else 1:04d}"

        entry = RentLedger(
            tenant_id   = tenant_id,
            unit_id     = t.unit_id,
            property_id = t.unit.property_id,
            entry_type  = entry_type,
            amount      = amount,
            description = description,
            reference   = ref_num,
            entry_date  = date.fromisoformat(entry_date),
            created_by_id = session.get("user_id"),
        )
        db.session.add(entry)
        db.session.commit()
        flash(f"Ledger adjustment {ref_num} added for {tenant.name}.", "success")
        return redirect(url_for("finance") + "#ledger")

    # ────────────────────────────────────────────
    # EXPENSE — ADD  (Admin)
    # ────────────────────────────────────────────
    @app.route("/finance/expenses/add", methods=["POST"])
    @role_required("Admin")
    def finance_expense_add():
        if not has_perm("edit_expenses"):
            flash("Access denied. Your department cannot record expenses.", "danger")
            return redirect(url_for("finance"))
        property_id   = request.form.get("property_id",  type=int)
        unit_id       = request.form.get("unit_id",      type=int) or None
        category      = request.form.get("category",     "Other")
        description   = request.form.get("description",  "").strip()
        amount        = request.form.get("amount",        type=float)
        vendor        = request.form.get("vendor",        "").strip()
        expense_date  = request.form.get("expense_date")
        status        = request.form.get("status",        "unpaid")
        paid_by       = request.form.get("paid_by",       "")
        paid_by_tid   = request.form.get("paid_by_tenant_id", type=int) or None
        notes         = request.form.get("notes",         "").strip()

        if not all([property_id, description, amount, expense_date]):
            flash("Property, description, amount and date are required.", "danger")
            return redirect(url_for("finance") + "#expenses")

        receipt_fn = None
        f = request.files.get("receipt")
        if f and f.filename:
            ext = f.filename.rsplit(".", 1)[-1].lower()
            if ext in RECEIPT_ALLOWED:
                receipt_fn = f"{uuid.uuid4().hex}.{ext}"
                f.save(os.path.join(app.config["RECEIPTS_FOLDER"], receipt_fn))
            else:
                flash("Receipt must be PDF, JPG, or PNG.", "danger")
                return redirect(url_for("finance") + "#expenses")

        exp = Expense(
            exp_number        = _next_exp_number(),
            property_id       = property_id,
            unit_id           = unit_id,
            category          = category,
            description       = description,
            amount            = amount,
            vendor            = vendor or None,
            expense_date      = date.fromisoformat(expense_date),
            status            = status,
            paid_by           = paid_by or None,
            paid_by_tenant_id = paid_by_tid,
            receipt_filename  = receipt_fn,
            notes             = notes or None,
            created_by_id     = session.get("user_id"),
        )
        db.session.add(exp)
        db.session.commit()
        flash(f"Expense {exp.exp_number} added successfully.", "success")
        return redirect(url_for("finance") + "#expenses")

    # ────────────────────────────────────────────
    # EXPENSE — EDIT  (Admin)
    # ────────────────────────────────────────────
    @app.route("/finance/expenses/<int:exp_id>/edit", methods=["POST"])
    @role_required("Admin")
    def finance_expense_edit(exp_id):
        if not has_perm("edit_expenses"):
            flash("Access denied. Your department cannot edit expenses.", "danger")
            return redirect(url_for("finance"))
        exp = Expense.query.get_or_404(exp_id)

        exp.property_id  = request.form.get("property_id",  type=int) or exp.property_id
        exp.unit_id      = request.form.get("unit_id",      type=int) or None
        exp.category     = request.form.get("category",     exp.category)
        exp.description  = request.form.get("description",  exp.description).strip()
        exp.amount       = request.form.get("amount",        type=float) or exp.amount
        exp.vendor       = request.form.get("vendor",        "").strip() or None
        exp.expense_date = date.fromisoformat(request.form.get("expense_date",
                           exp.expense_date.isoformat()))
        exp.status       = request.form.get("status",   exp.status)
        exp.paid_by      = request.form.get("paid_by",  "") or None
        exp.paid_by_tenant_id = request.form.get("paid_by_tenant_id", type=int) or None
        exp.notes        = request.form.get("notes", "").strip() or None

        f = request.files.get("receipt")
        if f and f.filename:
            ext = f.filename.rsplit(".", 1)[-1].lower()
            if ext in RECEIPT_ALLOWED:
                if exp.receipt_filename:
                    old = os.path.join(app.config["RECEIPTS_FOLDER"], exp.receipt_filename)
                    if os.path.exists(old):
                        os.remove(old)
                receipt_fn = f"{uuid.uuid4().hex}.{ext}"
                f.save(os.path.join(app.config["RECEIPTS_FOLDER"], receipt_fn))
                exp.receipt_filename = receipt_fn

        db.session.commit()
        flash(f"Expense {exp.exp_number} updated.", "success")
        return redirect(url_for("finance") + "#expenses")

    # ────────────────────────────────────────────
    # EXPENSE — DELETE  (Admin)
    # ────────────────────────────────────────────
    @app.route("/finance/expenses/<int:exp_id>/delete", methods=["POST"])
    @role_required("Admin")
    def finance_expense_delete(exp_id):
        # Deleting financial records: Finance + Management only
        if not has_perm("edit_payments"):
            flash("Access denied. Only Finance or Management can delete expense records.", "danger")
            return redirect(url_for("finance"))
        exp = Expense.query.get_or_404(exp_id)
        if exp.receipt_filename:
            path = os.path.join(app.config["RECEIPTS_FOLDER"], exp.receipt_filename)
            if os.path.exists(path):
                os.remove(path)
        num = exp.exp_number
        db.session.delete(exp)
        db.session.commit()
        flash(f"Expense {num} deleted.", "success")
        return redirect(url_for("finance") + "#expenses")

    # ────────────────────────────────────────────
    # RECEIPT DOWNLOAD
    # ────────────────────────────────────────────
    @app.route("/finance/expenses/<int:exp_id>/receipt")
    @login_required
    def finance_expense_receipt(exp_id):
        exp = Expense.query.get_or_404(exp_id)
        role    = session.get("role")
        user_id = session.get("user_id")

        if role == "Admin" and not (has_perm("view_finance_full") or has_perm("view_finance_costs")):
            abort(403)

        if role == "Tenant":
            if exp.paid_by_tenant_id != user_id:
                abort(403)
        elif role == "Landlord":
            props = Property.query.filter_by(landlord_id=user_id).all()
            if exp.property_id not in [p.id for p in props]:
                abort(403)

        if not exp.receipt_filename:
            abort(404)
        path = os.path.join(app.config["RECEIPTS_FOLDER"], exp.receipt_filename)
        if not os.path.exists(path):
            abort(404)
        return send_file(path, as_attachment=True,
                         download_name=f"receipt_{exp.exp_number}{os.path.splitext(exp.receipt_filename)[1]}")

    # ────────────────────────────────────────────
    # PAYOUT — REQUEST  (Landlord)
    # ────────────────────────────────────────────
    @app.route("/finance/payouts/request", methods=["POST"])
    @role_required("Landlord")
    def finance_payout_request():
        user_id = session.get("user_id")
        amount  = request.form.get("amount", type=float)
        notes   = request.form.get("notes", "").strip()

        if not amount or amount <= 0:
            flash("Enter a valid withdrawal amount.", "danger")
            return redirect(url_for("finance") + "#earnings")

        earnings = _landlord_earnings(user_id)
        if amount > earnings["withdrawable"]:
            flash(f"Amount exceeds withdrawable balance ({_fmt(earnings['withdrawable'])}).", "danger")
            return redirect(url_for("finance") + "#earnings")

        po = LandlordPayout(
            payout_number = _next_payout_number(),
            landlord_id   = user_id,
            amount        = amount,
            status        = "pending",
            notes         = notes or None,
        )
        db.session.add(po)
        db.session.commit()
        flash(f"Payout request {po.payout_number} submitted for {_fmt(amount)}.", "success")
        return redirect(url_for("finance") + "#earnings")

    # ────────────────────────────────────────────
    # PAYOUT — COMPLETE / REJECT  (Admin)
    # ────────────────────────────────────────────
    @app.route("/finance/payouts/<int:payout_id>/complete", methods=["POST"])
    @role_required("Admin")
    def finance_payout_complete(payout_id):
        if not has_perm("manage_payouts"):
            flash("Access denied. Only Finance or Management can process payouts.", "danger")
            return redirect(url_for("finance"))
        action = request.form.get("action", "complete")
        po     = LandlordPayout.query.get_or_404(payout_id)
        po.status       = "completed" if action == "complete" else "rejected"
        po.completed_at = datetime.utcnow()
        db.session.commit()
        flash(f"Payout {po.payout_number} {po.status}.", "success")
        return redirect(url_for("finance") + "#earnings")

    # ────────────────────────────────────────────
    # EXPORT — CSV
    # ────────────────────────────────────────────
    @app.route("/finance/export/csv")
    @login_required
    def finance_export_csv():
        role    = session.get("role")
        user_id = session.get("user_id")

        if role == "Tenant":
            abort(403)
        if role == "Admin" and not has_perm("export_financials"):
            abort(403)

        if role == "Admin":
            txns = Transaction.query.order_by(Transaction.date.desc()).all()
        else:
            props    = Property.query.filter_by(landlord_id=user_id).all()
            unit_ids = [u.id for p in props for u in p.units.all()]
            inv_ids  = [i.id for i in Invoice.query.filter(Invoice.unit_id.in_(unit_ids)).all()]
            txns     = (Transaction.query.filter(Transaction.invoice_id.in_(inv_ids))
                        .order_by(Transaction.date.desc()).all())

        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(["Reference", "Tenant", "Invoice", "Amount", "Date", "Method", "Status"])
        for t in txns:
            cw.writerow([
                t.txn_number,
                t.tenant.name if t.tenant else "Unknown",
                t.invoice.inv_number if t.invoice else "N/A",
                t.amount,
                t.date.strftime("%Y-%m-%d %H:%M") if t.date else "",
                t.method,
                t.status,
            ])

        return Response(si.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": f"attachment;filename=transactions_{date.today()}.csv"})

    # ────────────────────────────────────────────
    # EXPORT — PDF
    # ────────────────────────────────────────────
    @app.route("/finance/export/pdf")
    @login_required
    def finance_export_pdf():
        role    = session.get("role")
        user_id = session.get("user_id")

        if role == "Tenant":
            abort(403)
        if role == "Admin" and not has_perm("export_financials"):
            abort(403)

        if role == "Admin":
            txns = Transaction.query.order_by(Transaction.date.desc()).limit(100).all()
        else:
            props    = Property.query.filter_by(landlord_id=user_id).all()
            unit_ids = [u.id for p in props for u in p.units.all()]
            inv_ids  = [i.id for i in Invoice.query.filter(Invoice.unit_id.in_(unit_ids)).all()]
            txns     = (Transaction.query.filter(Transaction.invoice_id.in_(inv_ids))
                        .order_by(Transaction.date.desc()).limit(100).all())

        buf    = io.BytesIO()
        doc    = SimpleDocTemplate(buf, pagesize=letter)
        styles = getSampleStyleSheet()
        elems  = [
            Paragraph("Financial Report & Transactions", styles["Title"]),
            Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]),
            Spacer(1, 20),
        ]

        data = [["Reference", "Tenant", "Invoice", "Amount", "Date", "Status"]]
        for t in txns:
            data.append([
                t.txn_number,
                (t.tenant.name[:15] if t.tenant else "N/A"),
                t.invoice.inv_number if t.invoice else "N/A",
                _fmt(t.amount),
                t.date.strftime("%Y-%m-%d") if t.date else "",
                t.status.capitalize(),
            ])

        tbl = Table(data)
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#0D3A6B")),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 12),
            ("BACKGROUND",    (0, 1), (-1, -1), colors.HexColor("#F8FAFC")),
            ("GRID",          (0, 0), (-1, -1), 1, colors.HexColor("#E2E8F0")),
        ]))
        elems.append(tbl)
        doc.build(elems)
        buf.seek(0)

        return Response(buf.getvalue(), mimetype="application/pdf",
            headers={"Content-Disposition": f"attachment;filename=financial_report_{date.today()}.pdf"})
