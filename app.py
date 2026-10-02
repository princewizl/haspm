"""
HS Property Management — Flask Application
"""
import os
import json
import uuid
from datetime import datetime, date, timedelta
from functools import wraps
from collections import OrderedDict
from flask import (Flask, render_template, redirect, url_for, Response,
                   session, request, flash, jsonify, send_file, abort)
from werkzeug.utils import secure_filename
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
import zipfile
import mimetypes
import csv
import io
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from invoice_routes import register_invoice_routes
from finance_routes import register_finance_routes
from report_routes import register_report_routes
from message_routes import register_message_routes
from permissions import register_permissions, has_perm, dept_required, current_dept, DEPARTMENTS
from mailer import mail, send_test_email, send_notice_email
from models import db, User, Property, PropertyImage, Unit, Tenancy, Invoice, Transaction, Ticket, TenantDocument, RentLedger, Expense, LandlordPayout, NotificationLog, BankTransferClaim, Message, TechnicianRating, Attachment

# ─────────────────────────────────────────────
#  APP CONFIG
# ─────────────────────────────────────────────
app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Environment name: development | test | live.  Shown in the UI banner so nobody
# mistakes the staging site for production.
APP_ENV = os.environ.get("APP_ENV", "development")

# Writable state (database + saved integration config) lives here.  Under Docker
# this points at a mounted volume so it survives image rebuilds; locally it is
# just the project directory, so nothing changes for development.
DATA_DIR = os.environ.get("HSPM_DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

app.secret_key = os.environ.get("SECRET_KEY", "hs-property-secret-2025")

app.config["APP_ENV"]                        = APP_ENV
app.config["SQLALCHEMY_DATABASE_URI"]        = os.environ.get(
    "DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'hs_property.db')}")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Property images are served by Flask's /static route, so they must stay inside
# the static tree; the volume is mounted at static/uploads.
app.config["UPLOAD_FOLDER"]                  = os.path.join(BASE_DIR, "static", "uploads", "properties")
app.config["MAX_CONTENT_LENGTH"]             = 10 * 1024 * 1024  # 10 MB (matches the document upload limit)
app.config["DOCS_UPLOAD_FOLDER"] = os.environ.get(
    "DOCS_UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads", "documents"))
os.makedirs(app.config["DOCS_UPLOAD_FOLDER"], exist_ok=True)

# ── Email config (persisted in email_config.json) ─────────────────────
EMAIL_CONFIG_FILE = os.path.join(DATA_DIR, "email_config.json")

def _load_email_config():
    if os.path.exists(EMAIL_CONFIG_FILE):
        try:
            with open(EMAIL_CONFIG_FILE) as f:
                cfg = json.load(f)
            for k, v in cfg.items():
                app.config[k] = v
        except Exception:
            pass

_load_email_config()
app.config.setdefault("MAIL_SERVER",         "")
app.config.setdefault("MAIL_PORT",           587)
app.config.setdefault("MAIL_USE_TLS",        True)
app.config.setdefault("MAIL_USERNAME",       "")
app.config.setdefault("MAIL_PASSWORD",       "")
app.config.setdefault("MAIL_DEFAULT_SENDER", "")
# Hard kill-switch for outbound mail, set on the test environment.
if os.environ.get("MAIL_SUPPRESS_SEND", "0") == "1":
    app.config["MAIL_SUPPRESS_SEND"] = True
    app.config["MAIL_SERVER"] = ""

# ── Paystack config (persisted in paystack_config.json) ───────────────
PAYSTACK_CONFIG_FILE = os.path.join(DATA_DIR, "paystack_config.json")

def _load_paystack_config():
    if os.path.exists(PAYSTACK_CONFIG_FILE):
        try:
            with open(PAYSTACK_CONFIG_FILE) as f:
                cfg = json.load(f)
            for k, v in cfg.items():
                app.config[k] = v
        except Exception:
            pass

_load_paystack_config()
# ── Company commission (persisted in company_config.json) ─────────────
# Default share of collections that belongs to Hearts & Sleeves. Individual
# properties may override it; the remainder always goes to the landlord.
COMPANY_CONFIG_FILE = os.path.join(DATA_DIR, "company_config.json")

def _load_company_config():
    if os.path.exists(COMPANY_CONFIG_FILE):
        try:
            with open(COMPANY_CONFIG_FILE) as f:
                for k, v in json.load(f).items():
                    app.config[k] = v
        except Exception:
            pass

_load_company_config()
app.config.setdefault("DEFAULT_COMMISSION_PCT", 0.0)

app.config.setdefault("PAYSTACK_PUBLIC_KEY",  "")
app.config.setdefault("PAYSTACK_SECRET_KEY",  "")

# ── Bank transfer config (persisted in bank_config.json) ──────────────
BANK_CONFIG_FILE = os.path.join(BASE_DIR, "bank_config.json")

def _load_bank_config():
    if os.path.exists(BANK_CONFIG_FILE):
        try:
            with open(BANK_CONFIG_FILE) as f:
                cfg = json.load(f)
            for k, v in cfg.items():
                app.config[k] = v
        except Exception:
            pass

_load_bank_config()
app.config.setdefault("BANK_NAME",           "")
app.config.setdefault("BANK_ACCOUNT_NUMBER", "")
app.config.setdefault("BANK_ACCOUNT_NAME",   "")
app.config.setdefault("BANK_TRANSFER_NOTE",  "")

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
db.init_app(app)
mail.init_app(app)
register_invoice_routes(app)
register_finance_routes(app)
register_report_routes(app)
register_message_routes(app)
register_permissions(app)

# ── Start background scheduler (skip reloader child process) ──────────
def _start_scheduler():
    from scheduler import init_scheduler
    init_scheduler(app)

ENABLE_SCHEDULER = os.environ.get("ENABLE_SCHEDULER", "1") == "1"

