"""Rent payment receipts, laid out after the Hearts & Sleeves receipt template.

receipt_context(invoice) gathers everything the receipt shows from the
database; render_receipt_pdf(ctx) draws it on one A4 page and returns bytes.
Keeping the two apart means the data can be checked without parsing a PDF.
"""
import os
from datetime import datetime, timedelta
from io import BytesIO

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from models import BankTransferClaim, RentLedger, Tenancy, Transaction, User

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
LOGO     = os.path.join(BASE_DIR, "static", "img", "hslogo1.jpeg")
STAMP    = os.path.join(BASE_DIR, "static", "img", "paid-stamp.png")

COMPANY = {
    "name":    "Hearts and Sleeves Limited",
    "email":   "contact@haspm.com",
    "phone":   "+234 708 047 2675",
    "web":     "www.haspm.com",
    "address": "Suite 309, The Fusion Plaza, Olusegun Obasanjo Way, Wuye, Abuja",
    "portal":  "Hearts and Sleeves Tenant Portal",
}
WAT = timedelta(hours=1)          # stored times are UTC; Nigeria is UTC+1

NAVY   = HexColor("#0B2545")
TITLE  = HexColor("#13315C")
LABEL  = HexColor("#2F5E8A")
MUTED  = HexColor("#5B6B7C")
RULE   = HexColor("#C9D3DD")
BAND   = HexColor("#C5D3E0")
STRIPE = HexColor("#E3E9EF")
GREEN  = HexColor("#1E7B3C")
TEXT   = HexColor("#1E293B")

# ₦ and ✓ are not in the PDF base fonts, so a full Unicode font is embedded.
_FONTS_READY = False


