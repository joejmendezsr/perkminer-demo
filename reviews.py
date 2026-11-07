"""Star ratings, written reviews and feedback moderation for advertisers and freelancers.

Rules (as requested)
--------------------
* Only a member with a finalized transaction at that advertiser/freelancer (the one on the receipt
  that shows amount paid and cash back earned) can leave ONE rating (1-5 stars) and a comment.
* Reviews show behind a "View Reviews" link on every large listing; the average sits at the top of
  small listings and in the hero of large listings.
* Only 1- and 2-star reviews can go to feedback moderation, a 3-way secure chat between the
  advertiser (or freelancer), the member and Perk Miner support (an Interaction of type "Feedback",
  shown through the existing support_session page).
    - the advertiser has 3 days from the review to start moderation (button on the dashboard)
    - the session then has 7 days to be resolved; clicking the button again reopens an ended chat
    - after either window closes the review is locked and stays as written
    - when it is resolved, support can let the member edit the review once (7 days to do it)
* Every 1-star review alerts admin (dashboard count + email) who can suspend the advertiser/freelancer.

Freelancers use their hidden "shadow" Business row, so one code path serves both.
register(app, ns) is called once from app.py after freelance.register().
"""
from datetime import datetime, timedelta

START_DAYS = 3        # to start moderation after the review is posted
RESOLVE_DAYS = 7      # to resolve once moderation has started
EDIT_DAYS = 7         # member's time to edit after support allows it
MAX_COMMENT = 500
MIN_NEG_COMMENT = 10
RETURNED = ("returned", "refunded", "reversed")