if ENABLE_SCHEDULER and (not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
    _start_scheduler()


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def fmt(n):
    return f"₦{n:,.0f}"

app.jinja_env.globals["fmt"] = fmt


def _has_ext(filename, extensions):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in extensions


def allowed_image_file(filename):
    return _has_ext(filename, IMAGE_EXTENSIONS)


def save_property_image(file_obj):
    """Save uploaded image; return URL path or None."""
    if file_obj and file_obj.filename and allowed_image_file(file_obj.filename):
        ext      = file_obj.filename.rsplit(".", 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        path     = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file_obj.save(path)
        return url_for("static", filename=f"uploads/properties/{filename}")
    return None


# ─────────────────────────────────────────────
#  AUTH DECORATORS
# ─────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please sign in to continue.", "info")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "user_id" not in session:
                flash("Please sign in to continue.", "info")
                return redirect(url_for("login"))
            if session.get("role") not in roles:
                flash(f"Access denied. Your role ({session.get('role')}) cannot view this page.", "danger")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return decorated
    return decorator

DOC_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}
MAX_DOC_BYTES  = 10 * 1024 * 1024   # 10 MB

def allowed_document_file(filename):
    return _has_ext(filename, DOC_EXTENSIONS)
 

# ─────────────────────────────────────────────
#  AUTH ROUTES
# ─────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user     = User.query.filter_by(email=email).first()
        if not user:
            flash("No account found with that email address.", "danger")
            return render_template("login.html")
        if not user.check_password(password):
            flash("Incorrect password. Please try again.", "danger")
            return render_template("login.html")

        # Accounts predating password hashing are upgraded on first correct
        # login, so no one is locked out and plain text disappears over time.
        if user.password_needs_rehash:
            user.set_password(password)
            db.session.commit()
            app.logger.info("[auth] re-hashed legacy password for user %s", user.id)

        session["user_id"] = user.id
        session["user"]    = user.name
        session["role"]    = user.role
        session["email"]   = user.email
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    role       = session["role"]
    user_id    = session["user_id"]
    properties = Property.query.all()
    invoices   = Invoice.query.all()
    tickets    = Ticket.query.all()

    paid_rev  = sum(i.amount for i in invoices if i.status == "paid")
    pending_c = sum(1 for i in invoices if i.status == "pending")
    overdue_c = sum(1 for i in invoices if i.status == "overdue")
    open_t    = sum(1 for t in tickets if t.status == "open")
    urgent_t  = sum(1 for t in tickets if t.classification in ("emergency", "urgent")
                    and t.status != "resolved")

    occ_data  = [{"name": p.name, "pct": p.occupancy_pct,
                  "occ": p.occupied_units, "total": p.total_units} for p in properties]

    total_units = sum(p.total_units    for p in properties)
    total_occ   = sum(p.occupied_units for p in properties)

    # ── TENANT SPECIFIC DATA ──
    tenant_invoices = None
    tenant_tickets = None
    tenant_property_data = None

    if role == "Tenant":
        tenant_invoices = Invoice.query.filter_by(tenant_id=user_id).all()
        tenant_tickets  = Ticket.query.filter_by(tenant_id=user_id).all()
        
        # Fetch the current user and their active tenancy
        user = User.query.get(user_id)
        active_tenancy = user.tenancies.filter_by(is_active=True).first()
        
        # If they have an active tenancy, build the property dictionary
        if active_tenancy and active_tenancy.unit:
            unit = active_tenancy.unit
            prop = unit.prop
            
            tenant_property_data = {
                "name": prop.name,
                "address": prop.address,
                "unit_number": unit.unit_number,
                "images": [img.image_url for img in prop.images]
            }

    rev_data = [
        {"month": "Jan", "rev": 2100000, "exp": 420000},
        {"month": "Feb", "rev": 1950000, "exp": 380000},
        {"month": "Mar", "rev": 2300000, "exp": 510000},
        {"month": "Apr", "rev": 2150000, "exp": 440000},
        {"month": "May", "rev": 2450000, "exp": 395000},
        {"month": "Jun", "rev": 2680000, "exp": 460000},
    ]

    return render_template("dashboard.html",
        role=role, 
        properties=properties, 
        invoices=invoices[:5], 
        tickets=tickets,
        paid_rev=paid_rev, 
        pending_count=pending_c, 
        overdue_count=overdue_c,
        open_tickets=open_t, 
        urgent_count=urgent_t, 
        occ_data=occ_data,
        rev_data=rev_data, 
        total_units=total_units, 
        total_occ=total_occ,
        tenant_invoices=tenant_invoices, 
        tenant_tickets=tenant_tickets,
        tenant_property=tenant_property_data  # Pass the newly created dict to the template
    )


# ─────────────────────────────────────────────
#  PROPERTIES
# ─────────────────────────────────────────────
@app.route("/properties")
@role_required("Admin", "Landlord")
def properties():
    props = Property.query.order_by(Property.created_at.desc()).all()
    # Eagerly pull tenant data per property for modals
    tenants_by_prop = {}
    for p in props:
        tenants_by_prop[p.id] = []
        for unit in p.units:
            active = unit.tenancies.filter_by(is_active=True).first()
            if active:
                inv = Invoice.query.filter_by(
                    tenant_id=active.tenant_id, unit_id=unit.id
                ).order_by(Invoice.due_date.desc()).first()
                tenants_by_prop[p.id].append({
                    "id":     active.tenant_id,
                    "name":   active.tenant.name,
                    "unit":   unit.unit_number,
                    "rent":   active.annual_rent,
                    "due":    inv.due_date.strftime("%b %d, %Y") if inv else "—",
                    "status": inv.status if inv else "—",
                })

    # Shown in the delete confirmation, so nobody removes history by accident.
    delete_impact = {}
    if session["role"] == "Admin" and has_perm("manage_properties"):
        for p in props:
            linked = _property_linked_records(p)
            delete_impact[p.id] = ", ".join(
                f"{len(linked[k])} {label}" for k, label in (
                    ("unit_ids", "unit(s)"), ("tenancies", "tenancy(ies)"),
                    ("invoices", "invoice(s)"), ("transactions", "payment(s)"),
                    ("tickets", "service request(s)"), ("expenses", "expense(s)"))
                if linked[k])

    return render_template("properties.html",
        properties=props,
        tenants_by_prop=tenants_by_prop,
        delete_impact=delete_impact,
        role=session["role"],
    )


@app.route("/properties/add", methods=["POST"])
@role_required("Admin", "Landlord")
def add_property():
    # Only the Management department may add properties (staff side);
    # landlords may still register their own properties.
    if session.get("role") == "Admin" and not has_perm("manage_properties"):
        flash(f"Access denied. Only Management can add properties.", "danger")
        return redirect(url_for("properties"))
    name        = request.form.get("name", "").strip()
    address     = request.form.get("address", "").strip()
    prop_type   = request.form.get("type", "Residential")
    description = request.form.get("description", "").strip()
    # One rent per unit; the property's average is computed from them.
    try:
        unit_rents = [max(0.0, float(r)) for r in request.form.getlist("unit_rents")]
    except ValueError:
        flash("Each unit's rent must be a number.", "danger")
        return redirect(url_for("properties"))
    num_units   = len(unit_rents)
    pattern     = request.form.get("unit_naming_pattern", "").strip() or None

    # Blank means "inherit the global default from Settings".
    raw_pct     = request.form.get("commission_pct", "").strip()
    commission  = None
    if raw_pct != "":
        try:
            commission = max(0.0, min(100.0, float(raw_pct)))
        except ValueError:
            commission = None

    prop = Property(
        name=name, address=address, type=prop_type,
        description=description, avg_rent=0,
        landlord_id=session["user_id"],
        commission_pct=commission,
        unit_naming_pattern=pattern,
    )
    db.session.add(prop)
    db.session.flush() # Flush to get the prop.id

    # Handle multiple file uploads
    if "images" in request.files:
        files = request.files.getlist("images")
        for file in files:
            if file.filename: # Ensure it's an actual file
                img_url = save_property_image(file) # Your existing save function
                if img_url:
                    db.session.add(PropertyImage(property_id=prop.id, image_url=img_url))
    
    # Fallback external URLs (comma separated for multiple)
    ext_urls = request.form.get("image_urls", "").strip()
    if ext_urls:
        for url in ext_urls.split(","):
            if url.strip():
                db.session.add(PropertyImage(property_id=prop.id, image_url=url.strip()))

    # Auto-generate units using the property's naming pattern, e.g.
    # "Block A - Unit {n}" -> "Block A - Unit 1", "Block A - Unit 2", ...
    for i, rent in enumerate(unit_rents, start=1):
        db.session.add(Unit(
            unit_number=prop.format_unit_name(i), rent_amount=rent,
            is_occupied=False, property_id=prop.id,
        ))
    db.session.flush()
    prop.recalc_avg_rent()

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Could not create the property: that unit naming pattern produced "
              "duplicate unit names.", "danger")
        return redirect(url_for("properties"))

    flash(f"Property '{name}' added successfully with {num_units} unit(s)!", "success")
    return redirect(url_for("properties"))


@app.route("/properties/<int:prop_id>/edit", methods=["POST"])
@role_required("Admin", "Landlord")
def edit_property(prop_id):
    prop = Property.query.get_or_404(prop_id)
    if session.get("role") == "Admin" and not has_perm("manage_properties"):
        flash("Access denied. Only Management can edit property details.", "danger")
        return redirect(url_for("properties"))
    # A landlord may only edit their own property
    if session.get("role") == "Landlord" and prop.landlord_id != session["user_id"]:
        flash("Access denied. You can only edit your own properties.", "danger")
        return redirect(url_for("properties"))
    prop.name        = (request.form.get("name") or prop.name).strip()
    prop.address     = (request.form.get("address", prop.address) or "").strip()
    prop.type        = request.form.get("type", prop.type)
    prop.description = (request.form.get("description", prop.description) or "").strip()

    # Rents of existing units, posted as unit_rent_<unit id>.
    for unit in prop.units:
        raw = request.form.get(f"unit_rent_{unit.id}")
        if raw is not None and raw.strip() != "":
            try:
                unit.rent_amount = max(0.0, float(raw))
            except ValueError:
                flash(f"Rent for {unit.unit_number} must be a number - left unchanged.",
                      "danger")

    if "unit_naming_pattern" in request.form:
        prop.unit_naming_pattern = request.form.get("unit_naming_pattern", "").strip() or None

    # Only Management sets the commission rate; a landlord cannot change their
    # own cut. Blank clears the override and falls back to the global default.
    if "commission_pct" in request.form and has_perm("manage_properties"):
        raw = request.form.get("commission_pct", "").strip()
        if raw == "":
            prop.commission_pct = None
        else:
            try:
                prop.commission_pct = max(0.0, min(100.0, float(raw)))
            except ValueError:
                flash("Commission must be a number between 0 and 100 - left unchanged.",
                      "danger")

    # Growing the unit count adds units named from the pattern. Shrinking it is
    # not done here: units carry tenancy and invoice history.
    if request.form.get("units"):
        try:
            wanted = int(request.form["units"])
        except ValueError:
            wanted = prop.total_units
        current = prop.total_units
        if wanted > current:
            new_rents = request.form.getlist("new_unit_rents")
            for i in range(wanted - current):
                try:
                    rent = max(0.0, float(new_rents[i]))
                except (IndexError, ValueError):
                    rent = prop.avg_rent or 0
                db.session.add(Unit(
                    unit_number=prop.next_unit_name(),
                    rent_amount=rent, is_occupied=False,
                    property_id=prop.id,
                ))
                db.session.flush()
            flash(f"Added {wanted - current} unit(s) to '{prop.name}'.", "info")
        elif wanted < current:
            flash(f"'{prop.name}' still has {current} units. Units are not removed "
                  "automatically because they carry tenancy and invoice history.",
                  "info")

    db.session.flush()
    prop.recalc_avg_rent()

    # Photos: tick existing ones to remove, upload or paste URLs to add more.
    remove_ids = {int(i) for i in request.form.getlist("remove_images") if i.isdigit()}
    removed = 0
    for img in list(prop.images):
        if img.id in remove_ids:
            _delete_property_image_file(img.image_url)
            prop.images.remove(img)
            removed += 1

    added = 0
    for file in request.files.getlist("images"):
        if file.filename:
            img_url = save_property_image(file)
            if img_url:
                prop.images.append(PropertyImage(image_url=img_url))
                added += 1
            else:
                flash(f"'{file.filename}' was skipped - photos must be "
                      f"{', '.join(sorted(IMAGE_EXTENSIONS))}.", "danger")
    for url in request.form.get("image_urls", "").split(","):
        if url.strip():
            prop.images.append(PropertyImage(image_url=url.strip()))
            added += 1

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Could not save: that would create two units with the same name "
              "in this property.", "danger")
        return redirect(url_for("properties"))

    photos = []
    if added:
        photos.append(f"{added} photo(s) added")
    if removed:
        photos.append(f"{removed} removed")
    flash(f"Property '{prop.name}' updated"
          + (f" - {', '.join(photos)}." if photos else "."), "success")
    return redirect(url_for("properties"))


