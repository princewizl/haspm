"""Dedicated scheduler process.

The daily jobs email real tenants (invoices, overdue reminders, lease expiry).
APScheduler runs in-process, so if the web container ran it, every gunicorn
worker would fire the same job and tenants would get duplicate mail. The web
containers therefore start with ENABLE_SCHEDULER=0 and this single-instance
container owns the schedule.
"""
import logging
import os
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hs.scheduler_worker")

# app.py starts a scheduler of its own at import time unless this is off.
# Force it off here so importing the app cannot race the one we start below,
# regardless of what the compose file passes in.
os.environ["ENABLE_SCHEDULER"] = "0"

from app import app          # noqa: E402  (import order is deliberate)
from models import db        # noqa: E402
from scheduler import init_scheduler  # noqa: E402


def main():
    with app.app_context():
        db.create_all()

    sched = init_scheduler(app)
    if sched is None:
        log.error("scheduler failed to start; exiting so the container restarts")
        raise SystemExit(1)

    log.info("scheduler worker running — jobs: %s",
             [j.id for j in sched.get_jobs()])
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        log.info("shutting down scheduler")
        sched.shutdown()


if __name__ == "__main__":
    main()
