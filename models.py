"""
HS Property Management — Database Models
All relationships are defined here via SQLAlchemy ORM.
"""
import secrets
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


# ─────────────────────────────────────────────
#  USER
# ─────────────────────────────────────────────
class User(db.Model):
    __tablename__ = "users"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    email      = db.Column(db.String(180), unique=True, nullable=False)
    password   = db.Column(db.String(255), nullable=False)   # werkzeug hash; see set_password()
    role       = db.Column(db.String(20),  nullable=False, default="Tenant")  # Admin | Landlord | Tenant
    # Admin staff department: Management | Finance | Maintenance | Customer Service
    # NULL for non-admins; NULL admin = legacy super-admin (treated as Management)
    department = db.Column(db.String(30), nullable=True)
    phone      = db.Column(db.String(30))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)

    # Relationships
    # A landlord can own many properties
    properties  = db.relationship("Property",  back_populates="landlord",  lazy="dynamic",
                                  foreign_keys="Property.landlord_id")
    # A tenant can have one active tenancy at a time (but many over time)
    tenancies   = db.relationship("Tenancy",   back_populates="tenant",    lazy="dynamic")
    invoices    = db.relationship("Invoice",   back_populates="tenant",    lazy="dynamic")
    tickets     = db.relationship("Ticket",    back_populates="tenant",    lazy="dynamic")
    transactions = db.relationship("Transaction", back_populates="tenant", lazy="dynamic")

    # ── Password handling ────────────────────────────────────────────
    # Stored as a werkzeug hash. Accounts created before hashing was added
    # still hold a plain-text value; check_password() accepts those once so
    # nobody is locked out, and the caller re-hashes on the spot.

    def set_password(self, raw):
        """Hash and store a new password."""
        self.password = generate_password_hash(raw, method="pbkdf2:sha256")

    def check_password(self, raw):
        """True if `raw` matches. Legacy plain-text values still verify."""
        if not self.password:
            return False
        if self.password.startswith(("pbkdf2:", "scrypt:", "argon2")):
            return check_password_hash(self.password, raw)
        return secrets.compare_digest(self.password, raw)

    @property
    def password_needs_rehash(self):
        """True while this account is still on a legacy plain-text password."""
        return bool(self.password) and not self.password.startswith(
            ("pbkdf2:", "scrypt:", "argon2"))

    def __repr__(self):
        return f"<User {self.email} [{self.role}]>"


