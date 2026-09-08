"""
HS Property Management — Flask Application
"""
import os
import json
import uuid
from datetime import datetime, date
from functools import wraps
from collections import OrderedDict
from flask import (Flask, render_template, redirect, url_for, Response,
                   session, request, flash, jsonify, send_file, abort)
from werkzeug.utils import secure_filename
from sqlalchemy import func
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
from mailer import mail, send_test_email
from models import db, User, Property, PropertyImage, Unit, Tenancy, Invoice, Transaction, Ticket, TenantDocument, RentLedger, Expense, LandlordPayout, NotificationLog, BankTransferClaim, Message, TechnicianRating

# ─────────────────────────────────────────────
#  APP CONFIG
# ─────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = "hs-property-secret-2025"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config["SQLALCHEMY_DATABASE_URI"]        = f"sqlite:///{os.path.join(BASE_DIR, 'hs_property.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"]                  = os.path.join(BASE_DIR, "static", "uploads", "properties")
app.config["MAX_CONTENT_LENGTH"]             = 8 * 1024 * 1024   # 8 MB
app.config["DOCS_UPLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "uploads", "documents")
os.makedirs(app.config["DOCS_UPLOAD_FOLDER"], exist_ok=True)

# ── Email config (persisted in email_config.json) ─────────────────────
EMAIL_CONFIG_FILE = os.path.join(BASE_DIR, "email_config.json")

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

# ── Paystack config (persisted in paystack_config.json) ───────────────
PAYSTACK_CONFIG_FILE = os.path.join(BASE_DIR, "paystack_config.json")

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

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}

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

if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    _start_scheduler()


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def fmt(n):
    return f"₦{n:,.0f}"

