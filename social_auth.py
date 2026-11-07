"""
social_auth.py  --  "Continue with Google / Facebook / Microsoft / Apple" for Perk Miner
=========================================================================================

Works for BOTH members (User) and businesses (Business), and carries the referral code
of whoever invited the person all the way through sign-up, with no typing.

How the referral code is carried
--------------------------------
1.  Any page visited with ?ref=CODE (invite email, flyer QR, /intro video link, ...) stores the
    code in the session AND in a 30-day cookie ("pm_ref"). The latest valid link wins.
2.  /register and /business/register pre-fill the Referral Code box from that stored code, so
    it is also there for people who sign up with a password.
3.  If someone types a code in the box and then clicks a social button, the button sends the
    typed code along (a tiny script in partials/_social_buttons.html).
4.  When the new account is created, the stored code becomes sponsor_id / user_sponsor_id
    exactly like the existing password registration does.

Account rules (security)
------------------------
*  Identities are matched on (provider, provider's user id), never on e-mail alone.
*  An existing password account is linked automatically ONLY when the provider vouches that the
   e-mail is verified (Google, Apple, personal Microsoft accounts). Facebook and work/school
   Microsoft accounts can create NEW accounts, but never take over an existing one.
*  If the provider cannot vouch for the e-mail (work/school Microsoft), a 6-digit code is e-mailed
   before the account is created.
*  An existing but never-confirmed account gets its password replaced with a random one when it is
   linked, so nobody can pre-register someone else's e-mail and keep a working password.
*  Social log-ins skip the e-mail 2FA code (the provider already authenticated the person).
   Set SOCIAL_REQUIRE_EMAIL_2FA=1 only if you later add that step.

Environment variables (a button only appears for providers whose variables are set)
-----------------------------------------------------------------------------------
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
    FACEBOOK_APP_ID, FACEBOOK_APP_SECRET                (optional FACEBOOK_GRAPH_VERSION, default v23.0)
    MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET        (optional MICROSOFT_TENANT, default "common")
    APPLE_CLIENT_ID (the *Services ID*), APPLE_TEAM_ID, APPLE_KEY_ID, APPLE_PRIVATE_KEY
        (APPLE_PRIVATE_KEY = contents of the .p8 file; literal "\\n" sequences are accepted)
    SOCIAL_AUTH_BASE_URL    optional, e.g. https://www.perkminer.com  (defaults to BASE_URL in app.py)

Redirect (callback) URL to register with every provider:
    https://www.perkminer.com/auth/<provider>/callback      provider = google | facebook | microsoft | apple

Wiring (see SOCIAL_LOGIN_SETUP.md): one call in app.py, `social_auth = init_social_auth(app, ...)`.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import urlparse

from flask import (Blueprint, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from sqlalchemy.exc import IntegrityError

log = logging.getLogger("perkminer.social")

REF_COOKIE = "pm_ref"
REF_COOKIE_MAX_AGE = 30 * 24 * 3600      # 30 days
CTX_TTL = 15 * 60                         # a sign-in attempt must finish within 15 minutes
CODE_TTL = 10 * 60                        # e-mailed verification code lifetime
CODE_RESEND_SECONDS = 30
MAX_CODE_ATTEMPTS = 5
# Accounts holding any of these roles must keep using password + e-mailed code: a social log-in skips that
# second step, and these roles open admin / finance / moderation pages.
PRIVILEGED_ROLES = {"super_admin", "finance", "customer_support", "approve_reject_listings",
                    "approve_reject_testimonials", "feedback_moderation"}
MSA_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"   # Microsoft personal accounts
_MS_ISSUER_RE = re.compile(r"^https://login\.microsoftonline\.com/[0-9a-fA-F-]{36}/v2\.0$")

# order = order of the buttons on the page
PROVIDER_META = {
    "google":    {"label": "Google",    "icon": "google"},
    "facebook":  {"label": "Facebook",  "icon": "facebook"},
    "microsoft": {"label": "Microsoft", "icon": "microsoft"},
    "apple":     {"label": "Apple",     "icon": "apple"},
}


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def _truthy(value):
    """Apple sends email_verified as the STRING "true"; Google sends a boolean."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _ms_issuer_ok(_claims, value):
    return bool(value) and bool(_MS_ISSUER_RE.match(value))


