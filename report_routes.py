"""
HS Property — Reports module
Provides /reports (display) and /reports/export/csv (download) endpoints.
"""
from flask import session, request, render_template, redirect, url_for, Response, flash
from functools import wraps
from models import (db, User, Property, Unit, Tenancy, Invoice, Transaction,
                    RentLedger, Expense, LandlordPayout, BankTransferClaim)
from permissions import has_perm, current_dept
import csv
import io
from datetime import datetime, date, time as _time

ALL_REPORT_TYPES = ['summary', 'rent_ledger', 'invoices', 'payments', 'expenses',
                    'occupancy', 'arrears', 'payouts', 'bank_transfers']

# ── Report access matrix ──────────────────────────────────────────────
#  Every role and every admin department has an explicit allowlist.
#  Queries are additionally scoped server-side: Tenants only ever see
#  their own records; Landlords only their own properties' records.
REPORT_ACCESS = {
    # Admin departments
    'Management':       list(ALL_REPORT_TYPES),
    'Finance':          ['summary', 'rent_ledger', 'invoices', 'payments',
                         'expenses', 'arrears', 'payouts', 'bank_transfers'],
    'Maintenance':      ['expenses'],           # maintenance costs only
    'Customer Service': ['occupancy'],          # tenant/landlord data, no amounts
    # Non-staff roles
    'Landlord':         list(ALL_REPORT_TYPES),                    # scoped to own properties
    'Tenant':           ['summary', 'rent_ledger', 'invoices',     # scoped to own records
                         'payments', 'arrears', 'bank_transfers'],
}


