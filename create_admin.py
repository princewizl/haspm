from app import app
from models import User,db

with app.app_context():

    existing = User.query.filter_by(email="admin@example.com").first()

    if existing:
        print("Admin already exists.")
    else:
        admin = User(
            name="Administrator",
            email="olufemi.mohammed11@gmail.com",
            password="Pass@123",
            role="Admin",
            phone="08032035827"
        )

        db.session.add(admin)
        db.session.commit()

        print("Admin user created successfully.")