def _apple_private_key():
    return _env("APPLE_PRIVATE_KEY").replace("\\n", "\n")


def _apple_client_secret():
    """Apple wants a short-lived JWT (ES256, signed with your .p8 key) instead of a fixed secret."""
    from authlib.jose import jwt
    now = int(time.time())
    header = {"alg": "ES256", "kid": _env("APPLE_KEY_ID")}
    payload = {
        "iss": _env("APPLE_TEAM_ID"),
        "iat": now,
        "exp": now + 600,
        "aud": "https://appleid.apple.com",
        "sub": _env("APPLE_CLIENT_ID"),
    }
    token = jwt.encode(header, payload, _apple_private_key())
    return token.decode() if isinstance(token, bytes) else token


def _make_identity_model(db):
    class SocialIdentity(db.Model):
        """Links one provider login to one member OR one business account."""
        __tablename__ = "social_identity"
        id = db.Column(db.Integer, primary_key=True)
        provider = db.Column(db.String(20), nullable=False)
        subject = db.Column(db.String(255), nullable=False)       # the provider's stable user id
        account_type = db.Column(db.String(10), nullable=False)   # 'user' or 'business'
        account_id = db.Column(db.Integer, nullable=False, index=True)
        email = db.Column(db.String(200))
        created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
        last_login_at = db.Column(db.DateTime(timezone=True))
        __table_args__ = (
            db.UniqueConstraint("provider", "subject", "account_type", name="uq_social_identity"),
        )
    return SocialIdentity


class SocialAuth:
    """Small handle returned to app.py so the register views can ask for the pending referral."""

    def __init__(self, enabled, ref_lookup):
        self.enabled = enabled            # list of provider keys that are configured
        self._ref_lookup = ref_lookup

    def pending_referral(self, kind):
        """Referral code (str) remembered from an invite link, valid for this kind ("member" or
        "business"), or "" if there is none."""
        raw, _k, _owner = self._ref_lookup(kind)
        return raw or ""


