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
import uuid
from contextlib import contextmanager
from datetime import date, datetime
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
USER_NAME_SQL = "name"
USER_REF_COL = "ref_code"             # column that holds values like 'refJoeJMendez'
ADMIN_REF_VALUE = "refJoeJMendez"     # owner account: can always open /docuseries/admin
ALLOW_ROLE_ON_ADMIN = True            # True = any user with the docuseries role (docuseries_role = TRUE) can also open it

SESSION_USER_KEY = "user_id"          # session key your login sets
LOGIN_URL = "/login"
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
MAX_VIDEO_BYTES = int(os.environ.get("DOCUSERIES_MAX_VIDEO_MB", "300")) * 1024 * 1024
DURATION_TOLERANCE = 2.0              # seconds of slack on the 30/60 second limits

# Video slots. #1 = intro, #2 = registration experience, #3-#17 = experience videos.
VIDEO_SLOTS = {
    1: {"label": "Intro Video", "max_seconds": 60},
    2: {"label": "Pre-Registration / Post-Registration Video", "max_seconds": 30},
}
for _n in range(3, 18):
    VIDEO_SLOTS[_n] = {"label": f"Experience Video #{_n - 2}", "max_seconds": 60}

# Slots participants may upload to right now. Add 3..17 when the challenge starts
# (e.g. OPEN_SLOTS = set(range(1, 18))).
OPEN_SLOTS = {1, 2}

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


def get_setting(key, default=""):
    row = q_one("SELECT value FROM docuseries_settings WHERE key = %s", (key,))
    return (row or {}).get("value") or default


# ---------------------------------------------------------------------------
# Current user / permissions
# ---------------------------------------------------------------------------
def _load_user(uid):
    return q_one(
        f"SELECT {USER_ID_COL} AS id, {USER_EMAIL_COL} AS email, {USER_NAME_SQL} AS full_name, "
        f"{USER_REF_COL} AS ref_code, release_form, docuseries_terms, docuseries_welcomed, "
        f"docuseries_role FROM {USER_TABLE} WHERE {USER_ID_COL} = %s", (uid,))


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
def storage_mode():
    needed = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    return "r2" if all(os.environ.get(k) for k in needed) else "local"


def _s3():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
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


def save_private(stream, key, content_type):
    if storage_mode() == "r2":
        _s3().upload_fileobj(stream, _bucket(), key, ExtraArgs={"ContentType": content_type})
    else:
        path = _local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as out:
            shutil.copyfileobj(stream, out, length=1024 * 1024)


def private_size(key):
    """Size in bytes, or None if the object doesn't exist."""
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
        if storage_mode() == "r2":
            _s3().delete_object(Bucket=_bucket(), Key=key)
        else:
            _local_path(key).unlink(missing_ok=True)
    except Exception:
        current_app.logger.exception("docuseries: delete failed for %s", key)


def presigned_put(key, content_type, expires=900):
    return _s3().generate_presigned_url(
        "put_object", Params={"Bucket": _bucket(), "Key": key, "ContentType": content_type},
        ExpiresIn=expires)


def private_response(key, download_name=None, inline=False, expires=300):
    """Return a Flask response that delivers a private file."""
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
                     "ORDER BY display_name"):
        names.setdefault(row["episode_id"], []).append(row["display_name"])
    for ep in eps:
        ep["participants"] = names.get(ep["id"], [])
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
    return private_response(ep["playback_ref"], inline=True, expires=3600)


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
    slots = [{"number": n, **VIDEO_SLOTS[n], "video": uploaded.get(n)} for n in sorted(OPEN_SLOTS)]
    start = get_setting("challenge_start_date")
    try:
        start_date = date.fromisoformat(start) if start else None
    except ValueError:
        start_date = None
    return render_template("docuseries/member.html", user=user, first_time=first_time, slots=slots,
                           start_date=start_date, max_video_mb=MAX_VIDEO_BYTES // 1048576)


def _json_error(msg, status=400):
    return jsonify(error=msg), status


@docuseries_bp.post("/api/video/init")
@login_required_ds
@participant_required
def api_video_init():
    data = request.get_json(silent=True) or {}
    try:
        number = int(data.get("video_number", 0))
    except (TypeError, ValueError):
        number = 0
    if number not in OPEN_SLOTS:
        return _json_error("That video slot isn't open yet.")
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
    if size <= 0 or size > MAX_VIDEO_BYTES:
        return _json_error(f"Videos must be under {MAX_VIDEO_BYTES // 1048576} MB.")
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
    if request.content_length and request.content_length > MAX_VIDEO_BYTES + 2 * 1024 * 1024:
        return _json_error("That file is too large.", 413)
    save_private(upload.stream, row["storage_key"], row["content_type"])
    size = private_size(row["storage_key"])
    if size is None or size > MAX_VIDEO_BYTES:
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
    size = private_size(row["storage_key"])
    if size is None:
        return _json_error("We couldn't find your upload. Please try again.")
    if size > MAX_VIDEO_BYTES:
        _fail_video(row)
        return _json_error(f"Videos must be under {MAX_VIDEO_BYTES // 1048576} MB.")
    q_exec("UPDATE docuseries_videos SET status = 'ready', size_bytes = %s, completed_at = now() "
           "WHERE id = %s", (size, row["id"]))
    return jsonify(ok=True)


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
                           admin_roles_url=ADMIN_ROLES_URL)


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
    return private_response(row["release_form"], download_name=f"user{user_id}_talent_release{ext}")


@docuseries_bp.route("/live_locations")
@login_required_ds
@admin_required
def live_locations():
    rows = q_all("SELECT id, title, location_name, address, starts_at, notes "
                 "FROM docuseries_live_locations ORDER BY starts_at DESC LIMIT 50")
    return render_template("docuseries/live_locations.html", locations=rows)