def register(app, ns):
    db = ns["db"]
    from flask import (abort, flash, redirect, render_template, request, session, url_for)
    from flask_login import current_user, login_required
    from markupsafe import Markup, escape

    User = ns["User"]
    Business = ns["Business"]
    Interaction = ns["Interaction"]
    UserTransaction = ns["UserTransaction"]
    Message = ns["Message"]
    FreelanceBusiness = ns.get("FreelanceBusiness")

    # ------------------------------------------------------------------ model
    class BusinessReview(db.Model):
        __tablename__ = "business_review"
        __table_args__ = (db.UniqueConstraint("business_id", "user_id", name="uq_review_business_user"),)
        id = db.Column(db.Integer, primary_key=True)
        business_id = db.Column(db.Integer, db.ForeignKey("business.id"), nullable=False, index=True)
        user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
        transaction_id = db.Column(db.String(48))
        rating = db.Column(db.Integer, nullable=False)
        previous_rating = db.Column(db.Integer)
        comment = db.Column(db.Text)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        updated_at = db.Column(db.DateTime, default=datetime.utcnow)
        # moderation
        mod_started_at = db.Column(db.DateTime)
        mod_interaction_id = db.Column(db.Integer, db.ForeignKey("interaction.id"))
        mod_resolved_at = db.Column(db.DateTime)
        edit_until = db.Column(db.DateTime)          # set when support allows the member to edit
        edited_at = db.Column(db.DateTime)
        # admin alert for 1 star
        admin_alerted_at = db.Column(db.DateTime)
        admin_ack_at = db.Column(db.DateTime)
        admin_note = db.Column(db.Text)

        user = db.relationship("User", foreign_keys=[user_id])
        business = db.relationship("Business", foreign_keys=[business_id])

        # ---- state (computed on read, so nothing needs a background job) ----
        @property
        def negative(self):
            return (self.rating or 0) <= 2

        @property
        def start_deadline(self):
            return (self.created_at or datetime.utcnow()) + timedelta(days=START_DAYS)

        @property
        def resolve_deadline(self):
            return (self.mod_started_at + timedelta(days=RESOLVE_DAYS)) if self.mod_started_at else None

        @property
        def can_edit(self):
            return bool(self.edit_until and datetime.utcnow() <= self.edit_until)

        @property
        def state(self):
            """none | open | active | resolved | locked"""
            now = datetime.utcnow()
            if self.mod_resolved_at:
                return "resolved"
            if not self.negative:
                return "none"
            if not self.mod_started_at:
                return "open" if now <= self.start_deadline else "locked"
            return "active" if now <= self.resolve_deadline else "locked"

        @property
        def time_left(self):
            now = datetime.utcnow()
            end = self.start_deadline if self.state == "open" else self.resolve_deadline
            if not end or end <= now:
                return ""
            secs = int((end - now).total_seconds())
            d, h = secs // 86400, (secs % 86400) // 3600
            return f"{d}d {h}h" if d else f"{h}h {(secs % 3600) // 60}m"

        @property
        def reviewer(self):
            u = self.user
            nm = (getattr(u, "name", None) or "").strip() if u else ""
            if not nm:
                return "Perk Miner member"
            parts = nm.split()
            return parts[0] + (f" {parts[-1][0]}." if len(parts) > 1 else "")

    # ------------------------------------------------------------------ helpers
    def recalc(biz_id):
        rows = db.session.query(BusinessReview.rating).filter_by(business_id=biz_id).all()
        biz = Business.query.get(biz_id)
        if not biz:
            return
        n = len(rows)
        biz.rating_count = n
        biz.rating_avg = round(sum(r[0] for r in rows) / n, 2) if n else 0.0

    def eligible_txn(user, biz_id):
        """Latest finalized, not-returned purchase of this member at this business/freelancer."""
        if not user or not getattr(user, "is_authenticated", False):
            return None
        q = (UserTransaction.query
             .join(Interaction, Interaction.id == UserTransaction.interaction_id)
             .filter(Interaction.business_id == biz_id,
                     UserTransaction.user_referral_id == user.referral_code)
             .order_by(UserTransaction.date_time.desc()))
        for t in q.all():
            if (getattr(t, "status", None) or "").lower() not in RETURNED:
                return t
        return None

    def is_owner(biz, user):
        return bool(biz and user and getattr(user, "is_authenticated", False) and biz.owner_user_id == user.id)

    def public_target(biz):
        """Reviews are shown only for listings that are actually public."""
        if not biz:
            return False
        if biz.owner_user_id:
            fb = FreelanceBusiness.query.filter_by(shadow_business_id=biz.id).first() if FreelanceBusiness else None
            return bool(fb and fb.is_live and fb.status == "approved")
        return biz.status == "approved" and not biz.is_suspended

    def business_for_session():
        """The Business the current visitor manages: an advertiser login, or a member's freelancer listing."""
        bid = session.get("business_id")
        if bid:
            return Business.query.get(bid)
        if current_user.is_authenticated:
            return Business.query.filter_by(owner_user_id=current_user.id).first()
        return None

    def notify(to, subject, html):
        try:
            ns["send_email"](to, subject, html)
        except Exception as e:  # never break the request over an email
            app.logger.warning("review notify failed: %s", e)

    def support_email():
        return ns["os"].environ.get("REVIEW_ALERT_EMAIL", "support@perkminer.com")

    def site():
        root = request.url_root.rstrip("/")
        return "https://perkminer.com" if "onrender.com" in root else root

    # ------------------------------------------------------------------ template helpers
    def review_widget(biz_id, txn=None):
        """Everything the receipt page needs to show the rating form."""
        if not current_user.is_authenticated or not biz_id:
            return {"show": False}
        biz = Business.query.get(biz_id)
        if not biz or is_owner(biz, current_user):
            return {"show": False}
        t = eligible_txn(current_user, biz_id)
        if not t:
            return {"show": False}
        rv = BusinessReview.query.filter_by(business_id=biz_id, user_id=current_user.id).first()
        return {"show": True, "biz": biz, "review": rv, "txn": t,
                "can_edit": bool(rv and rv.can_edit), "can_new": rv is None,
                "state": rv.state if rv else None}

    def review_alerts(biz_id):
        """Negative reviews the advertiser/freelancer can still act on (dashboard button)."""
        if not biz_id:
            return []
        rows = (BusinessReview.query.filter(BusinessReview.business_id == biz_id,
                                            BusinessReview.rating <= 2)
                .order_by(BusinessReview.created_at.desc()).all())
        return [r for r in rows if r.state in ("open", "active")]

    def review_summary(biz_id):
        biz = Business.query.get(biz_id)
        return {"avg": float(biz.rating_avg or 0), "count": int(biz.rating_count or 0)} if biz else {"avg": 0, "count": 0}

    def review_edits_for_user():
        """Reviews support has re-opened for the logged-in member (shown on their active sessions page)."""
        if not current_user.is_authenticated:
            return []
        now = datetime.utcnow()
        return (BusinessReview.query.filter(BusinessReview.user_id == current_user.id,
                                            BusinessReview.edit_until.isnot(None),
                                            BusinessReview.edit_until >= now)
                .order_by(BusinessReview.updated_at.desc()).all())

    def review_edit_for_session(interaction):
        if not current_user.is_authenticated or interaction.service_type != "Feedback" \
                or interaction.user_id != current_user.id:
            return None
        rv = BusinessReview.query.filter_by(mod_interaction_id=interaction.id, user_id=current_user.id).first()
        return rv if rv and rv.can_edit else None

    app.jinja_env.globals.update(review_widget=review_widget, review_alerts=review_alerts,
                                 review_summary=review_summary, review_edits_for_user=review_edits_for_user,
                                 review_edit_for_session=review_edit_for_session)

    @app.route("/reviews/<int:rid>/modify")
    @login_required
    def review_modify(rid):
        rv = BusinessReview.query.get_or_404(rid)
        if rv.user_id != current_user.id:
            abort(403)
        if not rv.can_edit:
            flash("This review can't be changed right now.", "info")
            return redirect(url_for("user_biz_interactions"))
        return render_template("review_modify.html", rv=rv)

    @app.route("/reviews/session/<int:iid>/status")
    @login_required
    def review_session_status(iid):
        inter = Interaction.query.get_or_404(iid)
        if inter.user_id != current_user.id:
            abort(403)
        rv = review_edit_for_session(inter)
        return {"can_edit": bool(rv), "url": url_for("user_biz_interactions")}

    # ------------------------------------------------------------------ public: reviews list
    @app.route("/reviews/<int:biz_id>")
    def reviews_list(biz_id):
        biz = Business.query.get_or_404(biz_id)
        if not public_target(biz):
            abort(404)
        PAGE = 10
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            offset = 0
        q = BusinessReview.query.filter_by(business_id=biz_id)
        total = q.count()
        rows = q.order_by(BusinessReview.created_at.desc(), BusinessReview.id.desc()).offset(offset).limit(PAGE).all()
        dist = {i: 0 for i in range(1, 6)}
        if offset == 0:
            for rating, n in (db.session.query(BusinessReview.rating, db.func.count(BusinessReview.id))
                              .filter_by(business_id=biz_id).group_by(BusinessReview.rating).all()):
                dist[rating] = n
        nxt = (url_for("reviews_list", biz_id=biz_id, offset=offset + PAGE)
               if offset + PAGE < total else None)
        return render_template("partials/_reviews_list.html", reviews=rows, biz=biz, dist=dist,
                               total=total, first=(offset == 0), next_url=nxt, remaining=max(0, total - offset - PAGE))

    # ------------------------------------------------------------------ member: submit / edit
    @app.route("/reviews/submit", methods=["POST"])
    @login_required
    def review_submit():
        back = request.form.get("next") or request.referrer or url_for("user_receipts")
        if back.startswith("//") or not (back.startswith("/") or back.startswith(request.host_url)):
            back = url_for("user_receipts")
        try:
            biz_id = int(request.form.get("biz_id", 0))
            rating = int(request.form.get("rating", 0))
        except ValueError:
            flash("Please choose a star rating.", "warning")
            return redirect(back)
        comment = (request.form.get("comment") or "").strip()[:MAX_COMMENT]
        biz = Business.query.get(biz_id)
        if not biz or is_owner(biz, current_user):
            abort(404)
        txn = eligible_txn(current_user, biz_id)
        if not txn:
            flash("Only members with a completed, finalized purchase can leave a rating.", "warning")
            return redirect(back)
        if rating < 1 or rating > 5:
            flash("Please choose 1 to 5 stars.", "warning")
            return redirect(back)
        if rating <= 2 and len(comment) < MIN_NEG_COMMENT:
            flash("Please tell the business what went wrong (at least a sentence) so they can fix it.", "warning")
            return redirect(back)

        rv = BusinessReview.query.filter_by(business_id=biz_id, user_id=current_user.id).first()
        now = datetime.utcnow()
        if rv is None:
            rv = BusinessReview(business_id=biz_id, user_id=current_user.id, rating=rating, comment=comment,
                                transaction_id=txn.transaction_id, created_at=now, updated_at=now)
            db.session.add(rv)
            db.session.flush()
            msg = "Thank you! Your rating has been posted."
        elif rv.can_edit:
            rv.previous_rating = rv.rating
            rv.rating, rv.comment, rv.updated_at, rv.edited_at = rating, comment, now, now
            rv.edit_until = None
            if rv.mod_started_at and not rv.mod_resolved_at:
                rv.mod_resolved_at = now
            msg = "Your review has been updated. Thank you for giving them the chance to make it right."
        else:
            flash("You have already reviewed this one. Reviews can only be changed when Perk Miner support re-opens it.", "info")
            return redirect(back)

        recalc(biz_id)
        db.session.commit()
        flash(msg, "success")

        if rating == 1 and not rv.admin_alerted_at:
            rv.admin_alerted_at = now
            db.session.commit()
            notify(support_email(), f"1-star review: {biz.business_name}",
                   f"<p>A member left a <b>1-star</b> review for <b>{escape(biz.business_name)}</b>"
                   f"{' (freelancer)' if biz.owner_user_id else ''}.</p>"
                   f"<blockquote>{escape(comment)}</blockquote>"
                   f"<p>Review it at <a href='{site()}{url_for('admin_reviews')}'>{site()}{url_for('admin_reviews')}</a>. "
                   f"You can suspend the account there if this looks like fraud or something serious.</p>")
        if rating <= 2:
            if biz.owner_user_id:
                ou = User.query.get(biz.owner_user_id)
                addr = ou.email if ou else None
            else:
                addr = getattr(biz, "email", None)
            if addr:
                notify(addr, f"New {rating}-star review on Perk Miner",
                       f"<p>A member left a {rating}-star review. You have {START_DAYS} days to start feedback "
                       f"moderation from your dashboard to try to resolve it.</p>")
        return redirect(back)

    # ------------------------------------------------------------------ advertiser / freelancer: start moderation
    @app.route("/reviews/<int:rid>/resolve", methods=["POST"])
    def review_resolve(rid):
        rv = BusinessReview.query.get_or_404(rid)
        biz = rv.business
        owner_ok = (session.get("business_id") == rv.business_id) or is_owner(biz, current_user)
        if not owner_ok:
            abort(403)
        if rv.state not in ("open", "active"):
            flash("The time to resolve this feedback has passed, so the review stays as posted.", "warning")
            return redirect(request.referrer or "/")
        now = datetime.utcnow()
        if not rv.mod_started_at:
            rv.mod_started_at = now
        iact = Interaction.query.get(rv.mod_interaction_id) if rv.mod_interaction_id else None
        if iact is None:
            iact = Interaction(user_id=rv.user_id, business_id=rv.business_id, service_type="Feedback",
                               details=f"Feedback moderation: {rv.rating}-star review.", status="active")
            db.session.add(iact)
            db.session.flush()
            rv.mod_interaction_id = iact.id
            db.session.add(Message(
                interaction_id=iact.id, sender_type="customer_support", sender_id=0,
                text=(f"Feedback moderation opened for a {rv.rating}-star review.\n\nThe member wrote: "
                      f"\"{rv.comment}\"\n\nThis is a private chat between the member, {biz.business_name} and "
                      f"Perk Miner support. Please work together to resolve the issue. If it is resolved, support "
                      f"can let the member update their rating. This session can be reopened until "
                      f"{rv.resolve_deadline.strftime('%b %d, %Y')}.")))
            notify(support_email(), f"Feedback moderation started: {biz.business_name}",
                   f"<p><b>{escape(biz.business_name)}</b> started feedback moderation on a {rv.rating}-star review. "
                   f"Please join the 3-way chat: <a href='{site()}{url_for('support_session', interaction_id=iact.id)}'>"
                   f"open session</a> (also listed under Customer Support and Reviews &amp; Feedback).</p>")
            notify(rv.user.email, "A business wants to resolve your Perk Miner review",
                   f"<p><b>{escape(biz.business_name)}</b> would like to resolve the issue in your {rv.rating}-star "
                   f"review. Open your active sessions to chat privately (Perk Miner support is in the conversation): "
                   f"<a href='{site()}{url_for('user_biz_interactions')}'>{site()}{url_for('user_biz_interactions')}</a>.</p>")
        elif iact.status != "active":
            iact.status = "active"
            notify(support_email(), f"Feedback session reopened: {biz.business_name}",
                   f"<p>{escape(biz.business_name)} reopened a feedback session. "
                   f"<a href='{site()}{url_for('support_session', interaction_id=iact.id)}'>Open it</a>.</p>")
            db.session.add(Message(interaction_id=iact.id, sender_type="customer_support", sender_id=0,
                                   text=f"{biz.business_name} reopened this feedback session."))
        db.session.commit()
        return redirect(url_for("support_session", interaction_id=iact.id))

    # ------------------------------------------------------------------ admin
    def moderator_required(f):
        from functools import wraps

        @wraps(f)
        @login_required
        def wrapper(*a, **kw):
            if not (current_user.has_role("customer_support") or current_user.has_role("feedback_moderation")
                    or current_user.has_role("super_admin")):
                abort(403)
            return f(*a, **kw)
        return wrapper

    @app.route("/admin/reviews")
    @moderator_required
    def admin_reviews():
        base = BusinessReview.query.filter(BusinessReview.rating <= 2).order_by(BusinessReview.created_at.desc())
        rows = base.limit(200).all()
        alerts = [r for r in rows if r.rating == 1 and not r.admin_ack_at]
        active = [r for r in rows if r.state == "active" and r not in alerts]
        resolved = [r for r in rows if r.state == "resolved" and r not in alerts]
        other = [r for r in rows if r not in alerts and r not in active and r not in resolved]
        return render_template("admin_reviews.html", alerts=alerts, active=active, resolved=resolved, other=other)

    def _rv(rid):
        return BusinessReview.query.get_or_404(rid)

    @app.route("/admin/reviews/<int:rid>/ack", methods=["POST"])
    @moderator_required
    def admin_review_ack(rid):
        rv = _rv(rid)
        rv.admin_ack_at = datetime.utcnow()
        rv.admin_note = (request.form.get("note") or "").strip()[:500] or rv.admin_note
        db.session.commit()
        flash("Alert cleared.", "success")
        return redirect(url_for("admin_reviews"))

    @app.route("/admin/reviews/<int:rid>/allow-edit", methods=["POST"])
    @moderator_required
    def admin_review_allow_edit(rid):
        """Issue resolved: let the member change the rating/comment once."""
        rv = _rv(rid)
        now = datetime.utcnow()
        rv.edit_until = now + timedelta(days=EDIT_DAYS)
        if rv.mod_started_at and not rv.mod_resolved_at:
            rv.mod_resolved_at = now
        inter = Interaction.query.get(rv.mod_interaction_id) if rv.mod_interaction_id else None
        if inter and inter.status == "active":
            db.session.add(Message(interaction_id=inter.id, sender_type="customer_support",
                                   sender_id=current_user.id,
                                   text="Marked resolved. The member can now update their review from their receipt."))
        db.session.commit()
        notify(rv.user.email, "You can now update your Perk Miner review",
               f"<p>Thanks for working with {escape(rv.business.business_name)}. Open the receipt for your purchase "
               f"(<a href='{site()}{url_for('user_receipts')}'>My Receipts</a>) to change your rating or comment. "
               f"This is open for {EDIT_DAYS} days.</p>")
        flash("The member can now edit their review.", "success")
        return redirect(url_for("admin_reviews"))

    @app.route("/admin/reviews/<int:rid>/close-edit", methods=["POST"])
    @moderator_required
    def admin_review_close_edit(rid):
        rv = _rv(rid)
        rv.edit_until = None
        db.session.commit()
        flash("Editing closed; the review stays as posted.", "info")
        return redirect(url_for("admin_reviews"))

    @app.route("/admin/reviews/<int:rid>/suspend", methods=["POST"])
    @moderator_required
    def admin_review_suspend(rid):
        rv = _rv(rid)
        biz = rv.business
        reason = (request.form.get("reason") or "Suspended after a review flagged a serious problem.").strip()[:300]
        fb = FreelanceBusiness.query.filter_by(shadow_business_id=biz.id).first() if (biz.owner_user_id and FreelanceBusiness) else None
        if fb:
            fb.status, fb.is_live, fb.rejection_reason = "suspended", False, reason
        biz.is_suspended = True
        rv.admin_ack_at = datetime.utcnow()
        rv.admin_note = reason
        db.session.commit()
        flash(f"{biz.business_name} has been suspended.", "success")
        return redirect(url_for("admin_reviews"))

    def _admin_count():
        cutoff = datetime.utcnow() - timedelta(days=RESOLVE_DAYS)
        return BusinessReview.query.filter(db.or_(
            db.and_(BusinessReview.rating == 1, BusinessReview.admin_ack_at.is_(None)),
            db.and_(BusinessReview.mod_started_at.isnot(None), BusinessReview.mod_resolved_at.is_(None),
                    BusinessReview.mod_started_at > cutoff))).count()
    ns["review_admin_count"] = _admin_count
    ns["BusinessReview"] = BusinessReview
    return BusinessReview
