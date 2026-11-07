"""Freelancer / network-marketing directory for Perk Miner.

Members who are not registered businesses (freelancers, network marketers, side-gig owners) can
list themselves in a separate directory, using their normal member login.

How it plugs into the existing engine
-------------------------------------
* Each freelancer gets one hidden Business row (status = "freelancer", owner_user_id = the member).
  It is never shown in the business directory (every public query filters status == "approved").
  It carries the ad-fee balance, the referral code / business tree, the secure-messaging
  Interaction rows and the 10% ad-fee finalization, so all of that reuses the existing code.
* The listing content lives in FreelanceBusiness (draft_json -> admin approval -> live_json).
* Freelancer earnings are business-style earnings of the hidden row and are added to the member's
  own earnings by calculate_user_earnings_split(), so the regular member withdrawal works unchanged.

register(app, ns) is called once from app.py, with ns = app.py's globals().
"""
import json
import re
import secrets
import uuid
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from markupsafe import escape
from functools import wraps

MIN_DEPOSIT = 25.0
MAX_LINKS = 6
MAX_GALLERY = 4
MAX_OFFERINGS = 8

CATEGORIES = [
    "Network marketing / direct sales", "Health & wellness", "Beauty & skincare", "Travel",
    "Financial education", "Insurance", "Real estate", "Coaching & consulting",
    "Marketing & design", "Writing & content", "Web & tech services", "Photography & video",
    "Tutoring & education", "Crafts & handmade", "Virtual assistance", "Other freelance services",
]

SOCIALS = ["facebook", "instagram", "x", "linkedin", "tiktok", "youtube"]

REVIEW_LABELS = {
    "draft": "Draft - not submitted",
    "pending": "Waiting for admin review",
    "approved": "Approved and live",
    "rejected": "Changes requested",
    "suspended": "Suspended",
}


def blank_data():
    return {
        "name": "", "tagline": "", "category": "", "about": "", "offerings": [], "keywords": "",
        "perk": "", "region": "", "photo": "", "gallery": [], "email": "", "phone": "", "website": "",
        "socials": {k: "" for k in SOCIALS},
        "links": [],
    }


def clean_url(u):
    """'' if empty, None if invalid, else a safe http(s) URL."""
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    if len(u) > 500 or not re.match(r"^https?://[^\s/$.?#][^\s]*$", u, re.I):
        return None
    return u


def missing_items(d):
    out = []
    if not (d.get("name") or "").strip():
        out.append("listing name")
    if not d.get("category"):
        out.append("category")
    if len((d.get("about") or "").strip()) < 40:
        out.append("an About section (at least 40 characters)")
    if not d.get("photo"):
        out.append("a listing photo")
    if not [l for l in d.get("links", []) if l.get("url") and l.get("title")]:
        out.append("at least one referral link with a title")
    return out