# ─────────────────────────────────────────────
#  PROPERTY
# ─────────────────────────────────────────────
class Property(db.Model):
    __tablename__ = "properties"

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(150), nullable=False)
    address     = db.Column(db.String(300))
    type        = db.Column(db.String(50), default="Residential")   # Commercial | Residential | Mixed-Use
    description = db.Column(db.Text)
    avg_rent    = db.Column(db.Float, default=0)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    # Share of everything collected on this property that belongs to Hearts &
    # Sleeves; the remainder is the landlord's. NULL means "use the global
    # default from Settings", so changing that default moves every property
    # that has not been given an explicit rate.
    commission_pct = db.Column(db.Float, nullable=True)

    # Template used when naming units, e.g. "Block A - Unit {n}" or "Flat {n}".
    # {n} is the unit's sequence number. Unit names are unique per property.
    unit_naming_pattern = db.Column(db.String(120), nullable=True)

    # FK → landlord (User with role=Landlord or Admin)
    landlord_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    landlord    = db.relationship("User", back_populates="properties", foreign_keys=[landlord_id])

    # Relationships
    units        = db.relationship("Unit",    back_populates="prop", cascade="all, delete-orphan", lazy="dynamic")
    tickets      = db.relationship("Ticket",  back_populates="prop", lazy="dynamic")
    images = db.relationship("PropertyImage", backref="property", cascade="all, delete-orphan", lazy="joined")

    # ── Computed helpers ──────────────────────
    @property
    def total_units(self):
        return self.units.count()

    @property
    def occupied_units(self):
        return self.units.filter_by(is_occupied=True).count()

    @property
    def vacant_units(self):
        return self.total_units - self.occupied_units

    @property
    def occupancy_pct(self):
        if self.total_units == 0:
            return 0
        return round((self.occupied_units / self.total_units) * 100)

    @property
    def letter(self):
        return self.name[0].upper() if self.name else "?"

    # ── Commission ────────────────────────────
    DEFAULT_COMMISSION_PCT = 0.0

    @property
    def effective_commission_pct(self):
        """This property's rate, falling back to the configured global default."""
        if self.commission_pct is not None:
            return self.commission_pct
        try:
            from flask import current_app
            return float(current_app.config.get("DEFAULT_COMMISSION_PCT",
                                                self.DEFAULT_COMMISSION_PCT))
        except Exception:
            return self.DEFAULT_COMMISSION_PCT

    @property
    def uses_default_commission(self):
        return self.commission_pct is None

    def split(self, amount):
        """Split a collected amount into (company_share, landlord_share)."""
        pct = max(0.0, min(100.0, self.effective_commission_pct or 0.0))
        company = round((amount or 0) * pct / 100.0, 2)
        return company, round((amount or 0) - company, 2)

    # ── Unit naming ───────────────────────────
    DEFAULT_UNIT_PATTERN = "Unit {n}"

    @property
    def effective_unit_pattern(self):
        return (self.unit_naming_pattern or "").strip() or self.DEFAULT_UNIT_PATTERN

    def format_unit_name(self, n):
        """Render the naming pattern for sequence number `n`."""
        pattern = self.effective_unit_pattern
        if "{n}" not in pattern:
            pattern = pattern.rstrip() + " {n}"
        return pattern.replace("{n}", str(n)).strip()

    def next_unit_name(self):
        """First name from the pattern that is not already taken here."""
        taken = {u.unit_number for u in self.units}
        n = 1
        while self.format_unit_name(n) in taken:
            n += 1
            if n > 9999:
                break
        return self.format_unit_name(n)

    def __repr__(self):
        return f"<Property {self.name}>"

class PropertyImage(db.Model):
    __tablename__ = "property_images"
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id", ondelete="CASCADE"), nullable=False)
    image_url = db.Column(db.String(500), nullable=False)
# ─────────────────────────────────────────────
#  UNIT  (a single rentable space inside a property)
# ─────────────────────────────────────────────
class Unit(db.Model):
    __tablename__ = "units"
    # A unit name only has to be unique inside its own property - "Flat 1" can
    # exist in several buildings.
    __table_args__ = (
        db.UniqueConstraint("property_id", "unit_number", name="uq_unit_per_property"),
    )

    id          = db.Column(db.Integer, primary_key=True)
    unit_number = db.Column(db.String(20), nullable=False)   # e.g. "A1", "3B"
    floor       = db.Column(db.Integer, default=0)
    bedrooms    = db.Column(db.Integer, default=1)
    bathrooms   = db.Column(db.Float,   default=1)
    size_sqm    = db.Column(db.Float)
    rent_amount = db.Column(db.Float,   default=0)
    is_occupied = db.Column(db.Boolean, default=False)

    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False)
    prop        = db.relationship("Property", back_populates="units")

    # Relationships
    tenancies   = db.relationship("Tenancy", back_populates="unit", lazy="dynamic")
    invoices    = db.relationship("Invoice", back_populates="unit",  lazy="dynamic")
    tickets     = db.relationship("Ticket",  back_populates="unit",  lazy="dynamic")

    @property
    def current_tenant(self):
        active = self.tenancies.filter_by(is_active=True).first()
        return active.tenant if active else None

    def __repr__(self):
        return f"<Unit {self.unit_number} @ Property#{self.property_id}>"