app.jinja_env.globals["fmt"] = fmt


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_property_image(file_obj):
    """Save uploaded image; return URL path or None."""
    if file_obj and file_obj.filename and allowed_file(file_obj.filename):
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

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}
 
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
 

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
        if user.password != password:
            flash("Incorrect password. Please try again.", "danger")
            return render_template("login.html")
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
    urgent_t  = sum(1 for t in tickets if t.priority == "urgent" and t.status != "resolved")

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
                    "rent":   active.monthly_rent,
                    "due":    inv.due_date.strftime("%b %d, %Y") if inv else "—",
                    "status": inv.status if inv else "—",
                })

    return render_template("properties.html",
        properties=props,
        tenants_by_prop=tenants_by_prop,
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
    num_units   = int(request.form.get("units", 0) or 0)
    unit_rent   = float(request.form.get("unit_rent", 0) or 0)

    prop = Property(
        name=name, address=address, type=prop_type,
        description=description, avg_rent=unit_rent,
        landlord_id=session["user_id"],
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

    # Auto-generate units
    for i in range(1, num_units + 1):
        unit_num = f"U{i:02d}"
        db.session.add(Unit(
            unit_number=unit_num, rent_amount=unit_rent,
            is_occupied=False, property_id=prop.id,
        ))

    db.session.commit()
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
    prop.name        = request.form.get("name", prop.name).strip()
    prop.address     = request.form.get("address", prop.address).strip()
    prop.type        = request.form.get("type", prop.type)
    prop.description = request.form.get("description", prop.description).strip()
    prop.avg_rent    = float(request.form.get("avg_rent", prop.avg_rent) or prop.avg_rent)

    if "image" in request.files:
        new_url = save_property_image(request.files["image"])
        if new_url:
            prop.image_url = new_url

    ext_url = request.form.get("image_url", "").strip()
    if ext_url:
        prop.image_url = ext_url

    db.session.commit()
    flash(f"Property '{prop.name}' updated.", "success")
    return redirect(url_for("properties"))


@app.route("/properties/<int:prop_id>/delete", methods=["POST"])
@dept_required("manage_properties")
def delete_property(prop_id):
    prop = Property.query.get_or_404(prop_id)
    name = prop.name
    db.session.delete(prop)
    db.session.commit()
    flash(f"Property '{name}' deleted.", "success")
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
            "monthly_rent": active.monthly_rent if active else 0,
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
        password=password, # Note: In production, hash this using werkzeug.security
        is_active=True
    )
    db.session.add(new_user)
    db.session.flush() # Flush to get the new_user.id

    # If role is Tenant, handle optional tenancy assignment
    if role == "Tenant":
        property_id = request.form.get("property_id")
        unit_id = request.form.get("unit_id")
        monthly_rent = request.form.get("monthly_rent")

        if property_id and unit_id and monthly_rent:
            unit = Unit.query.filter_by(id=unit_id, property_id=property_id, is_occupied=False).first()
            if unit:
                unit.is_occupied = True
                new_tenancy = Tenancy(
                    tenant_id=new_user.id,
                    unit_id=unit.id,
                    monthly_rent=float(monthly_rent),
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
    monthly_rent = request.form.get("monthly_rent")
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
        user.password = password
        # user.password = generate_password_hash(password)  # recommended for production

    # Handle tenancy updates — rent/unit assignment is Management-only.
    # Customer Service edits stop at contact details; skipping this block also
    # prevents a contact-only edit from silently closing an active tenancy.
    if not is_management:
        db.session.commit()
        flash(f"Contact details for {user.name} updated successfully!", "success")
        return redirect(url_for("tenants"))

    active_tenancy = Tenancy.query.filter_by(tenant_id=user.id, is_active=True).first()

    if role == "Tenant" and property_id and unit_id and monthly_rent:
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
                monthly_rent=float(monthly_rent),
                is_active=True,
                start_date=date.fromisoformat(start_date) if start_date else date.today(),
                end_date=date.fromisoformat(end_date) if end_date else None
            )
            db.session.add(tenancy)
        else:
            tenancy.monthly_rent = float(monthly_rent)
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

    # Tenancy
    tenancy      = Tenancy.query.filter_by(tenant_id=u.id, is_active=True).first()
    past_tenancies = (Tenancy.query
                      .filter_by(tenant_id=u.id, is_active=False)
                      .order_by(Tenancy.end_date.desc()).all())

    # Financial
    invoices     = (Invoice.query.filter_by(tenant_id=u.id)
                    .order_by(Invoice.due_date.desc()).all())
    transactions = (Transaction.query.filter_by(tenant_id=u.id)
                    .order_by(Transaction.date.desc()).all())

    # Tickets
    tickets = (Ticket.query.filter_by(tenant_id=u.id)
               .order_by(Ticket.date_raised.desc()).all())

    # Documents
    documents = (TenantDocument.query.filter_by(tenant_id=u.id)
                 .order_by(TenantDocument.uploaded_at.desc()).all())

    # Notification history (Admin-only)
    notif_logs = []
    if session.get("role") == "Admin":
        notif_logs = (NotificationLog.query.filter_by(tenant_id=u.id)
                      .order_by(NotificationLog.sent_at.desc()).limit(20).all())

    # Summary stats
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
    )


@app.route("/service/submit", methods=["POST"])
@login_required
def submit_service():
    # If it's an Admin, accept their chosen priority. Otherwise, default to "medium".
    if session.get("role") == "Admin":
        priority = request.form.get("priority", "medium").lower()
    else:
        priority = "medium"
        
    new_ticket = Ticket(
        ticket_number=f"SR-{str(uuid.uuid4().hex[:6]).upper()}",
        title=request.form.get("title", "New Request"),
        description=request.form.get("description", ""),
        category=request.form.get("category", "General"),
        priority=priority,
        status="open",
        tenant_id=session["user_id"],
        property_id=request.form.get("property") # Assuming you link via ID
    )
    
    db.session.add(new_ticket)
    db.session.commit()
    
    flash("Service request submitted successfully!", "success")
    return redirect(url_for("service"))


