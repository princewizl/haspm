import os
import secrets

from app import app
from models import User, db

with app.app_context():

    existing = User.query.filter_by(email="admin@example.com").first()

    if existing:
        print("Admin already exists.")
    else:
        # Never hard-code a real credential. Supply one via ADMIN_PASSWORD,
        # otherwise a strong random password is generated and printed once.
        raw = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(16)

        admin = User(
            name="Administrator",
            email=os.environ.get("ADMIN_EMAIL", "olufemi.mohammed11@gmail.com"),
            role="Admin",
            phone="08032035827"
        )
        admin.set_password(raw)

        db.session.add(admin)
        db.session.commit()

        print("Admin user created successfully.")
        if not os.environ.get("ADMIN_PASSWORD"):
            print(f"Generated password (shown once): {raw}")