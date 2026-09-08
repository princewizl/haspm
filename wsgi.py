"""Gunicorn entry point.

`flask run` created the schema via the __main__ block in app.py; under gunicorn
that never executes, so the tables are created here instead. SQLite starts as an
empty file on a fresh volume, so this runs on every first boot of a new
environment and is a no-op afterwards.
"""
import logging
import os

from app import app
from models import db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hs.wsgi")


def _init_db():
    with app.app_context():
        db.create_all()
        log.info("[init] schema ready (%s)", app.config["SQLALCHEMY_DATABASE_URI"])


_init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
