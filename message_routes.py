from flask import request, redirect, url_for, render_template, session, flash
from models import db, User, Message
from datetime import datetime


def register_message_routes(app):

    @app.context_processor
    def inject_unread_count():
        if "user_id" in session:
            count = Message.query.filter_by(
                recipient_id=session["user_id"], read_at=None
            ).filter(Message.parent_id.is_(None)).count()
            return {"unread_msg_count": count}
        return {"unread_msg_count": 0}

    # ── Inbox / Sent ──────────────────────────
    @app.route("/messages")
    def messages():
        if "user_id" not in session:
            return redirect(url_for("login"))
        uid  = session["user_id"]
        role = session["role"]
        tab  = request.args.get("tab", "inbox")

        if tab == "sent":
            items = (Message.query
                     .filter_by(sender_id=uid)
                     .filter(Message.parent_id.is_(None))
                     .order_by(Message.created_at.desc()).all())
        else:
            items = (Message.query
                     .filter_by(recipient_id=uid)
                     .filter(Message.parent_id.is_(None))
                     .order_by(Message.created_at.desc()).all())

        if role in ("Tenant", "Landlord"):
            recipients = User.query.filter_by(role="Admin", is_active=True).all()
        else:
            recipients = User.query.filter(
                User.id != uid, User.is_active == True
            ).order_by(User.role, User.name).all()

        return render_template(
            "messages.html",
            items=items, tab=tab,
            recipients=recipients,
            role=role
        )

    # ── Send new message ──────────────────────
    @app.route("/messages/send", methods=["POST"])
    def messages_send():
        if "user_id" not in session:
            return redirect(url_for("login"))
        uid  = session["user_id"]
        role = session["role"]

        recipient_id = request.form.get("recipient_id", type=int)
        subject = request.form.get("subject", "").strip()
        body    = request.form.get("body", "").strip()

        if not recipient_id or not subject or not body:
            flash("All fields are required.", "danger")
            return redirect(url_for("messages"))

        recipient = User.query.get(recipient_id)
        if not recipient:
            flash("Recipient not found.", "danger")
            return redirect(url_for("messages"))

        if role in ("Tenant", "Landlord") and recipient.role != "Admin":
            flash("You can only message staff members.", "danger")
            return redirect(url_for("messages"))

        msg = Message(sender_id=uid, recipient_id=recipient_id,
                      subject=subject, body=body)
        db.session.add(msg)
        db.session.commit()
        flash("Message sent successfully.", "success")
        return redirect(url_for("messages"))

    # ── View thread ───────────────────────────
    @app.route("/messages/<int:msg_id>")
    def message_view(msg_id):
        if "user_id" not in session:
            return redirect(url_for("login"))
        uid = session["user_id"]
        msg = Message.query.get_or_404(msg_id)

        if msg.sender_id != uid and msg.recipient_id != uid:
            flash("Access denied.", "danger")
            return redirect(url_for("messages"))

        if msg.recipient_id == uid and msg.read_at is None:
            msg.read_at = datetime.utcnow()

        replies = (Message.query
                   .filter_by(parent_id=msg_id)
                   .order_by(Message.created_at.asc()).all())

        for r in replies:
            if r.recipient_id == uid and r.read_at is None:
                r.read_at = datetime.utcnow()

        db.session.commit()
        return render_template(
            "message_view.html",
            msg=msg, replies=replies, role=session["role"]
        )

    # ── Reply ─────────────────────────────────
    @app.route("/messages/<int:msg_id>/reply", methods=["POST"])
    def message_reply(msg_id):
        if "user_id" not in session:
            return redirect(url_for("login"))
        uid  = session["user_id"]
        role = session["role"]
        msg  = Message.query.get_or_404(msg_id)

        if msg.sender_id != uid and msg.recipient_id != uid:
            flash("Access denied.", "danger")
            return redirect(url_for("messages"))

        body = request.form.get("body", "").strip()
        if not body:
            flash("Reply cannot be empty.", "danger")
            return redirect(url_for("message_view", msg_id=msg_id))

        recipient_id = msg.sender_id if msg.recipient_id == uid else msg.recipient_id
        recipient = User.query.get(recipient_id)

        if role in ("Tenant", "Landlord") and recipient and recipient.role != "Admin":
            flash("You can only reply to staff members.", "danger")
            return redirect(url_for("message_view", msg_id=msg_id))

        reply = Message(
            sender_id=uid,
            recipient_id=recipient_id,
            subject="Re: " + msg.subject,
            body=body,
            parent_id=msg_id
        )
        db.session.add(reply)
        db.session.commit()
        flash("Reply sent.", "success")
        return redirect(url_for("message_view", msg_id=msg_id))

    # ── Delete ────────────────────────────────
    @app.route("/messages/<int:msg_id>/delete", methods=["POST"])
    def message_delete(msg_id):
        if "user_id" not in session:
            return redirect(url_for("login"))
        uid = session["user_id"]
        msg = Message.query.get_or_404(msg_id)

        if msg.sender_id != uid and msg.recipient_id != uid:
            flash("Access denied.", "danger")
        else:
            Message.query.filter_by(parent_id=msg_id).delete()
            db.session.delete(msg)
            db.session.commit()
            flash("Message deleted.", "success")

        return redirect(url_for("messages"))
