"""
seed.py — Populates the HS Property database with demo data.
Run once: python seed.py
"""
from app import app
from models import db, User, Property, Unit, Tenancy, Invoice, Transaction, Ticket
from datetime import date, timedelta, datetime

PROPERTY_IMAGES = [
    # Real Unsplash photos for Nigerian-style commercial/residential buildings
    "https://images.unsplash.com/photo-1486325212027-8081e485255e?w=800&q=80",  # Heritage Plaza – glass office
    "https://images.unsplash.com/photo-1580587771525-78b9dba3b914?w=800&q=80",  # Sunrise Court – white villa
    "https://images.unsplash.com/photo-1545324418-cc1a3fa10c00?w=800&q=80",  # Goldstone Towers – modern tower
    "https://images.unsplash.com/photo-1512917774080-9991f1c4c750?w=800&q=80",  # Parkview Annex – upscale house
]

def seed():
    with app.app_context():
        db.drop_all()
        db.create_all()

        # ── Users ──────────────────────────────────────────────────────────
        admin = User(name="Admin User",       email="admin@hs.com",      password="admin123",  role="Admin",    phone="+234 800 000 0001")
        lord  = User(name="Mr. Hassan Sani",  email="landlord@hs.com",   password="land123",   role="Landlord", phone="+234 800 000 0002")
        t1    = User(name="Adebayo Okafor",   email="tenant@hs.com",     password="tenant123", role="Tenant",   phone="+234 801 234 5678")
        t2    = User(name="Chioma Eze",       email="c.eze@gmail.com",   password="tenant123", role="Tenant",   phone="+234 803 456 7890")
        t3    = User(name="Marcus Johnson",   email="m.johnson@corp.com",password="tenant123", role="Tenant",   phone="+234 805 678 9012")
        t4    = User(name="Fatima Al-Hassan", email="f.alhassan@nbn.gov",password="tenant123", role="Tenant",   phone="+234 809 012 3456")
        t5    = User(name="David Nwachukwu",  email="d.nwachukwu@gmail.com",password="tenant123",role="Tenant",phone="+234 811 234 5678")
        db.session.add_all([admin, lord, t1, t2, t3, t4, t5])
        db.session.flush()

        # ── Properties ─────────────────────────────────────────────────────
        p1 = Property(
            name="Heritage Plaza",
            address="14 Marina Street, Lagos Island, Lagos",
            type="Commercial",
            description="A premium commercial complex at the heart of Lagos Island. Home to corporate offices, retail outlets, and a rooftop café with panoramic harbour views.",
            image_url=PROPERTY_IMAGES[0],
            avg_rent=450_000,
            landlord_id=lord.id,
        )
        p2 = Property(
            name="Sunrise Court",
            address="7 Adeola Odeku, Victoria Island, Lagos",
            type="Residential",
            description="Serene gated residential estate in the prestigious Victoria Island corridor. Each unit comes with 24/7 security, standby generator, and a communal pool.",
            image_url=PROPERTY_IMAGES[1],
            avg_rent=320_000,
            landlord_id=lord.id,
        )
        p3 = Property(
            name="Goldstone Towers",
            address="22 Ahmadu Bello Way, Central Business District, Abuja",
            type="Mixed-Use",
            description="Iconic 20-storey mixed-use tower dominating Abuja's skyline. Ground floors host premium retail; upper floors offer executive serviced apartments.",
            image_url=PROPERTY_IMAGES[2],
            avg_rent=620_000,
            landlord_id=lord.id,
        )
        p4 = Property(
            name="Parkview Annex",
            address="5 Parkview Estate, Ikoyi, Lagos",
            type="Residential",
            description="Exclusive detached and semi-detached homes in the leafy Parkview Estate. Close to top international schools, embassies, and fine dining.",
            image_url=PROPERTY_IMAGES[3],
            avg_rent=280_000,
            landlord_id=lord.id,
        )
        db.session.add_all([p1, p2, p3, p4])
        db.session.flush()

        # ── Units ──────────────────────────────────────────────────────────
        def make_units(prop, specs):
            units = []
            for num, rent, occ in specs:
                u = Unit(unit_number=num, rent_amount=rent, is_occupied=occ, property_id=prop.id)
                units.append(u)
            db.session.add_all(units)
            db.session.flush()
            return units

        u_p1 = make_units(p1, [
            ("A1",450000,True), ("A2",450000,True), ("A3",450000,True),
            ("B1",460000,True), ("B2",460000,False),("B3",460000,True),
            ("C1",440000,True), ("C2",440000,True), ("C3",440000,True),
            ("D1",455000,False),("D2",455000,True), ("D3",455000,False),
        ])
        u_p2 = make_units(p2, [
            ("B1",320000,True), ("B2",320000,True), ("B3",320000,True),
            ("B4",325000,True), ("B5",325000,True), ("B6",325000,True),
            ("B7",315000,True), ("B8",315000,True),
        ])
        u_p3 = make_units(p3, [
            ("C1",550000,True), ("C2",550000,True), ("C3",550000,False),
            ("C4",580000,True), ("C5",580000,True), ("C6",580000,True),
            ("C7",620000,True), ("C8",620000,False),("C9",620000,True),
            ("C10",600000,True),("C11",600000,True),("C12",550000,True),
            ("C13",560000,True),("C14",560000,False),("C15",570000,True),
            ("C16",570000,True),("C17",585000,True),("C18",585000,False),
            ("C19",590000,True),("C20",590000,False),
        ])
        u_p4 = make_units(p4, [
            ("D1",280000,True), ("D2",280000,True), ("D3",285000,False),
            ("D4",285000,True), ("D5",290000,False),("D6",290000,False),
        ])

        # ── Tenancies ──────────────────────────────────────────────────────
        today = date.today()
        tenancies = [
            Tenancy(tenant_id=t1.id, unit_id=u_p1[0].id, monthly_rent=450000, start_date=today-timedelta(days=180), is_active=True),
            Tenancy(tenant_id=t2.id, unit_id=u_p2[2].id, monthly_rent=320000, start_date=today-timedelta(days=90),  is_active=True),
            Tenancy(tenant_id=t3.id, unit_id=u_p3[11].id,monthly_rent=550000, start_date=today-timedelta(days=60),  is_active=True),
            Tenancy(tenant_id=t4.id, unit_id=u_p3[6].id, monthly_rent=620000, start_date=today-timedelta(days=120), is_active=True),
            Tenancy(tenant_id=t5.id, unit_id=u_p4[1].id, monthly_rent=280000, start_date=today-timedelta(days=45),  is_active=True),
        ]
        db.session.add_all(tenancies)
        db.session.flush()

        # ── Invoices ───────────────────────────────────────────────────────
        invoices = [
            Invoice(inv_number="INV-001", type="Rent",           amount=450000, due_date=date(2025,6,1),  status="paid",    paystack_ref="PSK-88291", tenant_id=t1.id, unit_id=u_p1[0].id),
            Invoice(inv_number="INV-002", type="Rent",           amount=320000, due_date=date(2025,6,5),  status="pending", tenant_id=t2.id, unit_id=u_p2[2].id),
            Invoice(inv_number="INV-003", type="Rent",           amount=550000, due_date=date(2025,5,28), status="overdue", tenant_id=t3.id, unit_id=u_p3[11].id),
            Invoice(inv_number="INV-004", type="Rent",           amount=620000, due_date=date(2025,6,10), status="pending", tenant_id=t4.id, unit_id=u_p3[6].id),
            Invoice(inv_number="INV-005", type="Service Charge", amount=25000,  due_date=date(2025,5,20), status="paid",    paystack_ref="PSK-77183", tenant_id=t1.id, unit_id=u_p1[0].id),
            Invoice(inv_number="INV-006", type="Rent",           amount=280000, due_date=date(2025,5,25), status="overdue", tenant_id=t5.id, unit_id=u_p4[1].id),
        ]
        db.session.add_all(invoices)
        db.session.flush()

        # ── Transactions ───────────────────────────────────────────────────
        txns = [
            Transaction(txn_number="TXN-001", paystack_ref="PSK-88291", amount=450000, method="Paystack Card", status="confirmed", tenant_id=t1.id, invoice_id=invoices[0].id, date=datetime(2025,5,29)),
            Transaction(txn_number="TXN-002", paystack_ref="PSK-77183", amount=25000,  method="Bank Transfer",  status="confirmed", tenant_id=t1.id, invoice_id=invoices[4].id, date=datetime(2025,5,19)),
            Transaction(txn_number="TXN-003", paystack_ref="PSK-66021", amount=320000, method="Paystack Card", status="pending",   tenant_id=t2.id, invoice_id=invoices[1].id, date=datetime(2025,5,30)),
        ]
        db.session.add_all(txns)
        db.session.flush()

        # ── Tickets ────────────────────────────────────────────────────────
        tickets = [
            Ticket(ticket_number="SR-001", title="AC Unit not cooling",        category="HVAC",      priority="high",   status="in-progress", technician="Emeka Obi",     date_raised=datetime(2025,5,28), tenant_id=t3.id, property_id=p1.id, unit_id=u_p1[2].id),
            Ticket(ticket_number="SR-002", title="Water leakage in bathroom",  category="Plumbing",  priority="urgent", status="open",        technician=None,             date_raised=datetime(2025,5,29), tenant_id=t2.id, property_id=p2.id, unit_id=u_p2[1].id),
            Ticket(ticket_number="SR-003", title="Broken window latch",        category="Carpentry", priority="low",    status="resolved",    technician="Tunde Adebisi", date_raised=datetime(2025,5,22), tenant_id=t4.id, property_id=p3.id, unit_id=u_p3[6].id,  date_resolved=datetime(2025,5,25)),
            Ticket(ticket_number="SR-004", title="Electrical fault in kitchen",category="Electrical",priority="urgent", status="open",        technician=None,             date_raised=datetime(2025,5,30), tenant_id=t5.id, property_id=p4.id, unit_id=u_p4[1].id),
            Ticket(ticket_number="SR-005", title="Paint peeling from walls",   category="General",   priority="medium", status="open",        technician=None,             date_raised=datetime(2025,5,27), tenant_id=t1.id, property_id=p1.id, unit_id=u_p1[0].id),
        ]
        db.session.add_all(tickets)
        db.session.commit()

        print("✅  Database seeded successfully!")
        print(f"   Users:        {User.query.count()}")
        print(f"   Properties:   {Property.query.count()}")
        print(f"   Units:        {Unit.query.count()}")
        print(f"   Tenancies:    {Tenancy.query.count()}")
        print(f"   Invoices:     {Invoice.query.count()}")
        print(f"   Transactions: {Transaction.query.count()}")
        print(f"   Tickets:      {Ticket.query.count()}")

if __name__ == "__main__":
    seed()
