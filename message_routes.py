from flask import request, redirect, url_for, render_template, session, flash
from sqlalchemy import or_
from sqlalchemy.orm import joinedload
from models import db, User, Message
from datetime import datetime


# Messages are stored two levels deep: a root message (parent_id IS NULL) and
# its replies.  A "thread" is a root plus every reply that hangs off it.
def _thread_key(m):
    return m.parent_id or m.id


def _thread_has_files(message_ids):
    from app import Attachment
    if not message_ids:
        return False
    return db.session.query(
        Attachment.query.filter(
            Attachment.parent_type == Attachment.PARENT_MESSAGE,
            Attachment.parent_id.in_(message_ids)).exists()).scalar()


def _build_threads(uid, tab, q=""):
    """Group every message this user can see into threads, newest activity first.

    Inbox = threads where the user received at least one message.
    Sent   = threads where the user sent at least one message.
    A thread can legitimately appear in both, exactly like a mail client.
    """
    involved = (Message.query
                .options(joinedload(Message.sender), joinedload(Message.recipient))
                .filter(or_(Message.sender_id == uid, Message.recipient_id == uid))
                .order_by(Message.created_at.asc())
                .all())

    grouped = {}
    for m in involved:
        grouped.setdefault(_thread_key(m), []).append(m)

    # A reply is visible without its root when the root was addressed elsewhere;
    # pull in any root we are missing so the subject line is never blank.
    have = {m.id for m in involved}
    missing = [k for k in grouped if k not in have]
    roots = {}
    if missing:
        for r in (Message.query
                  .options(joinedload(Message.sender), joinedload(Message.recipient))
                  .filter(Message.id.in_(missing)).all()):
            roots[r.id] = r
    for m in involved:
        if m.parent_id is None:
            roots[m.id] = m

    threads = []
    for key, msgs in grouped.items():
        root = roots.get(key)
        if root is None:
            continue

        msgs.sort(key=lambda m: m.created_at or datetime.min)
        last = msgs[-1]

        received = [m for m in msgs if m.recipient_id == uid]
        sent     = [m for m in msgs if m.sender_id == uid]

        if tab == "sent" and not sent:
            continue
        if tab != "sent" and not received:
            continue

        other = root.recipient if root.sender_id == uid else root.sender
        unread = sum(1 for m in received if m.read_at is None)

        if q:
            needle = q.lower()
            haystack = " ".join([
                root.subject or "",
                other.name or "",
                other.email or "",
                " ".join(m.body or "" for m in msgs),
            ]).lower()
            if needle not in haystack:
                continue

        threads.append({
            "root": root,
            "other": other,
            "last": last,
            "count": len(msgs),
            "replies": len(msgs) - 1 if root in msgs else len(msgs),
            "unread": unread,
            "last_at": last.created_at,
            "snippet": (last.body or "").strip().replace("\n", " ")[:110],
            "has_files": _thread_has_files([m.id for m in msgs]),
        })

    threads.sort(key=lambda t: t["last_at"] or datetime.min, reverse=True)
    return threads