# ─────────────────────────────────────────────
#  TENANCY  (the lease agreement between a tenant and a unit)
# ─────────────────────────────────────────────
class Tenancy(db.Model):
    __tablename__ = "tenancies"

    id           = db.Column(db.Integer, primary_key=True)
    start_date   = db.Column(db.Date, nullable=False)
    end_date     = db.Column(db.Date)
    # Rent is paid annually. The column was previously called monthly_rent but
    # already held the yearly figure; migrate_v2.py renames it in place.
    annual_rent  = db.Column(db.Float, nullable=False)
    is_active    = db.Column(db.Boolean, default=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    tenant_id    = db.Column(db.Integer, db.ForeignKey("users.id"),  nullable=False)
    unit_id      = db.Column(db.Integer, db.ForeignKey("units.id"),  nullable=False)

    tenant       = db.relationship("User", back_populates="tenancies")
    unit         = db.relationship("Unit", back_populates="tenancies")

    def __repr__(self):
        return f"<Tenancy tenant#{self.tenant_id} unit#{self.unit_id}>"


# ─────────────────────────────────────────────
#  INVOICE
# ─────────────────────────────────────────────
class Invoice(db.Model):
    __tablename__ = "invoices"

    id          = db.Column(db.Integer, primary_key=True)
    inv_number  = db.Column(db.String(20), unique=True, nullable=False)  # INV-001
    type        = db.Column(db.String(50), default="Rent")               # Rent | Service Charge | Utility
    amount      = db.Column(db.Float, nullable=False)
    due_date    = db.Column(db.Date, nullable=False)
    status      = db.Column(db.String(20), default="pending")            # pending | paid | overdue
    paystack_ref = db.Column(db.String(100))
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    tenant_id   = db.Column(db.Integer, db.ForeignKey("users.id"),  nullable=False)
    unit_id     = db.Column(db.Integer, db.ForeignKey("units.id"),  nullable=True)

    tenant      = db.relationship("User", back_populates="invoices")
    unit        = db.relationship("Unit", back_populates="invoices")

    # A paid invoice has one transaction
    transaction = db.relationship("Transaction", back_populates="invoice", uselist=False)

    def __repr__(self):
        return f"<Invoice {self.inv_number} [{self.status}]>"


# ─────────────────────────────────────────────
#  TRANSACTION  (confirmed payment record)
# ─────────────────────────────────────────────
class Transaction(db.Model):
    __tablename__ = "transactions"

    id           = db.Column(db.Integer, primary_key=True)
    txn_number   = db.Column(db.String(20), unique=True, nullable=False)  # TXN-001
    paystack_ref = db.Column(db.String(100))
    amount       = db.Column(db.Float, nullable=False)
    method       = db.Column(db.String(60), default="Paystack Card")
    status       = db.Column(db.String(20), default="pending")            # pending | confirmed
    date         = db.Column(db.DateTime, default=datetime.utcnow)

    tenant_id    = db.Column(db.Integer, db.ForeignKey("users.id"),    nullable=False)
    invoice_id   = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)

    tenant       = db.relationship("User",    back_populates="transactions")
    invoice      = db.relationship("Invoice", back_populates="transaction")

    def __repr__(self):
        return f"<Transaction {self.txn_number} [{self.status}]>"


# ─────────────────────────────────────────────
#  TICKET  (maintenance / service request)
# ─────────────────────────────────────────────
class Ticket(db.Model):
    __tablename__ = "tickets"

    id           = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(20), unique=True, nullable=False)  # SR-001
    title        = db.Column(db.String(200), nullable=False)
    description  = db.Column(db.Text)
    category     = db.Column(db.String(60), default="General")             # HVAC | Plumbing | Electrical …
    priority     = db.Column(db.String(20), default="medium")              # low | medium | high | urgent
    status       = db.Column(db.String(20), default="open")                # open | in-progress | resolved
    technician   = db.Column(db.String(120))
    date_raised  = db.Column(db.DateTime, default=datetime.utcnow)
    date_resolved = db.Column(db.DateTime)

    tenant_id    = db.Column(db.Integer, db.ForeignKey("users.id"),       nullable=False)
    property_id  = db.Column(db.Integer, db.ForeignKey("properties.id"),  nullable=False)
    unit_id      = db.Column(db.Integer, db.ForeignKey("units.id"),       nullable=True)

    tenant       = db.relationship("User",     back_populates="tickets")
    prop         = db.relationship("Property", back_populates="tickets")
    unit         = db.relationship("Unit",     back_populates="tickets")

    def __repr__(self):
        return f"<Ticket {self.ticket_number} [{self.status}]>"

