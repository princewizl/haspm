"""
Migration: Change Order items 2.1–2.3
  - users.department column (Admin departments)
  - technician_ratings table
  - messages table (if not already created)

Safe to run multiple times. Run on the server with:  python migrate_departments.py
"""
from sqlalchemy import text
from app import app, db


def column_exists(table, column):
    with db.engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


with app.app_context():
    # 1. New tables (messages, technician_ratings) — no-op if they exist
    db.create_all()
    print("create_all: OK")

    # 2. users.department column
    if not column_exists("users", "department"):
        with db.engine.connect() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN department VARCHAR(30)"))
            conn.commit()
        print("users.department: ADDED")
    else:
        print("users.department: already present")

    print("Migration complete.")