def register(app, ns):
    db = ns["db"]
    from flask import (abort, flash, jsonify, redirect, render_template, request, url_for)
    from flask_login import current_user, login_required
    from sqlalchemy import or_

    User = ns["User"]
    Business = ns["Business"]
    Interaction = ns["Interaction"]
    Message = ns["Message"]
    BusinessTransaction = ns["BusinessTransaction"]
    csrf = ns["csrf"]

    # ------------------------------------------------------------------ models
    class FreelanceBusiness(db.Model):
        __tablename__ = "freelance_business"
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
        shadow_business_id = db.Column(db.Integer, db.ForeignKey("business.id"), unique=True)
        enabled = db.Column(db.Boolean, nullable=False, default=True)
        status = db.Column(db.String(20), nullable=False, default="draft")   # draft pending approved rejected suspended
        is_live = db.Column(db.Boolean, nullable=False, default=False)
        draft_json = db.Column(db.Text)
        live_json = db.Column(db.Text)
        display_name = db.Column(db.String(100))
        category = db.Column(db.String(60))
        tagline = db.Column(db.String(160))
        profile_photo = db.Column(db.String(255))
        search_text = db.Column(db.Text)
        slug = db.Column(db.String(40), unique=True)      # custom web address: perkminer.com/f/<slug>
        rejection_reason = db.Column(db.Text)
        terms_agreed_at = db.Column(db.DateTime)
        submitted_at = db.Column(db.DateTime)
        approved_at = db.Column(db.DateTime)
        reviewed_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

        user = db.relationship("User", foreign_keys=[user_id], backref=db.backref("freelance_profile", uselist=False))
        shadow = db.relationship("Business", foreign_keys=[shadow_business_id])

        def _load(self, raw):
            d = blank_data()
            try:
                d.update(json.loads(raw)) if raw else None
            except (ValueError, TypeError):
                pass
            d["socials"] = {**{k: "" for k in SOCIALS}, **(d.get("socials") or {})}
            return d

        @property
        def draft(self):
            return self._load(self.draft_json or self.live_json)

        @property
        def live(self):
            return self._load(self.live_json)

        @property
        def status_label(self):
            return REVIEW_LABELS.get(self.status, self.status)

    ns["FreelanceBusiness"] = FreelanceBusiness

    # ------------------------------------------------------------------ helpers
    def fl_for(user):
        if not user or not getattr(user, "id", None):
            return None
        return FreelanceBusiness.query.filter_by(user_id=user.id).first()

    def shadow_for(user):
        fb = fl_for(user)
        return fb.shadow if fb and fb.enabled and fb.shadow_business_id else None

    ns["freelance_shadow_for_user"] = shadow_for

    def unique_biz_name(base, exclude_id=None):
        base = (base or "Freelancer").strip()[:90] or "Freelancer"
        name, n = base, 1
        while True:
            q = Business.query.filter(db.func.lower(Business.business_name) == name.lower())
            if exclude_id:
                q = q.filter(Business.id != exclude_id)
            if not q.first():
                return name
            n += 1
            name = f"{base} ({n})"[:100]

    def sponsor_shadow_id(user):
        """The freelancer tree is member-to-member: the sponsor is the member who invited this member,
        but only if that member is an active freelancer too."""
        if not user.sponsor_id:
            return None
        sp = FreelanceBusiness.query.filter_by(user_id=user.sponsor_id, enabled=True).first()
        return sp.shadow_business_id if sp and sp.shadow_business_id else None

    def link_sponsors(fb):
        """Connect this freelancer to its freelancer sponsor, and connect any invited freelancers
        that were waiting for this member to become a freelancer."""
        sb = fb.shadow
        if sb is None:
            return
        if sb.sponsor_id is None:
            sb.sponsor_id = sponsor_shadow_id(fb.user)
        waiting = (Business.query.join(FreelanceBusiness, FreelanceBusiness.shadow_business_id == Business.id)
                   .join(User, User.id == FreelanceBusiness.user_id)
                   .filter(User.sponsor_id == fb.user_id, Business.sponsor_id.is_(None),
                           Business.id != sb.id).all())
        for w in waiting:
            w.sponsor_id = sb.id

    def ensure_shadow(user, fb, name):
        if fb.shadow_business_id and fb.shadow:
            return fb.shadow
        base = "FRL" + re.sub(r"[^A-Za-z0-9]", "", name or "")[:12]
        sponsor_biz_id = sponsor_shadow_id(user)
        sb = Business(
            business_name=unique_biz_name(name or f"Freelancer {user.id}"),
            business_email=f"freelancer-{user.id}@freelancer.perkminer.invalid",
            password=ns["bcrypt"].generate_password_hash(secrets.token_urlsafe(32)).decode("utf-8"),
            referral_code=ns["make_unique_referral_code"](base if len(base) > 3 else f"FRL{user.id}"),
            sponsor_id=sponsor_biz_id,
            user_sponsor_id=None,          # member-to-member tree only: no member earns "business referral" pay here
            email_confirmed=True,
            status="freelancer",
            owner_user_id=user.id,
            category="Other",
        )
        db.session.add(sb)
        db.session.flush()
        fb.shadow_business_id = sb.id
        fb.shadow = sb
        return sb

    RESERVED_SLUGS = {"admin", "api", "login", "logout", "register", "dashboard", "freelancer", "freelance",
                      "freelancers", "perkminer", "perk-miner", "support", "terms", "privacy", "search", "static",
                      "business", "member", "members", "help", "about", "contact", "www"}

    def clean_slug(raw):
        """('', None) if empty; (slug, None) if ok; ('', message) if not."""
        slug = re.sub(r"[\s_]+", "-", (raw or "").strip().lower()).strip("-")
        if not slug:
            return "", None
        if not re.match(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$", slug) or "--" in slug:
            return "", "web address (use 3-40 letters, numbers and single dashes)"
        if slug in RESERVED_SLUGS:
            return "", "web address (that one is reserved)"
        return slug, None

    def site_base():
        """The real public address. Ignores a YOUR_DOMAIN that points at the *.onrender.com address."""
        base = (ns.get("YOUR_DOMAIN") or "").rstrip("/")
        if not base or "onrender.com" in base.lower():
            base = "https://perkminer.com"
        return base

    def public_url(fb):
        """Always the real site address, never the host the request happened to arrive on."""
        base = site_base()
        return base + (url_for("freelance_listing_slug", slug=fb.slug) if fb.slug
                       else url_for("freelance_listing", fid=fb.id))

    def balance_of(fb):
        return float(fb.shadow.account_balance or 0) if fb and fb.shadow else 0.0

    def refresh_cols(fb, d):
        fb.display_name = (d.get("name") or "")[:100]
        fb.category = d.get("category") or None
        fb.tagline = (d.get("tagline") or "")[:160]
        fb.profile_photo = d.get("photo") or None
        parts = [d.get("name"), d.get("tagline"), d.get("category"), d.get("about"), d.get("keywords"),
                 d.get("region"), d.get("perk")] + list(d.get("offerings") or []) + \
                [l.get("title", "") for l in d.get("links", [])]
        fb.search_text = " ".join(str(p) for p in parts if p).lower()

    def view(fb, d, bal=None):
        """Dict shaped for the shared listing markup."""
        bal = balance_of(fb) if bal is None else bal
        return {
            "id": fb.id, "url": (url_for("freelance_listing_slug", slug=fb.slug) if fb.slug
                                 else url_for("freelance_listing", fid=fb.id)),
            "business_name": d.get("name"), "category": d.get("category") or "Freelancer",
            "tagline": d.get("tagline"), "profile_photo": d.get("photo"), "about_us": d.get("about"),
            "perks": d.get("perk"), "offerings": d.get("offerings") or [], "region": d.get("region"),
            "gallery": d.get("gallery") or [], "email": d.get("email"), "phone": d.get("phone"),
            "website": d.get("website"), "socials": d.get("socials") or {}, "links": d.get("links") or [],
            "account_balance": bal, "link_count": len([l for l in d.get("links", []) if l.get("url")]),
            "biz_id": fb.shadow_business_id,
            "rating_avg": float(getattr(fb.shadow, "rating_avg", 0) or 0) if fb.shadow else 0.0,
            "rating_count": int(getattr(fb.shadow, "rating_count", 0) or 0) if fb.shadow else 0,
        }

    def upload(f):
        if f and f.filename and ns["allowed_file"](f.filename):
            try:
                return ns["cloudinary"].uploader.upload(f).get("secure_url")
            except Exception as e:  # network / cloudinary problem: keep the old image
                app.logger.warning("freelance image upload failed: %s", e)
        return None

    # ------------------------------------------------------------------ dashboard form
    def handle_profile_post(user, form, files):
        """Called from the member dashboard when the profile wizard is saved. Returns a message or ''."""
        if form.get("fl_present") != "1":
            return ""
        fb = fl_for(user)
        enabled = form.get("fl_enabled") == "1"
        if not enabled:
            if fb and fb.enabled:
                fb.enabled = False
                db.session.commit()
                return "Your freelancer listing is hidden. Tick the box again any time to bring it back."
            return ""

        d = deepcopy(fb.draft) if fb else blank_data()
        errors = []

        def txt(key, field, limit):
            if field in form:
                d[key] = (form.get(field) or "").strip()[:limit]

        txt("name", "fl_name", 100); txt("tagline", "fl_tagline", 160); txt("about", "fl_about", 3000)
        txt("keywords", "fl_keywords", 300); txt("perk", "fl_perk", 255); txt("region", "fl_region", 120)
        txt("phone", "fl_phone", 40); txt("email", "fl_email", 200)
        if "fl_category" in form:
            c = form.get("fl_category")
            d["category"] = c if c in CATEGORIES else ""
        if "fl_offerings" in form:
            lines = [re.sub(r"\s+", " ", l).strip()[:70] for l in (form.get("fl_offerings") or "").splitlines()]
            d["offerings"] = [l for l in lines if l][:MAX_OFFERINGS]

        slug_in = None
        if "fl_slug" in form:
            slug_in, slug_err = clean_slug(form.get("fl_slug"))
            if slug_err:
                errors.append(slug_err)
            elif slug_in:
                taken = FreelanceBusiness.query.filter(FreelanceBusiness.slug == slug_in)
                if fb:
                    taken = taken.filter(FreelanceBusiness.id != fb.id)
                if taken.first():
                    errors.append("web address (already taken - try another)")

        w = clean_url(form.get("fl_website"))
        if w is None:
            errors.append("website address")
        else:
            d["website"] = w
        for k in SOCIALS:
            v = clean_url(form.get(f"fl_{k}"))
            if v is None:
                errors.append(f"{k} link")
            else:
                d["socials"][k] = v

        new_photo = upload(files.get("fl_photo"))
        if new_photo:
            d["photo"] = new_photo
        gallery = list(d.get("gallery") or [])
        for i in range(MAX_GALLERY):
            if form.get(f"fl_gallery_rm_{i}") and i < len(gallery):
                gallery[i] = ""
        for i in range(MAX_GALLERY):
            u = upload(files.get(f"fl_gallery_{i}"))
            if u:
                while len(gallery) <= i:
                    gallery.append("")
                gallery[i] = u
        d["gallery"] = [g for g in gallery if g][:MAX_GALLERY]

        old_links = list(d.get("links") or [])
        links = []
        for i in range(MAX_LINKS):
            old = old_links[i] if i < len(old_links) else {}
            if form.get(f"fl_link_rm_{i}"):
                continue
            title = (form.get(f"fl_link_title_{i}") or "").strip()[:100]
            raw_url = form.get(f"fl_link_url_{i}")
            url = clean_url(raw_url)
            desc = (form.get(f"fl_link_desc_{i}") or "").strip()[:300]
            img = upload(files.get(f"fl_link_img_{i}")) or old.get("image", "")
            if not (title or (raw_url or "").strip() or desc):
                continue
            if url is None:
                errors.append(f"referral link {i + 1} address")
                url = ""
            links.append({"title": title, "url": url or "", "desc": desc, "image": img})
        d["links"] = links

        if errors:
            db.session.rollback()
            return "Not saved - please check the " + ", ".join(errors) + "."

        if not fb:
            fb = FreelanceBusiness(user_id=user.id, status="draft")
            db.session.add(fb)
        fb.enabled = True
        if slug_in is not None:
            fb.slug = slug_in or None
        ensure_shadow(user, fb, d.get("name") or user.name or f"Freelancer {user.id}")
        link_sponsors(fb)
        fb.draft_json = json.dumps(d)
        if not fb.is_live:
            refresh_cols(fb, d)
        if form.get("fl_agree") == "1" and not fb.terms_agreed_at:
            fb.terms_agreed_at = datetime.utcnow()
        if fb.status != "suspended":
            fb.status = "draft"        # edits always need a fresh submit; the live version stays up meanwhile
        db.session.commit()

        note = f" Custom address saved: {site_base()}/f/{fb.slug}." if fb.slug else ""
        if form.get("fl_submit") == "1":
            return try_submit(fb) + note
        return "Freelancer listing saved as a draft." + note

    def try_submit(fb):
        d = fb.draft
        gaps = missing_items(d)
        if fb.status == "suspended":
            return "Your listing is suspended. Please contact support."
        if gaps:
            return "Saved as a draft. Still needed: " + ", ".join(gaps) + "."
        if not fb.terms_agreed_at:
            return "Saved as a draft. You must agree to the Freelancer Terms before submitting."
        if balance_of(fb) < MIN_DEPOSIT:
            return (f"Saved as a draft. Add at least ${MIN_DEPOSIT:.0f} to your freelancer account "
                    "balance (Freelancer Console) to submit for review.")
        fb.status = "pending"
        fb.submitted_at = datetime.utcnow()
        fb.rejection_reason = None
        db.session.commit()
        return "Submitted for review. We will email you when it is approved."

    ns["freelance_handle_profile_post"] = handle_profile_post

    def dashboard_context(user):
        fb = fl_for(user)
        ctx = {"fl": fb, "fl_on": bool(fb and fb.enabled), "fl_categories": CATEGORIES,
               "fl_max_links": MAX_LINKS, "fl_max_gallery": MAX_GALLERY, "fl_min_deposit": MIN_DEPOSIT}
        d = fb.draft if fb else blank_data()
        links = list(d.get("links") or [])
        while len(links) < MAX_LINKS:
            links.append({"title": "", "url": "", "desc": "", "image": ""})
        ctx.update(fl_data=d, fl_links=links, fl_offerings_text="\n".join(d.get("offerings") or []))
        ctx["fl_balance"] = balance_of(fb)
        ctx["fl_missing"] = missing_items(d) if fb else []
        ctx["fl_total"] = ctx["fl_avail"] = ctx["fl_pending"] = Decimal(0)
        if fb and fb.enabled and fb.shadow:
            t, a, p = ns["calculate_business_earnings_split"](fb.shadow, delay_days=7)
            ctx.update(fl_total=t, fl_avail=a, fl_pending=p)
            ctx["fl_ref_link"] = site_base() + url_for("register", ref=user.referral_code)
            ctx["fl_public_url"] = public_url(fb)
        return ctx

    ns["freelance_dashboard_context"] = dashboard_context

    def freelance_earnings(user, delay_days=7):
        """(total, available, pending) earned by the member's freelancer listing (zeros if none)."""
        sb = shadow_for(user)
        if not sb:
            return Decimal(0), Decimal(0), Decimal(0)
        return ns["calculate_business_earnings_split"](sb, delay_days=delay_days)

    ns["freelance_earnings"] = freelance_earnings

    # ------------------------------------------------------------------ access helpers
    def admin_ok():
        return current_user.is_authenticated and (current_user.has_role("approve_reject_freelancers")
                                                  or current_user.has_role("super_admin"))

    def admin_only(f):
        @wraps(f)
        @login_required
        def wrapper(*a, **kw):
            if not admin_ok():
                abort(403)
            return f(*a, **kw)
        return wrapper

    def member_fl_required(f):
        @wraps(f)
        @login_required
        def wrapper(*a, **kw):
            fb = fl_for(current_user)
            if not fb or not fb.enabled or not fb.shadow_business_id:
                flash("Turn on 'I'm a Freelancer or have a networking business' in View/Edit Profile first.", "warning")
                return redirect(url_for("dashboard"))
            return f(fb, *a, **kw)
        return wrapper

    @app.before_request
    def freelance_canonical_host():
        """Public freelancer pages always live on the real domain, not the *.onrender.com address."""
        if request.method == "GET" and request.endpoint in ("freelance_directory", "freelance_listing",
                                                           "freelance_listing_slug", "freelance_terms"):
            host = request.host.lower()
            if host.endswith(".onrender.com"):
                return redirect(site_base() + request.full_path.rstrip("?"), code=301)

    # ------------------------------------------------------------------ public pages
    def live_query():
        return FreelanceBusiness.query.filter(FreelanceBusiness.is_live.is_(True),
                                              FreelanceBusiness.enabled.is_(True))

    @app.route("/freelance-directory")
    def freelance_directory():
        q = (request.args.get("q") or "").strip()
        cat = request.args.get("category") or ""
        page = request.args.get("page", 1, type=int)
        query = live_query()
        if q:
            for word in q.lower().split()[:6]:
                query = query.filter(FreelanceBusiness.search_text.ilike(f"%{word}%"))
        if cat in CATEGORIES:
            query = query.filter(FreelanceBusiness.category == cat)
        pag = query.order_by(FreelanceBusiness.approved_at.desc().nullslast(), FreelanceBusiness.id.desc()) \
                   .paginate(page=page, per_page=12, error_out=False)
        items = [view(fb, fb.live) for fb in pag.items]
        return render_template("freelance_directory.html", items=items, pagination=pag, q=q, category=cat,
                               categories=CATEGORIES, total=pag.total)

    @app.route("/freelance/<int:fid>")
    @login_required
    def freelance_listing(fid):
        fb = FreelanceBusiness.query.get_or_404(fid)
        if fb.slug and fb.is_live and fb.enabled:
            return redirect(url_for("freelance_listing_slug", slug=fb.slug), code=301)
        return render_listing(fb)

    @app.route("/f/<slug>")
    @login_required
    def freelance_listing_slug(slug):
        fb = FreelanceBusiness.query.filter_by(slug=slug.lower()).first_or_404()
        return render_listing(fb)

    def render_listing(fb):
        is_owner = fb.user_id == current_user.id
        preview = False
        if fb.is_live and fb.enabled:
            d = fb.live
        elif is_owner or admin_ok():
            d, preview = fb.draft, True
        else:
            abort(404)
        finished = 0
        if fb.shadow_business_id:
            finished = BusinessTransaction.query.filter(
                BusinessTransaction.business_referral_id == fb.shadow.referral_code,
                or_(BusinessTransaction.sponsoree_mutual_referral_id.is_(None),
                    BusinessTransaction.sponsoree_mutual_referral_id == "")).count()
        return render_template("freelance_listing.html", fl=view(fb, d), fb=fb, canonical=public_url(fb), is_owner=is_owner,
                               preview=preview, finalized_count=finished)

    @app.route("/freelance/<int:fid>/contact", methods=["POST"])
    @login_required
    def freelance_contact(fid):
        fb = FreelanceBusiness.query.get_or_404(fid)
        if not (fb.is_live and fb.enabled and fb.shadow_business_id):
            abort(404)
        if fb.user_id == current_user.id:
            flash("This is your own listing.", "info")
            return redirect(url_for("freelance_listing", fid=fid))
        sb = fb.shadow
        text = (request.form.get("message_text") or "").strip()[:2000]
        inter = Interaction.query.filter_by(user_id=current_user.id, business_id=sb.id, status="active").first()
        new = inter is None
        if new:
            inter = Interaction(user_id=current_user.id, business_id=sb.id, status="active",
                                service_type="Freelancer Inquiry",
                                details=text or "Interested in your freelancer listing.",
                                budget_low=0, budget_high=0,
                                referral_code=getattr(current_user, "referral_code", None))
            db.session.add(inter)
            db.session.flush()
        if text:
            db.session.add(Message(interaction_id=inter.id, sender_type="user", sender_id=current_user.id, text=text))
        db.session.commit()
        if new:
            try:
                ns["send_email"](
                    fb.user.email, "A Perk Miner member wants to connect with you",
                    f"<p>{(current_user.name or 'A member')} contacted you through your Perk Miner freelancer listing.</p>"
                    f"<p><a href=\"{site_base() + url_for('freelance_console')}\">Open your Freelancer Console</a> to reply.</p>")
            except Exception as e:
                app.logger.warning("freelancer notify email failed: %s", e)
        return redirect(url_for("active_session", interaction_id=inter.id))

    @app.route("/freelance/<int:fid>/purchase")
    @login_required
    def freelance_purchase(fid):
        fb = FreelanceBusiness.query.get_or_404(fid)
        if not (fb.is_live and fb.enabled and fb.shadow_business_id) or fb.user_id == current_user.id:
            abort(404)
        return redirect(url_for("start_purchase", biz_id=fb.shadow_business_id))

    @app.route("/freelancer-terms")
    def freelance_terms():
        return render_template("freelance_terms.html")

    # ------------------------------------------------------------------ freelancer console
    @app.route("/freelancer")
    @member_fl_required
    def freelance_console(fb):
        sb = fb.shadow
        convos = (Interaction.query.filter_by(business_id=sb.id, status="active")
                  .order_by(Interaction.updated_at.desc()).limit(50).all())
        t, a, p = ns["calculate_business_earnings_split"](sb, delay_days=7)
        ready = not missing_items(fb.draft) and fb.terms_agreed_at and balance_of(fb) >= MIN_DEPOSIT
        return render_template(
            "freelance_console.html", fb=fb, sb=sb, balance=balance_of(fb), convos=convos,
            total=t, avail=a, pending=p, missing=missing_items(fb.draft), ready=ready,
            ref_link=site_base() + url_for("register", ref=current_user.referral_code),
            public_url=public_url(fb),
            min_deposit=MIN_DEPOSIT)

    @app.route("/freelancer/submit", methods=["POST"])
    @member_fl_required
    def freelance_submit(fb):
        flash(try_submit(fb), "info")
        return redirect(url_for("freelance_console"))

    @app.route("/freelancer/fund")
    @member_fl_required
    def freelance_fund(fb):
        return render_template("freelance_fund.html", account_balance=balance_of(fb),
                               STRIPE_PUBLISHABLE=ns["os"].environ.get("STRIPE_PUBLISHABLE"))

    @csrf.exempt
    @app.route("/freelancer/create-checkout-session", methods=["POST"])
    @member_fl_required
    def freelance_checkout(fb):
        data = request.get_json(silent=True) or {}
        try:
            amount = float(data.get("amount"))
            if amount < MIN_DEPOSIT:
                return jsonify({"error": f"Minimum ${MIN_DEPOSIT:.2f} required."}), 400
            if amount > 10000:
                return jsonify({"error": "Maximum single deposit is $10,000."}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid amount."}), 400
        domain = site_base()
        email = (current_user.email or "").strip()
        params = dict(
            mode="payment",
            line_items=[{"price_data": {"currency": "usd",
                                        "product_data": {"name": "Fund Freelancer Advertising Balance"},
                                        "unit_amount": int(round(amount * 100))}, "quantity": 1}],
            success_url=domain + "/freelancer?fund_success=1",
            cancel_url=domain + "/freelancer/fund?canceled=1",
            metadata={"purpose": "fund_account", "business_id": str(fb.shadow_business_id),
                      "freelancer_user_id": str(current_user.id)})
        # Stripe rejects the whole session if the prefilled email is not a valid address
        # (e.g. a social-login relay/placeholder), so only prefill when it looks valid.
        if re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", email) and not email.endswith(".invalid"):
            params["customer_email"] = email
        try:
            s = ns["stripe"].checkout.Session.create(**params)
            return jsonify({"sessionId": s.id})
        except Exception as e:
            app.logger.exception("Freelancer checkout error for user %s: %s", current_user.id, e)
            msg = getattr(e, "user_message", None) or "Checkout session failed."
            return jsonify({"error": msg}), 500

    @app.route("/freelancer/find-member", methods=["POST"])
    @member_fl_required
    def freelance_find_member(fb):
        """Manual in-person sale: look a member up by referral code (or the code from their QR)."""
        code = (request.form.get("member_code") or "").strip()
        code = code.rstrip("/").split("/")[-1]
        member = User.query.filter(db.func.lower(User.referral_code) == code.lower()).first() if code else None
        if not member:
            flash("No member found with that referral code.", "warning")
            return redirect(url_for("freelance_console"))
        if member.id == current_user.id:
            flash("You cannot finalize a sale with yourself.", "warning")
            return redirect(url_for("freelance_console"))
        inter = open_interaction_for(fb, member)
        return redirect(url_for("freelance_finalize", interaction_id=inter.id))

    def open_interaction_for(fb, member):
        inter = Interaction.query.filter_by(user_id=member.id, business_id=fb.shadow_business_id,
                                            status="active").first()
        if not inter:
            inter = Interaction(user_id=member.id, business_id=fb.shadow_business_id, status="active",
                                service_type="Purchase", details="In-person / manual sale",
                                budget_low=0, budget_high=0, referral_code=member.referral_code)
            db.session.add(inter)
            db.session.commit()
        return inter

    ns["freelance_open_interaction"] = open_interaction_for

    def prior_purchase(fb, interaction):
        """True when this customer already completed a purchase with this freelancer (other session)."""
        return (db.session.query(BusinessTransaction.id)
                .join(Interaction, Interaction.id == BusinessTransaction.interaction_id)
                .filter(Interaction.business_id == fb.shadow_business_id,
                        Interaction.user_id == interaction.user_id,
                        Interaction.id != interaction.id,
                        or_(BusinessTransaction.sponsoree_mutual_referral_id.is_(None),
                            BusinessTransaction.sponsoree_mutual_referral_id == ""))
                .first() is not None)

    @app.route("/freelancer/finalize/<int:interaction_id>", methods=["GET", "POST"])
    @member_fl_required
    def freelance_finalize(fb, interaction_id):
        inter = Interaction.query.get_or_404(interaction_id)
        if inter.business_id != fb.shadow_business_id:
            abort(403)
        summary = ns["get_finalization_summary"](inter)
        repeat = summary is None and prior_purchase(fb, inter)
        if request.method == "POST" and summary is None:
            if repeat:
                flash("This member already made their initial purchase with you. The 10% advertising fee "
                      "only applies to a member's first purchase, so there is nothing to finalize.", "info")
            else:
                try:
                    amount = float(request.form.get("amount", 0))
                    if amount <= 0:
                        raise ValueError("Enter an amount greater than $0.00.")
                    summary = ns["finalize_interaction"](inter, fb.shadow, amount,
                                                         local_date_time=request.form.get("local_datetime"))
                    flash("Transaction finalized and all rewards/commissions assigned!", "success")
                except ns["AlreadyFinalizedError"] as e:
                    flash(str(e), "warning")
                except Exception as e:
                    db.session.rollback()
                    flash(str(e), "danger")
            if summary is None:
                summary = ns["get_finalization_summary"](inter)
        db.session.refresh(fb.shadow)
        return render_template("freelance_finalize.html", interaction=inter, summary=summary, repeat=repeat,
                               account_balance=fb.shadow.account_balance, now=datetime.now())

    # ------------------------------------------------------------------ admin
    @app.route("/admin/freelancers")
    @admin_only
    def admin_freelancers():
        pending = FreelanceBusiness.query.filter_by(status="pending").order_by(FreelanceBusiness.submitted_at.asc()).all()
        live = FreelanceBusiness.query.filter_by(is_live=True).order_by(FreelanceBusiness.display_name.asc()).all()
        susp = FreelanceBusiness.query.filter_by(status="suspended").order_by(FreelanceBusiness.display_name.asc()).all()
        return render_template("admin_freelance_queue.html", pending=pending, live=live, suspended=susp)

    @app.route("/admin/freelancers/<int:fid>", methods=["GET", "POST"])
    @admin_only
    def admin_freelance_review(fid):
        fb = FreelanceBusiness.query.get_or_404(fid)
        if request.method == "POST":
            action = request.form.get("action")
            reason = (request.form.get("reason") or "").strip()[:1000]
            member = fb.user
            if action == "approve" and fb.status == "pending":
                d = fb.draft
                if missing_items(d):
                    flash("This listing is incomplete and cannot be approved.", "danger")
                    return redirect(url_for("admin_freelance_review", fid=fid))
                fb.live_json = json.dumps(d)
                refresh_cols(fb, d)
                fb.is_live, fb.status = True, "approved"
                fb.approved_at, fb.rejection_reason = datetime.utcnow(), None
                fb.reviewed_by_id = current_user.id
                if fb.shadow:
                    fb.shadow.business_name = unique_biz_name(d.get("name"), fb.shadow.id)
                    fb.shadow.profile_photo = d.get("photo")
                db.session.commit()
                notify(member, "Your Perk Miner freelancer listing is approved",
                       "<p>Great news - your freelancer listing is now live in the Perk Miner freelance directory.</p>")
                flash("Approved and published.", "success")
            elif action == "reject" and fb.status == "pending":
                if not reason:
                    flash("Please give a reason so the member knows what to fix.", "warning")
                    return redirect(url_for("admin_freelance_review", fid=fid))
                fb.status, fb.rejection_reason = "rejected", reason
                fb.reviewed_by_id = current_user.id
                db.session.commit()
                notify(member, "Changes needed on your Perk Miner freelancer listing",
                       f"<p>Your listing was not approved yet.</p><p><b>Reason:</b> {escape(reason)}</p>"
                       "<p>Open View/Edit Profile on your dashboard to update it and resubmit. "
                       "Any previously approved version stays live.</p>")
                flash("Sent back to the member with your reason.", "info")
            elif action == "suspend":
                fb.is_live, fb.status = False, "suspended"
                fb.rejection_reason = reason or fb.rejection_reason
                db.session.commit()
                flash("Listing suspended and removed from the directory.", "warning")
            elif action == "reinstate" and fb.status == "suspended":
                fb.status = "approved" if fb.live_json else "draft"
                fb.is_live = bool(fb.live_json)
                db.session.commit()
                flash("Listing reinstated.", "success")
            return redirect(url_for("admin_freelancers"))
        d = fb.draft
        return render_template("admin_freelance_review.html", fb=fb, fl=view(fb, d), missing=missing_items(d),
                               balance=balance_of(fb), changed=(fb.is_live and fb.draft_json != fb.live_json))

    def notify(user, subject, html):
        try:
            ns["send_email"](user.email, subject, html)
        except Exception as e:
            app.logger.warning("freelance notify failed: %s", e)

    ns["freelance_pending_count"] = lambda: FreelanceBusiness.query.filter_by(status="pending").count()
    return FreelanceBusiness
