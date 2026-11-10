"""Routes for switching between the 10% and 2% advertising fee plans (see feeplan.py).

* POST /business/ad-fee-plan       - business dashboard button (business login)
* POST /freelancer/ad-fee-plan     - freelancer console button (member with a freelancer listing)

Choosing the plan for the first time happens on listing_disclaimer.html (businesses) and in step 4 of
the freelancer profile wizard (freelancers); both just post an `ad_fee_plan` field.
register(app, ns) is called once from app.py.
"""
from datetime import datetime

import feeplan


def apply_plan(biz, plan):
    """Set a Business' plan. Returns True when it actually changed.

    Going to the 2% plan switches off everything it does not include: featured placement is removed
    here, live tracking and the public profile page check the plan wherever they are served
    (public_businesses_query for the profile, sitemap and AI endpoints).
    """
    plan = feeplan.LOW if plan == feeplan.LOW else feeplan.FULL
    if feeplan.plan_of(biz) == plan:
        return False
    biz.ad_fee_plan = plan
    biz.ad_fee_plan_changed_at = datetime.utcnow()
    if plan == feeplan.LOW:
        biz.featured = False
        biz.manual_feature = False
    return True


def register(app, ns):
    db = ns["db"]

    def promo_lock(biz):
        """(locked, remaining) for the 10% -> 2% switch.

        An advertiser who redeemed a promo code stays on the 10% plan until the advertising fees paid
        since the redemption add up to the promo amount, i.e. until the free funds are used up.
        """
        label = getattr(biz, "promo_code_used", None)
        if not label:
            return False, 0.0
        amount = None
        for table in (ns.get("PROMO_CODES") or {}, ns.get("FL_PROMOS") or {}):
            for p in table.values():
                if p.get("label") == label:
                    amount = float(p["amount"])
        if amount is None:
            fl = ns.get("freelance_promo_amount")
            amount = float(fl(label)) if fl else 0.0
        BT = ns["BusinessTransaction"]
        if not getattr(biz, "referral_code", None):
            return True, float(amount)
        q = db.session.query(db.func.coalesce(db.func.sum(BT.ad_fee), 0.0)).filter(BT.business_referral_id == biz.referral_code)
        q = q.filter(db.or_(BT.sponsoree_mutual_referral_id.is_(None), BT.sponsoree_mutual_referral_id == ""))
        if getattr(biz, "promo_redeemed_at", None):
            q = q.filter(BT.date_time >= biz.promo_redeemed_at)
        spent = float(q.scalar() or 0.0)
        remaining = round(amount - spent, 2)
        return remaining > 0.004, max(remaining, 0.0)

    def _plan_cache():
        from flask import g
        if not hasattr(g, "_txn_plan_cache"):
            g._txn_plan_cache = {}
        return g._txn_plan_cache

    def preload_txn_plans(rows):
        """Fetch the plan each finalized transaction was charged under, in one query (for history lists)."""
        cache = _plan_cache()
        BT = ns["BusinessTransaction"]
        ids = {getattr(r, "transaction_id", None) for r in rows}
        ids = {i for i in ids if i and i not in cache}
        if not ids:
            return
        for tid, plan in (db.session.query(BT.transaction_id, BT.fee_plan)
                          .filter(BT.transaction_id.in_(list(ids))).all()):
            if tid not in cache or plan == feeplan.LOW:
                cache[tid] = plan or feeplan.FULL
        for i in ids:
            cache.setdefault(i, feeplan.FULL)

    def txn_plan(txn):
        """'full' or 'low': the plan in force when this transaction was finalized (old rows are 'full')."""
        own = getattr(txn, "fee_plan", None)
        if own:
            return own
        tid = getattr(txn, "transaction_id", None)
        if not tid:
            return feeplan.FULL
        cache = _plan_cache()
        if tid not in cache:
            preload_txn_plans([txn])
        return cache.get(tid, feeplan.FULL)

    ns["txn_plan"] = txn_plan
    ns["preload_txn_plans"] = preload_txn_plans
    app.jinja_env.globals["txn_plan"] = txn_plan
    app.jinja_env.globals["txn_is_low"] = lambda t: txn_plan(t) == feeplan.LOW

    ns["promo_lock"] = promo_lock
    app.jinja_env.globals["promo_lock"] = promo_lock

    def lock_msg(biz, remaining):
        return ("You redeemed the promo code " + str(biz.promo_code_used) + " on the 10% ad fee plan. You can switch to "
                "the 2% plan after your promo funds are used (about $%.2f of the promo credit is still unused)." % remaining)
    from flask import flash, redirect, request, session, url_for
    from flask_login import current_user, login_required

    Business = ns["Business"]
    business_login_required = ns["business_login_required"]

    @app.route("/business/ad-fee-plan", methods=["POST"])
    @business_login_required
    def business_switch_ad_fee_plan():
        biz = Business.query.get_or_404(session.get("business_id"))
        plan = request.form.get("ad_fee_plan")
        if plan not in (feeplan.FULL, feeplan.LOW):
            flash("Please choose a plan.", "danger")
            return redirect(url_for("business_dashboard"))
        if not request.form.get("confirm_plan_change"):
            flash("Please tick the box to confirm you understand the change.", "danger")
            return redirect(url_for("business_dashboard"))
        if plan == feeplan.LOW and not feeplan.is_low(biz):
            locked, remaining = promo_lock(biz)
            if locked:
                flash(lock_msg(biz, remaining), "danger")
                return redirect(url_for("business_dashboard"))
        if apply_plan(biz, plan):
            db.session.commit()
            if plan == feeplan.LOW:
                flash("You are now on the 2% ad fee plan. Your public profile page is suspended and featured "
                      "placement, live tracking and promo codes are off. Members now earn 0.4% cash back on "
                      "purchases from you. This applies to transactions finalized from now on.", "warning")
            else:
                flash("You are now on the 10% ad fee plan with every feature. Your public profile page is live "
                      "again; search engines and AI assistants can take days or weeks to pick it up. Members "
                      "earn 2% cash back on purchases from you, applied to transactions finalized from now on.",
                      "success")
        else:
            flash("You are already on that plan.", "info")
        return redirect(url_for("business_dashboard"))

    @app.route("/freelancer/ad-fee-plan", methods=["POST"])
    @login_required
    def freelance_switch_ad_fee_plan():
        FreelanceBusiness = ns["FreelanceBusiness"]
        fb = FreelanceBusiness.query.filter_by(user_id=current_user.id).first()
        if not fb or not fb.enabled or not fb.shadow_business_id:
            flash("Turn on your freelancer listing first.", "warning")
            return redirect(url_for("dashboard"))
        plan = request.form.get("ad_fee_plan")
        if plan not in (feeplan.FULL, feeplan.LOW):
            flash("Please choose a plan.", "danger")
            return redirect(url_for("freelance_console"))
        if not request.form.get("confirm_plan_change"):
            flash("Please tick the box to confirm you understand the change.", "danger")
            return redirect(url_for("freelance_console"))
        if plan == feeplan.LOW and not feeplan.is_low(fb.shadow):
            locked, remaining = promo_lock(fb.shadow)
            if locked:
                flash(lock_msg(fb.shadow, remaining), "danger")
                return redirect(url_for("freelance_console"))
        if apply_plan(fb.shadow, plan):
            db.session.commit()
            if plan == feeplan.LOW:
                flash("You are now on the 2% ad fee plan. Members earn 0.4% cash back on purchases from you "
                      "and promo codes and your custom web address are off. This applies to transactions "
                      "finalized from now on.", "warning")
            else:
                flash("You are now on the 10% ad fee plan with every feature. Members earn 2% cash back on "
                      "purchases from you, applied to transactions finalized from now on.", "success")
        else:
            flash("You are already on that plan.", "info")
        return redirect(url_for("freelance_console"))

    ns["apply_fee_plan"] = apply_plan