@app.route("/service/assign/<int:ticket_id>", methods=["POST"])
@dept_required("edit_maintenance", "log_complaints")
def assign_ticket(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    
    technician = request.form.get("technician")
    priority = request.form.get("priority")
    
    # Update both the technician and the newly evaluated priority
    if technician:
        ticket.technician = technician
        ticket.status = "in-progress"
        
    if priority:
        ticket.priority = priority.lower()
        
    db.session.commit()
    
    flash(f"Ticket #{ticket.ticket_number} assigned to {technician} as {priority.capitalize()} priority!", "success")
    return redirect(url_for("service"))


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

    ticket.status = "resolved"
    ticket.date_resolved = datetime.utcnow()
    db.session.commit()
    flash(f"Ticket #{ticket.ticket_number} marked as resolved!", "success")
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

# ─────────────────────────────────────────────
#  DOCUMENTS
# ─────────────────────────────────────────────

@app.route("/documents")
@dept_required("view_tenant_full")
def documents():
    q = request.args.get("q", "").lower()
    tenant_id = request.args.get("tenant_id", type=int)
 
    # Build tenant list (only users with role=Tenant)
    tenant_users = User.query.filter_by(role="Tenant").all()
 
    if q:
        tenant_users = [u for u in tenant_users if q in u.name.lower() or q in u.email.lower()]
 
    tenant_list = []
    for u in tenant_users:
        active = Tenancy.query.filter_by(tenant_id=u.id, is_active=True).first()
        doc_count = TenantDocument.query.filter_by(tenant_id=u.id).count()
        tenant_list.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "property_name": active.unit.prop.name if active else "—",
            "unit_number": active.unit.unit_number if active else "—",
            "doc_count": doc_count,
        })
 
    # Wrap tenant_list items as simple objects accessible by attribute in Jinja
    class _T:
        def __init__(self, d):
            self.__dict__.update(d)
 
    tenant_list = [_T(t) for t in tenant_list]
 
    # Selected tenant detail
    selected_tenant = None
    documents_list = []
 
    if tenant_id:
        user = User.query.get_or_404(tenant_id)
        if user.role != "Tenant":
            abort(403)
 
        active = Tenancy.query.filter_by(tenant_id=user.id, is_active=True).first()
 
        class _ST:
            pass
 
        st = _ST()
        st.id = user.id
        st.name = user.name
        st.email = user.email
        st.property_name = active.unit.prop.name if active else "—"
        st.unit_number = active.unit.unit_number if active else "—"
        selected_tenant = st
 
        documents_list = (
            TenantDocument.query
            .filter_by(tenant_id=tenant_id)
            .order_by(TenantDocument.uploaded_at.desc())
            .all()
        )
 
    return render_template(
        "documents.html",
        tenants=tenant_list,
        selected_tenant=selected_tenant,
        documents=documents_list,
        q=q,
        role=session["role"],
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
 
    if not allowed_file(file.filename):
        flash("File type not allowed. Use PDF, JPG, or PNG.", "danger")
        return redirect(url_for("documents", tenant_id=tenant_id))
 
    # Read into memory to check size
    file_bytes = file.read()
    if len(file_bytes) > 10 * 1024 * 1024:
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
@dept_required("view_tenant_full")
def view_document(doc_id):
    doc = TenantDocument.query.get_or_404(doc_id)
    upload_folder = app.config.get("DOCS_UPLOAD_FOLDER",
                                   os.path.join(os.path.dirname(__file__), "uploads", "documents"))
    path = os.path.join(upload_folder, doc.stored_filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype=doc.mime_type, as_attachment=False,
                     download_name=doc.original_filename)
 
 
# ── Download (force download) ────────────────────────────────────────
@app.route("/documents/download/<int:doc_id>")
@dept_required("view_tenant_full")
def download_document(doc_id):
    doc = TenantDocument.query.get_or_404(doc_id)
    upload_folder = app.config.get("DOCS_UPLOAD_FOLDER",
                                   os.path.join(os.path.dirname(__file__), "uploads", "documents"))
    path = os.path.join(upload_folder, doc.stored_filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype=doc.mime_type, as_attachment=True,
                     download_name=doc.original_filename)
 
 
# ── Download all docs for a tenant as ZIP ───────────────────────────
@app.route("/documents/<int:tenant_id>/download-all")
@dept_required("view_tenant_full")
def download_all_documents(tenant_id):
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