def _delete_property_image_file(image_url):
    """Remove an uploaded property photo from disk. External URLs are left alone."""
    prefix = url_for("static", filename="uploads/properties/")
    if image_url and image_url.startswith(prefix):
        path = os.path.join(app.config["UPLOAD_FOLDER"],
                            os.path.basename(image_url[len(prefix):]))
        try:
            os.remove(path)
        except OSError:
            pass


def _property_linked_records(prop):
    """Everything that hangs off a property, grouped for deletion.

    Units cascade from the property in the ORM, but tenancies, invoices,
    tickets, ledger entries and expenses reference the property or its units
    with NOT NULL foreign keys and no cascade. Deleting the property alone
    makes SQLAlchemy try to null those keys, which is the IntegrityError that
    used to surface on every property with history.
    """
    unit_ids = [u.id for u in prop.units]
    in_units = (lambda col: col.in_(unit_ids)) if unit_ids else (lambda col: db.false())

    tickets  = Ticket.query.filter(db.or_(Ticket.property_id == prop.id,
                                          in_units(Ticket.unit_id))).all()
    invoices = Invoice.query.filter(in_units(Invoice.unit_id)).all()
    invoice_ids = [i.id for i in invoices]
    return {
        "unit_ids":     unit_ids,
        "tickets":      tickets,
        "invoices":     invoices,
        "transactions": Transaction.query.filter(Transaction.invoice_id.in_(invoice_ids)).all()
                        if invoice_ids else [],
        "claims":       BankTransferClaim.query.filter(
                            BankTransferClaim.invoice_id.in_(invoice_ids)).all()
                        if invoice_ids else [],
        "tenancies":    Tenancy.query.filter(in_units(Tenancy.unit_id)).all(),
        "ledger":       RentLedger.query.filter(db.or_(RentLedger.property_id == prop.id,
                                                       in_units(RentLedger.unit_id))).all(),
        "expenses":     Expense.query.filter(db.or_(Expense.property_id == prop.id,
                                                    in_units(Expense.unit_id))).all(),
    }


@app.route("/properties/<int:prop_id>/delete", methods=["POST"])
@dept_required("manage_properties")
def delete_property(prop_id):
    prop = Property.query.get_or_404(prop_id)
    name = prop.name
    linked = _property_linked_records(prop)

    # Children before parents, so no foreign key is ever left dangling.
    files = []
    for t in linked["tickets"]:
        TechnicianRating.query.filter_by(ticket_id=t.id).delete()
        for att in Attachment.query.filter_by(parent_type=Attachment.PARENT_TICKET,
                                              parent_id=t.id):
            files.append(os.path.join(_attach_folder(), att.stored_filename))
            db.session.delete(att)
        db.session.delete(t)
    for group in ("claims", "transactions", "invoices", "tenancies", "ledger", "expenses"):
        for row in linked[group]:
            db.session.delete(row)
    image_urls = [img.image_url for img in prop.images]
    db.session.delete(prop)            # units and images cascade

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        app.logger.error("[properties] delete of %s failed: %s", prop_id, e)
        flash(f"Could not delete '{name}': it is still referenced elsewhere. "
              "Nothing was removed.", "danger")
        return redirect(url_for("properties"))

    # Only touch files once the database change is committed.
    for url in image_urls:
        _delete_property_image_file(url)
    for path in files:
        try:
            os.remove(path)
        except OSError:
            pass

    flash(f"Property '{name}' deleted, together with {len(linked['unit_ids'])} unit(s), "
          f"{len(linked['tenancies'])} tenancy(ies), {len(linked['invoices'])} invoice(s) "
          f"and {len(linked['tickets'])} service request(s).", "success")
    return redirect(url_for("properties"))

from flask import jsonify

@app.route("/api/properties/<int:property_id>/vacant-units")
@role_required("Admin", "Landlord")
def vacant_units(property_id):
    units = Unit.query.filter_by(property_id=property_id, is_occupied=False).all()

    return jsonify([
        {
            "id": u.id,
            "unit_number": u.unit_number,
            "rent_amount": u.rent_amount or 0
        }
        for u in units
    ])

# ─────────────────────────────────────────────
#  TENANTS
# ─────────────────────────────────────────────
@app.route("/tenants")
@role_required("Admin", "Landlord")
def tenants():
    q = request.args.get("q", "").lower()
    filter_role = request.args.get("filter", "Tenant")
    properties = Property.query.all()

    query = User.query
    if filter_role:
        query = query.filter_by(role=filter_role)

    all_users = query.all()

    if q:
        all_users = [u for u in all_users if q in u.name.lower() or q in u.email.lower()]

    tenant_data = []
    for u in all_users:
        active = Tenancy.query.filter_by(tenant_id=u.id, is_active=True).first()
        inv = Invoice.query.filter_by(tenant_id=u.id).order_by(Invoice.due_date.desc()).first()

        tenant_data.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "phone": u.phone,
            "role": u.role,
            "department": u.department,
            "is_active": u.is_active,

            "property_id": active.unit.property_id if active else None,
            "unit_id": active.unit.id if active else None,

            "property_name": active.unit.prop.name if active else "—",
            "unit_number": active.unit.unit_number if active else "—",
            "annual_rent": active.annual_rent if active else 0,
            "due_date": inv.due_date.strftime("%b %d, %Y") if inv and inv.due_date else "—",
            "invoice_status": inv.status if inv else "—",
        })

    return render_template(
        "tenants.html",
        tenants=tenant_data,
        q=q,
        filter_role=filter_role,
        role=session["role"],
        properties=properties
    )

@app.route("/tenants/add", methods=["POST"])
@dept_required("manage_staff", "onboard_users")
def add_tenant():
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone = request.form.get("phone", "").strip()
    role = request.form.get("role", "Tenant")
    password = request.form.get("password")

    if role not in ("Tenant", "Landlord", "Admin"):
        flash("Invalid role selected.", "danger")
        return redirect(url_for("tenants"))

    # Only Management can create staff (Admin) accounts or assign departments
    if role == "Admin" and not has_perm("manage_staff"):
        flash("Access denied. Only Management can create staff accounts.", "danger")
        return redirect(url_for("tenants"))

    department = None
    if role == "Admin":
        department = request.form.get("department", "").strip()
        if department not in DEPARTMENTS:
            flash("Please select a valid department for the staff account.", "danger")
            return redirect(url_for("tenants"))

    # Check if user already exists
    if User.query.filter_by(email=email).first():
        flash("An account with that email already exists.", "danger")
        return redirect(url_for("tenants"))

    new_user = User(
        name=f"{first_name} {last_name}",
        email=email,
        phone=phone,
        role=role,
        department=department,
        is_active=True
    )
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.flush() # Flush to get the new_user.id

    # If role is Tenant, handle optional tenancy assignment
    if role == "Tenant":
        property_id = request.form.get("property_id")
        unit_id = request.form.get("unit_id")
        annual_rent = request.form.get("annual_rent")

        if property_id and unit_id and annual_rent:
            unit = Unit.query.filter_by(id=unit_id, property_id=property_id, is_occupied=False).first()
            if unit:
                unit.is_occupied = True
                new_tenancy = Tenancy(
                    tenant_id=new_user.id,
                    unit_id=unit.id,
                    annual_rent=float(annual_rent),
                    is_active=True,
                    start_date=date.today()
                )
                db.session.add(new_tenancy)

    db.session.commit()
    flash(f"User {new_user.name} added successfully!", "success")
    return redirect(url_for("tenants"))