def init_social_auth(app, *, csrf, db, User, Business, bcrypt, login_user,
                     resolve_referral_code, make_unique_referral_code,
                     random_referral_code, random_business_code, send_email,
                     base_url=None):
    from authlib.integrations.flask_client import OAuth

    SocialIdentity = _make_identity_model(db)
    oauth = OAuth(app)
    bp = Blueprint("social_auth", __name__)

    # ------------------------------------------------------------------ providers
    enabled = []
    if _env("GOOGLE_CLIENT_ID") and _env("GOOGLE_CLIENT_SECRET"):
        oauth.register(
            "google",
            client_id=_env("GOOGLE_CLIENT_ID"), client_secret=_env("GOOGLE_CLIENT_SECRET"),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
        enabled.append("google")
    if _env("FACEBOOK_APP_ID") and _env("FACEBOOK_APP_SECRET"):
        ver = _env("FACEBOOK_GRAPH_VERSION", "v23.0")
        oauth.register(
            "facebook",
            client_id=_env("FACEBOOK_APP_ID"), client_secret=_env("FACEBOOK_APP_SECRET"),
            authorize_url="https://www.facebook.com/dialog/oauth",
            access_token_url=f"https://graph.facebook.com/{ver}/oauth/access_token",
            api_base_url=f"https://graph.facebook.com/{ver}/",
            client_kwargs={"scope": "email public_profile"},
        )
        enabled.append("facebook")
    if _env("MICROSOFT_CLIENT_ID") and _env("MICROSOFT_CLIENT_SECRET"):
        tenant = _env("MICROSOFT_TENANT", "common")
        oauth.register(
            "microsoft",
            client_id=_env("MICROSOFT_CLIENT_ID"), client_secret=_env("MICROSOFT_CLIENT_SECRET"),
            server_metadata_url=f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
        enabled.append("microsoft")
    if all(_env(k) for k in ("APPLE_CLIENT_ID", "APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY")):
        try:
            first_secret = _apple_client_secret()      # also proves the .p8 key is readable
            oauth.register(
                "apple",
                client_id=_env("APPLE_CLIENT_ID"), client_secret=first_secret,
                server_metadata_url="https://appleid.apple.com/.well-known/openid-configuration",
                client_kwargs={"scope": "name email", "token_endpoint_auth_method": "client_secret_post"},
            )
            enabled.append("apple")
        except Exception:
            log.exception("APPLE_* settings are present but unusable; Apple sign-in is disabled")
    enabled.sort(key=list(PROVIDER_META).index)
    log.info("Social sign-in enabled for: %s", ", ".join(enabled) or "(none)")

    public_base = (_env("SOCIAL_AUTH_BASE_URL") or base_url or "").rstrip("/")

    # ------------------------------------------------------------------ referral memory
    def _ref_format_ok(raw):
        return bool(raw) and len(raw) <= 40 and not re.search(r"\s", raw)

    def _pending_ref_raw():
        return (session.get("pm_ref") or request.cookies.get(REF_COOKIE) or "").strip()

    def _ref_lookup(kind, raw=None):
        """-> (raw_code, owner_kind, owner) if the code resolves for this kind, else (None, None, None).
        Members can only be sponsored by members; businesses by members or businesses."""
        raw = (raw if raw is not None else _pending_ref_raw()).strip()
        if not _ref_format_ok(raw):
            return None, None, None
        owner_kind, owner = resolve_referral_code(raw, include_businesses=(kind == "business"))
        if owner is None:
            return None, None, None
        return raw, owner_kind, owner

    @app.before_request
    def _capture_ref():
        if request.method not in ("GET", "HEAD") or request.endpoint in (None, "static"):
            return
        raw = (request.args.get("ref") or "").strip()
        if not _ref_format_ok(raw):
            return
        # remember it only if it belongs to a real member or business
        _k, owner = resolve_referral_code(raw, include_businesses=True)
        if owner is None:
            return
        session["pm_ref"] = raw
        g.pm_ref_cookie = raw

    @app.after_request
    def _store_ref_cookie(resp):
        raw = g.pop("pm_ref_cookie", None)
        if raw:
            secure = request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"
            resp.set_cookie(REF_COOKIE, raw, max_age=REF_COOKIE_MAX_AGE, httponly=True,
                            secure=secure, samesite="Lax")
        return resp

    @app.context_processor
    def _social_context():
        return {"social_providers": [dict(key=k, **PROVIDER_META[k]) for k in enabled]}

    # ------------------------------------------------------------------ small helpers
    def _page(kind, mode="register"):
        if kind == "business":
            return url_for("business_register" if mode == "register" else "business_login")
        return url_for("register" if mode == "register" else "login")

    def _random_pw_hash():
        # accounts created through a social provider get an unusable random password; the person can
        # still choose one later with "Forgot password".
        return bcrypt.generate_password_hash(secrets.token_urlsafe(32)).decode("utf-8")

    def _code_mac(nonce, code):
        key = app.config["SECRET_KEY"]
        key = key.encode() if isinstance(key, str) else key
        return hmac.new(key, f"{nonce}:{code}".encode(), hashlib.sha256).hexdigest()

    def _find_account(kind, email):
        if kind == "business":
            return Business.query.filter_by(business_email=email).first()
        return User.query.filter_by(email=email).first()

    def _get_pending():
        pend = session.get("social_pending")
        if not pend or time.time() - pend.get("ts", 0) > CTX_TTL:
            session.pop("social_pending", None)
            return None
        return pend

    def _link(kind, account, identity):
        db.session.add(SocialIdentity(
            provider=identity["provider"], subject=identity["subject"],
            account_type="business" if kind == "business" else "user",
            account_id=account.id, email=identity["email"][:200]))

    def _is_privileged(kind, account):
        if kind == "business":
            return False
        return any(getattr(r, "name", None) in PRIVILEGED_ROLES for r in (getattr(account, "roles", None) or []))

    def _refuse_privileged(kind):
        flash("For security, staff and admin accounts must log in with email, password and the emailed code.", "warning")
        return redirect(_page(kind, "login"))

    def _finish_login(kind, account, link_row=None):
        """Sign the person in exactly like the existing password + 2FA flow does at its last step."""
        email = account.business_email if kind == "business" else account.email
        if _is_privileged(kind, account):
            return _refuse_privileged(kind)
        if getattr(account, "is_suspended", False):
            flash('Account suspended, contact <a href="mailto:fromperkpay@gmail.com">Contact Support</a>.', "danger")
            return redirect(_page(kind, "login"))
        if not account.email_confirmed:
            # should not normally happen (social accounts are created confirmed); use the normal path
            if kind == "business":
                session["pending_business_email"] = email
                return redirect(url_for("business_verify_email"))
            session["pending_email"] = email
            return redirect(url_for("verify_email"))
        if link_row is not None:
            link_row.last_login_at = db.func.now()
            db.session.commit()
        session.pop("social_ctx", None)
        session.pop("social_pending", None)
        flash("Login successful!")
        if kind == "business":
            session["business_id"] = account.id
            return redirect(url_for("business_dashboard"))
        login_user(account)
        return redirect(url_for("dashboard"))

    def _create_account(kind, pend):
        """Create the member/business from a verified pending identity, link it, and log in."""
        email = pend["email"]
        identity = {k: pend[k] for k in ("provider", "subject", "email")}
        if _find_account(kind, email):
            flash("An account with this email already exists. Please log in.", "warning")
            return redirect(_page(kind, "login"))
        try:
            if kind == "business":
                name = pend["business_name"]
                if Business.query.filter_by(business_name=name).first():
                    flash("That business name is already registered. Please choose another.", "warning")
                    return redirect(url_for("social_auth.complete"))
                sponsor_id = user_sponsor_id = None
                raw, owner_kind, owner = _ref_lookup("business", pend.get("ref") or "")
                if owner is not None:
                    if owner_kind == "business":
                        sponsor_id = owner.id
                    else:
                        user_sponsor_id = owner.id
                account = Business(
                    business_name=name, business_email=email, password=_random_pw_hash(),
                    referral_code=make_unique_referral_code(random_business_code(name)),
                    sponsor_id=sponsor_id, user_sponsor_id=user_sponsor_id, email_confirmed=True)
            else:
                sponsor_id = None
                raw, owner_kind, owner = _ref_lookup("member", pend.get("ref") or "")
                if owner is not None:
                    sponsor_id = owner.id
                account = User(
                    email=email, password=_random_pw_hash(),
                    name=(pend.get("name") or "")[:100] or None,
                    referral_code=make_unique_referral_code(random_referral_code(email)),
                    sponsor_id=sponsor_id, email_confirmed=True)
            db.session.add(account)
            db.session.flush()                      # get account.id
            _link(kind, account, identity)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            log.warning("social sign-up hit a uniqueness conflict for %s", email)
            flash("We couldn't create that account (it may already exist). Please try again or log in.", "danger")
            return redirect(_page(kind, "login"))
        row = SocialIdentity.query.filter_by(
            provider=identity["provider"], subject=identity["subject"],
            account_type="business" if kind == "business" else "user").first()
        return _finish_login(kind, account, row)

    def _send_code(pend):
        code = f"{secrets.randbelow(10**6):06d}"
        nonce = secrets.token_hex(8)
        pend.update(code_nonce=nonce, code_mac=_code_mac(nonce, code),
                    code_exp=time.time() + CODE_TTL, code_sent=time.time(), attempts=0)
        session["social_pending"] = pend
        send_email(pend["email"], "Your Perk Miner verification code",
                   f"<p>Your Perk Miner verification code is: <b>{code}</b></p>"
                   f"<p>It expires in 10 minutes. If you didn't try to sign up, ignore this email.</p>")

    # ------------------------------------------------------------------ identity from provider
    def _identity_from_token(provider, client, token):
        if provider == "facebook":
            resp = client.get("me", params={"fields": "id,name,email"}, token=token)
            resp.raise_for_status()
            data = resp.json()
            email = (data.get("email") or "").strip().lower()
            # Facebook only returns an e-mail it has confirmed; trusted for NEW accounts (not for linking)
            return {"provider": provider, "subject": str(data["id"]), "email": email,
                    "email_verified": bool(email), "can_link": False, "name": data.get("name") or ""}

        claims = token.get("userinfo")
        if not claims or not claims.get("sub"):
            raise ValueError("no id_token claims")
        sub = str(claims["sub"])

        if provider == "google":
            email = (claims.get("email") or "").strip().lower()
            ok = bool(email) and _truthy(claims.get("email_verified"))
            return {"provider": provider, "subject": sub, "email": email, "email_verified": ok,
                    "can_link": ok, "name": claims.get("name") or ""}

        if provider == "apple":
            email = (claims.get("email") or "").strip().lower()
            ok = bool(email) and _truthy(claims.get("email_verified"))
            name = ""
            try:
                u = json.loads(request.args.get("user") or "{}")     # only sent the very first time
                n = u.get("name") or {}
                name = f"{n.get('firstName', '')} {n.get('lastName', '')}".strip()
            except (ValueError, AttributeError):
                pass
            return {"provider": provider, "subject": sub, "email": email, "email_verified": ok,
                    "can_link": ok, "name": name}

        if provider == "microsoft":
            email = (claims.get("email") or "").strip().lower()
            if not email:
                pu = (claims.get("preferred_username") or "").strip().lower()
                email = pu if "@" in pu else ""
            # Only personal Microsoft accounts have an e-mail we can trust. Work/school accounts can carry
            # an e-mail the tenant admin typed in, so those must prove the address with an e-mailed code.
            personal = claims.get("tid") == MSA_TENANT_ID and bool(claims.get("email"))
            return {"provider": provider, "subject": sub, "email": email, "email_verified": personal,
                    "can_link": personal, "name": claims.get("name") or ""}

        raise ValueError("unknown provider")

    # ------------------------------------------------------------------ routes
    @bp.route("/auth/<provider>/start")
    def start(provider):
        if provider not in enabled:
            abort(404)
        kind = "business" if request.args.get("kind") == "business" else "member"
        mode = "login" if request.args.get("mode") == "login" else "register"

        # keep the user on the canonical host so the session cookie is there when the provider returns
        if public_base and not app.debug:
            want = urlparse(public_base).netloc
            if want and request.host != want:
                return redirect(public_base + request.full_path.rstrip("?"))

        typed = (request.args.get("ref") or "").strip()
        if typed:
            raw, _k, owner = _ref_lookup(kind, typed)
            if owner is None:
                flash("Invalid referral code.")
                return redirect(_page(kind, mode))
            session["pm_ref"] = raw
            g.pm_ref_cookie = raw

        ctx = {"provider": provider, "kind": kind, "mode": mode, "ts": time.time()}
        redirect_uri = (public_base or request.url_root.rstrip("/")) + url_for("social_auth.callback", provider=provider)
        client = oauth.create_client(provider)
        extra = {}
        if provider == "apple":
            client.client_secret = _apple_client_secret()
            extra["response_mode"] = "form_post"
            # Apple's scope list has no "openid", so Authlib would not create/check a nonce for us;
            # we do it ourselves (see callback()).
            ctx["nonce"] = secrets.token_urlsafe(20)
            extra["nonce"] = ctx["nonce"]
        elif provider in ("google", "microsoft"):
            extra["prompt"] = "select_account"
        session["social_ctx"] = ctx
        return client.authorize_redirect(redirect_uri, **extra)

    @csrf.exempt     # Apple POSTs here from apple.com; the signed `state` value protects this endpoint
    @bp.route("/auth/<provider>/callback", methods=["GET", "POST"])
    def callback(provider):
        if provider not in enabled:
            abort(404)
        if request.method == "POST":
            # Apple's form_post is a cross-site POST, so the browser withholds our session cookie. Bounce
            # to a normal GET (cookies ARE sent on top-level GET navigations) carrying the same fields.
            if provider != "apple":
                abort(405)
            keep = ("code", "state", "user", "error", "error_description")
            params = {k: request.form[k] for k in keep if request.form.get(k)}
            return redirect(url_for("social_auth.callback", provider=provider, **params), code=303)

        ctx = session.get("social_ctx")
        if (not ctx or ctx.get("provider") != provider
                or time.time() - ctx.get("ts", 0) > CTX_TTL):
            flash("Your sign-in session expired. Please try again.", "warning")
            return redirect(url_for("login"))
        kind, mode = ctx["kind"], ctx["mode"]

        client = oauth.create_client(provider)
        try:
            kwargs = {}
            if provider == "apple":
                client.client_secret = _apple_client_secret()
            if provider == "microsoft":
                kwargs["claims_options"] = {"iss": {"essential": True, "validate": _ms_issuer_ok}}
            token = client.authorize_access_token(**kwargs)
            if provider == "apple":
                token["userinfo"] = client.parse_id_token(token, nonce=ctx.get("nonce"))
            identity = _identity_from_token(provider, client, token)
        except Exception as exc:            # cancelled, expired, tampered, provider outage ...
            log.warning("social sign-in failed (%s): %s: %s", provider, type(exc).__name__, exc)
            flash("Sign-in with %s was cancelled or didn't complete. Please try again." % PROVIDER_META[provider]["label"], "warning")
            return redirect(_page(kind, mode))
        return _after_identity(identity, kind, mode)

    def _after_identity(identity, kind, mode):
        account_type = "business" if kind == "business" else "user"
        row = SocialIdentity.query.filter_by(provider=identity["provider"], subject=identity["subject"],
                                             account_type=account_type).first()
        if row:
            model = Business if kind == "business" else User
            account = db.session.get(model, row.account_id)
            if account is not None:
                return _finish_login(kind, account, row)
            db.session.delete(row)          # stale link to a deleted account
            db.session.commit()

        email = identity["email"]
        if not email or "@" not in email or len(email) > 200:
            flash("%s didn't share an email address with us. Please register with your email instead."
                  % PROVIDER_META[identity["provider"]]["label"], "warning")
            return redirect(_page(kind, "register"))

        existing = _find_account(kind, email)
        if existing is not None:
            if _is_privileged(kind, existing):
                return _refuse_privileged(kind)         # never link a provider to a staff/admin account
            if identity["can_link"]:
                if not existing.email_confirmed:
                    # never-confirmed account: whoever registered it may not own the address, so the
                    # old password must stop working before the real owner is signed in
                    existing.password = _random_pw_hash()
                    existing.email_confirmed = True
                _link(kind, existing, identity)
                db.session.commit()
                row = SocialIdentity.query.filter_by(provider=identity["provider"], subject=identity["subject"],
                                                     account_type=account_type).first()
                return _finish_login(kind, existing, row)
            flash("An account with this email already exists. Log in with your email and password.", "warning")
            return redirect(_page(kind, "login"))

        # brand-new person
        raw, _k, _o = _ref_lookup(kind)
        pend = {"provider": identity["provider"], "subject": identity["subject"], "email": email,
                "email_verified": identity["email_verified"], "name": identity["name"], "kind": kind,
                "ref": raw or "", "ts": time.time()}
        session["social_pending"] = pend
        if kind == "business":
            return redirect(url_for("social_auth.complete"))           # needs a business name first
        if pend["email_verified"]:
            return _create_account("member", pend)
        _send_code(pend)
        flash("We emailed you a 6-digit code to confirm your email address.")
        return redirect(url_for("social_auth.verify"))

    @bp.route("/auth/complete", methods=["GET", "POST"])
    def complete():
        """Businesses: social providers don't know the business name, so ask for it (and show the referral
        code that was filled in automatically from the invitation)."""
        pend = _get_pending()
        if not pend or pend.get("kind") != "business":
            flash("Please start again.", "warning")
            return redirect(url_for("business_register"))
        shown_ref = pend.get("ref") or ""
        name = pend.get("business_name") or ""
        if request.method == "POST":
            name = (request.form.get("business_name") or "").strip()
            shown_ref = (request.form.get("referral_code") or "").strip()
            error = None
            if len(name) < 2 or len(name) > 100:
                error = "Please enter your business name (2 to 100 characters)."
            elif Business.query.filter_by(business_name=name).first():
                error = "Business name already registered."
            elif shown_ref:
                raw, _k, owner = _ref_lookup("business", shown_ref)
                if owner is None:
                    error = "Invalid referral code."
            if error:
                flash(error, "danger")
            else:
                pend["business_name"] = name
                pend["ref"] = shown_ref
                session["social_pending"] = pend
                if pend["email_verified"]:
                    return _create_account("business", pend)
                _send_code(pend)
                flash("We emailed you a 6-digit code to confirm your email address.")
                return redirect(url_for("social_auth.verify"))
        return render_template("social_complete.html", email=pend["email"],
                               provider_label=PROVIDER_META[pend["provider"]]["label"],
                               business_name=name, referral_code=shown_ref,
                               ref_auto=bool(shown_ref and shown_ref == (pend.get("ref") or "")))

    @bp.route("/auth/verify", methods=["GET", "POST"])
    def verify():
        pend = _get_pending()
        if not pend or not pend.get("code_mac"):
            flash("Please start again.", "warning")
            return redirect(url_for("login"))
        kind = pend["kind"]
        if request.method == "POST":
            entered = re.sub(r"\D", "", request.form.get("code") or "")
            if pend.get("attempts", 0) >= MAX_CODE_ATTEMPTS or time.time() > pend.get("code_exp", 0):
                session.pop("social_pending", None)
                flash("That code expired. Please start again.", "warning")
                return redirect(_page(kind, "register"))
            if entered and hmac.compare_digest(_code_mac(pend["code_nonce"], entered), pend["code_mac"]):
                return _create_account(kind, pend)
            pend["attempts"] = pend.get("attempts", 0) + 1
            session["social_pending"] = pend
            flash("Incorrect code.", "danger")
        wait = max(0, int(CODE_RESEND_SECONDS - (time.time() - pend.get("code_sent", 0))))
        return render_template("social_verify.html", email=pend["email"], wait_seconds=wait)

    @bp.route("/auth/verify/resend", methods=["POST"])
    def verify_resend():
        pend = _get_pending()
        if pend and pend.get("code_mac") and time.time() - pend.get("code_sent", 0) >= CODE_RESEND_SECONDS:
            _send_code(pend)
            flash("A new code was sent.")
        return redirect(url_for("social_auth.verify"))

    app.register_blueprint(bp)
    return SocialAuth(enabled, _ref_lookup)