class TenantDocument(db.Model):
    __tablename__ = "tenant_documents"
 
    id                = db.Column(db.Integer, primary_key=True)
    tenant_id         = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    doc_type          = db.Column(db.String(50), nullable=False, default="other")
    # e.g. rental_application | tenancy_agreement | id_document | payment_proof | other
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename   = db.Column(db.String(255), nullable=False, unique=True)  # UUID-based, safe name
    label             = db.Column(db.String(200))
    file_size         = db.Column(db.Integer)     # bytes
    mime_type         = db.Column(db.String(80))
    uploaded_at       = db.Column(db.DateTime, default=datetime.utcnow)
 
    # Relationship
    tenant = db.relationship("User", backref=db.backref("documents", lazy="dynamic"))
 
    @property
    def file_size_str(self):
        """Human-readable file size."""
        if not self.file_size:
            return "—"
        if self.file_size < 1024:
            return f"{self.file_size} B"
        elif self.file_size < 1024 * 1024:
            return f"{self.file_size / 1024:.1f} KB"
        else:
            return f"{self.file_size / (1024*1024):.2f} MB"
 
    def __repr__(self):
        return f"<TenantDocument {self.original_filename} [{self.tenant_id}]>"


# ─────────────────────────────────────────────
#  RENT LEDGER  (manual adjustments & deposit tracking)
# ─────────────────────────────────────────────
class RentLedger(db.Model):
    __tablename__ = "rent_ledger"

    id            = db.Column(db.Integer, primary_key=True)
    tenant_id     = db.Column(db.Integer, db.ForeignKey("users.id"),       nullable=False)
    unit_id       = db.Column(db.Integer, db.ForeignKey("units.id"),       nullable=False)
    property_id   = db.Column(db.Integer, db.ForeignKey("properties.id"),  nullable=False)
    entry_type    = db.Column(db.String(20), nullable=False)   # charge | credit | adjustment
    amount        = db.Column(db.Float, nullable=False)
    description   = db.Column(db.String(250))
    reference     = db.Column(db.String(100))                  # ADJ-xxx, DEP-xxx, etc.
    entry_date    = db.Column(db.Date, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    tenant   = db.relationship("User",     foreign_keys=[tenant_id],
                               backref=db.backref("ledger_entries", lazy="dynamic"))
    unit     = db.relationship("Unit",     backref=db.backref("ledger_entries", lazy="dynamic"))
    prop     = db.relationship("Property", backref=db.backref("ledger_entries", lazy="dynamic"))

    def __repr__(self):
        return f"<RentLedger {self.entry_type} {self.amount} tenant#{self.tenant_id}>"


# ─────────────────────────────────────────────
#  EXPENSE  (property expense tracking)
# ─────────────────────────────────────────────
class Expense(db.Model):
    __tablename__ = "expenses"

    CATEGORIES = ["Maintenance", "Utilities", "Repairs", "Cleaning",
                  "Insurance", "Management Fee", "Legal", "Other"]

    id                 = db.Column(db.Integer, primary_key=True)
    exp_number         = db.Column(db.String(20), unique=True, nullable=False)  # EXP-0001
    property_id        = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False)
    unit_id            = db.Column(db.Integer, db.ForeignKey("units.id"),       nullable=True)
    category           = db.Column(db.String(50),  nullable=False, default="Other")
    description        = db.Column(db.String(300), nullable=False)
    amount             = db.Column(db.Float,        nullable=False)
    vendor             = db.Column(db.String(150))
    expense_date       = db.Column(db.Date,         nullable=False)
    status             = db.Column(db.String(20),   default="unpaid")   # paid | unpaid
    paid_by            = db.Column(db.String(20))                       # Admin | Landlord | Tenant
    paid_by_tenant_id  = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    receipt_filename   = db.Column(db.String(255))
    notes              = db.Column(db.Text)
    created_by_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)

    prop           = db.relationship("Property", backref=db.backref("expenses", lazy="dynamic"))
    unit           = db.relationship("Unit",     backref=db.backref("expenses", lazy="dynamic"))
    paid_by_tenant = db.relationship("User", foreign_keys=[paid_by_tenant_id])
    created_by     = db.relationship("User", foreign_keys=[created_by_id])

    def __repr__(self):
        return f"<Expense {self.exp_number} ₦{self.amount}>"