@app.route("/tenants/<int:user_id>/edit", methods=["POST"])
@dept_required("manage_staff", "edit_contacts")
def edit_tenant(user_id):
    user = User.query.get_or_404(user_id)

    # Customer Service may edit tenant/landlord contact details only.
    # Staff accounts, role changes, passwords, departments and tenancy/rent
    # assignments require Management (manage_staff).
    is_management = has_perm("manage_staff")
    if user.role == "Admin" and not is_management:
        flash("Access denied. Only Management can edit staff accounts.", "danger")
        return redirect(url_for("tenants"))

    first_name = request.form.get("first_name", "").strip()
    last_name  = request.form.get("last_name", "").strip()
    email      = request.form.get("email", "").strip().lower()
    phone      = request.form.get("phone", "").strip()
    role       = request.form.get("role", user.role) if is_management else user.role
    password   = request.form.get("password", "").strip() if is_management else ""

    if role not in ("Tenant", "Landlord", "Admin"):
        flash("Invalid role selected.", "danger")
        return redirect(url_for("tenants"))

    if is_management and role == "Admin":
        department = request.form.get("department", "").strip()
        if department in DEPARTMENTS:
            user.department = department
    elif is_management and role != "Admin":
        user.department = None

    property_id = request.form.get("property_id")
    unit_id     = request.form.get("unit_id")
    annual_rent = request.form.get("annual_rent")
    start_date   = request.form.get("start_date")
    end_date     = request.form.get("end_date")

    # Prevent duplicate email on another user
    if email and User.query.filter(User.email == email, User.id != user.id).first():
        flash("Another account already uses that email address.", "danger")
        return redirect(url_for("tenants"))

    # Update account details
    if first_name or last_name:
        user.name = f"{first_name} {last_name}".strip()
    if email:
        user.email = email
    if phone:
        user.phone = phone
    user.role = role

    # Update password only if provided
    if password:
        user.set_password(password)

    # Handle tenancy updates — rent/unit assignment is Management-only.
    # Customer Service edits stop at contact details; skipping this block also
    # prevents a contact-only edit from silently closing an active tenancy.
    if not is_management:
        db.session.commit()
        flash(f"Contact details for {user.name} updated successfully!", "success")
        return redirect(url_for("tenants"))

    active_tenancy = Tenancy.query.filter_by(tenant_id=user.id, is_active=True).first()

    if role == "Tenant" and property_id and unit_id and annual_rent:
        unit = Unit.query.filter_by(
            id=unit_id,
            property_id=property_id,
            is_occupied=False
        ).first()

        if not unit:
            flash("Selected unit is invalid or already occupied.", "danger")
            return redirect(url_for("tenants"))

        # Free previous unit if changing assignment
        if active_tenancy and active_tenancy.unit_id != unit.id:
            if active_tenancy.unit:
                active_tenancy.unit.is_occupied = False
            active_tenancy.is_active = False

        # Create or update tenancy
        tenancy = active_tenancy if active_tenancy and active_tenancy.unit_id == unit.id else None

        if not tenancy:
            tenancy = Tenancy(
                tenant_id=user.id,
                unit_id=unit.id,
                annual_rent=float(annual_rent),
                is_active=True,
                start_date=date.fromisoformat(start_date) if start_date else date.today(),
                end_date=date.fromisoformat(end_date) if end_date else None
            )
            db.session.add(tenancy)
        else:
            tenancy.annual_rent = float(annual_rent)
            if start_date:
                tenancy.start_date = date.fromisoformat(start_date)
            tenancy.end_date = date.fromisoformat(end_date) if end_date else None
            tenancy.is_active = True

        unit.is_occupied = True

    else:
        # If user is no longer a tenant, close any active tenancy
        if active_tenancy:
            if active_tenancy.unit:
                active_tenancy.unit.is_occupied = False
            active_tenancy.is_active = False

    db.session.commit()
    flash(f"User {user.name} updated successfully.", "success")
    return redirect(url_for("tenants"))


@app.route("/tenants/<int:user_id>/deactivate", methods=["POST"])
@dept_required("manage_staff")
def deactivate_tenant(user_id):
    user = User.query.get_or_404(user_id)
    user.is_active = False
    
    # Optionally, end their active tenancy
    active_tenancy = Tenancy.query.filter_by(tenant_id=user.id, is_active=True).first()
    if active_tenancy:
        active_tenancy.is_active = False
        active_tenancy.unit.is_occupied = False

    db.session.commit()
    flash(f"User {user.name} has been deactivated.", "warning")
    return redirect(url_for("tenants"))


@app.route("/tenants/<int:user_id>/activate", methods=["POST"])
@dept_required("manage_staff")
def activate_tenant(user_id):
    user = User.query.get_or_404(user_id)
    user.is_active = True
    db.session.commit()
    flash(f"User {user.name} has been reactivated.", "success")
    return redirect(url_for("tenants"))


@app.route("/tenants/<int:user_id>/nudge", methods=["POST"])
@dept_required("log_complaints")
def nudge_tenant(user_id):
    user = User.query.get_or_404(user_id)
    # Placeholder for actual notification logic (e.g., SMTP email, WhatsApp API)
    flash(f"Payment reminder sent to {user.name}.", "success")
    return redirect(url_for("tenants"))


@app.route("/tenants/<int:user_id>/delete", methods=["POST"])
@dept_required("manage_staff")
def delete_tenant(user_id):
    user = User.query.get_or_404(user_id)
    name = user.name
    
    # Free up the unit if they were occupying one
    active_tenancy = Tenancy.query.filter_by(tenant_id=user.id, is_active=True).first()
    if active_tenancy:
        active_tenancy.unit.is_occupied = False

    db.session.delete(user)
    db.session.commit()
    flash(f"User {name} permanently deleted.", "danger")
    return redirect(url_for("tenants"))


# ─────────────────────────────────────────────
#  USER PROFILE  GET /users/<id>
# ─────────────────────────────────────────────
@app.route("/users/<int:user_id>")
@role_required("Admin", "Landlord")
def user_profile(user_id):
    u = User.query.get_or_404(user_id)

    # A profile only loads what its subject can actually have. Landlords and
    # staff have no tenancy, no rent invoices and no rent ledger, so those
    # queries are skipped entirely rather than rendering empty rent sections.
    is_tenant   = u.role == "Tenant"
    is_landlord = u.role == "Landlord"
    is_staff    = u.role == "Admin"

    tenancy = past_tenancies = None
    invoices = transactions = tickets = documents = []
    owned_properties = []
    landlord_stats   = None
    staff_activity   = None

    if is_tenant:
        tenancy = Tenancy.query.filter_by(tenant_id=u.id, is_active=True).first()
        past_tenancies = (Tenancy.query
                          .filter_by(tenant_id=u.id, is_active=False)
                          .order_by(Tenancy.end_date.desc()).all())
        invoices = (Invoice.query.filter_by(tenant_id=u.id)
                    .order_by(Invoice.due_date.desc()).all())
        transactions = (Transaction.query.filter_by(tenant_id=u.id)
                        .order_by(Transaction.date.desc()).all())
        tickets = (Ticket.query.filter_by(tenant_id=u.id)
                   .order_by(Ticket.date_raised.desc()).all())
        documents = (TenantDocument.query.filter_by(tenant_id=u.id)
                     .order_by(TenantDocument.uploaded_at.desc()).all())

    elif is_landlord:
        owned_properties = (Property.query.filter_by(landlord_id=u.id)
                            .order_by(Property.name).all())
        prop_ids = [p.id for p in owned_properties]
        # Maintenance raised anywhere on their portfolio.
        if prop_ids:
            tickets = (Ticket.query.filter(Ticket.property_id.in_(prop_ids))
                       .order_by(Ticket.date_raised.desc()).limit(25).all())
        from finance_routes import _landlord_earnings
        landlord_stats = _landlord_earnings(u.id)

    elif is_staff:
        # What this staff member handles, not what they owe.
        staff_activity = dict(
            department=u.department or "Management",
            assigned_tickets=Ticket.query.filter(
                Ticket.technician == u.name).order_by(
                Ticket.date_raised.desc()).limit(25).all(),
        )

    # Notification history (Admin-only)
    notif_logs = []
    if session.get("role") == "Admin":
        notif_logs = (NotificationLog.query.filter_by(tenant_id=u.id)
                      .order_by(NotificationLog.sent_at.desc()).limit(20).all())

    # Rent summary - tenants only; the template hides these for other roles.
    total_charged = sum(inv.amount for inv in invoices)
    total_paid    = sum(t.amount for t in transactions if t.status == "confirmed")
    balance       = total_charged - total_paid

    def fmt(n):
        return f"₦{n:,.0f}"

    return render_template(
        "user_profile.html",
        u             = u,
        tenancy       = tenancy,
        past_tenancies= past_tenancies,
        invoices      = invoices,
        transactions  = transactions,
        tickets       = tickets,
        documents     = documents,
        notif_logs    = notif_logs,
        is_tenant     = is_tenant,
        is_landlord   = is_landlord,
        is_staff      = is_staff,
        owned_properties = owned_properties,
        landlord_stats   = landlord_stats,
        staff_activity   = staff_activity,
        total_charged = total_charged,
        total_paid    = total_paid,
        balance       = balance,
        role          = session["role"],
        fmt           = fmt,
        today         = date.today(),
    )


# ─────────────────────────────────────────────
#  INVOICES
# ─────────────────────────────────────────────



# ─────────────────────────────────────────────
#  SERVICE REQUESTS
# ─────────────────────────────────────────────
@app.route("/service")
@login_required
def service():
    role    = session["role"]
    user_id = session["user_id"]

    # Finance department sees maintenance costs only (via Finance → Expenses),
    # not job details.
    if role == "Admin" and not (has_perm("view_maintenance_full") or has_perm("view_maintenance_status")):
        flash("Access denied. Your department cannot view maintenance job details.", "danger")
        return redirect(url_for("dashboard"))

    visible = Ticket.query.filter_by(tenant_id=user_id).all() if role == "Tenant" else Ticket.query.all()
    TECHNICIANS = ["Emeka Obi", "Tunde Adebisi", "Ifeanyi Nwosu", "Bola Fashola", "Yemi Ogun"]

    # Technician rating summary (avg + count per technician) for staff
    rating_summary = []
    if role == "Admin":
        rows = (db.session.query(
                    TechnicianRating.technician,
                    func.avg(TechnicianRating.stars),
                    func.count(TechnicianRating.id))
                .group_by(TechnicianRating.technician)
                .order_by(func.avg(TechnicianRating.stars).desc())
                .all())
        rating_summary = [
            {"technician": r[0], "avg": round(r[1] or 0, 1), "count": r[2]}
            for r in rows
        ]

    return render_template("service.html",
        tickets=visible,
        open_count=sum(1 for t in visible if t.status == "open"),
        prog_count=sum(1 for t in visible if t.status == "in-progress"),
        done_count=sum(1 for t in visible if t.status == "resolved"),
        technicians=TECHNICIANS,
        properties=Property.query.all(),
        role=role,
        rating_summary=rating_summary,
        classes=Ticket.CLASSES,
        tenant_classes=Ticket.TENANT_CLASSES,
        can_classify=_can_classify(),
        open_ticket=request.args.get("open", type=int),
    )


def _can_classify():
    return session.get("role") == "Admin" and (
        has_perm("edit_maintenance") or has_perm("log_complaints"))


def _post_system_message(sender_id, recipients, subject, body, email_template=None, **ctx):
    """Drop a message in each recipient's inbox, and email them when SMTP is set.

    The inbox copy is the record; email is best-effort (and always off on test).
    """
    for r in recipients:
        db.session.add(Message(sender_id=sender_id, recipient_id=r.id,
                               subject=subject[:200], body=body))
        if email_template and r.email:
            send_notice_email(app, r.email, subject, email_template,
                              recipient_name=r.name, body=body, **ctx)


def _notify_maintenance_team(ticket, sender_id):
    """Emergency requests go straight to the Maintenance department."""
    team = User.query.filter_by(role="Admin", department="Maintenance", is_active=True).all()
    if not team:
        # No Maintenance staff set up yet: fall back to Management so an
        # emergency never lands in nobody's inbox.
        team = User.query.filter(User.role == "Admin", User.is_active.is_(True),
                                 db.or_(User.department == "Management",
                                        User.department.is_(None))).all()
    unit = f", {ticket.unit.unit_number}" if ticket.unit else ""
    body = (f"EMERGENCY maintenance request {ticket.ticket_number}\n\n"
            f"{ticket.title}\n"
            f"Property: {ticket.prop.name if ticket.prop else '-'}{unit}\n"
            f"Reported by: {ticket.tenant.name if ticket.tenant else '-'}\n\n"
            f"{ticket.description or ''}\n\n"
            f"Respond within 1-4 hours. Open: {url_for('service', _external=True)}")
    _post_system_message(sender_id, team,
                         f"EMERGENCY: {ticket.title} ({ticket.ticket_number})", body,
                         "email/maintenance_notice.html", ticket=ticket,
                         heading="Emergency maintenance request")
    return len(team)


@app.route("/service/submit", methods=["POST"])
@login_required
def submit_service():
    choices = Ticket.CLASSES if _can_classify() else Ticket.TENANT_CLASSES
    picked  = request.form.get("issue_type", "")
    if picked not in choices:
        flash("Please select the type of issue.", "danger")
        return redirect(url_for("service"))

    new_ticket = Ticket(
        ticket_number=f"SR-{str(uuid.uuid4().hex[:6]).upper()}",
        title=request.form.get("title", "New Request"),
        description=request.form.get("description", ""),
        category=request.form.get("category", "General"),
        status="open",
        tenant_id=session["user_id"],
        property_id=request.form.get("property"),
        date_raised=datetime.utcnow(),
        # Staff-only classes are not a tenant choice; record the closest one.
        tenant_category=picked if picked in Ticket.TENANT_CLASSES else "routine",
    )
    new_ticket.apply_classification(picked)

    # A tenant's request belongs to the unit they rent in that property.
    if session.get("role") == "Tenant":
        tenancy = Tenancy.query.filter_by(tenant_id=session["user_id"], is_active=True).first()
        if tenancy and tenancy.unit and str(tenancy.unit.property_id) == str(new_ticket.property_id):
            new_ticket.unit_id = tenancy.unit_id

    if picked == "scheduled":
        try:
            new_ticket.scheduled_for = datetime.strptime(
                request.form.get("scheduled_for", ""), "%Y-%m-%d").date()
        except ValueError:
            flash("Pick a date for the scheduled task.", "danger")
            return redirect(url_for("service"))

    db.session.add(new_ticket)
    db.session.flush()      # need the id before attaching files

    saved, errors = save_attachments(
        request.files.getlist("attachments"),
        Attachment.PARENT_TICKET, new_ticket.id,
        kind=Attachment.KIND_ATTACHMENT)

    notified = 0
    if picked == "emergency":
        notified = _notify_maintenance_team(new_ticket, session["user_id"])
    db.session.commit()

    for e in errors:
        flash(e, "danger")
    msg = "Service request submitted successfully!"
    if saved:
        msg += f" {saved} photo/file(s) attached."
    if picked == "emergency":
        msg += " The maintenance team has been alerted."
        app.logger.info("[service] emergency %s alerted %d staff",
                        new_ticket.ticket_number, notified)
    flash(msg, "success")
    return redirect(url_for("service"))