def _fonts():
    global _FONTS_READY
    if not _FONTS_READY:
        fdir = os.path.join(BASE_DIR, "static", "fonts")
        pdfmetrics.registerFont(TTFont("Sans", os.path.join(fdir, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("Sans-Bold", os.path.join(fdir, "DejaVuSans-Bold.ttf")))
        _FONTS_READY = True


# ── formatting ───────────────────────────────────────────────────────

def naira(n):
    return f"₦{(n or 0):,.2f}"


def _day(d):
    return f"{d.day} {d:%B %Y}" if d else "-"


def _stamp(dt):
    """'13 November 2026, 10:48 AM' in Nigerian time."""
    if not dt:
        return "-"
    local = dt + WAT
    return f"{local.day} {local:%B %Y}, {local:%I:%M %p}".replace(", 0", ", ")


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _initials(name):
    parts = [p for p in (name or "").split() if p[:1].isalpha()]
    return "".join(p[0] for p in parts[:2]).upper() or "TN"


def _base36(n, width=5):
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out.rjust(width, "0")


def receipt_number(txn, tenant):
    """HSL/R/<code>/<initials>/<nn>, e.g. HSL/R/7KWZ2/EI/01.

    The code is derived from the transaction id, scrambled so consecutive
    receipts do not look sequential (7919 is coprime with 36^5, so it stays
    unique); nn counts this tenant's receipts.
    """
    code = _base36((txn.id * 7919 + 104729) % 36 ** 5)
    seq = (Transaction.query.filter(Transaction.tenant_id == txn.tenant_id,
                                    Transaction.status == "confirmed",
                                    Transaction.id <= txn.id).count()) or 1
    return f"HSL/R/{code}/{_initials(tenant.name)}/{seq:02d}"


def _city_country(address):
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if len(parts) < 2:
        return "Nigeria"
    city = parts[-1]
    return city if "nigeria" in city.lower() else f"{city}, Nigeria"


def _method_label(method):
    m = (method or "").lower()
    if m.startswith("bank transfer"):
        return "Bank Transfer"
    if m.startswith("manual"):
        return "Recorded by staff"
    if "paystack" in m:
        return "Paystack (Card / Online)"
    return method or "-"


# ── data ─────────────────────────────────────────────────────────────

def receipt_context(invoice, now=None):
    """Everything the receipt prints, for a paid invoice. None if not paid."""
    txn = invoice.transaction
    if invoice.status != "paid" or txn is None:
        return None
    now = now or datetime.utcnow()
    tenant = invoice.tenant
    unit = invoice.unit
    prop = unit.prop if unit else None

    # The tenancy this invoice billed: same tenant and unit, active first.
    tenancy = None
    if unit:
        tenancy = (Tenancy.query.filter_by(tenant_id=tenant.id, unit_id=unit.id)
                   .order_by(Tenancy.is_active.desc(), Tenancy.start_date.desc()).first())
    term = tenancy.term_for(invoice.due_date) if tenancy else None
    if tenancy and term is None:
        term = tenancy.term_for(tenancy.start_date)
    next_renewal = None
    if term and not (tenancy.end_date and term[1] >= tenancy.end_date):
        next_renewal = term[1] + timedelta(days=1)

    claim = (BankTransferClaim.query.filter_by(invoice_id=invoice.id, status="confirmed")
             .order_by(BankTransferClaim.reviewed_at.desc()).first())
    processor = None
    if getattr(txn, "processed_by_id", None):
        processor = User.query.get(txn.processed_by_id)
    elif claim and claim.reviewed_by:
        processor = User.query.get(claim.reviewed_by)
    if processor:
        processed_by = f"{processor.name} ({processor.department or 'Management'})"
    elif "paystack" in (txn.method or "").lower():
        processed_by = "Paystack (automatic)"
    else:
        processed_by = "Hearts and Sleeves"

    paid_at = claim.submitted_at if claim and claim.submitted_at else txn.date
    reference = (claim.bank_ref if claim and claim.bank_ref else txn.paystack_ref) or txn.txn_number

    deposit = 0.0
    if unit:
        deposit = sum(e.amount for e in RentLedger.query.filter_by(
            tenant_id=tenant.id, unit_id=unit.id, entry_type="credit")
            if (e.reference or "").upper().startswith("DEP"))

    rent = invoice.amount if (invoice.type or "Rent") == "Rent" else 0.0
    service = invoice.amount if invoice.type == "Service Charge" else 0.0
    charges = [("Base Rent", rent), ("Service Charge", service)]
    if invoice.type not in ("Rent", "Service Charge", None):
        charges.append((invoice.type, invoice.amount))
    received = txn.amount or 0.0
    outstanding = max(0.0, round((invoice.amount or 0) - received, 2))

    unit_type = "-"
    if unit:
        beds = unit.bedrooms or 0
        unit_type = (f"{beds} Bedroom" if beds else "Unit") + (f" · {prop.type}" if prop and prop.type else "")

    method = _method_label(txn.method)
    return {
        "receipt_no":   receipt_number(txn, tenant),
        "issue_date":   _stamp(txn.date),
        "payment_date": _stamp(paid_at),
        "issued_on":    _stamp(now) + " WAT",
        "period":       term,
        "period_text":  f"{_day(term[0])} – {_day(term[1])}" if term else "-",
        "total":        received,
        "method":       method,
        "fully_paid":   outstanding == 0,
        "property": [
            ("Address",        prop.address if prop and prop.address else "-"),
            ("City / Country", _city_country(prop.address if prop else "")),
            ("Type",           unit_type),
            ("Unit ID",        unit.unit_number if unit else "-"),
        ],
        "tenant": [
            ("Tenant Name", tenant.name),
            ("Lease ID",    f"LSE/{tenancy.start_date.year}/{tenancy.id:04d}" if tenancy else "-"),
            ("Email",       tenant.email or "-"),
            ("Phone",       tenant.phone or "-"),
        ],
        "lease": [
            ("Lease Start",      _day(term[0]) if term else "-"),
            ("Lease End",        _day(term[1]) if term else "-"),
            ("Annual Rent",      naira(tenancy.annual_rent if tenancy else invoice.amount)),
            ("Security Deposit", naira(deposit)),
        ],
        "charges":     charges,
        "outstanding": outstanding,
        "payment": [
            ("Method",            method),
            ("Payment Reference", reference),
            ("Portal",            COMPANY["portal"]),
        ],
        "transaction": [
            ("Receipt No.",       None),        # filled from receipt_no
            ("Payment Status",    "Cleared"),
            ("Payment Reference", reference),
            ("Next Renewal",      _day(next_renewal) if next_renewal else "Not renewing"),
            ("Processed By",      processed_by),
            ("Issued On",         _stamp(now) + " WAT"),
        ],
        "invoice_number": invoice.inv_number,
    }


# ── drawing ──────────────────────────────────────────────────────────

class _Page:
    """Top-down drawing helpers: y is measured from the top of the page."""

    def __init__(self, c, width, height):
        self.c, self.W, self.H = c, width, height
        self.L, self.R = 36, width - 36

    def y(self, top):
        return self.H - top

    def text(self, x, top, s, font="Sans", size=8, color=TEXT, align="left", max_w=None):
        s = "" if s is None else str(s)
        if max_w:
            while s and pdfmetrics.stringWidth(s, font, size) > max_w:
                s = s[:-2] + "…" if len(s) > 2 else ""
        self.c.setFont(font, size)
        self.c.setFillColor(color)
        if align == "right":
            self.c.drawRightString(x, self.y(top), s)
        elif align == "center":
            self.c.drawCentredString(x, self.y(top), s)
        else:
            self.c.drawString(x, self.y(top), s)

    def rect(self, x, top, w, h, color):
        self.c.setFillColor(color)
        self.c.rect(x, self.y(top + h), w, h, stroke=0, fill=1)

    def rule(self, x1, x2, top, color=RULE, width=0.6):
        self.c.setStrokeColor(color)
        self.c.setLineWidth(width)
        self.c.line(x1, self.y(top), x2, self.y(top))


def _ordinal_date(p, x, top, d, size, font, color, align_right_at=None):
    """'14th June 2027' with a raised ordinal; returns the width drawn."""
    parts = [(str(d.day), font, size, 0), (_ordinal(d.day), font, size * 0.6, size * 0.35),
             (f" {d:%B %Y}", font, size, 0)]
    width = sum(pdfmetrics.stringWidth(t, f, s) for t, f, s, _ in parts)
    if align_right_at is not None:
        x = align_right_at - width
    for t, f, s, rise in parts:
        p.c.setFont(f, s)
        p.c.setFillColor(color)
        p.c.drawString(x, p.y(top) + rise, t)
        x += pdfmetrics.stringWidth(t, f, s)
    return width


def _draw_stamp(p, cx, top, r):
    """The green PAID stamp: an image if one is provided, else drawn."""
    if os.path.exists(STAMP):
        p.c.drawImage(ImageReader(STAMP), cx - r, p.y(top + 2 * r), 2 * r, 2 * r, mask="auto")
        return
    c, cy = p.c, p.y(top + r)
    c.saveState()
    c.setStrokeColor(GREEN)
    c.setFillColor(GREEN)
    c.setLineWidth(2.2)
    c.circle(cx, cy, r, stroke=1, fill=0)
    c.setLineWidth(0.9)
    c.circle(cx, cy, r - 6, stroke=1, fill=0)
    c.translate(cx, cy)
    c.rotate(-12)
    c.setLineWidth(1.4)
    c.rect(-r * 0.78, -9, r * 1.56, 20, stroke=1, fill=0)
    c.setFont("Sans-Bold", 17)
    c.drawCentredString(0, -5, "PAID")
    c.setFont("Sans-Bold", 6.5)
    c.drawCentredString(0, r * 0.45, "THANK YOU!")
    c.drawCentredString(0, -r * 0.58, "★ ★ ★")
    c.restoreState()


def _section_title(p, x1, x2, top, title, band=False):
    if band:
        p.rect(x1, top, x2 - x1, 15, BAND)
        p.text(x1 + 4, top + 10.5, title, "Sans-Bold", 7.5, TITLE)
        return top + 15
    p.text(x1, top + 10, title, "Sans-Bold", 7.5, TITLE)
    p.rule(x1, x2, top + 15, LABEL, 0.8)
    return top + 15


def _rows(p, x1, x2, top, rows, value_x, h=19, label_color=MUTED, bold=False, label_size=7.3):
    for label, value in rows:
        p.text(x1 + 4, top + h * 0.62, label, "Sans", label_size, label_color)
        p.text(value_x, top + h * 0.62, value, "Sans-Bold" if bold else "Sans", 7.3, TEXT,
               max_w=x2 - value_x - 4)
        top += h
        p.rule(x1, x2, top)
    return top


def render_receipt_pdf(ctx):
    _fonts()
    buf = BytesIO()
    W, H = A4
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Receipt {ctx['receipt_no']}")
    c.setAuthor(COMPANY["name"])
    c.setSubject(f"Rent payment receipt for {ctx['invoice_number']}")
    p = _Page(c, W, H)
    L, R = p.L, p.R

    # ── header: logo, stamp, receipt meta ───────────────────────────
    if os.path.exists(LOGO):
        img = ImageReader(LOGO)
        iw, ih = img.getSize()
        lw = 235
        c.drawImage(img, L - 8, p.y(22 + lw * ih / iw), lw, lw * ih / iw, mask="auto")
    _draw_stamp(p, 312, 14, 44)
    meta_x, val_x = 372, 424
    for i, (label, value) in enumerate([("Receipt No.", ctx["receipt_no"]),
                                        ("Issue Date", ctx["issue_date"]),
                                        ("Payment Date", ctx["payment_date"]),
                                        ("Rent Period", ctx["period_text"])]):
        top = 42 + i * 13
        p.text(meta_x, top, label, "Sans", 6.6, MUTED)
        p.text(val_x, top, value, "Sans-Bold", 6.6, TEXT, max_w=R - val_x)

    p.text(L, 120, "RENT PAYMENT RECEIPT", "Sans-Bold", 15, TITLE)
    if ctx["fully_paid"]:
        p.text(R, 119, "✓ Verified Rent Payment  ·  Payment Received in Full", "Sans-Bold", 7.5, GREEN,
               align="right")

    # ── total band ──────────────────────────────────────────────────
    p.rect(L, 128, R - L, 70, NAVY)
    p.text(L + 10, 142, "Total Rent Received", "Sans", 7, HexColor("#AFC3D9"))
    p.text(L + 10, 166, naira(ctx["total"]), "Sans-Bold", 19, white)
    p.text(L + 10, 188, f"Received from tenant via {ctx['method']}", "Sans", 7, HexColor("#AFC3D9"))
    p.text(R - 10, 152, "Annual Rental Period", "Sans", 7, HexColor("#AFC3D9"), align="right")
    if ctx["period"]:
        start, end = ctx["period"]
        end_w = _ordinal_date(p, 0, 166, end, 9, "Sans-Bold", white, align_right_at=R - 10)
        dash = " – "
        dash_w = pdfmetrics.stringWidth(dash, "Sans-Bold", 9)
        p.text(R - 10 - end_w, 166, dash, "Sans-Bold", 9, white, align="right")
        _ordinal_date(p, 0, 166, start, 9, "Sans-Bold", white, align_right_at=R - 10 - end_w - dash_w)

    # ── property and tenant details ─────────────────────────────────
    mid = (L + R) / 2
    top = _section_title(p, L, mid - 8, 206, "Property Details")
    _rows(p, L, mid - 8, top, ctx["property"], L + 82, h=24, label_color=LABEL, bold=True)
    top = _section_title(p, mid + 8, R, 206, "Tenant Information")
    end = _rows(p, mid + 8, R, top, ctx["tenant"], mid + 92, h=24, label_color=LABEL, bold=True)

    # ── lease terms ─────────────────────────────────────────────────
    top = _section_title(p, L, R, end + 12, "Lease Terms", band=True)
    top = _rows(p, L, R, top, ctx["lease"], L + 230)

    # ── charge breakdown ────────────────────────────────────────────
    top = _section_title(p, L, R, top + 12, "Charge Breakdown", band=True)
    p.rect(L, top, R - L, 14, NAVY)
    p.text(L + 4, top + 10, "Description", "Sans-Bold", 7.5, white)
    p.text(R - 4, top + 10, "Amount", "Sans-Bold", 7.5, white, align="right")
    top += 14
    for i, (desc, amount) in enumerate(ctx["charges"]):
        if i % 2:
            p.rect(L, top, R - L, 14, STRIPE)
        p.text(L + 4, top + 10, desc, "Sans", 7.3, TEXT)
        p.text(R - 4, top + 10, naira(amount), "Sans-Bold", 7.3, TEXT, align="right")
        top += 14
    p.rect(L, top, R - L, 15, NAVY)
    p.text(L + 4, top + 10.5, "Total Received", "Sans-Bold", 7.8, white)
    p.text(R - 4, top + 10.5, naira(ctx["total"]), "Sans-Bold", 7.8, white, align="right")
    top += 15
    owed_color = GREEN if ctx["outstanding"] == 0 else HexColor("#B42318")
    p.text(L + 4, top + 12, "Outstanding Balance", "Sans-Bold", 7.5, owed_color)
    p.text(R - 4, top + 12, naira(ctx["outstanding"]) + (" ✓" if ctx["outstanding"] == 0 else ""),
           "Sans-Bold", 7.5, owed_color, align="right")
    top += 17
    p.rule(L, R, top)

    # ── payment method and transaction details ──────────────────────
    top = _section_title(p, L, R, top + 4, "Payment Method")
    top = _rows(p, L, R, top, ctx["payment"], L + 230, bold=True)
    top = _section_title(p, L, R, top + 4, "Transaction Details")
    rows = [(k, ctx["receipt_no"] if v is None else v) for k, v in ctx["transaction"]]
    _rows(p, L, R, top, rows, L + 230)

    # ── footer ──────────────────────────────────────────────────────
    foot = [
        "This is a computer-generated receipt issued for record-keeping purposes. "
        f"No signature is required. For queries, contact {COMPANY['name']}",
        f"{COMPANY['name']} | {COMPANY['email']} | {COMPANY['phone']} | {COMPANY['web']}",
        COMPANY["address"],
    ]
    for i, line in enumerate(foot):
        p.text(W / 2, H - 40 + i * 9, line, "Sans", 6.3, MUTED, align="center")

    c.showPage()
    c.save()
    return buf.getvalue()