# ─────────────────────────────────────────────
#  LANDLORD PAYOUT
# ─────────────────────────────────────────────
class LandlordPayout(db.Model):
    __tablename__ = "landlord_payouts"

    id             = db.Column(db.Integer, primary_key=True)
    payout_number  = db.Column(db.String(20), unique=True, nullable=False)  # PAY-0001
    landlord_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount         = db.Column(db.Float,   nullable=False)
    status         = db.Column(db.String(20), default="pending")  # pending | completed | rejected
    requested_at   = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at   = db.Column(db.DateTime, nullable=True)
    notes          = db.Column(db.Text)

    landlord = db.relationship("User", backref=db.backref("payouts", lazy="dynamic"))

    def __repr__(self):
        return f"<LandlordPayout {self.payout_number} [{self.status}]>"


# ─────────────────────────────────────────────
#  BANK TRANSFER CLAIM  (tenant-submitted proof)
# ─────────────────────────────────────────────
class BankTransferClaim(db.Model):
    __tablename__ = "bank_transfer_claims"

    id            = db.Column(db.Integer, primary_key=True)
    invoice_id    = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    tenant_id     = db.Column(db.Integer, db.ForeignKey("users.id"),    nullable=False)
    amount        = db.Column(db.Float, nullable=False)
    bank_ref      = db.Column(db.String(100))      # bank teller / transaction ID
    transfer_date = db.Column(db.Date, nullable=True)
    note          = db.Column(db.Text)
    status        = db.Column(db.String(20), default="pending")  # pending | confirmed | rejected
    submitted_at  = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at   = db.Column(db.DateTime, nullable=True)
    reviewed_by   = db.Column(db.Integer,  db.ForeignKey("users.id"), nullable=True)

    invoice  = db.relationship("Invoice", backref=db.backref("transfer_claims", lazy="dynamic"))
    tenant   = db.relationship("User", foreign_keys=[tenant_id],
                               backref=db.backref("transfer_claims", lazy="dynamic"))
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])

    def __repr__(self):
        return f"<BankTransferClaim inv#{self.invoice_id} [{self.status}]>"


# ─────────────────────────────────────────────
#  NOTIFICATION LOG  (tracks emails sent by scheduler)
# ─────────────────────────────────────────────
class NotificationLog(db.Model):
    __tablename__ = "notification_logs"

    id          = db.Column(db.Integer, primary_key=True)
    notif_type  = db.Column(db.String(50), nullable=False)  # rent_invoice | lease_expiry | overdue
    tenant_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    ref_id      = db.Column(db.Integer, nullable=True)       # invoice.id or tenancy.id
    recipient   = db.Column(db.String(180))
    status      = db.Column(db.String(20), default="sent")   # sent | failed | suppressed
    sent_at     = db.Column(db.DateTime, default=datetime.utcnow)

    tenant = db.relationship("User", backref=db.backref("notification_logs", lazy="dynamic"))

    def __repr__(self):
        return f"<NotificationLog {self.notif_type} tenant#{self.tenant_id} [{self.status}]>"