@app.route("/service/assign/<int:ticket_id>", methods=["POST"])
@dept_required("edit_maintenance", "log_complaints")
def assign_ticket(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    technician = request.form.get("technician", "").strip()
    if not technician:
        flash("Choose a technician to assign.", "danger")
        return redirect(url_for("service"))

    ticket.technician = technician
    ticket.status = "in-progress"
    db.session.commit()

    flash(f"Ticket #{ticket.ticket_number} assigned to {technician}.", "success")
    return redirect(url_for("service"))


@app.route("/service/<int:ticket_id>/classify", methods=["POST"])
@dept_required("edit_maintenance", "log_complaints")
def classify_ticket(ticket_id):
    """Staff reclassification, with the follow-up each class asks for."""
    ticket = Ticket.query.get_or_404(ticket_id)
    key = request.form.get("classification", "")
    if key not in Ticket.CLASSES:
        flash("Unknown classification.", "danger")
        return redirect(url_for("service"))
    meta = Ticket.CLASSES[key]
    was  = ticket.classification

    if key == "tenant_responsibility":
        try:
            estimate = float(request.form.get("charge_estimate", ""))
        except ValueError:
            estimate = -1
        reason = request.form.get("charge_reason", "").strip()
        if estimate < 0 or not reason:
            flash("Tenant Responsibility needs a charge estimate and a reason.", "danger")
            return redirect(url_for("service", open=ticket.id))
        ticket.charge_estimate = estimate
        ticket.charge_reason   = reason[:300]

    elif key == "scheduled":
        try:
            ticket.scheduled_for = datetime.strptime(
                request.form.get("scheduled_for", ""), "%Y-%m-%d").date()
        except ValueError:
            flash("Pick a date to put this on the preventive maintenance calendar.", "danger")
            return redirect(url_for("service", open=ticket.id))

    elif key == "landlord_approval":
        landlord = ticket.prop.landlord if ticket.prop else None
        if not landlord:
            flash(f"{ticket.prop.name if ticket.prop else 'This property'} has no "
                  "landlord on record to send the approval request to.", "danger")
            return redirect(url_for("service", open=ticket.id))
        note = request.form.get("approval_note", "").strip()
        ticket.approval_status       = "pending"
        ticket.approval_note         = note or None
        ticket.approval_requested_at = datetime.utcnow()
        ticket.approval_decided_at   = None
        body = (f"Your approval is needed for maintenance request {ticket.ticket_number} "
                f"at {ticket.prop.name}.\n\n{ticket.title}\n{ticket.description or ''}\n\n"
                + (f"Note from our team: {note}\n\n" if note else "")
                + f"Approve or decline here: {url_for('service', _external=True, open=ticket.id)}")
        _post_system_message(session["user_id"], [landlord],
                             f"Approval needed: {ticket.title} ({ticket.ticket_number})",
                             body, "email/maintenance_notice.html", ticket=ticket,
                             heading="Landlord approval required")

    ticket.apply_classification(key)
    ticket.classified_by_id = session["user_id"]
    ticket.classified_at    = datetime.utcnow()

    if key == "emergency" and was != "emergency":
        _notify_maintenance_team(ticket, session["user_id"])

    db.session.commit()

    follow_up = {
        "emergency":             " The maintenance team has been alerted.",
        "scheduled":             f" Added to the preventive maintenance calendar for "
                                 f"{ticket.scheduled_for:%b %d, %Y}." if ticket.scheduled_for else "",
        "tenant_responsibility": " Charge estimate recorded.",
        "landlord_approval":     " Approval request sent to the landlord.",
    }.get(key, "")
    flash(f"{ticket.ticket_number} classified as {meta['label']}.{follow_up}", "success")
    return redirect(url_for("service", open=ticket.id))


@app.route("/service/<int:ticket_id>/approval", methods=["POST"])
@role_required("Landlord", "Admin")
def decide_approval(ticket_id):
    """The landlord approves or declines a Landlord Approval Required request."""
    ticket = Ticket.query.get_or_404(ticket_id)
    is_owner = ticket.prop and ticket.prop.landlord_id == session["user_id"]
    # Management may record a decision the landlord gave by phone or email.
    if not (is_owner or has_perm("manage_properties")):
        flash("Only the property's landlord can approve this request.", "danger")
        return redirect(url_for("service"))
    if ticket.classification != "landlord_approval" or ticket.approval_status != "pending":
        flash("This request is not waiting for approval.", "info")
        return redirect(url_for("service"))

    decision = request.form.get("decision")
    if decision not in ("approved", "rejected"):
        flash("Choose approve or decline.", "danger")
        return redirect(url_for("service", open=ticket.id))

    note = request.form.get("note", "").strip()
    ticket.approval_status     = decision
    ticket.approval_decided_at = datetime.utcnow()
    if note:
        ticket.approval_note = ((ticket.approval_note + "\n\n") if ticket.approval_note else "") \
                               + f"Landlord: {note}"

    # Tell whoever asked for the approval.
    if ticket.classified_by and ticket.classified_by.id != session["user_id"]:
        verdict = "approved" if decision == "approved" else "declined"
        _post_system_message(
            session["user_id"], [ticket.classified_by],
            f"Landlord {verdict}: {ticket.title} ({ticket.ticket_number})",
            f"{session.get('user')} {verdict} maintenance request {ticket.ticket_number}."
            + (f"\n\n{note}" if note else ""))
    db.session.commit()

    flash(f"{ticket.ticket_number} {'approved' if decision == 'approved' else 'declined'}.",
          "success")
    return redirect(url_for("service", open=ticket.id))


@app.route("/service/calendar")
@role_required("Admin", "Landlord")
def maintenance_calendar():
    """Preventive maintenance calendar: every Scheduled request, by month."""
    import calendar as _cal
    if session["role"] == "Admin" and not (has_perm("view_maintenance_full")
                                           or has_perm("view_maintenance_status")):
        flash("Access denied. Your department cannot view maintenance job details.", "danger")
        return redirect(url_for("dashboard"))

    today = date.today()
    try:
        year, month = (int(x) for x in request.args.get("month", "").split("-"))
        date(year, month, 1)
    except (ValueError, TypeError):
        year, month = today.year, today.month

    first = date(year, month, 1)
    last  = date(year, month, _cal.monthrange(year, month)[1])
    q = Ticket.query.filter(Ticket.classification == "scheduled",
                            Ticket.scheduled_for >= first, Ticket.scheduled_for <= last)
    if session["role"] == "Landlord":
        q = q.join(Property, Ticket.property_id == Property.id).filter(
            Property.landlord_id == session["user_id"])
    by_day = {}
    for t in q.order_by(Ticket.scheduled_for).all():
        by_day.setdefault(t.scheduled_for.day, []).append(t)

    prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
    next_m = (last + timedelta(days=1)).strftime("%Y-%m")
    return render_template("maintenance_calendar.html",
        weeks=_cal.Calendar(firstweekday=0).monthdayscalendar(year, month),
        by_day=by_day, month_label=first.strftime("%B %Y"),
        prev_month=prev_m, next_month=next_m,
        today=today if (today.year, today.month) == (year, month) else None,
        unscheduled=Ticket.query.filter(Ticket.classification == "scheduled",
                                        Ticket.scheduled_for.is_(None)).count(),
        role=session["role"],
    )


@app.route("/service/resolve/<int:ticket_id>", methods=["POST"])
@role_required("Admin", "Landlord")
def resolve_ticket(ticket_id):
    # Closing maintenance jobs: Maintenance dept + Management (landlords keep
    # their existing ability to resolve tickets on their own properties).
    if session.get("role") == "Admin" and not has_perm("edit_maintenance"):
        flash("Access denied. Only Maintenance or Management can close jobs.", "danger")
        return redirect(url_for("service"))

    ticket = Ticket.query.get_or_404(ticket_id)
    if session.get("role") == "Landlord" and ticket.prop and ticket.prop.landlord_id != session["user_id"]:
        flash("Access denied. You can only resolve tickets on your own properties.", "danger")
        return redirect(url_for("service"))

    note  = request.form.get("completion_note", "").strip()
    files = request.files.getlist("proof")

    saved, errors = save_attachments(
        files, Attachment.PARENT_TICKET, ticket.id,
        kind=Attachment.KIND_PROOF, caption=note or None)
    for e in errors:
        flash(e, "danger")

    # Refuse to close a job with no evidence at all - that is the point of the
    # proof-of-completion attachment.
    already = attachments_for(Attachment.PARENT_TICKET, ticket.id,
                              kind=Attachment.KIND_PROOF)
    if not saved and not already:
        flash("Attach a photo or document as proof of completion before closing "
              f"#{ticket.ticket_number}.", "danger")
        db.session.rollback()
        return redirect(url_for("service"))

    ticket.status = "resolved"
    ticket.date_resolved = datetime.utcnow()
    db.session.commit()
    flash(f"Ticket #{ticket.ticket_number} marked as resolved"
          + (f" with {saved} proof file(s)." if saved else "."), "success")
    return redirect(url_for("service"))


@app.route("/service/rate/<int:ticket_id>", methods=["POST"])
@role_required("Tenant")
def rate_technician(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)

    if ticket.tenant_id != session["user_id"]:
        flash("Access denied. You can only rate your own service requests.", "danger")
        return redirect(url_for("service"))
    if ticket.status != "resolved":
        flash("You can only rate a technician after the job is resolved.", "warning")
        return redirect(url_for("service"))
    if not ticket.technician:
        flash("This ticket has no technician assigned to rate.", "warning")
        return redirect(url_for("service"))
    if TechnicianRating.query.filter_by(ticket_id=ticket.id).first():
        flash("You have already rated this service request.", "info")
        return redirect(url_for("service"))

    try:
        stars = int(request.form.get("stars", 0))
    except (TypeError, ValueError):
        stars = 0
    if stars < 1 or stars > 5:
        flash("Please select a rating between 1 and 5 stars.", "danger")
        return redirect(url_for("service"))

    comment = request.form.get("comment", "").strip()[:1000]

    db.session.add(TechnicianRating(
        ticket_id=ticket.id,
        tenant_id=session["user_id"],
        technician=ticket.technician,
        stars=stars,
        comment=comment or None,
    ))
    db.session.commit()
    flash(f"Thank you! Your {stars}-star rating for {ticket.technician} has been recorded.", "success")
    return redirect(url_for("service"))


# Finance routes are handled by finance_routes.py

@app.errorhandler(413)
def _file_too_large(e):
    """Werkzeug aborts oversized requests before the route runs."""
    flash("That file is too large. The maximum upload size is 10 MB.", "danger")
    return redirect(request.referrer or url_for("dashboard"))


# ─────────────────────────────────────────────
#  DOCUMENTS
# ─────────────────────────────────────────────

DOC_TYPES = [
    ("rental_application", "Rental Application"),
    ("tenancy_agreement",  "Tenancy Agreement"),
    ("id_document",        "ID Document"),
    ("payment_proof",      "Payment Proof"),
    ("other",              "Other"),
]

# Documents every active tenant is expected to have on file.
REQUIRED_DOC_TYPES = ["tenancy_agreement", "id_document"]


class _Row:
    """Lightweight attribute bag so Jinja can use dot access."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _can_manage_documents():
    return session.get("role") == "Admin" and has_perm("edit_contacts")


def _doc_owner_or_staff(doc):
    """A document is reachable by the person it belongs to, or by staff
    whose department grants full tenant access."""
    if session.get("user_id") == doc.tenant_id:
        return True
    return session.get("role") == "Admin" and has_perm("view_tenant_full")


def _document_stats(docs):
    by_type = {key: 0 for key, _ in DOC_TYPES}
    total_bytes = 0
    for d in docs:
        by_type[d.doc_type] = by_type.get(d.doc_type, 0) + 1
        total_bytes += d.file_size or 0
    if total_bytes < 1024:
        size_str = f"{total_bytes} B"
    elif total_bytes < 1024 * 1024:
        size_str = f"{total_bytes / 1024:.1f} KB"
    else:
        size_str = f"{total_bytes / (1024 * 1024):.1f} MB"
    missing = [label for key, label in DOC_TYPES
               if key in REQUIRED_DOC_TYPES and not by_type.get(key)]
    return _Row(count=len(docs), size_str=size_str, by_type=by_type, missing=missing)


@app.route("/documents")
@login_required
def documents():
    """Staff browse every tenant's file; tenants and landlords see their own."""
    role      = session["role"]
    q         = request.args.get("q", "").lower()
    doc_type  = request.args.get("doc_type", "")
    tenant_id = request.args.get("tenant_id", type=int)

    # ── Self-service view for tenants and landlords ──────────────────
    if role != "Admin":
        me = User.query.get_or_404(session["user_id"])
        own = (TenantDocument.query
               .filter_by(tenant_id=me.id)
               .order_by(TenantDocument.uploaded_at.desc())
               .all())
        stats = _document_stats(own)

        shown = own
        if doc_type:
            shown = [d for d in shown if d.doc_type == doc_type]
        if q:
            shown = [d for d in shown
                     if q in (d.original_filename or "").lower()
                     or q in (d.label or "").lower()]

        active = Tenancy.query.filter_by(tenant_id=me.id, is_active=True).first()
        return render_template(
            "documents.html",
            self_mode=True,
            me=me,
            selected_tenant=_Row(
                id=me.id, name=me.name, email=me.email,
                property_name=active.unit.prop.name if active else "—",
                unit_number=active.unit.unit_number if active else "—",
            ),
            tenants=[], documents=shown, stats=stats,
            doc_types=DOC_TYPES, doc_type=doc_type, q=q,
            can_manage=False, role=role,
        )

    # ── Staff view ───────────────────────────────────────────────────
    if not has_perm("view_tenant_full"):
        flash(f"Access denied. The {current_dept()} department cannot view tenant documents.",
              "danger")
        return redirect(url_for("dashboard"))

    tenant_users = User.query.filter_by(role="Tenant").order_by(User.name).all()
    if q:
        tenant_users = [u for u in tenant_users
                        if q in u.name.lower() or q in (u.email or "").lower()]

    # One grouped count query instead of a COUNT per tenant.
    counts = dict(
        db.session.query(TenantDocument.tenant_id, func.count(TenantDocument.id))
        .group_by(TenantDocument.tenant_id).all()
    )

    tenant_list = []
    for u in tenant_users:
        active = Tenancy.query.filter_by(tenant_id=u.id, is_active=True).first()
        tenant_list.append(_Row(
            id=u.id, name=u.name, email=u.email,
            property_name=active.unit.prop.name if active else "—",
            unit_number=active.unit.unit_number if active else "—",
            doc_count=counts.get(u.id, 0),
        ))

    selected_tenant = None
    documents_list  = []
    stats           = None

    if tenant_id:
        user = User.query.get_or_404(tenant_id)
        if user.role != "Tenant":
            abort(403)

        active = Tenancy.query.filter_by(tenant_id=user.id, is_active=True).first()
        selected_tenant = _Row(
            id=user.id, name=user.name, email=user.email,
            property_name=active.unit.prop.name if active else "—",
            unit_number=active.unit.unit_number if active else "—",
        )

        all_docs = (TenantDocument.query
                    .filter_by(tenant_id=tenant_id)
                    .order_by(TenantDocument.uploaded_at.desc())
                    .all())
        stats = _document_stats(all_docs)

        documents_list = all_docs
        if doc_type:
            documents_list = [d for d in documents_list if d.doc_type == doc_type]

    return render_template(
        "documents.html",
        self_mode=False,
        tenants=tenant_list,
        selected_tenant=selected_tenant,
        documents=documents_list,
        stats=stats,
        doc_types=DOC_TYPES,
        doc_type=doc_type,
        q=q,
        can_manage=_can_manage_documents(),
        role=role,
    )


# ── Upload a document for a tenant ─────────────────────────────────
@app.route("/documents/<int:tenant_id>/upload", methods=["POST"])
@dept_required("edit_contacts")
def upload_document(tenant_id):
    user = User.query.get_or_404(tenant_id)
    if user.role != "Tenant":
        abort(403)
 
    if "file" not in request.files:
        flash("No file selected.", "danger")
        return redirect(url_for("documents", tenant_id=tenant_id))
 
    file = request.files["file"]
    if file.filename == "":
        flash("No file selected.", "danger")
        return redirect(url_for("documents", tenant_id=tenant_id))
 
    if not allowed_document_file(file.filename):
        flash("File type not allowed. Use PDF, JPG, or PNG.", "danger")
        return redirect(url_for("documents", tenant_id=tenant_id))
 
    # Read into memory to check size
    file_bytes = file.read()
    if len(file_bytes) > MAX_DOC_BYTES:
        flash("File is too large. Maximum size is 10 MB.", "danger")
        return redirect(url_for("documents", tenant_id=tenant_id))
 
    original_filename = secure_filename(file.filename)
    ext = original_filename.rsplit(".", 1)[1].lower()
    stored_filename = f"{uuid.uuid4().hex}.{ext}"
 
    upload_folder = app.config.get("DOCS_UPLOAD_FOLDER",
                                   os.path.join(os.path.dirname(__file__), "uploads", "documents"))
    os.makedirs(upload_folder, exist_ok=True)
 
    dest = os.path.join(upload_folder, stored_filename)
    with open(dest, "wb") as f:
        f.write(file_bytes)
 
    mime_type, _ = mimetypes.guess_type(original_filename)
 
    doc = TenantDocument(
        tenant_id=tenant_id,
        doc_type=request.form.get("doc_type", "other"),
        original_filename=original_filename,
        stored_filename=stored_filename,
        label=request.form.get("label", "").strip() or None,
        file_size=len(file_bytes),
        mime_type=mime_type or "application/octet-stream",
    )
    db.session.add(doc)
    db.session.commit()
 
    flash(f"Document '{original_filename}' uploaded successfully.", "success")
    return redirect(url_for("documents", tenant_id=tenant_id))
 
 
# ── View (inline in browser) ────────────────────────────────────────
@app.route("/documents/view/<int:doc_id>")
@login_required
def view_document(doc_id):
    doc = TenantDocument.query.get_or_404(doc_id)
    if not _doc_owner_or_staff(doc):
        abort(403)
    upload_folder = app.config.get("DOCS_UPLOAD_FOLDER",
                                   os.path.join(os.path.dirname(__file__), "uploads", "documents"))
    path = os.path.join(upload_folder, doc.stored_filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype=doc.mime_type, as_attachment=False,
                     download_name=doc.original_filename)
 
 
# ── Download (force download) ────────────────────────────────────────
@app.route("/documents/download/<int:doc_id>")
@login_required
def download_document(doc_id):
    doc = TenantDocument.query.get_or_404(doc_id)
    if not _doc_owner_or_staff(doc):
        abort(403)
    upload_folder = app.config.get("DOCS_UPLOAD_FOLDER",
                                   os.path.join(os.path.dirname(__file__), "uploads", "documents"))
    path = os.path.join(upload_folder, doc.stored_filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype=doc.mime_type, as_attachment=True,
                     download_name=doc.original_filename)
 
 
# ── Download all docs for a tenant as ZIP ───────────────────────────
@app.route("/documents/<int:tenant_id>/download-all")
@login_required
def download_all_documents(tenant_id):
    if session.get("user_id") != tenant_id and not (
            session.get("role") == "Admin" and has_perm("view_tenant_full")):
        abort(403)
    user = User.query.get_or_404(tenant_id)
    docs = TenantDocument.query.filter_by(tenant_id=tenant_id).all()
 
    if not docs:
        flash("No documents to download.", "info")
        return redirect(url_for("documents", tenant_id=tenant_id))
 
    upload_folder = app.config.get("DOCS_UPLOAD_FOLDER",
                                   os.path.join(os.path.dirname(__file__), "uploads", "documents"))
 
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in docs:
            path = os.path.join(upload_folder, doc.stored_filename)
            if os.path.exists(path):
                arcname = f"{doc.doc_type}/{doc.original_filename}"
                zf.write(path, arcname)
 
    zip_buffer.seek(0)
    safe_name = secure_filename(user.name.replace(" ", "_"))
    return send_file(zip_buffer, mimetype="application/zip", as_attachment=True,
                     download_name=f"{safe_name}_documents.zip")
 
 
# ── Delete a document ────────────────────────────────────────────────
@app.route("/documents/delete/<int:doc_id>", methods=["POST"])
@dept_required("edit_contacts")
def delete_document(doc_id):
    doc = TenantDocument.query.get_or_404(doc_id)
    tenant_id = doc.tenant_id
 
    upload_folder = app.config.get("DOCS_UPLOAD_FOLDER",
                                   os.path.join(os.path.dirname(__file__), "uploads", "documents"))
    path = os.path.join(upload_folder, doc.stored_filename)
    if os.path.exists(path):
        os.remove(path)
 
    db.session.delete(doc)
    db.session.commit()
 
    flash("Document deleted.", "success")
    return redirect(url_for("documents", tenant_id=tenant_id))


# ─────────────────────────────────────────────
#  ATTACHMENTS  (messages + service requests)
# ─────────────────────────────────────────────
ATTACH_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "webp", "gif"}
MAX_ATTACH_BYTES  = 10 * 1024 * 1024   # 10 MB, same ceiling as documents
MAX_ATTACH_FILES  = 5                  # per message / per upload