def register_report_routes(app):

    def _login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            return f(*args, **kwargs)
        return decorated

    def _allowed_report_types(role):
        """Explicit allowlist per role / admin department (REPORT_ACCESS)."""
        if role == 'Admin':
            return list(REPORT_ACCESS.get(current_dept() or 'Management', []))
        return list(REPORT_ACCESS.get(role, []))

    def _parse_date(s):
        try:
            return datetime.strptime(s, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None

    def _dt_start(d):
        return datetime.combine(d, _time.min)

    def _dt_end(d):
        return datetime.combine(d, _time.max)

    def _landlord_pids(uid):
        return [p.id for p in Property.query.filter_by(landlord_id=uid).all()]

    def _amt(v):
        return f'{v:,.2f}' if v else '0.00'

    # ── shared query builders ──────────────────────────────────────────────
    def _inv_q(role, uid, lp_ids, tid, pid, fstatus, dt_from, dt_to):
        q = Invoice.query
        if role == 'Tenant':
            q = q.filter(Invoice.tenant_id == uid)
        if role == 'Landlord' or pid:
            q = q.join(Unit, Unit.id == Invoice.unit_id)
            if role == 'Landlord':
                q = q.filter(Unit.property_id.in_(lp_ids))
            if pid:
                q = q.filter(Unit.property_id == pid)
        if tid:
            q = q.filter(Invoice.tenant_id == tid)
        if fstatus:
            q = q.filter(Invoice.status == fstatus)
        if dt_from:
            q = q.filter(Invoice.created_at >= _dt_start(dt_from))
        if dt_to:
            q = q.filter(Invoice.created_at <= _dt_end(dt_to))
        return q

    def _txn_q(role, uid, lp_ids, tid, pid, fstatus, dt_from, dt_to):
        q = Transaction.query
        if role == 'Tenant':
            q = q.filter(Transaction.tenant_id == uid)
        if role == 'Landlord' or pid:
            q = (q.join(Invoice, Invoice.id == Transaction.invoice_id)
                  .join(Unit, Unit.id == Invoice.unit_id))
            if role == 'Landlord':
                q = q.filter(Unit.property_id.in_(lp_ids))
            if pid:
                q = q.filter(Unit.property_id == pid)
        if tid:
            q = q.filter(Transaction.tenant_id == tid)
        if fstatus:
            q = q.filter(Transaction.status == fstatus)
        if dt_from:
            q = q.filter(Transaction.date >= dt_from)
        if dt_to:
            q = q.filter(Transaction.date <= dt_to)
        return q

    def _led_q(role, uid, lp_ids, tid, pid, fstatus, dt_from, dt_to):
        q = RentLedger.query
        if role == 'Tenant':
            q = q.filter(RentLedger.tenant_id == uid)
        elif role == 'Landlord':
            q = q.filter(RentLedger.property_id.in_(lp_ids))
        if tid:
            q = q.filter(RentLedger.tenant_id == tid)
        if pid:
            q = q.filter(RentLedger.property_id == pid)
        if fstatus:
            q = q.filter(RentLedger.entry_type == fstatus)
        if dt_from:
            q = q.filter(RentLedger.entry_date >= dt_from)
        if dt_to:
            q = q.filter(RentLedger.entry_date <= dt_to)
        return q

    def _exp_q(role, uid, lp_ids, pid, fstatus, dt_from, dt_to):
        if role == 'Tenant':
            return Expense.query.filter(False)
        q = Expense.query
        if role == 'Landlord':
            q = q.filter(Expense.property_id.in_(lp_ids))
        if pid:
            q = q.filter(Expense.property_id == pid)
        if fstatus:
            q = q.filter(Expense.status == fstatus)
        if dt_from:
            q = q.filter(Expense.expense_date >= dt_from)
        if dt_to:
            q = q.filter(Expense.expense_date <= dt_to)
        return q

    # ── /reports ──────────────────────────────────────────────────────────
    @app.route('/reports')
    @_login_required
    def reports():
        role = session.get('role')
        uid  = session.get('user_id')

        allowed_types = _allowed_report_types(role)
        if not allowed_types:
            flash("Access denied. Your department does not have reporting access.", "danger")
            return redirect(url_for('dashboard'))

        rtype     = request.args.get('type', 'summary')
        if rtype not in allowed_types:
            rtype = allowed_types[0]
        date_from = request.args.get('date_from', '')
        date_to   = request.args.get('date_to', '')
        tenant_id = request.args.get('tenant_id', '')
        prop_id   = request.args.get('property_id', '')
        fstatus   = request.args.get('status', '')

        dt_from = _parse_date(date_from)
        dt_to   = _parse_date(date_to)
        tid  = int(tenant_id) if tenant_id else None
        pid  = int(prop_id)   if prop_id   else None
        lp_ids = _landlord_pids(uid) if role == 'Landlord' else []

        # filter dropdown data
        if role == 'Admin':
            all_tenants    = User.query.filter_by(role='Tenant').order_by(User.name).all()
            all_properties = Property.query.order_by(Property.name).all()
        elif role == 'Landlord':
            all_tenants = (User.query
                           .join(Tenancy, Tenancy.tenant_id == User.id)
                           .join(Unit, Unit.id == Tenancy.unit_id)
                           .filter(Unit.property_id.in_(lp_ids))
                           .distinct().order_by(User.name).all())
            all_properties = Property.query.filter(Property.id.in_(lp_ids)).order_by(Property.name).all()
        else:
            all_tenants = []
            all_properties = []

        rows   = []
        totals = {}
        kpis   = {}

        if rtype == 'summary':
            invs = _inv_q(role, uid, lp_ids, tid, pid, '', dt_from, dt_to).all()
            exps = _exp_q(role, uid, lp_ids, pid, '', dt_from, dt_to).all()

            paid_amt    = sum(i.amount for i in invs if i.status == 'paid')
            expense_amt = sum(e.amount for e in exps)

            if role == 'Admin':
                active_t   = User.query.filter_by(role='Tenant', is_active=True).count()
                total_u    = Unit.query.count()
                occupied_u = Unit.query.filter_by(is_occupied=True).count()
                total_props = Property.query.count()
            elif role == 'Landlord':
                active_t = (User.query
                            .join(Tenancy, Tenancy.tenant_id == User.id)
                            .join(Unit, Unit.id == Tenancy.unit_id)
                            .filter(Unit.property_id.in_(lp_ids), Tenancy.is_active == True)
                            .distinct().count())
                total_u    = Unit.query.filter(Unit.property_id.in_(lp_ids)).count()
                occupied_u = Unit.query.filter(Unit.property_id.in_(lp_ids), Unit.is_occupied == True).count()
                total_props = len(lp_ids)
            else:
                active_t = total_u = occupied_u = total_props = 0

            kpis = {
                'total_invoiced':  sum(i.amount for i in invs),
                'total_paid':      paid_amt,
                'total_pending':   sum(i.amount for i in invs if i.status == 'pending'),
                'total_overdue':   sum(i.amount for i in invs if i.status == 'overdue'),
                'total_expenses':  expense_amt,
                'net_income':      paid_amt - expense_amt,
                'active_tenants':  active_t,
                'total_units':     total_u,
                'occupied_units':  occupied_u,
                'occupancy_pct':   round(100 * occupied_u / total_u, 1) if total_u else 0,
                'total_properties': total_props,
            }
            recent_q = Invoice.query
            if role == 'Tenant':
                recent_q = recent_q.filter(Invoice.tenant_id == uid)
            elif role == 'Landlord':
                recent_q = (recent_q.join(Unit, Unit.id == Invoice.unit_id)
                                    .filter(Unit.property_id.in_(lp_ids)))
            rows = recent_q.order_by(Invoice.created_at.desc()).limit(20).all()

        elif rtype == 'rent_ledger':
            rows = _led_q(role, uid, lp_ids, tid, pid, fstatus, dt_from, dt_to).order_by(RentLedger.entry_date.desc()).all()
            charges = sum(r.amount for r in rows if r.entry_type == 'charge')
            credits = sum(r.amount for r in rows if r.entry_type == 'credit')
            totals  = {
                'charges':     charges,
                'credits':     credits,
                'adjustments': sum(r.amount for r in rows if r.entry_type == 'adjustment'),
                'net':         charges - credits,
                'count':       len(rows),
            }

        elif rtype == 'invoices':
            rows = _inv_q(role, uid, lp_ids, tid, pid, fstatus, dt_from, dt_to).order_by(Invoice.created_at.desc()).all()
            totals = {
                'total':   sum(r.amount for r in rows),
                'paid':    sum(r.amount for r in rows if r.status == 'paid'),
                'pending': sum(r.amount for r in rows if r.status == 'pending'),
                'overdue': sum(r.amount for r in rows if r.status == 'overdue'),
                'count':   len(rows),
            }

        elif rtype == 'payments':
            rows = _txn_q(role, uid, lp_ids, tid, pid, fstatus, dt_from, dt_to).order_by(Transaction.date.desc()).all()
            totals = {
                'total':         sum(r.amount for r in rows),
                'paystack':      sum(r.amount for r in rows if r.method == 'paystack'),
                'bank_transfer': sum(r.amount for r in rows if r.method == 'bank_transfer'),
                'other':         sum(r.amount for r in rows if r.method not in ('paystack', 'bank_transfer')),
                'count':         len(rows),
            }

        elif rtype == 'expenses':
            rows = _exp_q(role, uid, lp_ids, pid, fstatus, dt_from, dt_to).order_by(Expense.expense_date.desc()).all()
            cat_map = {}
            for e in rows:
                cat_map[e.category] = cat_map.get(e.category, 0) + e.amount
            totals = {
                'total':       sum(r.amount for r in rows),
                'paid':        sum(r.amount for r in rows if r.status == 'paid'),
                'unpaid':      sum(r.amount for r in rows if r.status == 'unpaid'),
                'by_category': sorted(cat_map.items(), key=lambda x: x[1], reverse=True),
                'count':       len(rows),
            }

        elif rtype == 'occupancy':
            if role == 'Admin':
                props = Property.query.order_by(Property.name).all()
            elif role == 'Landlord':
                props = Property.query.filter(Property.id.in_(lp_ids)).order_by(Property.name).all()
            else:
                props = []
            if pid:
                props = [p for p in props if p.id == pid]
            rows = props
            total_u = sum(p.total_units for p in props)
            totals  = {
                'total_units': total_u,
                'occupied':    sum(p.occupied_units for p in props),
                'vacant':      sum(p.vacant_units for p in props),
                'overall_pct': round(100 * sum(p.occupied_units for p in props) / total_u, 1) if total_u else 0,
                'count':       len(props),
            }

        elif rtype == 'arrears':
            inv_q_base = Invoice.query.filter(Invoice.status == 'overdue')
            if role == 'Tenant':
                inv_q_base = inv_q_base.filter(Invoice.tenant_id == uid)
            elif role == 'Landlord':
                inv_q_base = (inv_q_base.join(Unit, Unit.id == Invoice.unit_id)
                                        .filter(Unit.property_id.in_(lp_ids)))
                if pid:
                    inv_q_base = inv_q_base.filter(Unit.property_id == pid)
            else:
                if pid:
                    inv_q_base = (inv_q_base.join(Unit, Unit.id == Invoice.unit_id)
                                            .filter(Unit.property_id == pid))
            if tid:
                inv_q_base = inv_q_base.filter(Invoice.tenant_id == tid)

            arrears_map = {}
            for inv in inv_q_base.order_by(Invoice.due_date).all():
                t = inv.tenant
                if not t:
                    continue
                if t.id not in arrears_map:
                    arrears_map[t.id] = {
                        'tenant': t,
                        'unit': inv.unit,
                        'invoices': [],
                        'total': 0.0,
                        'oldest_due': inv.due_date,
                    }
                arrears_map[t.id]['invoices'].append(inv)
                arrears_map[t.id]['total'] += inv.amount
                if inv.due_date and arrears_map[t.id]['oldest_due'] and inv.due_date < arrears_map[t.id]['oldest_due']:
                    arrears_map[t.id]['oldest_due'] = inv.due_date

            rows = sorted(arrears_map.values(), key=lambda x: x['total'], reverse=True)
            totals = {
                'total_owed':    sum(r['total'] for r in rows),
                'tenant_count':  len(rows),
                'invoice_count': sum(len(r['invoices']) for r in rows),
            }

        elif rtype == 'payouts':
            if role == 'Tenant':
                rows, totals = [], {}
            else:
                q = LandlordPayout.query
                if role == 'Landlord':
                    q = q.filter_by(landlord_id=uid)
                if fstatus:
                    q = q.filter(LandlordPayout.status == fstatus)
                if dt_from:
                    q = q.filter(LandlordPayout.requested_at >= _dt_start(dt_from))
                if dt_to:
                    q = q.filter(LandlordPayout.requested_at <= _dt_end(dt_to))
                rows = q.order_by(LandlordPayout.requested_at.desc()).all()
                totals = {
                    'total':     sum(r.amount for r in rows),
                    'completed': sum(r.amount for r in rows if r.status == 'completed'),
                    'pending':   sum(r.amount for r in rows if r.status == 'pending'),
                    'count':     len(rows),
                }

        elif rtype == 'bank_transfers':
            if role == 'Tenant':
                q = BankTransferClaim.query.filter_by(tenant_id=uid)
            elif role == 'Landlord':
                q = (BankTransferClaim.query
                     .join(Invoice, Invoice.id == BankTransferClaim.invoice_id)
                     .join(Unit, Unit.id == Invoice.unit_id)
                     .filter(Unit.property_id.in_(lp_ids)))
            else:
                q = BankTransferClaim.query
            if tid and role != 'Tenant':
                q = q.filter(BankTransferClaim.tenant_id == tid)
            if fstatus:
                q = q.filter(BankTransferClaim.status == fstatus)
            if dt_from:
                q = q.filter(BankTransferClaim.submitted_at >= _dt_start(dt_from))
            if dt_to:
                q = q.filter(BankTransferClaim.submitted_at <= _dt_end(dt_to))
            rows = q.order_by(BankTransferClaim.submitted_at.desc()).all()
            totals = {
                'total':     sum(r.amount for r in rows),
                'confirmed': sum(r.amount for r in rows if r.status == 'confirmed'),
                'pending':   len([r for r in rows if r.status == 'pending']),
                'rejected':  len([r for r in rows if r.status == 'rejected']),
                'count':     len(rows),
            }

        return render_template(
            'reports.html',
            rtype=rtype,
            rows=rows,
            totals=totals,
            kpis=kpis,
            filters=dict(date_from=date_from, date_to=date_to,
                         tenant_id=tenant_id, property_id=prop_id, status=fstatus),
            all_tenants=all_tenants,
            all_properties=all_properties,
            allowed_types=allowed_types,
            today=date.today(),
        )

    # ── /reports/export/csv ───────────────────────────────────────────────
    @app.route('/reports/export/csv')
    @_login_required
    def reports_export_csv():
        role = session.get('role')
        uid  = session.get('user_id')

        rtype     = request.args.get('type', 'invoices')
        if rtype not in _allowed_report_types(role):
            flash("Access denied. Your department cannot export this report.", "danger")
            return redirect(url_for('dashboard'))
        date_from = request.args.get('date_from', '')
        date_to   = request.args.get('date_to', '')
        tenant_id = request.args.get('tenant_id', '')
        prop_id   = request.args.get('property_id', '')
        fstatus   = request.args.get('status', '')

        dt_from = _parse_date(date_from)
        dt_to   = _parse_date(date_to)
        tid  = int(tenant_id) if tenant_id else None
        pid  = int(prop_id)   if prop_id   else None
        lp_ids = _landlord_pids(uid) if role == 'Landlord' else []

        output = io.StringIO()
        writer = csv.writer(output)

        if rtype == 'rent_ledger':
            writer.writerow(['Date', 'Tenant', 'Unit', 'Property', 'Entry Type', 'Amount (N)', 'Description', 'Reference'])
            for r in _led_q(role, uid, lp_ids, tid, pid, fstatus, dt_from, dt_to).order_by(RentLedger.entry_date.desc()).all():
                writer.writerow([
                    r.entry_date,
                    r.tenant.name if r.tenant else '',
                    r.unit.unit_number if r.unit else '',
                    r.prop.name if r.prop else '',
                    r.entry_type, _amt(r.amount),
                    r.description or '', r.reference or '',
                ])

        elif rtype == 'invoices':
            writer.writerow(['Invoice #', 'Date', 'Tenant', 'Unit', 'Type', 'Amount (N)', 'Due Date', 'Status'])
            for r in _inv_q(role, uid, lp_ids, tid, pid, fstatus, dt_from, dt_to).order_by(Invoice.created_at.desc()).all():
                writer.writerow([
                    r.inv_number,
                    r.created_at.date() if r.created_at else '',
                    r.tenant.name if r.tenant else '',
                    r.unit.unit_number if r.unit else '',
                    r.type, _amt(r.amount), r.due_date, r.status,
                ])

        elif rtype == 'payments':
            writer.writerow(['TXN #', 'Date', 'Tenant', 'Invoice #', 'Amount (N)', 'Method', 'Status'])
            for r in _txn_q(role, uid, lp_ids, tid, pid, fstatus, dt_from, dt_to).order_by(Transaction.date.desc()).all():
                writer.writerow([
                    r.txn_number, r.date,
                    r.tenant.name if r.tenant else '',
                    r.invoice.inv_number if r.invoice else '',
                    _amt(r.amount), r.method, r.status,
                ])

        elif rtype == 'expenses':
            writer.writerow(['Exp #', 'Date', 'Property', 'Unit', 'Category', 'Description', 'Amount (N)', 'Vendor', 'Status'])
            for r in _exp_q(role, uid, lp_ids, pid, fstatus, dt_from, dt_to).order_by(Expense.expense_date.desc()).all():
                writer.writerow([
                    r.exp_number, r.expense_date,
                    r.prop.name if r.prop else '',
                    r.unit.unit_number if r.unit else '',
                    r.category, r.description or '',
                    _amt(r.amount), r.vendor or '', r.status,
                ])

        elif rtype == 'arrears':
            writer.writerow(['Tenant', 'Email', 'Phone', 'Invoice #', 'Amount (N)', 'Due Date', 'Days Overdue'])
            q = Invoice.query.filter(Invoice.status == 'overdue')
            if role == 'Tenant':
                q = q.filter(Invoice.tenant_id == uid)
            elif role == 'Landlord':
                q = (q.join(Unit, Unit.id == Invoice.unit_id)
                      .filter(Unit.property_id.in_(lp_ids)))
            if tid:
                q = q.filter(Invoice.tenant_id == tid)
            for r in q.order_by(Invoice.due_date).all():
                days = (date.today() - r.due_date).days if r.due_date else 0
                writer.writerow([
                    r.tenant.name if r.tenant else '',
                    r.tenant.email if r.tenant else '',
                    r.tenant.phone if r.tenant else '',
                    r.inv_number, _amt(r.amount), r.due_date, days,
                ])

        elif rtype == 'bank_transfers':
            writer.writerow(['Submitted', 'Tenant', 'Invoice #', 'Amount (N)', 'Bank Ref', 'Transfer Date', 'Status', 'Reviewed At'])
            if role == 'Tenant':
                q = BankTransferClaim.query.filter_by(tenant_id=uid)
            elif role == 'Landlord':
                q = (BankTransferClaim.query
                     .join(Invoice, Invoice.id == BankTransferClaim.invoice_id)
                     .join(Unit, Unit.id == Invoice.unit_id)
                     .filter(Unit.property_id.in_(lp_ids)))
            else:
                q = BankTransferClaim.query
            if fstatus:
                q = q.filter(BankTransferClaim.status == fstatus)
            if dt_from:
                q = q.filter(BankTransferClaim.submitted_at >= _dt_start(dt_from))
            if dt_to:
                q = q.filter(BankTransferClaim.submitted_at <= _dt_end(dt_to))
            for r in q.order_by(BankTransferClaim.submitted_at.desc()).all():
                writer.writerow([
                    r.submitted_at.date() if r.submitted_at else '',
                    r.tenant.name if r.tenant else '',
                    r.invoice.inv_number if r.invoice else '',
                    _amt(r.amount), r.bank_ref or '', r.transfer_date or '',
                    r.status,
                    r.reviewed_at.date() if r.reviewed_at else '',
                ])

        elif rtype == 'payouts':
            writer.writerow(['Payout #', 'Landlord', 'Amount (N)', 'Status', 'Requested', 'Completed', 'Notes'])
            if role != 'Tenant':
                q = LandlordPayout.query
                if role == 'Landlord':
                    q = q.filter_by(landlord_id=uid)
                if fstatus:
                    q = q.filter(LandlordPayout.status == fstatus)
                if dt_from:
                    q = q.filter(LandlordPayout.requested_at >= _dt_start(dt_from))
                if dt_to:
                    q = q.filter(LandlordPayout.requested_at <= _dt_end(dt_to))
                for r in q.order_by(LandlordPayout.requested_at.desc()).all():
                    writer.writerow([
                        r.payout_number,
                        r.landlord.name if r.landlord else '',
                        _amt(r.amount), r.status,
                        r.requested_at.date() if r.requested_at else '',
                        r.completed_at.date() if r.completed_at else '',
                        r.notes or '',
                    ])

        elif rtype == 'occupancy':
            writer.writerow(['Property', 'Type', 'Address', 'Total Units', 'Occupied', 'Vacant', 'Occupancy %', 'Avg Rent (N)'])
            if role == 'Admin':
                props = Property.query.order_by(Property.name).all()
            elif role == 'Landlord':
                props = Property.query.filter(Property.id.in_(lp_ids)).order_by(Property.name).all()
            else:
                props = []
            for p in props:
                writer.writerow([
                    p.name, p.type, p.address,
                    p.total_units, p.occupied_units, p.vacant_units,
                    f'{p.occupancy_pct:.1f}%', _amt(p.avg_rent or 0),
                ])

        else:
            writer.writerow(['No exportable data for this report type.'])

        output.seek(0)
        filename = f'hs-property-{rtype}-{date.today().isoformat()}.csv'
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )
