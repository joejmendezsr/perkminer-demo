"""
Perk Miner Docuseries blueprint
================================

Routes
------
GET       /docuseries                          public landing page
GET/POST  /docuseries/pre-register             public; download + upload Talent Release Form, accept terms
GET       /docuseries/talent-release-form      public; downloads the blank form (PDF)
GET       /docuseries/member                   login + participant; video upload dashboard
GET       /docuseries/episodes                 public; full-page viewer
GET       /docuseries/media/episode/<id>       public; streams an episode stored as a private file
GET       /docuseries/admin                    admin only (ref code == refJoeJMendez)
GET       /docuseries/live_locations           admin only (sample page for now)
GET       /docuseries/admin/download/...       admin only; video + release-form downloads
POST      /docuseries/api/video/...            participant video upload API

Register once in your app factory / app.py:

    from docuseries_routes import docuseries_bp
    app.register_blueprint(docuseries_bp)

Everything you are likely to need to adapt (table/column names, URLs, session key)
is in the CONFIG block directly below.
"""
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import wraps
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from flask import (Blueprint, abort, current_app, flash, g, has_request_context,
                   jsonify, redirect, render_template, request, send_file,
                   send_from_directory, session, url_for)

# ---------------------------------------------------------------------------
# CONFIG  -- adapt these to your existing app
# ---------------------------------------------------------------------------
USER_TABLE = '"user"'                 # "user" is a reserved word in Postgres, so it must be quoted
USER_ID_COL = "id"
USER_EMAIL_COL = "email"
USER_NAME_SQL = "name"                # your user table has a single full-name column
USER_REF_COL = "referral_code"        # column that holds the owner value below
ADMIN_REF_VALUE = "refJoeJMendez"     # owner account: can always open /docuseries/admin
ALLOW_ROLE_ON_ADMIN = True            # True = any user with the docuseries role can also open it
ROLE_NAME = "docuseries"             # name of the row in your Role table (user.has_role("docuseries"))

SESSION_USER_KEY = "user_id"          # session key your login sets
LOGIN_URL = "/login"
BUSINESS_PROMO_CODE = "Challenge10"   # advertiser promo code participants can share
BUSINESS_SHARE_URL_TEMPLATE = "{base}/business/register?ref={code}"
REGISTER_URL = "/register"
DASHBOARD_URL = "/dashboard"
ADMIN_ROLES_URL = "/admin_roles_landing"

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
PENDING_COOKIE = "ds_pending"
PENDING_DAYS = 7

RELEASE_EXT = {"pdf", "jpg", "jpeg", "png", "heic", "heif"}
RELEASE_CTYPES = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                  "png": "image/png", "heic": "image/heic", "heif": "image/heif"}
MAX_RELEASE_BYTES = 10 * 1024 * 1024

ALLOWED_VIDEO_TYPES = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
                       "video/x-m4v": ".m4v", "video/3gpp": ".3gp"}
EXT_TO_VIDEO_TYPE = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
                     ".m4v": "video/x-m4v", ".3gp": "video/3gpp"}
DURATION_TOLERANCE = 2.0              # seconds of slack on the 30/60 second limits

# ---- Live recordings: participant status, directions, Google address suggestions -------------------
LOCATION_STATUSES = {"going": "I plan on going", "on_the_way": "On the way",
                     "arrived": "Arrived", "not_going": "Not going"}
ADMIN_LISTED_STATUSES = ("arrived", "on_the_way", "going")      # shown by name to the admin, in this order
# Environment variable(s) that hold your Google Maps key (first one found wins). Add yours if it differs,
# or set DOCUSERIES_MAPS_KEY_ENV=YOUR_VAR_NAME in Render.
MAPS_KEY_ENV_NAMES = ("GOOGLE_MAPS_API_KEY", "GOOGLE_API_KEY", "GOOGLE_PLACES_API_KEY", "MAPS_API_KEY")
MAPS_BIAS = {"lat": 36.1699, "lng": -115.1398, "radius": 60000}   # favour Las Vegas in the suggestions

# ---- Challenge + milestone configuration -----------------------------------------------
CHALLENGE_DAYS = 45
TX_TABLE = "user_transaction"         # <-- your transactions table (cash back + tier commissions)
BIZ_TABLE = "business"                # <-- your business / advertiser table (has user_sponsor_id)
WITHDRAWN_COL = "withdrawn_total"     # user column: total withdrawn
EARNINGS_COL = "grand_total_earnings" # user column: lifetime earnings

# Video slots. #1 and #2 are always open. #3-#17 unlock when the participant reaches the
# milestone (and the challenge has started). Lifetime-earnings milestones unlock at these amounts:
EARNING_STEPS = [100, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 5000]   # videos #8 .. #17

VIDEO_SLOTS = {
    1: {"title": "Upload your intro video", "max_seconds": 60,
        "hint": 'Start with: "My name is [name] from [city and state] and I\'m doing the '
                'Perk Miner 45-day challenge because [reason]."'},
    2: {"title": "Upload your pre-registration / post-registration video", "max_seconds": 30},
    3: {"title": "Your experience inviting others to become a member", "max_seconds": 60,
        "unlock": "Unlocks when someone you invited joins Perk Miner."},
    4: {"title": "Your experience seeing cash back and referral commissions", "max_seconds": 60,
        "unlock": "Unlocks when you have earned cash back from your own purchases and at least one referral commission."},
    5: {"title": "Your experience withdrawing funds to your bank account", "max_seconds": 60,
        "unlock": "Unlocks after your first withdrawal to your bank account."},
    6: {"title": "Your experience inviting your first business", "max_seconds": 60,
        "unlock": "Unlocks when a business you invited becomes an advertiser."},
    7: {"title": "Your experience earning your first referral commission from a business sale", "max_seconds": 60,
        "unlock": "Unlocks when a business you referred finalizes a transaction."},
}
for _i, _amt in enumerate(EARNING_STEPS):
    VIDEO_SLOTS[8 + _i] = {"title": f"Your experience earning ${_amt:,} in lifetime earnings", "max_seconds": 60,
                           "unlock": f"Unlocks when your lifetime earnings reach ${_amt:,}."}
ALWAYS_OPEN = {1, 2}

ASSET_DIR = Path(__file__).resolve().parent / "assets"
RELEASE_FORM_FILE = "Perk_Miner_Docuseries_Talent_Release_Form.pdf"

docuseries_bp = Blueprint("docuseries", __name__, url_prefix="/docuseries",
                          template_folder="templates")


# ---------------------------------------------------------------------------
# Database helpers (plain psycopg2 -- swap _conn() for your existing helper if you have one)
# ---------------------------------------------------------------------------
def _dsn():
    """Find the database URL: DATABASE_URL (env or config), else your SQLAlchemy URI."""
    dsn = (current_app.config.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
           or current_app.config.get("SQLALCHEMY_DATABASE_URI"))
    if not dsn:
        raise RuntimeError("docuseries: set DATABASE_URL or SQLALCHEMY_DATABASE_URI")
    if hasattr(dsn, "render_as_string"):  # SQLAlchemy URL object
        dsn = dsn.render_as_string(hide_password=False)
    # psycopg2 wants postgresql://  (not postgres:// or postgresql+psycopg2://)
    return re.sub(r"^postgres(ql)?(\+\w+)?://", "postgresql://", str(dsn))


def _conn():
    return psycopg2.connect(_dsn(), cursor_factory=psycopg2.extras.RealDictCursor)


@contextmanager
def _cursor(commit=False):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def q_all(sql, params=()):
    with _cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def q_one(sql, params=()):
    with _cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def q_exec(sql, params=()):
    """Run a write. Returns the first row if the statement has RETURNING, else None."""
    with _cursor(commit=True) as cur:
        cur.execute(sql, params)
        return cur.fetchone() if cur.description else None


def q_tx(statements):
    """Run several writes [(sql, params), ...] in ONE transaction (all or nothing)."""
    with _cursor(commit=True) as cur:
        for sql, params in statements:
            cur.execute(sql, params)


def get_setting(key, default=""):
    row = q_one("SELECT value FROM docuseries_settings WHERE key = %s", (key,))
    return (row or {}).get("value") or default


# ---------------------------------------------------------------------------
# Challenge dates + milestones
# ---------------------------------------------------------------------------
def challenge_info():
    """Start/end dates and where today falls. Admin sets the start date on /docuseries/admin."""
    raw = get_setting("challenge_start_date")
    try:
        start = date.fromisoformat(raw) if raw else None
    except ValueError:
        start = None
    info = {"start": start, "end": None, "started": False, "finished": False, "day": None,
            "days": CHALLENGE_DAYS}
    if start:
        today = datetime.now(LOCAL_TZ).date()
        info["end"] = start + timedelta(days=CHALLENGE_DAYS - 1)
        info["started"] = today >= start
        info["finished"] = today > info["end"]
        if info["started"] and not info["finished"]:
            info["day"] = (today - start).days + 1
    return info


def compute_milestones(user_id):
    """Which milestone videos (#3-#17) this participant has earned.
    Returns (set_of_video_numbers, error_text_or_None). Never raises."""
    sql = f"""
    SELECT
      EXISTS (SELECT 1 FROM {USER_TABLE} s WHERE s.sponsor_id = u.id) AS m3,
      (EXISTS (SELECT 1 FROM {TX_TABLE} t
                WHERE t.user_referral_id = u.referral_code AND t.cash_back > 0)
       AND EXISTS (SELECT 1 FROM {TX_TABLE} t
                WHERE (t.tier2_user_referral_id = u.referral_code AND t.tier2_commission > 0)
                   OR (t.tier3_user_referral_id = u.referral_code AND t.tier3_commission > 0)
                   OR (t.tier4_user_referral_id = u.referral_code AND t.tier4_commission > 0)
                   OR (t.tier5_user_referral_id = u.referral_code AND t.tier5_commission > 0))) AS m4,
      (COALESCE(u.{WITHDRAWN_COL}, 0) > 0) AS m5,
      EXISTS (SELECT 1 FROM {BIZ_TABLE} b WHERE b.user_sponsor_id = u.id) AS m6,
      EXISTS (SELECT 1 FROM {TX_TABLE} t
                WHERE t.tier1_business_user_referral_id = u.referral_code) AS m7,
      COALESCE(u.{EARNINGS_COL}, 0) AS earned
    FROM {USER_TABLE} u WHERE u.{USER_ID_COL} = %s"""
    try:
        row = q_one(sql, (user_id,))
    except Exception as exc:
        current_app.logger.exception("docuseries: milestone query failed")
        return set(), f"{type(exc).__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else ''}"
    if not row:
        return set(), None
    done = {n for n, col in ((3, "m3"), (4, "m4"), (5, "m5"), (6, "m6"), (7, "m7")) if row.get(col)}
    earned = Decimal(str(row.get("earned") or 0))
    for i, amount in enumerate(EARNING_STEPS):
        if earned > Decimal(amount) - Decimal("0.01"):      # same as "> 99.99", "> 499.99", ...
            done.add(8 + i)
    return done, None


def slot_unlocked(user_id, number):
    """(ok, message). Slots 1-2 are always open; 3-17 need the challenge started + milestone reached."""
    if number not in VIDEO_SLOTS:
        return False, "That video slot doesn't exist."
    if number in ALWAYS_OPEN:
        return True, ""
    if not challenge_info()["started"]:
        return False, "Milestone videos unlock after the challenge begins."
    done, err = compute_milestones(user_id)
    if err:
        return False, "We couldn't check your milestones right now. Please try again later."
    if number not in done:
        return False, "You haven't reached the milestone for this video yet."
    return True, ""


# ---------------------------------------------------------------------------
# Current user / permissions
# ---------------------------------------------------------------------------
def _has_docuseries_role(uid):
    """True if the user has the 'docuseries' role in your Role table."""
    try:  # normal path: your User.has_role() (Flask-Login current_user)
        from flask_login import current_user as fl_user
        if fl_user.is_authenticated and str(fl_user.get_id()) == str(uid):
            return bool(fl_user.has_role(ROLE_NAME))
    except Exception:
        pass
    try:  # fallback: read your user_roles / role tables directly
        return bool(q_one("SELECT 1 AS ok FROM user_roles ur JOIN role r ON r.id = ur.role_id "
                          "WHERE ur.user_id = %s AND r.name = %s", (uid, ROLE_NAME)))
    except Exception:
        return False


def _load_user(uid):
    user = q_one(
        f"SELECT {USER_ID_COL} AS id, {USER_EMAIL_COL} AS email, {USER_NAME_SQL} AS full_name, "
        f"{USER_REF_COL} AS ref_code, release_form, docuseries_terms, docuseries_welcomed "
        f"FROM {USER_TABLE} WHERE {USER_ID_COL} = %s", (uid,))
    if user:
        user["docuseries_role"] = _has_docuseries_role(uid)
    return user


def _logged_in_user_id():
    """Id of the logged-in user. Works with Flask-Login (what your app uses), and
    falls back to a plain session key (SESSION_USER_KEY) if Flask-Login isn't active."""
    try:
        from flask_login import current_user as fl_user
        if fl_user.is_authenticated:
            uid = fl_user.get_id()
            return int(uid) if str(uid).isdigit() else uid
    except Exception:
        pass
    return session.get(SESSION_USER_KEY)


def current_user():
    uid = _logged_in_user_id()
    if not uid:
        return None
    if "docuseries_user" not in g:
        user = _load_user(uid)
        # A visitor who completed /pre-register before creating their account gets
        # their form attached automatically the first time we see them logged in.
        if user and not is_participant(user) and request.cookies.get(PENDING_COOKIE):
            try:
                if claim_pending_docuseries(user["id"]):
                    user = _load_user(uid)
            except Exception:  # never break a page because of this
                current_app.logger.exception("docuseries: claim failed")
        g.docuseries_user = user
    return g.docuseries_user


def is_participant(user):
    return bool(user and user.get("docuseries_terms") and user.get("release_form"))


def is_docuseries_admin(user):
    if not user:
        return False
    if user.get("ref_code") == ADMIN_REF_VALUE:
        return True
    return ALLOW_ROLE_ON_ADMIN and bool(user.get("docuseries_role"))


def claim_pending_docuseries(user_id):
    """Attach a release form uploaded on /pre-register (before the person had an account)
    to the newly registered / logged-in user. Safe to call from your registration view too:

        from docuseries_routes import claim_pending_docuseries
        claim_pending_docuseries(new_user_id)
    """
    token = request.cookies.get(PENDING_COOKIE)
    if not token:
        return False
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, release_form FROM docuseries_pending "
                "WHERE token = %s AND claimed_user_id IS NULL "
                f"AND created_at > now() - interval '{PENDING_DAYS} days' FOR UPDATE", (token,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                g.docuseries_clear_cookie = True
                return False
            cur.execute(
                f"UPDATE {USER_TABLE} SET release_form = %s, docuseries_terms = TRUE, "
                f"docuseries_terms_at = now() WHERE {USER_ID_COL} = %s", (row["release_form"], user_id))
            cur.execute("UPDATE docuseries_pending SET claimed_user_id = %s, claimed_at = now() "
                        "WHERE id = %s", (user_id, row["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    g.docuseries_clear_cookie = True
    g.pop("docuseries_user", None)
    return True


def login_required_ds(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/docuseries/api/"):
                return jsonify(error="Please log in again."), 401
            nxt = quote(request.full_path.rstrip("?"), safe="")
            return redirect(f"{LOGIN_URL}?next={nxt}")
        return view(*args, **kwargs)
    return wrapper


def participant_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_participant(current_user()):
            if request.path.startswith("/docuseries/api/"):
                return jsonify(error="Complete the docuseries pre-registration first."), 403
            flash("Please complete the docuseries pre-registration first (upload your Talent "
                  "Release Form and accept the terms).", "error")
            return redirect(url_for("docuseries.pre_register"))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_docuseries_admin(current_user()):
            abort(403)
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Template helpers (available in every template once the blueprint is registered)
# ---------------------------------------------------------------------------
@docuseries_bp.app_context_processor
def _inject():
    def docuseries_participant():
        return has_request_context() and is_participant(current_user())

    def docuseries_role():
        u = current_user() if has_request_context() else None
        return bool(u and u.get("docuseries_role"))

    return {"docuseries_participant": docuseries_participant, "docuseries_role": docuseries_role}


def _local(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


@docuseries_bp.app_template_filter("ds_dt")
def ds_dt(value):
    dt = _local(value)
    if not dt:
        return ""
    hour = dt.hour % 12 or 12
    return f"{dt:%a, %b} {dt.day}, {dt:%Y} · {hour}:{dt:%M} {dt:%p} PT"


@docuseries_bp.app_template_filter("ds_input")
def ds_input(value):
    """Format a datetime for an <input type="datetime-local"> (Pacific time)."""
    dt = _local(value)
    return dt.strftime("%Y-%m-%dT%H:%M") if dt else ""


def maps_api_key():
    """Browser key for Google address suggestions. It is placed in the admin page, so it MUST be
    restricted to your website (HTTP referrers) in Google Cloud. Returns '' if none is set."""
    names = [os.environ.get("DOCUSERIES_MAPS_KEY_ENV", "").strip()] + list(MAPS_KEY_ENV_NAMES)
    for name in filter(None, names):
        value = os.environ.get(name) or current_app.config.get(name)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def directions_url(address, place_id=None, lat=None, lng=None):
    """Google Maps link that opens turn-by-turn driving directions from the person's current location."""
    dest = (address or "").strip() or (f"{lat},{lng}" if lat is not None and lng is not None else "")
    if not dest:
        return None
    url = "https://www.google.com/maps/dir/?api=1&travelmode=driving&destination=" + quote(dest)
    if place_id:
        url += "&destination_place_id=" + quote(place_id)
    return url


def _decorate_location(row):
    row["directions_url"] = directions_url(row.get("address") or row.get("location_name"),
                                           row.get("place_id"), row.get("latitude"), row.get("longitude"))
    return row


def _parse_local(value):
    """'2026-12-05T10:00' (typed in Pacific time) -> aware datetime, or None."""
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%dT%H:%M").replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return None


@docuseries_bp.app_template_filter("ds_date")
def ds_date(value):
    if not value:
        return ""
    return f"{value:%A, %B} {value.day}, {value:%Y}"


@docuseries_bp.app_template_filter("ds_mb")
def ds_mb(value):
    return f"{(value or 0) / 1048576:.1f} MB"


@docuseries_bp.after_app_request
def _clear_pending_cookie(resp):
    if g.get("docuseries_clear_cookie"):
        resp.delete_cookie(PENDING_COOKIE)
    return resp


# ---------------------------------------------------------------------------
# Private file storage: Cloudflare R2 (S3 API) in production, local disk for dev
# ---------------------------------------------------------------------------
CLD_PREFIX = "docuseries/"      # folder inside your Cloudinary account


def _cld_configured():
    """Cloudinary is used when DOCUSERIES_STORAGE=cloudinary and the SDK is already configured
    (CLOUDINARY_URL, or cloudinary.config(...) in your app)."""
    if os.environ.get("DOCUSERIES_STORAGE", "").strip().lower() != "cloudinary":
        return False
    try:
        import cloudinary
        c = cloudinary.config()
        return bool(c.cloud_name and c.api_key and c.api_secret)
    except Exception:
        return False


def storage_mode():
    if _cld_configured():
        return "cloudinary"
    needed = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    return "r2" if all(os.environ.get(k) for k in needed) else "local"


def max_video_bytes():
    """Per-video size limit. Cloudinary's free plan caps videos at 100 MB (paid plans: 2 GB+)."""
    env = os.environ.get("DOCUSERIES_MAX_VIDEO_MB")
    if env:
        return int(env) * 1024 * 1024
    return (100 if storage_mode() == "cloudinary" else 300) * 1024 * 1024


def storage_ready():
    """False when uploads would land on a temporary disk that is wiped on every deploy
    (e.g. Render without R2 or a persistent disk). We refuse uploads rather than lose them."""
    if storage_mode() in ("r2", "cloudinary") or os.environ.get("DOCUSERIES_LOCAL_DIR"):
        return True
    return bool(current_app.debug or os.environ.get("DOCUSERIES_ALLOW_EPHEMERAL"))


def _s3():
    import boto3
    from botocore.config import Config
    try:
        # boto3 1.36+ adds checksum parameters by default that Cloudflare R2 rejects
        # (and that break browser uploads through presigned URLs), so switch them off.
        cfg = Config(signature_version="s3v4", request_checksum_calculation="when_required",
                     response_checksum_validation="when_required")
    except TypeError:  # older botocore doesn't have these options (and doesn't need them)
        cfg = Config(signature_version="s3v4")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=cfg,
    )


def _bucket():
    return os.environ["R2_BUCKET"]


def _local_path(key):
    base = (Path(os.environ.get("DOCUSERIES_LOCAL_DIR") or
                 Path(current_app.instance_path) / "docuseries_private")).resolve()
    path = (base / key).resolve()
    if base not in path.parents:
        abort(400)
    return path


def _cld_target(key):
    """Map a stored key to (Cloudinary public_id, resource_type, format).
    Videos are 'video' assets (public_id without extension); everything else is 'raw'
    (public_id keeps its extension). All assets use type 'authenticated' = never public."""
    ext = Path(key).suffix.lstrip(".").lower()
    if key.startswith(("videos/", "episodes/")):
        return CLD_PREFIX + (key[:-(len(ext) + 1)] if ext else key), "video", (ext or None)
    return CLD_PREFIX + key, "raw", None


def _cld_resource(key):
    """Cloudinary's record of the asset (bytes, duration, format...) or None if missing."""
    import cloudinary.api
    pid, rtype, _ = _cld_target(key)
    try:
        return cloudinary.api.resource(pid, resource_type=rtype, type="authenticated")
    except Exception:
        return None


def save_private(stream, key, content_type):
    if storage_mode() == "cloudinary":
        import cloudinary.uploader
        pid, rtype, _ = _cld_target(key)
        cloudinary.uploader.upload(stream, public_id=pid, resource_type=rtype, type="authenticated",
                                   overwrite=False, use_filename=False, unique_filename=False)
    elif storage_mode() == "r2":
        _s3().upload_fileobj(stream, _bucket(), key, ExtraArgs={"ContentType": content_type})
    else:
        path = _local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as out:
            shutil.copyfileobj(stream, out, length=1024 * 1024)


def private_size(key):
    """Size in bytes, or None if the object doesn't exist."""
    if storage_mode() == "cloudinary":
        info = _cld_resource(key)
        return int(info.get("bytes") or 0) if info else None
    if storage_mode() == "r2":
        from botocore.exceptions import ClientError
        try:
            return _s3().head_object(Bucket=_bucket(), Key=key)["ContentLength"]
        except ClientError:
            return None
    path = _local_path(key)
    return path.stat().st_size if path.exists() else None


def delete_private(key):
    try:
        if storage_mode() == "cloudinary":
            import cloudinary.uploader
            pid, rtype, _ = _cld_target(key)
            cloudinary.uploader.destroy(pid, resource_type=rtype, type="authenticated", invalidate=True)
        elif storage_mode() == "r2":
            _s3().delete_object(Bucket=_bucket(), Key=key)
        else:
            _local_path(key).unlink(missing_ok=True)
    except Exception:
        current_app.logger.exception("docuseries: delete failed for %s", key)


def presigned_put(key, content_type, expires=900):
    return _s3().generate_presigned_url(
        "put_object", Params={"Bucket": _bucket(), "Key": key, "ContentType": content_type},
        ExpiresIn=expires)


def private_response(key, download_name=None, inline=False, expires=300, stream=False):
    """Return a Flask response that delivers a private file.
    stream=True is for playing video in a <video> tag (Cloudinary: signed CDN link that supports seeking)."""
    if storage_mode() == "cloudinary":
        import cloudinary.utils
        pid, rtype, fmt = _cld_target(key)
        opts = {"resource_type": rtype, "type": "authenticated", "expires_at": int(time.time()) + expires}
        if not inline:
            opts["attachment"] = True
        try:
            if inline and stream:   # signed delivery link (plays and seeks like normal video)
                url = cloudinary.utils.cloudinary_url(pid, resource_type=rtype, type="authenticated",
                                                      sign_url=True, secure=True, format=fmt)[0]
            else:                   # expiring, signed download link
                url = cloudinary.utils.private_download_url(pid, fmt, **opts)
        except Exception:
            current_app.logger.exception("docuseries: Cloudinary link failed; trying signed URL")
            url = cloudinary.utils.cloudinary_url(pid, resource_type=rtype, type="authenticated",
                                                  sign_url=True, secure=True, format=fmt)[0]
        resp = redirect(url)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    if storage_mode() == "r2":
        disp = "inline" if inline else f'attachment; filename="{download_name or Path(key).name}"'
        params = {"Bucket": _bucket(), "Key": key, "ResponseContentDisposition": disp}
        url = _s3().generate_presigned_url("get_object", Params=params, ExpiresIn=expires)
        resp = redirect(url)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    path = _local_path(key)
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=not inline, download_name=download_name or path.name,
                     conditional=True, max_age=0)


def probe_duration(path):
    """Duration in seconds using ffprobe if it is installed, else None."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20)
        return float(out.stdout.strip())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Release form validation
# ---------------------------------------------------------------------------
def _sniff_ok(head, ext):
    if ext == "pdf":
        return head.startswith(b"%PDF")
    if ext in ("jpg", "jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in ("heic", "heif"):
        return b"ftyp" in head[:16]
    return False


def save_release_form(file):
    if not storage_ready():
        current_app.logger.error("docuseries: file storage is not configured (set DOCUSERIES_STORAGE=cloudinary, or the R2_* variables)")
        raise ValueError("File uploads aren't available right now. Please try again soon.")
    if not file or not file.filename:
        raise ValueError("Please upload your signed Talent Release Form.")
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in RELEASE_EXT:
        raise ValueError("Upload the form as a PDF, JPG, PNG or HEIC file.")
    head = file.stream.read(16)
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size == 0 or not _sniff_ok(head, ext):
        raise ValueError("That file doesn't look like a valid PDF or image. Please try again.")
    if size > MAX_RELEASE_BYTES:
        raise ValueError("The file is too large. Please keep it under 10 MB.")
    key = f"release_forms/{uuid.uuid4().hex}.{ext}"
    save_private(file.stream, key, RELEASE_CTYPES[ext])
    return key


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------
@docuseries_bp.route("/", strict_slashes=False)
def landing():
    return render_template("docuseries/landing.html")


@docuseries_bp.route("/pre-registration")
def pre_registration_alias():
    return redirect(url_for("docuseries.pre_register"), code=301)


@docuseries_bp.route("/talent-release-form")
def talent_release_form():
    return send_from_directory(ASSET_DIR, RELEASE_FORM_FILE, as_attachment=True,
                               download_name=RELEASE_FORM_FILE)


@docuseries_bp.route("/pre-register", methods=["GET", "POST"])
def pre_register():
    user = current_user()

    def show(status=200):
        return render_template("docuseries/pre_register.html", user=user,
                               already=is_participant(user), register_url=REGISTER_URL), status

    if request.method == "GET":
        return show()

    if not request.form.get("agree_terms"):
        flash("Please check the box to agree to the terms and conditions.", "error")
        return show(400)
    try:
        key = save_release_form(request.files.get("release_form"))
    except ValueError as exc:
        flash(str(exc), "error")
        return show(400)

    if user:  # already has an account: save straight to the user row
        q_exec(f"UPDATE {USER_TABLE} SET release_form = %s, docuseries_terms = TRUE, "
               f"docuseries_terms_at = now() WHERE {USER_ID_COL} = %s", (key, user["id"]))
        g.pop("docuseries_user", None)
        flash("You're in! Your Talent Release Form was received.", "success")
        return redirect(url_for("docuseries.member"))

    # No account yet: park the form until they register (claimed automatically).
    token = secrets.token_urlsafe(32)
    q_exec("INSERT INTO docuseries_pending (token, release_form) VALUES (%s, %s)", (token, key))
    resp = redirect(f"{REGISTER_URL}?docuseries=1")
    resp.set_cookie(PENDING_COOKIE, token, max_age=PENDING_DAYS * 86400, httponly=True,
                    secure=request.is_secure, samesite="Lax")
    return resp


# ---------------------------------------------------------------------------
# Episodes (public)
# ---------------------------------------------------------------------------
NAMES_DISPLAY_LIMIT = 100       # characters of participant names shown before "Show more"


def summarize_names(names, limit=NAMES_DISPLAY_LIMIT):
    """-> (short_text, full_text, was_truncated). The short text never exceeds `limit` characters
    and is cut between names, so a name is never chopped in half (unless one name alone is longer)."""
    full = ", ".join(n for n in names if n)
    if len(full) <= limit:
        return full, full, False
    short = ""
    for n in names:
        candidate = n if not short else short + ", " + n
        if len(candidate) > limit:
            break
        short = candidate
    if not short:                      # a single name longer than the limit
        short = full[:limit].rstrip()
    return short, full, True


def parse_youtube_id(value):
    """Accepts a YouTube link (watch, youtu.be, embed, shorts, live) or a bare 11-character id."""
    v = (value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", v):
        return v
    m = re.search(r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/)|youtu\.be/)"
                  r"([A-Za-z0-9_-]{11})", v)
    return m.group(1) if m else None


def build_player(ep):
    kind, ref = ep["playback_type"], (ep["playback_ref"] or "").strip()
    if kind == "youtube" and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", ref):
        return {"kind": "iframe",
                "src": f"https://www.youtube-nocookie.com/embed/{ref}?rel=0&modestbranding=1&iv_load_policy=3"}
    code = os.environ.get("CF_STREAM_CUSTOMER_CODE")
    if kind == "stream" and code and re.fullmatch(r"[A-Za-z0-9._-]{20,}", ref):
        return {"kind": "iframe",
                "src": f"https://customer-{code}.cloudflarestream.com/{ref}/iframe?preload=true"}
    if kind == "file" and ref:
        return {"kind": "video", "src": url_for("docuseries.episode_media", episode_id=ep["id"])}
    return None


@docuseries_bp.route("/episodes")
def episodes():
    eps = q_all(
        "SELECT id, episode_number, title, description, playback_type, playback_ref, "
        "poster_url, air_date, duration_minutes FROM docuseries_episodes "
        "WHERE published ORDER BY episode_number")
    names = {}
    for row in q_all("SELECT episode_id, display_name FROM docuseries_episode_participants "
                     "ORDER BY id"):
        names.setdefault(row["episode_id"], []).append(row["display_name"])
    for ep in eps:
        ep["participants"] = names.get(ep["id"], [])
        ep["names_short"], ep["names_full"], ep["names_truncated"] = summarize_names(ep["participants"])
        ep["player"] = build_player(ep)
    wanted = request.args.get("ep", type=int)
    selected = next((e for e in eps if e["episode_number"] == wanted), eps[0] if eps else None)
    return render_template("docuseries/episodes.html", episodes=eps, selected=selected)


@docuseries_bp.route("/media/episode/<int:episode_id>")
def episode_media(episode_id):
    ep = q_one("SELECT playback_type, playback_ref FROM docuseries_episodes "
               "WHERE id = %s AND published", (episode_id,))
    if not ep or ep["playback_type"] != "file":
        abort(404)
    return private_response(ep["playback_ref"], inline=True, expires=3600, stream=True)


# ---------------------------------------------------------------------------
# Participant dashboard + video upload API
# ---------------------------------------------------------------------------
@docuseries_bp.route("/member")
@login_required_ds
@participant_required
def member():
    user = current_user()
    first_time = not user["docuseries_welcomed"]
    if first_time:
        q_exec(f"UPDATE {USER_TABLE} SET docuseries_welcomed = TRUE WHERE {USER_ID_COL} = %s",
               (user["id"],))
    uploaded = {r["video_number"]: r for r in q_all(
        "SELECT video_number, size_bytes, duration_seconds, completed_at "
        "FROM docuseries_videos WHERE user_id = %s AND status = 'ready'", (user["id"],))}
    ch = challenge_info()
    done, milestone_error = compute_milestones(user["id"]) if ch["started"] else (set(), None)
    slots = []
    for n in sorted(VIDEO_SLOTS):
        if n in uploaded:
            status = "uploaded"
        elif n in ALWAYS_OPEN or (ch["started"] and n in done):
            status = "open"
        else:
            status = "locked"
        slots.append({"number": n, **VIDEO_SLOTS[n], "video": uploaded.get(n), "status": status})
    live = q_all("SELECT id, title, location_name, address, starts_at, ends_at, notes, directions, "
                 "place_id, latitude, longitude FROM docuseries_live_locations "
                 "WHERE COALESCE(ends_at, starts_at + interval '3 hours') >= now() "
                 "ORDER BY starts_at LIMIT 10")
    mine = {r["location_id"]: r["status"] for r in q_all(
        "SELECT location_id, status FROM docuseries_location_status WHERE user_id = %s", (user["id"],))}
    for row in live:
        _decorate_location(row)
        row["my_status"] = mine.get(row["id"], "")
    return render_template("docuseries/member.html", user=user, first_time=first_time, slots=slots,
                           challenge=ch, live_locations=live, milestone_error=milestone_error,
                           location_statuses=LOCATION_STATUSES,
                           max_video_mb=max_video_bytes() // 1048576)


def _json_error(msg, status=400):
    return jsonify(error=msg), status


@docuseries_bp.post("/api/video/init")
@login_required_ds
@participant_required
def api_video_init():
    if not storage_ready():
        current_app.logger.error("docuseries: file storage is not configured (set DOCUSERIES_STORAGE=cloudinary, or the R2_* variables)")
        return _json_error("Video uploads aren't available yet. Please try again soon.", 503)
    data = request.get_json(silent=True) or {}
    try:
        number = int(data.get("video_number", 0))
    except (TypeError, ValueError):
        number = 0
    ok, why = slot_unlocked(current_user()["id"], number)
    if not ok:
        return _json_error(why, 403 if number in VIDEO_SLOTS else 400)
    spec = VIDEO_SLOTS[number]

    filename = str(data.get("filename") or "")
    ext = os.path.splitext(filename)[1].lower()
    ctype = (data.get("content_type") or "").lower()
    if ctype not in ALLOWED_VIDEO_TYPES:
        ctype = EXT_TO_VIDEO_TYPE.get(ext, "")
    if not ctype:
        return _json_error("Please upload an MP4, MOV or WebM video.")
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0 or size > max_video_bytes():
        return _json_error(f"Videos must be under {max_video_bytes() // 1048576} MB.")
    duration = data.get("duration")
    try:
        duration = round(float(duration), 1) if duration not in (None, "") else None
    except (TypeError, ValueError):
        duration = None
    if duration is not None and duration > spec["max_seconds"] + DURATION_TOLERANCE:
        return _json_error(f"This video is {duration:.0f} seconds. The limit is {spec['max_seconds']} seconds.")

    user = current_user()
    key = f"videos/{user['id']}/{number:02d}-{uuid.uuid4().hex}{ALLOWED_VIDEO_TYPES[ctype]}"
    row = q_exec(
        "INSERT INTO docuseries_videos (user_id, video_number, storage_key, content_type, size_bytes, "
        "duration_seconds, status) VALUES (%s, %s, %s, %s, %s, %s, 'pending') "
        "ON CONFLICT (user_id, video_number) DO UPDATE SET storage_key = EXCLUDED.storage_key, "
        "content_type = EXCLUDED.content_type, size_bytes = EXCLUDED.size_bytes, "
        "duration_seconds = EXCLUDED.duration_seconds, status = 'pending', created_at = now() "
        "WHERE docuseries_videos.status <> 'ready' RETURNING id",
        (user["id"], number, key, ctype, size, duration))
    if not row:
        return _json_error("You've already uploaded this video. It can only be uploaded once.", 409)

    if storage_mode() == "cloudinary":
        import cloudinary
        import cloudinary.utils
        pid, _, _ = _cld_target(key)
        cfg = cloudinary.config()
        signed = {"public_id": pid, "timestamp": int(time.time()), "type": "authenticated"}
        fields = dict(signed, api_key=cfg.api_key,
                      signature=cloudinary.utils.api_sign_request(signed, cfg.api_secret))
        return jsonify(mode="cloudinary", video_id=row["id"], fields=fields,
                       upload_url=f"https://api.cloudinary.com/v1_1/{cfg.cloud_name}/video/upload")
    if storage_mode() == "r2":
        return jsonify(mode="direct", video_id=row["id"], content_type=ctype,
                       upload_url=presigned_put(key, ctype))
    return jsonify(mode="server", video_id=row["id"],
                   upload_url=url_for("docuseries.api_video_upload", video_id=row["id"]))


def _pending_video(video_id):
    return q_one("SELECT id, video_number, storage_key, content_type, status FROM docuseries_videos "
                 "WHERE id = %s AND user_id = %s", (video_id, current_user()["id"]))


def _fail_video(row):
    delete_private(row["storage_key"])
    q_exec("UPDATE docuseries_videos SET status = 'failed' WHERE id = %s", (row["id"],))


@docuseries_bp.post("/api/video/<int:video_id>/upload")
@login_required_ds
@participant_required
def api_video_upload(video_id):
    """Server-side upload path (local storage / development)."""
    if storage_mode() != "local":
        abort(404)
    row = _pending_video(video_id)
    if not row or row["status"] != "pending":
        return _json_error("Upload session not found. Please start again.", 404)
    upload = request.files.get("file")
    if not upload:
        return _json_error("No file received.")
    if request.content_length and request.content_length > max_video_bytes() + 2 * 1024 * 1024:
        return _json_error("That file is too large.", 413)
    save_private(upload.stream, row["storage_key"], row["content_type"])
    size = private_size(row["storage_key"])
    if size is None or size > max_video_bytes():
        _fail_video(row)
        return _json_error("That file is too large.", 413)
    limit = VIDEO_SLOTS[row["video_number"]]["max_seconds"]
    duration = probe_duration(_local_path(row["storage_key"]))
    if duration is not None and duration > limit + DURATION_TOLERANCE:
        _fail_video(row)
        return _json_error(f"This video is {duration:.0f} seconds. The limit is {limit} seconds.")
    q_exec("UPDATE docuseries_videos SET status = 'ready', size_bytes = %s, completed_at = now(), "
           "duration_seconds = COALESCE(%s, duration_seconds), duration_verified = %s WHERE id = %s",
           (size, round(duration, 1) if duration is not None else None, duration is not None, row["id"]))
    return jsonify(ok=True)


@docuseries_bp.post("/api/video/<int:video_id>/complete")
@login_required_ds
@participant_required
def api_video_complete(video_id):
    """Called by the browser after a direct-to-R2 upload finishes."""
    row = _pending_video(video_id)
    if not row:
        return _json_error("Upload not found.", 404)
    if row["status"] == "ready":
        return jsonify(ok=True)
    if storage_mode() == "cloudinary":
        info = _cld_resource(row["storage_key"])
        if not info:
            return _json_error("We couldn't find your upload. Please try again.")
        size = int(info.get("bytes") or 0)
        duration = info.get("duration")
        limit = VIDEO_SLOTS[row["video_number"]]["max_seconds"]
        if size > max_video_bytes():
            _fail_video(row)
            return _json_error(f"Videos must be under {max_video_bytes() // 1048576} MB.")
        if duration is not None and duration > limit + DURATION_TOLERANCE:
            _fail_video(row)
            return _json_error(f"This video is {duration:.0f} seconds. The limit is {limit} seconds.")
        key, fmt = row["storage_key"], info.get("format")
        if fmt and Path(key).suffix.lstrip(".").lower() != fmt.lower():
            key = key.rsplit(".", 1)[0] + "." + fmt.lower()
        q_exec("UPDATE docuseries_videos SET status = 'ready', size_bytes = %s, completed_at = now(), "
               "duration_seconds = COALESCE(%s, duration_seconds), duration_verified = %s, "
               "storage_key = %s WHERE id = %s",
               (size, round(float(duration), 1) if duration is not None else None,
                duration is not None, key, row["id"]))
        return jsonify(ok=True)
    size = private_size(row["storage_key"])
    if size is None:
        return _json_error("We couldn't find your upload. Please try again.")
    if size > max_video_bytes():
        _fail_video(row)
        return _json_error(f"Videos must be under {max_video_bytes() // 1048576} MB.")
    q_exec("UPDATE docuseries_videos SET status = 'ready', size_bytes = %s, completed_at = now() "
           "WHERE id = %s", (size, row["id"]))
    return jsonify(ok=True)


@docuseries_bp.post("/api/location/<int:loc_id>/status")
@login_required_ds
@participant_required
def api_location_status(loc_id):
    """Participant sets (or clears) their status for a live recording."""
    status = str((request.get_json(silent=True) or {}).get("status") or "").strip()
    if status and status not in LOCATION_STATUSES:
        return _json_error("That status isn't valid.")
    if not q_one("SELECT id FROM docuseries_live_locations WHERE id = %s", (loc_id,)):
        return _json_error("That live recording no longer exists.", 404)
    uid = current_user()["id"]
    if status:
        q_exec("INSERT INTO docuseries_location_status (location_id, user_id, status) VALUES (%s, %s, %s) "
               "ON CONFLICT (location_id, user_id) DO UPDATE SET status = EXCLUDED.status, updated_at = now()",
               (loc_id, uid, status))
    else:   # "Select one" = no status
        q_exec("DELETE FROM docuseries_location_status WHERE location_id = %s AND user_id = %s", (loc_id, uid))
    return jsonify(ok=True, status=status, label=LOCATION_STATUSES.get(status, ""))


def _main_app_url(endpoint):
    """URL of one of the main app's own endpoints (flyers, invite form), or '' if it does not exist."""
    try:
        return url_for(endpoint)
    except Exception:
        return ""


def _business_share_url(user):
    """The participant's business invite link, built the same way as the dashboard's business_share_url."""
    code = (user.get("ref_code") or "").strip()
    try:
        return url_for("business_register", ref=code, _external=True)
    except Exception:
        # Fallback only if the endpoint is missing; keep BUSINESS_SHARE_URL_TEMPLATE pointing at the same page.
        return BUSINESS_SHARE_URL_TEMPLATE.format(code=quote(code, safe=""), base=request.host_url.rstrip("/"))


@docuseries_bp.route("/how_to_invite_businesses")
@login_required_ds
@participant_required
def how_to_invite_businesses():
    """Participant-only playbook, flyer download and copy-and-send scripts (all include the member's own link)."""
    user = current_user()
    return render_template(
        "docuseries/how_to_invite_businesses.html", user=user,
        business_share_url=_business_share_url(user),
        promo_code=BUSINESS_PROMO_CODE,
        flyer_url=_main_app_url("member_business_flyer"),
        flyer_download_url=_main_app_url("download_member_business_flyer"),
        invite_post_url=_main_app_url("invite"))

# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@docuseries_bp.route("/admin")
@login_required_ds
@admin_required
def admin():
    people = q_all(
        f"SELECT {USER_ID_COL} AS id, {USER_NAME_SQL} AS full_name, {USER_EMAIL_COL} AS email, "
        f"release_form, docuseries_terms_at FROM {USER_TABLE} WHERE docuseries_terms IS TRUE "
        f"ORDER BY docuseries_terms_at DESC NULLS LAST, {USER_ID_COL}")
    vids = {}
    total_videos = 0
    for v in q_all("SELECT id, user_id, video_number, size_bytes, duration_seconds "
                   "FROM docuseries_videos WHERE status = 'ready'"):
        vids.setdefault(v["user_id"], {})[v["video_number"]] = v
        total_videos += 1
    locations = q_all("SELECT id, title, location_name, address, starts_at, notes "
                      "FROM docuseries_live_locations WHERE starts_at >= now() - interval '1 day' "
                      "ORDER BY starts_at")
    return render_template("docuseries/admin.html", people=people, vids=vids, slots=range(1, 18),
                           locations=locations, total_videos=total_videos,
                           admin_roles_url=ADMIN_ROLES_URL, storage=storage_mode(),
                           challenge=challenge_info(),
                           milestone_error=compute_milestones(current_user()["id"])[1],
                           storage_ok=storage_ready())


@docuseries_bp.route("/admin/download/video/<int:video_id>")
@login_required_ds
@admin_required
def admin_download_video(video_id):
    v = q_one("SELECT v.storage_key, v.video_number, v.user_id FROM docuseries_videos v "
              "WHERE v.id = %s AND v.status = 'ready'", (video_id,))
    if not v:
        abort(404)
    ext = Path(v["storage_key"]).suffix
    return private_response(v["storage_key"],
                            download_name=f"user{v['user_id']}_video{v['video_number']:02d}{ext}")


@docuseries_bp.route("/admin/download/release/<int:user_id>")
@login_required_ds
@admin_required
def admin_download_release(user_id):
    row = q_one(f"SELECT release_form FROM {USER_TABLE} WHERE {USER_ID_COL} = %s", (user_id,))
    if not row or not row["release_form"]:
        abort(404)
    ext = Path(row["release_form"]).suffix
    # ?view=1 opens the PDF/image in the browser; without it the file downloads
    return private_response(row["release_form"], download_name=f"user{user_id}_talent_release{ext}",
                            inline=request.args.get("view") == "1")


@docuseries_bp.post("/admin/challenge")
@login_required_ds
@admin_required
def admin_set_challenge():
    raw = (request.form.get("start_date") or "").strip()
    if raw:
        try:
            date.fromisoformat(raw)
        except ValueError:
            flash("That isn't a valid date.", "error")
            return redirect(url_for("docuseries.admin"))
    q_exec("INSERT INTO docuseries_settings (key, value) VALUES ('challenge_start_date', %s) "
           "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (raw,))
    flash("Challenge start date saved." if raw else "Challenge start date cleared (shows as 'To be announced').",
          "success")
    return redirect(url_for("docuseries.admin"))


@docuseries_bp.route("/live_locations")
@login_required_ds
@admin_required
def live_locations():
    rows = q_all("SELECT id, title, location_name, address, starts_at, ends_at, notes, directions, "
                 "place_id, latitude, longitude FROM docuseries_live_locations "
                 "ORDER BY starts_at DESC LIMIT 100")
    people = {}
    for r in q_all(f"SELECT s.location_id, s.status, s.updated_at, {USER_NAME_SQL} AS full_name "
                   f"FROM docuseries_location_status s JOIN {USER_TABLE} u ON u.{USER_ID_COL} = s.user_id"):
        people.setdefault(r["location_id"], []).append(r)
    order = {st: i for i, st in enumerate(ADMIN_LISTED_STATUSES)}
    for row in rows:
        _decorate_location(row)
        mine = people.get(row["id"], [])
        row["counts"] = {st: sum(1 for m in mine if m["status"] == st) for st in LOCATION_STATUSES}
        row["members"] = sorted((m for m in mine if m["status"] in order),
                                key=lambda m: (order[m["status"]], (m.get("full_name") or "").lower()))
    edit_id = request.args.get("edit", type=int)
    edit = next((r for r in rows if r["id"] == edit_id), None) if edit_id else None
    return render_template("docuseries/live_locations.html", locations=rows, edit=edit,
                           maps_key=maps_api_key(), maps_bias=MAPS_BIAS, statuses=LOCATION_STATUSES)


@docuseries_bp.post("/live_locations/save")
@login_required_ds
@admin_required
def live_location_save():
    f = request.form
    loc_id = f.get("id", type=int)
    title, place = (f.get("title") or "").strip(), (f.get("location_name") or "").strip()
    starts = _parse_local(f.get("starts_at"))
    ends = _parse_local(f.get("ends_at")) if (f.get("ends_at") or "").strip() else None
    back = url_for("docuseries.live_locations", edit=loc_id) if loc_id else url_for("docuseries.live_locations")
    if not title or not place or not starts:
        flash("Please enter a title, a location name and a date and time.", "error")
        return redirect(back)
    if ends and ends <= starts:
        flash("The end time must be after the start time.", "error")
        return redirect(back)
    place_id = (f.get("place_id") or "").strip()
    place_id = place_id if re.fullmatch(r"[A-Za-z0-9_-]{10,300}", place_id) else None
    lat = lng = None
    try:
        lat, lng = float(f.get("latitude", "")), float(f.get("longitude", ""))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            lat = lng = None
    except ValueError:
        lat = lng = None
    vals = (title, place, (f.get("address") or "").strip() or None, starts, ends,
            (f.get("notes") or "").strip() or None, (f.get("directions") or "").strip() or None,
            place_id, lat, lng)
    if loc_id:
        q_exec("UPDATE docuseries_live_locations SET title=%s, location_name=%s, address=%s, starts_at=%s, "
               "ends_at=%s, notes=%s, directions=%s, place_id=%s, latitude=%s, longitude=%s WHERE id=%s",
               vals + (loc_id,))
        flash("Live recording updated.", "success")
    else:
        q_exec("INSERT INTO docuseries_live_locations (title, location_name, address, starts_at, ends_at, "
               "notes, directions, place_id, latitude, longitude) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", vals)
        flash("Live recording added. Participants can now see it on their dashboard.", "success")
    return redirect(url_for("docuseries.live_locations"))


@docuseries_bp.post("/live_locations/<int:loc_id>/delete")
@login_required_ds
@admin_required
def live_location_delete(loc_id):
    q_exec("DELETE FROM docuseries_live_locations WHERE id = %s", (loc_id,))
    flash("Live recording deleted.", "success")
    return redirect(url_for("docuseries.live_locations"))


# ---------------------------------------------------------------------------
# Admin: episodes (upload final videos, name the participants)
# ---------------------------------------------------------------------------
EP_KEY_RE = re.compile(r"^episodes/[0-9a-f]{32}\.(mp4|mov|webm|m4v|3gp)$")
MAX_PARTICIPANT_NAMES = 500


def max_episode_bytes():
    """Size limit for an uploaded episode file. Cloudinary's free plan caps videos at 100 MB, which is far
    too small for 20-minute episodes: use a YouTube link or R2 (or a paid Cloudinary plan)."""
    env = os.environ.get("DOCUSERIES_MAX_EPISODE_MB")
    if env:
        return int(env) * 1024 * 1024
    return {"cloudinary": 100, "r2": 5000}.get(storage_mode(), 2000) * 1024 * 1024


def _parse_names(text):
    """One name per line -> cleaned, de-duplicated list (order kept). Returns (names, error)."""
    names, seen = [], set()
    for line in (text or "").splitlines():
        n = " ".join(line.split())
        if not n or n.lower() in seen:
            continue
        if len(n) > 100:
            return None, f"A name is longer than 100 characters: {n[:30]}..."
        seen.add(n.lower())
        names.append(n)
    if len(names) > MAX_PARTICIPANT_NAMES:
        return None, f"Please list no more than {MAX_PARTICIPANT_NAMES} names."
    return names, None


def _episode_rows():
    eps = q_all("SELECT id, episode_number, title, description, playback_type, playback_ref, poster_url, "
                "air_date, duration_minutes, published FROM docuseries_episodes ORDER BY episode_number")
    names = {}
    for r in q_all("SELECT episode_id, display_name FROM docuseries_episode_participants ORDER BY id"):
        names.setdefault(r["episode_id"], []).append(r["display_name"])
    for e in eps:
        e["participants"] = names.get(e["id"], [])
    return eps


@docuseries_bp.route("/admin/episodes")
@login_required_ds
@admin_required
def admin_episodes():
    eps = _episode_rows()
    edit_id = request.args.get("edit", type=int)
    edit = next((e for e in eps if e["id"] == edit_id), None) if edit_id else None
    pool = [r["full_name"] for r in q_all(
        f"SELECT {USER_NAME_SQL} AS full_name FROM {USER_TABLE} WHERE docuseries_terms IS TRUE "
        f"ORDER BY {USER_NAME_SQL}") if r.get("full_name")]
    next_number = (max([e["episode_number"] for e in eps]) + 1) if eps else 1
    return render_template("docuseries/admin_episodes.html", episodes=eps, edit=edit, pool=pool,
                           next_number=next_number, storage=storage_mode(), storage_ok=storage_ready(),
                           max_episode_mb=max_episode_bytes() // 1048576)


@docuseries_bp.post("/admin/episodes/save")
@login_required_ds
@admin_required
def admin_episode_save():
    f = request.form
    ep_id = f.get("id", type=int)
    back = url_for("docuseries.admin_episodes", edit=ep_id) if ep_id else url_for("docuseries.admin_episodes")

    def fail(msg):
        flash(msg, "error")
        return redirect(back)

    number = f.get("episode_number", type=int)
    title = (f.get("title") or "").strip()
    if not number or number < 1:
        return fail("Please enter the episode number.")
    if not title:
        return fail("Please enter a title.")
    names, err = _parse_names(f.get("participants"))
    if err:
        return fail(err)
    air = None
    if (f.get("air_date") or "").strip():
        try:
            air = date.fromisoformat(f["air_date"].strip())
        except ValueError:
            return fail("That air date isn't valid.")
    minutes = f.get("duration_minutes", type=int)
    poster = (f.get("poster_url") or "").strip() or None
    if poster and not poster.lower().startswith("https://"):
        return fail("The thumbnail link must start with https://")
    clash = (q_one("SELECT id FROM docuseries_episodes WHERE episode_number = %s AND id <> %s", (number, ep_id))
             if ep_id else q_one("SELECT id FROM docuseries_episodes WHERE episode_number = %s", (number,)))
    if clash:
        return fail(f"Episode {number} already exists. Pick a different number or edit that episode.")

    existing = (q_one("SELECT playback_type, playback_ref FROM docuseries_episodes WHERE id = %s", (ep_id,))
                if ep_id else None)
    source = f.get("source")
    if source == "youtube":
        ptype, ref = "youtube", parse_youtube_id(f.get("youtube"))
        if not ref:
            return fail("That doesn't look like a YouTube link. Paste the full link from your browser.")
    elif source == "file":
        ref = (f.get("file_key") or "").strip() or (existing["playback_ref"] if existing and
                                                    existing["playback_type"] == "file" else "")
        if not EP_KEY_RE.match(ref or ""):
            return fail("Please upload the episode video first (wait for it to finish).")
        if private_size(ref) is None:
            return fail("We couldn't find the uploaded video. Please upload it again.")
        ptype = "file"
    elif source == "keep" and existing:
        ptype, ref = existing["playback_type"], existing["playback_ref"]
    else:
        return fail("Choose a video source: a YouTube link or an uploaded file.")

    vals = (number, title, (f.get("description") or "").strip() or None, ptype, ref, poster, air, minutes,
            bool(f.get("published")))
    if ep_id:
        if not existing:
            return fail("That episode no longer exists.")
        q_exec("UPDATE docuseries_episodes SET episode_number=%s, title=%s, description=%s, playback_type=%s, "
               "playback_ref=%s, poster_url=%s, air_date=%s, duration_minutes=%s, published=%s WHERE id=%s",
               vals + (ep_id,))
    else:
        row = q_exec("INSERT INTO docuseries_episodes (episode_number, title, description, playback_type, "
                     "playback_ref, poster_url, air_date, duration_minutes, published) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id", vals)
        ep_id = row["id"]
    stmts = [("DELETE FROM docuseries_episode_participants WHERE episode_id = %s", (ep_id,))]
    stmts += [("INSERT INTO docuseries_episode_participants (episode_id, display_name) VALUES (%s, %s)",
               (ep_id, n)) for n in names]
    q_tx(stmts)
    if existing and existing["playback_type"] == "file" and existing["playback_ref"] != ref:
        delete_private(existing["playback_ref"])      # the video was replaced: remove the old file
    flash(f"Episode {number} saved" + (" and published." if vals[-1] else " as a draft (not public yet)."),
          "success")
    return redirect(url_for("docuseries.admin_episodes"))


@docuseries_bp.post("/admin/episodes/<int:ep_id>/delete")
@login_required_ds
@admin_required
def admin_episode_delete(ep_id):
    row = q_one("SELECT playback_type, playback_ref FROM docuseries_episodes WHERE id = %s", (ep_id,))
    if row:
        q_exec("DELETE FROM docuseries_episodes WHERE id = %s", (ep_id,))   # names are removed with it
        if row["playback_type"] == "file":
            delete_private(row["playback_ref"])
    flash("Episode deleted.", "success")
    return redirect(url_for("docuseries.admin_episodes"))


# ---- episode file upload (admin only) -------------------------------------------------------
def _cld_signed_upload(key):
    import cloudinary
    import cloudinary.utils
    pid, _, _ = _cld_target(key)
    cfg = cloudinary.config()
    signed = {"public_id": pid, "timestamp": int(time.time()), "type": "authenticated"}
    fields = dict(signed, api_key=cfg.api_key,
                  signature=cloudinary.utils.api_sign_request(signed, cfg.api_secret))
    return f"https://api.cloudinary.com/v1_1/{cfg.cloud_name}/video/upload", fields


@docuseries_bp.post("/admin/api/episode-upload/init")
@login_required_ds
@admin_required
def api_episode_upload_init():
    if not storage_ready():
        return _json_error("File storage isn't configured yet (see the storage notice on the admin page).", 503)
    data = request.get_json(silent=True) or {}
    ext = os.path.splitext(str(data.get("filename") or ""))[1].lower()
    ctype = (data.get("content_type") or "").lower()
    if ctype not in ALLOWED_VIDEO_TYPES:
        ctype = EXT_TO_VIDEO_TYPE.get(ext, "")
    if not ctype:
        return _json_error("Please upload an MP4, MOV or WebM video.")
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0 or size > max_episode_bytes():
        return _json_error(f"The video must be under {max_episode_bytes() // 1048576} MB for your current "
                           f"storage. For long episodes, paste a YouTube link instead.")
    key = f"episodes/{uuid.uuid4().hex}{ALLOWED_VIDEO_TYPES[ctype]}"
    mode = storage_mode()
    if mode == "cloudinary":
        url, fields = _cld_signed_upload(key)
        return jsonify(mode="cloudinary", key=key, upload_url=url, fields=fields)
    if mode == "r2":
        return jsonify(mode="direct", key=key, content_type=ctype, upload_url=presigned_put(key, ctype))
    return jsonify(mode="server", key=key,
                   upload_url=url_for("docuseries.api_episode_upload_local", key=key))


@docuseries_bp.post("/admin/api/episode-upload/local")
@login_required_ds
@admin_required
def api_episode_upload_local():
    """Server-side upload for local disk storage (development / persistent disk)."""
    if storage_mode() != "local" or not storage_ready():
        abort(404)
    key = request.args.get("key", "")
    upload = request.files.get("file")
    if not EP_KEY_RE.match(key) or not upload:
        return _json_error("Invalid upload.")
    save_private(upload.stream, key, EXT_TO_VIDEO_TYPE.get(os.path.splitext(key)[1], "video/mp4"))
    return jsonify(ok=True)


@docuseries_bp.post("/admin/api/episode-upload/complete")
@login_required_ds
@admin_required
def api_episode_upload_complete():
    key = str((request.get_json(silent=True) or {}).get("key") or "")
    if not EP_KEY_RE.match(key):
        return _json_error("Invalid upload.")
    duration = None
    if storage_mode() == "cloudinary":
        info = _cld_resource(key)
        size = int(info.get("bytes") or 0) if info else None
        if info:
            duration = info.get("duration")
            fmt = (info.get("format") or "").lower()
            if fmt and os.path.splitext(key)[1].lstrip(".") != fmt and fmt in ("mp4", "mov", "webm", "m4v", "3gp"):
                key = key.rsplit(".", 1)[0] + "." + fmt
    else:
        size = private_size(key)
    if size is None:
        return _json_error("We couldn't find the uploaded file. Please try again.")
    if size > max_episode_bytes():
        delete_private(key)
        return _json_error(f"The video must be under {max_episode_bytes() // 1048576} MB.")
    return jsonify(ok=True, key=key, size=size,
                   duration_minutes=(max(1, round(float(duration) / 60)) if duration else None))