def register_message_routes(app):

    @app.context_processor
    def inject_unread_count():
        """Unread badge counts every unread message, replies included."""
        if "user_id" in session:
            count = Message.query.filter_by(
                recipient_id=session["user_id"], read_at=None
            ).count()
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
        q    = request.args.get("q", "").strip()

        threads = _build_threads(uid, tab, q)

        # Counts for the sidebar are unfiltered by the search box.
        inbox_count = len(_build_threads(uid, "inbox"))
        sent_count  = len(_build_threads(uid, "sent"))

        if role in ("Tenant", "Landlord"):
            recipients = User.query.filter_by(role="Admin", is_active=True).all()
        else:
            recipients = User.query.filter(
                User.id != uid, User.is_active == True
            ).order_by(User.role, User.name).all()

        return render_template(
            "messages.html",
            threads=threads, tab=tab, q=q,
            inbox_count=inbox_count, sent_count=sent_count,
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

        if recipient_id == uid:
            flash("You cannot send a message to yourself.", "danger")
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
        db.session.flush()          # need msg.id to attach files

        from app import save_attachments, Attachment
        saved, errors = save_attachments(
            request.files.getlist("attachments"),
            Attachment.PARENT_MESSAGE, msg.id)
        db.session.commit()

        for e in errors:
            flash(e, "danger")
        flash("Message sent successfully."
              + (f" {saved} file(s) attached." if saved else ""), "success")
        return redirect(url_for("message_view", msg_id=msg.id))

    # ── View thread ───────────────────────────
    @app.route("/messages/<int:msg_id>")
    def message_view(msg_id):
        if "user_id" not in session:
            return redirect(url_for("login"))
        uid = session["user_id"]
        msg = Message.query.get_or_404(msg_id)

        # Opening a reply link lands on the thread it belongs to.
        if msg.parent_id:
            return redirect(url_for("message_view", msg_id=msg.parent_id))

        if msg.sender_id != uid and msg.recipient_id != uid:
            flash("Access denied.", "danger")
            return redirect(url_for("messages"))

        if msg.recipient_id == uid and msg.read_at is None:
            msg.read_at = datetime.utcnow()

        replies = (Message.query
                   .options(joinedload(Message.sender))
                   .filter_by(parent_id=msg_id)
                   .order_by(Message.created_at.asc()).all())

        for r in replies:
            if r.recipient_id == uid and r.read_at is None:
                r.read_at = datetime.utcnow()

        db.session.commit()

        other = msg.recipient if msg.sender_id == uid else msg.sender
        return render_template(
            "message_view.html",
            msg=msg, replies=replies, other=other, role=session["role"]
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

        subject = msg.subject or ""
        reply = Message(
            sender_id=uid,
            recipient_id=recipient_id,
            subject=subject if subject.lower().startswith("re:") else "Re: " + subject,
            body=body,
            parent_id=msg_id
        )
        db.session.add(reply)
        db.session.flush()

        from app import save_attachments, Attachment
        saved, errors = save_attachments(
            request.files.getlist("attachments"),
            Attachment.PARENT_MESSAGE, reply.id)
        db.session.commit()

        for e in errors:
            flash(e, "danger")
        flash("Reply sent." + (f" {saved} file(s) attached." if saved else ""), "success")
        return redirect(url_for("message_view", msg_id=msg_id))

    # ── Mark a thread read / unread ───────────
    @app.route("/messages/<int:msg_id>/mark", methods=["POST"])
    def message_mark(msg_id):
        if "user_id" not in session:
            return redirect(url_for("login"))
        uid = session["user_id"]
        msg = Message.query.get_or_404(msg_id)

        if msg.sender_id != uid and msg.recipient_id != uid:
            flash("Access denied.", "danger")
            return redirect(url_for("messages"))

        state = request.form.get("state", "read")
        stamp = datetime.utcnow() if state == "read" else None

        thread = [msg] + Message.query.filter_by(parent_id=msg.id).all()
        for m in thread:
            if m.recipient_id == uid:
                m.read_at = stamp
        db.session.commit()

        flash("Marked as unread." if stamp is None else "Marked as read.", "success")
        return redirect(request.form.get("next") or url_for("messages"))

    # ── Mark every inbox thread read ──────────
    @app.route("/messages/mark-all-read", methods=["POST"])
    def messages_mark_all_read():
        if "user_id" not in session:
            return redirect(url_for("login"))
        uid = session["user_id"]
        now = datetime.utcnow()

        unread = Message.query.filter_by(recipient_id=uid, read_at=None).all()
        for m in unread:
            m.read_at = now
        db.session.commit()

        flash(f"{len(unread)} message(s) marked as read." if unread
              else "No unread messages.", "success")
        return redirect(url_for("messages", tab=request.form.get("tab", "inbox")))

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
            # Remove the thread's files from disk too, so deleting a
            # conversation does not leave orphans behind.
            import os
            from app import Attachment, _attach_folder

            replies = Message.query.filter_by(parent_id=msg_id).all()
            ids = [m.id for m in replies] + [msg.id]
            folder = _attach_folder()
            for a in Attachment.query.filter(
                    Attachment.parent_type == Attachment.PARENT_MESSAGE,
                    Attachment.parent_id.in_(ids)).all():
                fp = os.path.join(folder, a.stored_filename)
                if os.path.exists(fp):
                    os.remove(fp)
                db.session.delete(a)

            for r in replies:
                db.session.delete(r)
            db.session.delete(msg)
            db.session.commit()
            flash("Conversation deleted.", "success")

        return redirect(url_for("messages", tab=request.form.get("tab", "inbox")))