def allowed_attachment_file(filename):
    return _has_ext(filename, ATTACH_EXTENSIONS)


def _attach_folder():
    folder = app.config.get(
        "ATTACH_UPLOAD_FOLDER",
        os.path.join(os.path.dirname(app.config["DOCS_UPLOAD_FOLDER"]), "attachments"))
    os.makedirs(folder, exist_ok=True)
    return folder


def save_attachments(files, parent_type, parent_id, kind=None, caption=None):
    """Persist uploaded files against a parent. Returns (saved, [errors])."""
    kind   = kind or Attachment.KIND_ATTACHMENT
    folder = _attach_folder()
    saved, errors = 0, []

    for f in (files or [])[:MAX_ATTACH_FILES]:
        if not f or not f.filename:
            continue
        if not allowed_attachment_file(f.filename):
            errors.append(f"{f.filename}: only PDF, JPG, PNG, WEBP and GIF are allowed.")
            continue

        data = f.read()
        if len(data) > MAX_ATTACH_BYTES:
            errors.append(f"{f.filename}: larger than 10 MB.")
            continue
        if not data:
            continue

        original = secure_filename(f.filename)
        ext      = original.rsplit(".", 1)[1].lower()
        stored   = f"{uuid.uuid4().hex}.{ext}"
        with open(os.path.join(folder, stored), "wb") as out:
            out.write(data)

        mime, _ = mimetypes.guess_type(original)
        db.session.add(Attachment(
            parent_type=parent_type, parent_id=parent_id, kind=kind,
            original_filename=original, stored_filename=stored,
            file_size=len(data), mime_type=mime or "application/octet-stream",
            caption=(caption or None),
            uploaded_by_id=session.get("user_id"),
        ))
        saved += 1

    return saved, errors