# ─────────────────────────────────────────────
#  MESSAGE  (internal inbox between users and staff)
# ─────────────────────────────────────────────
class Message(db.Model):
    __tablename__ = "messages"

    id           = db.Column(db.Integer, primary_key=True)
    sender_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    subject      = db.Column(db.String(200), nullable=False)
    body         = db.Column(db.Text, nullable=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    read_at      = db.Column(db.DateTime, nullable=True)
    parent_id    = db.Column(db.Integer, db.ForeignKey("messages.id"), nullable=True)

    sender    = db.relationship("User", foreign_keys=[sender_id],
                                backref=db.backref("sent_messages", lazy="dynamic"))
    recipient = db.relationship("User", foreign_keys=[recipient_id],
                                backref=db.backref("received_messages", lazy="dynamic"))

    def __repr__(self):
        return f"<Message #{self.id} from #{self.sender_id} to #{self.recipient_id}>"


# ─────────────────────────────────────────────
#  ATTACHMENT  (files on messages and service requests)
# ─────────────────────────────────────────────
class Attachment(db.Model):
    """A file hanging off a message or a service request.

    Deliberately generic rather than two near-identical tables: both modules
    need the same upload, listing and download behaviour, and the parent is
    identified by (parent_type, parent_id).
    """
    __tablename__ = "attachments"
    __table_args__ = (
        db.Index("ix_attachment_parent", "parent_type", "parent_id"),
    )

    PARENT_MESSAGE = "message"
    PARENT_TICKET  = "ticket"

    # On a ticket, an attachment is either context from the tenant when they
    # report the fault, or evidence from staff that the work is finished.
    KIND_ATTACHMENT = "attachment"
    KIND_PROOF      = "completion_proof"

    id                = db.Column(db.Integer, primary_key=True)
    parent_type       = db.Column(db.String(20), nullable=False)
    parent_id         = db.Column(db.Integer,    nullable=False)
    kind              = db.Column(db.String(30), nullable=False, default=KIND_ATTACHMENT)

    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename   = db.Column(db.String(255), nullable=False, unique=True)
    file_size         = db.Column(db.Integer)
    mime_type         = db.Column(db.String(80))
    caption           = db.Column(db.String(250))

    uploaded_by_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    uploaded_at       = db.Column(db.DateTime, default=datetime.utcnow)

    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id])

    @property
    def is_image(self):
        return (self.mime_type or "").startswith("image/")

    @property
    def file_size_str(self):
        n = self.file_size or 0
        if not n:
            return "—"
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / (1024 * 1024):.1f} MB"

    @property
    def icon(self):
        if self.is_image:
            return "file-earmark-image"
        if (self.mime_type or "").endswith("pdf"):
            return "file-earmark-pdf"
        return "paperclip"

    def __repr__(self):
        return f"<Attachment {self.parent_type}#{self.parent_id} {self.original_filename}>"


# ─────────────────────────────────────────────
#  TECHNICIAN RATING  (tenant rates technician after resolution)
# ─────────────────────────────────────────────
class TechnicianRating(db.Model):
    __tablename__ = "technician_ratings"

    id         = db.Column(db.Integer, primary_key=True)
    ticket_id  = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=False, unique=True)
    tenant_id  = db.Column(db.Integer, db.ForeignKey("users.id"),   nullable=False)
    technician = db.Column(db.String(120), nullable=False)   # snapshot of ticket.technician
    stars      = db.Column(db.Integer, nullable=False)       # 1–5
    comment    = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    ticket = db.relationship("Ticket", backref=db.backref("rating", uselist=False))
    tenant = db.relationship("User",   backref=db.backref("technician_ratings", lazy="dynamic"))

    def __repr__(self):
        return f"<TechnicianRating {self.stars}★ {self.technician} ticket#{self.ticket_id}>"