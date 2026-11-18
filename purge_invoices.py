"""Delete every invoice, with the payment records that hang off them.

Run inside the environment's container so it uses that environment's database:

  docker compose run --rm --no-deps web-live python purge_invoices.py        # preview only
  docker compose run --rm --no-deps web-live python purge_invoices.py --yes  # delete

Payments (transactions) and bank-transfer claims cannot exist without their
invoice, so they are deleted too. Tenants, tenancies, properties, units,
expenses, ledger entries and landlord payouts are not touched.

Take a backup first: ./deploy/backup.sh live
"""
import sys

from app import app
from models import db, Invoice, Transaction, BankTransferClaim, NotificationLog

CONFIRM = "--yes" in sys.argv


def main():
    with app.app_context():
        invoices = Invoice.query.count()
        paid     = Invoice.query.filter_by(status="paid").count()
        txns     = Transaction.query.count()
        claims   = BankTransferClaim.query.count()
        total    = db.session.query(db.func.coalesce(db.func.sum(Transaction.amount), 0)).scalar()
        notices  = NotificationLog.query.filter_by(notif_type="rent_invoice").count()

        print(f"Database: {app.config['SQLALCHEMY_DATABASE_URI']}")
        print(f"  invoices              {invoices}  ({paid} paid)")
        print(f"  payments/transactions {txns}  (N{total:,.2f} recorded)")
        print(f"  bank-transfer claims  {claims}")
        print(f"  invoice email log     {notices}")

        if not invoices and not txns and not claims:
            print("\nNothing to delete.")
            return
        if not CONFIRM:
            print("\nPreview only - nothing was deleted. Re-run with --yes to delete.")
            return

        # Children first: claims and transactions reference invoices.
        BankTransferClaim.query.delete(synchronize_session=False)
        Transaction.query.delete(synchronize_session=False)
        Invoice.query.delete(synchronize_session=False)
        # The log of "invoice emailed" notices points at invoice ids that no
        # longer exist; clearing it lets the next real invoice be emailed.
        NotificationLog.query.filter_by(notif_type="rent_invoice").delete(synchronize_session=False)
        db.session.commit()
        print(f"\nDeleted {invoices} invoice(s), {txns} payment(s), {claims} transfer claim(s).")


if __name__ == "__main__":
    main()