def attachments_for(parent_type, parent_id, kind=None):
    q = Attachment.query.filter_by(parent_type=parent_type, parent_id=parent_id)
    if kind:
        q = q.filter_by(kind=kind)
    return q.order_by(Attachment.uploaded_at.asc()).all()


app.jinja_env.globals["attachments_for"] = attachments_for


def _can_reach_attachment(att):
    """Only people who can see the parent record may see its files."""
    uid  = session.get("user_id")
    role = session.get("role")
    if not uid:
        return False

    if att.parent_type == Attachment.PARENT_MESSAGE:
        msg = Message.query.get(att.parent_id)
        if not msg:
            return False
        # A file on a reply belongs to everyone in that thread.
        root = Message.query.get(msg.parent_id) if msg.parent_id else msg
        parties = {msg.sender_id, msg.recipient_id}
        if root:
            parties |= {root.sender_id, root.recipient_id}
        return uid in parties

    if att.parent_type == Attachment.PARENT_TICKET:
        tkt = Ticket.query.get(att.parent_id)
        if not tkt:
            return False
        if tkt.tenant_id == uid:
            return True
        if role == "Admin":
            return has_perm("view_maintenance_full") or has_perm("view_maintenance_status")
        if role == "Landlord":
            return tkt.prop is not None and tkt.prop.landlord_id == uid
        return False

    return False


@app.route("/attachments/<int:att_id>")
@login_required
def view_attachment(att_id):
    att = Attachment.query.get_or_404(att_id)
    if not _can_reach_attachment(att):
        abort(403)
    path = os.path.join(_attach_folder(), att.stored_filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype=att.mime_type, as_attachment=False,
                     download_name=att.original_filename)


@app.route("/attachments/<int:att_id>/download")
@login_required
def download_attachment(att_id):
    att = Attachment.query.get_or_404(att_id)
    if not _can_reach_attachment(att):
        abort(403)
    path = os.path.join(_attach_folder(), att.stored_filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype=att.mime_type, as_attachment=True,
                     download_name=att.original_filename)


@app.route("/attachments/<int:att_id>/delete", methods=["POST"])
@login_required
def delete_attachment(att_id):
    att = Attachment.query.get_or_404(att_id)
    # Only the uploader, or Management, can remove a file.
    if att.uploaded_by_id != session.get("user_id") and not (
            session.get("role") == "Admin" and has_perm("manage_properties")):
        abort(403)

    path = os.path.join(_attach_folder(), att.stored_filename)
    if os.path.exists(path):
        os.remove(path)
    db.session.delete(att)
    db.session.commit()
    flash("Attachment removed.", "success")
    return redirect(request.referrer or url_for("dashboard"))


# ─────────────────────────────────────────────
#  SETTINGS
# ─────────────────────────────────────────────
@app.route("/settings")
@login_required
def settings():
    logs = []
    if session.get("role") == "Admin":
        logs = (NotificationLog.query
                .order_by(NotificationLog.sent_at.desc())
                .limit(50).all())
    return render_template("settings.html", role=session["role"],
                           default_commission_pct=app.config.get("DEFAULT_COMMISSION_PCT", 0.0),
                           commission_properties=Property.query.order_by(Property.name).all(),
                           email_cfg={
                               "MAIL_SERVER":         app.config.get("MAIL_SERVER", ""),
                               "MAIL_PORT":           app.config.get("MAIL_PORT", 587),
                               "MAIL_USE_TLS":        app.config.get("MAIL_USE_TLS", True),
                               "MAIL_USERNAME":       app.config.get("MAIL_USERNAME", ""),
                               "MAIL_DEFAULT_SENDER": app.config.get("MAIL_DEFAULT_SENDER", ""),
                           },
                           paystack_cfg={
                               "PAYSTACK_PUBLIC_KEY": app.config.get("PAYSTACK_PUBLIC_KEY", ""),
                               "PAYSTACK_SECRET_KEY": app.config.get("PAYSTACK_SECRET_KEY", ""),
                           },
                           bank_cfg={
                               "BANK_NAME":           app.config.get("BANK_NAME", ""),
                               "BANK_ACCOUNT_NUMBER": app.config.get("BANK_ACCOUNT_NUMBER", ""),
                               "BANK_ACCOUNT_NAME":   app.config.get("BANK_ACCOUNT_NAME", ""),
                               "BANK_TRANSFER_NOTE":  app.config.get("BANK_TRANSFER_NOTE", ""),
                           },
                           notification_logs=logs)


@app.route("/settings/update", methods=["POST"])
@login_required
def update_settings():
    flash("Settings updated successfully!", "success")
    return redirect(url_for("settings"))


@app.route("/settings/email", methods=["POST"])
@dept_required("manage_settings")
def save_email_config():
    cfg = {
        "MAIL_SERVER":         request.form.get("mail_server", "").strip(),
        "MAIL_PORT":           int(request.form.get("mail_port", 587) or 587),
        "MAIL_USE_TLS":        request.form.get("mail_tls") == "1",
        "MAIL_USERNAME":       request.form.get("mail_username", "").strip(),
        "MAIL_PASSWORD":       request.form.get("mail_password", "").strip(),
        "MAIL_DEFAULT_SENDER": request.form.get("mail_sender", "").strip(),
    }
    # Don't overwrite password if left blank (user keeping existing)
    existing_pw = app.config.get("MAIL_PASSWORD", "")
    if not cfg["MAIL_PASSWORD"] and existing_pw:
        cfg["MAIL_PASSWORD"] = existing_pw

    for k, v in cfg.items():
        app.config[k] = v

    with open(EMAIL_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

    # Re-init mail with new config
    mail.init_app(app)
    flash("Email configuration saved successfully.", "success")
    return redirect(url_for("settings") + "#email")


@app.route("/settings/paystack", methods=["POST"])
@dept_required("manage_settings")
def save_paystack_config():
    pub  = request.form.get("paystack_public_key", "").strip()
    sec  = request.form.get("paystack_secret_key", "").strip()

    existing_sec = app.config.get("PAYSTACK_SECRET_KEY", "")
    if not sec and existing_sec:
        sec = existing_sec

    cfg = {"PAYSTACK_PUBLIC_KEY": pub, "PAYSTACK_SECRET_KEY": sec}
    for k, v in cfg.items():
        app.config[k] = v

    with open(PAYSTACK_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

    flash("Paystack keys saved successfully.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/bank", methods=["POST"])
@dept_required("manage_settings")
def save_bank_config():
    cfg = {
        "BANK_NAME":           request.form.get("bank_name", "").strip(),
        "BANK_ACCOUNT_NUMBER": request.form.get("bank_account_number", "").strip(),
        "BANK_ACCOUNT_NAME":   request.form.get("bank_account_name", "").strip(),
        "BANK_TRANSFER_NOTE":  request.form.get("bank_transfer_note", "").strip(),
    }
    for k, v in cfg.items():
        app.config[k] = v
    with open(BANK_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    flash("Bank account details saved.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/commission", methods=["POST"])
@dept_required("manage_settings")
def save_commission_config():
    raw = request.form.get("default_commission_pct", "").strip()
    try:
        pct = max(0.0, min(100.0, float(raw or 0)))
    except ValueError:
        flash("Commission must be a number between 0 and 100.", "danger")
        return redirect(url_for("settings"))

    app.config["DEFAULT_COMMISSION_PCT"] = pct
    with open(COMPANY_CONFIG_FILE, "w") as f:
        json.dump({"DEFAULT_COMMISSION_PCT": pct}, f, indent=2)

    inheriting = Property.query.filter(Property.commission_pct.is_(None)).count()
    flash(f"Default commission set to {pct:g}%. {inheriting} propert"
          f"{'y' if inheriting == 1 else 'ies'} use this default.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/email/test", methods=["POST"])
@dept_required("manage_settings")
def test_email():
    to = request.form.get("test_recipient", "").strip() or session.get("email")
    if not to:
        flash("No recipient email address provided.", "danger")
        return redirect(url_for("settings") + "#email")
    ok = send_test_email(app, to)
    if ok:
        flash(f"Test email sent to {to} — check your inbox.", "success")
    else:
        flash("Failed to send test email. Check your SMTP settings and try again.", "danger")
    return redirect(url_for("settings") + "#email")


@app.route("/admin/run-jobs", methods=["POST"])
@dept_required("manage_settings")
def run_jobs_now():
    from scheduler import run_all_jobs
    try:
        run_all_jobs(app)
        flash("All scheduler jobs ran successfully (overdue check, monthly invoices, lease expiry).", "success")
    except Exception as exc:
        flash(f"Scheduler error: {exc}", "danger")
    return redirect(url_for("settings") + "#email")


# ─────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)