from flask import (
    Flask, request, redirect, url_for, render_template, flash, session, abort,
    jsonify, send_from_directory, Response, send_file
)

from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message as MailMessage
from flask_bcrypt import Bcrypt
from flask_login import (
    LoginManager, UserMixin, login_user, login_required, logout_user, current_user
)
from flask_wtf import FlaskForm, CSRFProtect, RecaptchaField
from wtforms import (
    StringField, PasswordField, SubmitField, DecimalField, SelectField, FileField,
    TextAreaField, Form
)
from wtforms.validators import (
    DataRequired, Email, Length, EqualTo, Optional, NumberRange
)
from werkzeug.utils import secure_filename
from sqlalchemy import func, case
from decimal import Decimal, ROUND_HALF_UP
from flask import flash, redirect, url_for
from functools import wraps
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from flask import render_template_string
from sqlalchemy.orm import joinedload
from flask import current_app
from itsdangerous import URLSafeTimedSerializer
from itsdangerous import URLSafeSerializer

import os
import stripe
import logging
from datetime import datetime, date
import json
from sqlalchemy import or_, and_, func, literal
from flask_migrate import Migrate
import re
import random
import string
import time
import csv
import uuid
import hmac
import hashlib
from io import StringIO, BytesIO
import cloudinary
import cloudinary.uploader
import qrcode
import base64
import secrets

# --- Cart Logic ---
def get_cart():
    return session.get("cart", {})

def get_valid_coupons():
    # TODO: implement real coupon logic (db query, etc.)
    return {'TEST10': 0.10, 'WELCOME20': 0.20}  # example dict {code: discount_pct}

def save_cart(cart):
    session["cart"] = cart
    session.modified = True

def add_to_cart(product_id, quantity=1):
    cart = get_cart()
    pid = str(product_id)
    cart[pid] = cart.get(pid, 0) + quantity
    save_cart(cart)

def remove_from_cart(product_id):
    cart = get_cart()
    pid = str(product_id)
    if pid in cart:
        del cart[pid]
        save_cart(cart)

def send_order_alert(business_email, product_name, amount, buyer_email=None):
    msg = MailMessage(
        subject="New Online Order Received!",
        recipients=[business_email],
        html=(
            f"<h3>You've received a new order!</h3>"
            f"<p><b>Product:</b> {product_name}<br>"
            f"<b>Amount:</b> ${amount:.2f}<br>"
            + (f"<b>Buyer Email:</b> {buyer_email}<br>" if buyer_email else "") +
            f"</p><hr><p>Login to your dashboard for details.</p>"
        ),
        sender="orders@perkminer.com"
    )
    try:
        mail.send(msg)
    except Exception as e:
        import logging
        logging.error("Order email failed: %s", e)

def send_customer_receipt(buyer_email, product_name, amount, business_name, download_url=None):
    html = (
        f"<h3>Order Confirmation from {business_name}</h3>"
        f"<p>Thank you for your order!</p>"
        f"<b>Product:</b> {product_name}<br>"
        f"<b>Amount Paid:</b> ${amount:.2f}<br>"
    )
    if download_url:
        html += f'<p>Your download is ready: <a href="{download_url}">Download here</a></p>'
    html += "<p>If you have any questions, please contact the business directly.</p>"
    msg = MailMessage(
        subject=f"Your Order Receipt – {business_name}",
        recipients=[buyer_email],
        html=html,
        sender="orders@perkminer.com"
    )
    try:
        mail.send(msg)
    except Exception as e:
        logging.error("Customer receipt email failed: %s", e)

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not any(r.name == 'super_admin' for r in current_user.roles):
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

def role_required(role_name):
    def decorator(f):
        @wraps(f)
        @login_required
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated or not current_user.has_role(role_name):
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def get_interaction_for_business_and_user(business_id, user_id):
    return Interaction.query.filter_by(
        business_id=business_id,
        user_id=user_id,
        status="active"
    ).first()

def business_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('business_id'):
            flash('Please log in as a business to access this page.', 'warning')
            return redirect(url_for('business_login', next=request.path))
        return f(*args, **kwargs)
    return decorated_function

cloudinary.config(
  cloud_name = 'dmrntlcfd',
  api_key = '786387955898581',
  api_secret = 'cLtDoC44BarYjVrr3dIgi_0XiKo'
)

app = Flask(__name__)

# ────────────────────────────────────────────────
# Flask SECRET_KEY – must come from env var (no hardcoded fallback!)
# ────────────────────────────────────────────────
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    import logging
    logging.critical(
        "CRITICAL: SECRET_KEY is missing in environment variables! "
        "This is a MAJOR security risk – sessions/CSRF are insecure. "
        "Set a strong SECRET_KEY in Render → Environment immediately. "
        "Generate one: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
    # Optional: fail fast in local dev (comment out in prod if you want app to limp along)
    # if os.environ.get('FLASK_ENV') == 'development':
    #     raise ValueError("SECRET_KEY not set in environment!")

app.config['SECRET_KEY'] = SECRET_KEY
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', "sqlite:///site.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 465))
app.config['MAIL_USE_SSL'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
app.config['RECAPTCHA_PUBLIC_KEY'] = '6LdhAV8sAAAAABwITf0HytcbADISlcMd87NP-i2H'
app.config['RECAPTCHA_PRIVATE_KEY'] = '6LdhAV8sAAAAAFi9YjxnZqFLUl3SlQjHc1g7IEOq'

SHOP_SECRET = os.environ.get("SHOP_SECRET", app.config['SECRET_KEY'])
shop_serializer = URLSafeSerializer(SHOP_SECRET, salt="shop-redirect")

UPLOAD_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

csrf = CSRFProtect(app)
db = SQLAlchemy(app)
mail = Mail(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
migrate = Migrate(app, db)
serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
stripe.api_key = os.environ.get("STRIPE_API_KEY")

def find_top_business_with_user_sponsor(biz, max_depth=50):
    visited_ids = set()
    depth = 0
    while biz and biz.sponsor_id and depth < max_depth:
        if biz.id in visited_ids:
            # Detected a cycle (business sponsoring itself or a circular chain)
            break
        visited_ids.add(biz.id)
        parent = db.session.get(Business, biz.sponsor_id)
        if not parent:
            break
        biz = parent
        depth += 1
    return biz

# ────────────────────────────────────────────────
# Check Stripe key early – prevent silent failures
# ────────────────────────────────────────────────
if not stripe.api_key:
    import logging
    logging.error(
        "CRITICAL: STRIPE_API_KEY is missing or empty in environment variables! "
        "Stripe payments will fail. Set it in Render dashboard → Environment."
    )
    # Optional: in local dev, raise to fail fast (comment out in prod if preferred)
    # if os.environ.get('FLASK_ENV') == 'development':
    #     raise ValueError("STRIPE_API_KEY not set in environment!")

STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')

# ────────────────────────────────────────────────
# Check Stripe webhook secret early – prevent silent signature failures
# ────────────────────────────────────────────────
if not STRIPE_WEBHOOK_SECRET:
    import logging
    logging.error(
        "CRITICAL: STRIPE_WEBHOOK_SECRET is missing or empty in environment variables! "
        "Stripe webhook signature verification will fail. "
        "Set it in Render dashboard → Environment. "
        "You can generate one in Stripe Dashboard → Developers → Webhooks → Add endpoint → Signing secret."
    )
    # Optional: fail fast in local dev (comment out in prod if preferred)
    # if os.environ.get('FLASK_ENV') == 'development':
    #     raise ValueError("STRIPE_WEBHOOK_SECRET not set in environment!")

YOUR_DOMAIN = os.environ.get('YOUR_DOMAIN', 'https://perkminer.com').rstrip('/')

if not os.environ.get('YOUR_DOMAIN'):
    import logging
    logging.warning(
        "YOUR_DOMAIN not set in environment variables — falling back to production domain: %s. "
        "Set YOUR_DOMAIN in Render → Environment for preview/staging branches.",
        YOUR_DOMAIN
    )

# ────────────────────────────────────────────────
# Better global logging setup – nicer format + conditional level
# ────────────────────────────────────────────────
import logging
import sys

# Set level: DEBUG in local dev, INFO in production
log_level = logging.DEBUG if os.environ.get('FLASK_ENV') == 'development' else logging.INFO

# Create a formatter with timestamp, level, logger name, message
formatter = logging.Formatter(
    '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Console handler (Render captures stdout, so this is sufficient)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)

# Root logger configuration
root_logger = logging.getLogger()
root_logger.setLevel(log_level)
root_logger.addHandler(console_handler)

# Optional: reduce noise from some libraries (e.g. werkzeug, sqlalchemy)
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('sqlalchemy').setLevel(logging.WARNING)

# Test log to confirm setup
logging.info("Logging initialized – level: %s", logging.getLevelName(log_level))

SESSION_EMAIL_RESEND_KEY = "last_resend_email_time"
MIN_PASSWORD_LENGTH = 8

# ----------------- WTForms (All Your Forms) -------------------
class ServiceRequestForm(FlaskForm):
    service_type = SelectField(
        "Type of Service Requested",
        choices=[
            ("-", "Select One"),
            ("administative", "Administrative Services"),
            ("advertising", "Advertising Services"),
            ("agricultural", "Agricultural Services"),
            ("art", "Art Services"),
            ("assisted", "Assisted Living Services"),
            ("ATV", "ATV Services"),
            ("automotive", "Automotive Services"),
            ("bar", "Bar Services"),
            ("boating", "Boating Services"),
            ("bookkeeping", "Bookkeeping Services"),
            ("brewery", "Brewery Services"),
            ("butler", "Butler Services"),
            ("callcenter", "Call Center Services"),
            ("catering", "Catering Services"),
            ("cellphone", "Cellphone Services"),
            ("childcare", "Childcare services"),
            ("cleaning", "Cleaning Services"),
            ("club", "Club Services"),
            ("computer", "Computer Services"),
            ("concierge", "Concierge Services"),
            ("consulting", "Consulting Services"),
            ("construction", "Construction Services"),
            ("contractor", "Contractor Services"),
            ("delivery", "Delivery Services"),
            ("design", "Design Services"),
            ("drink", "Drink Services"),
            ("education", "Education Services"),
            ("electronics", "Electronics Services"),
            ("engineering", "Engineering Services"),
            ("equipment", "Equipment Services"),
            ("entertainment", "Entertainment Services"),
            ("event", "Event Services"),
            ("farm", "Farm Services"),
            ("film", "Film Services"),
            ("financial", "Financial Services"),
            ("fishing", "Fishing Services"),
            ("food", "Food Services"),
            ("gaming", "Gaming Services"),
            ("gift", "Gift Services"),
            ("gun", "Guns and Ammo Services"),
            ("handyman", "Handyman Services"),
            ("health", "Health Services"),
            ("healthcare", "Health Care Services"),
            ("homedecor", "Home Decor Services"),
            ("horticulture", "Horticulture Services"),
            ("hunting", "Hunting Services"),
            ("industrial", "Industrial Services"),
            ("insurance", "Insurance Services"),
            ("investigative", "Investigative Services"),
            ("job", "Job Services"),
            ("labor", "Labor Services"),
            ("lawn", "Lawn Care"),
            ("landscaping", "Landscaping Services"),
            ("legal", "Legal Services"),
            ("lessons", "Lessons"),
            ("maintenance", "Maintenance Services"),
            ("marketing", "Marketing Services"),
            ("mortgage", "Mortgage Services"),
            ("motorcycle", "Motorcycle Services"),
            ("moving", "Moving Services"),
            ("music", "Music Services"),
            ("organization", "Organization"),
            ("other", "Other Services"),
            ("parking", "Parking Services"),
            ("party", "Party Services"),
            ("performing", "Performing Arts"),
            ("personal", "Personal Services"),
            ("pest", "Pest Control Services"),
            ("pet", "Pet Services"),
            ("phone", "Phone Services"),
            ("photography", "Photography Services"),
            ("planning", "Planning Services"),
            ("public", "Public Relations Services"),
            ("printing", "Printing Services"),
            ("professional", "Professional Services"),
            ("property", "Property Management Services"),
            ("realestate", "Real Estate Services"),
            ("rentalcar", "Rental Car Services"),
            ("rentalequipment", "Rental Equipment Services"),
            ("recreation", "Recreation Services"),
            ("research", "Research Services"),
            ("restaurant", "Restaurant Services"),
            ("restoration", "Restoration Services"),
            ("retirement", "Retirement Services"),
            ("rv", "RV Services"),
            ("secretarial", "Secretarial Services"),
            ("security", "Security-Protection Services"),
            ("software", "Software Services"),
            ("sports", "Sports Services"),
            ("storage", "Storage Services"),
            ("tax", "Tax Services"),
            ("tech", "Tech Support Services"),
            ("temp", "Temp Services"),
            ("translation", "Translation Services"),
            ("transportation", "Transportation Services"),
            ("training", "Training Services"),
            ("travel", "Travel Services"),
            ("tree", "Tree Services"),
            ("utilities", "Utilities Services"),
            ("video", "Video Services"),
            ("wastemanagement", "Waste Management Services"),
            ("wedding", "Wedding Services"),
            ("welding", "Welding Services"),
            ("wellness", "Wellness Services"),
            # Add more as needed here
        ],
        validators=[DataRequired()]
    )
    details = TextAreaField("Service Details", validators=[DataRequired(), Length(max=1000)])
    budget_low = DecimalField("Budget (Low End)", validators=[NumberRange(min=0)], default=0)
    budget_high = DecimalField("Budget (High End)", validators=[NumberRange(min=0)], default=0)
    submit = SubmitField("Submit Request")

class TwoFactorForm(FlaskForm):
    code = StringField('Enter the 6-digit code', validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField('Verify')

class EditUserForm(FlaskForm):
    name = StringField('Name', validators=[Optional()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    submit = SubmitField('Save')

class BusinessEditForm(FlaskForm):
    business_name = StringField('Business Name', validators=[Optional()])
    business_email = StringField('Business Email', validators=[DataRequired(), Email()])
    category = StringField('Category', validators=[Optional()])
    finalization = StringField('Finalization', validators=[Optional()])
    phone_number = StringField('Phone Number', validators=[Optional()])
    address = StringField('Address', validators=[Optional()])
    submit = SubmitField('Save')

class InviteForm(FlaskForm):
    invitee_email = StringField('Invitee Email', validators=[DataRequired(), Email()])
    submit = SubmitField('Send Invitation')

class BusinessInviteForm(FlaskForm):
    invitee_email = StringField('Invitee Email', validators=[DataRequired(), Email()])
    submit = SubmitField('Send Invitation')

class VerifyCodeForm(FlaskForm):
    code = StringField('Code', validators=[DataRequired()])
    submit = SubmitField('Verify')

class RegisterForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=8)])
    referral_code = StringField('Referral Code', validators=[Optional()])
    recaptcha = RecaptchaField()
    submit = SubmitField('Register')

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    recaptcha = RecaptchaField()
    submit = SubmitField('Login')

class RewardForm(FlaskForm):
    invoice_amount = DecimalField('Purchase Amount', validators=[DataRequired(), NumberRange(min=0.01, max=2500)], places=2, default=0)
    downline_level = SelectField(
        'Downline Level',
        choices=[
            ('1', 'Tier 1: (your purchases)'),
            ('2', 'Tier 2: (direct referral purchases)'),
            ('3', 'Tier 3: (Tier 2 referral purchases)'),
            ('4', 'Tier 4: (Tier 3 referral purchases)'),
            ('5', 'Tier 5: (Tier 4 referral purchases)')
        ],
        validators=[DataRequired()],
        default='1'
    )
    submit = SubmitField('Calculate My Reward')

class BusinessRegisterForm(FlaskForm):
    business_name = StringField('Business Name', validators=[DataRequired()])
    business_email = StringField('Business Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=8)])
    referral_code = StringField('Referral Code', validators=[Optional()])
    submit = SubmitField('Register')

class BusinessLoginForm(FlaskForm):
    business_email = StringField('Business Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    recaptcha = RecaptchaField()
    submit = SubmitField('Login')

class BusinessRewardForm(FlaskForm):
    invoice_amount = DecimalField('Purchase Amount', validators=[DataRequired(), NumberRange(min=0.01, max=2500)], places=2, default=0)
    downline_level = SelectField(
        'Downline Level',
        choices=[
            ('1', 'Tier 1: (your invoices)'),
            ('2', 'Tier 2: (direct referral invoices)'),
            ('3', 'Tier 3: (Tier 2 referral invoices)'),
            ('4', 'Tier 4: (Tier 3 referral invoices)'),
            ('5', 'Tier 5: (Tier 4 referral invoices)')
        ],
        validators=[DataRequired()],
        default='1'
    )
    submit = SubmitField('Calculate My Reward')

class UserProfileForm(FlaskForm):
    name = StringField('Name', validators=[Optional(), Length(max=100)])
    profile_photo = FileField('Upload Profile Photo')
    submit = SubmitField('Save Profile')

class BusinessProfileForm(FlaskForm):
    business_name = StringField('Business Name', validators=[Optional(), Length(max=100)])
    profile_photo = FileField('Upload Profile Photo')
    phone_number = StringField('Phone Number', validators=[Optional(), Length(max=30)])
    address = StringField('Address', validators=[Optional(), Length(max=255)])
    latitude = StringField('Latitude', validators=[Optional()])
    longitude = StringField('Longitude', validators=[Optional()])
    submit = SubmitField('Save Profile')

class EmptyForm(FlaskForm):
    submit = SubmitField('Submit')

class ForgotPasswordForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    submit = SubmitField('Send reset link')

class StaffForgotPasswordForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    submit = SubmitField("Send Reset Instructions")

class ResetPasswordForm(FlaskForm):
    password = PasswordField('New Password', validators=[DataRequired(), Length(min=8)])
    submit = SubmitField('Reset Password')

# ---------------------- Validators, Roles, and Utilities ----------------------

EMAIL_REGEX = r'^[\w.-]+@[\w.-]+\.\w{2,}$'

def valid_email(email):
    return re.match(EMAIL_REGEX, email or "")

def valid_password(pw):
    return pw and len(pw) >= 8

def random_email_code():
    import string, random
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def random_referral_code(email):
    base_code = "REF" + (email.split("@")[0].replace(".", "")[:12])
    code = base_code
    counter = 1
    while User.query.filter_by(referral_code=code).first():
        code = f"{base_code}{counter}"
        counter += 1
    return code

def random_business_code(business_name):
    base_code = "BIZ" + (business_name.replace(" ", "")[:12])
    code = base_code
    counter = 1
    while Business.query.filter_by(referral_code=code).first():
        code = f"{base_code}{counter}"
        counter += 1
    return code

def send_email(to, subject, html_body):
    msg = MailMessage(
        subject=subject,
        recipients=[to],
        html=html_body,
        sender=app.config['MAIL_USERNAME']
    )
    try:
        mail.send(msg)
    except Exception as e:
        logging.error("EMAIL SEND ERROR: %s", e)

def build_invite_email(inviter_name, join_url, video_url):
    html_body = f"""

<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>You have been invited to join PerkMiner.com!</title>
        <style type="text/css">
            body {{ margin: 0; padding: 0; -webkit-font-smoothing: antialiased; }}
            table, td {{ border-collapse: collapse; }}
            a {{ color: #0066cc; text-decoration: none; }}
            .button {{
            display: inline-block;
            padding: 16px 36px;
            background-color: #6366f1;
            color: white !important;
            font-family: Arial, Helvetica, sans-serif;
            font-size: 18px;
            font-weight: bold;
            text-decoration: none;
            border-radius: 8px;
            line-height: 1;
            }}
            .button:hover {{ background-color: #4f46e5 !important; }}
        </style>
    </head>
        <body style="margin:0; padding:0; background-color:#f3f4f6;">

        <!-- Main Wrapper -->
            <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color:#f3f4f6;">
                <tr>
                    <td align="center" style="padding: 20px 10px;">

        <!-- Container -->
            <table role="presentation" width="600" border="0" cellspacing="0" cellpadding="0" style="background-color:#ffffff; border-radius:12px; overflow:hidden; box-shadow:0 4px 12px rgba(0,0,0,0.1); max-width:600px;">

        <!-- Top Message -->
                <tr>
                    <td align="center" style="padding: 40px 30px 20px; font-family: Arial, Helvetica, sans-serif; font-size: 28px; font-weight: bold; color: #1f2937; line-height: 1.2;">
                        {inviter_name} has invited you to join Perk Miner (https://perkminer.com)!
                    </td>
                </tr>

        <!-- Join Button -->
    <tr>
        <td align="center" style="padding: 0 40px 50px;">
            <a href="{join_url}" class="button" target="_blank" style=" font-size:20px; padding:18px 48px;">
            Join PerkMiner Now
            </a>
        </td>
    </tr>

        <!-- Hero Banner with Logo -->
                <tr>
                    <td style="position:relative;">
                        <img src="https://res.cloudinary.com/dmrntlcfd/image/upload/v1771635742/Email_Background_kgfx10.jpg" width="600"
                        alt="PerkMiner Hero Banner"
                        style="display:block; width:100%; height:auto; border:0;" border="0">
        </td>
    </tr>

        <!-- Introduction Text + Watch Video Button -->
    <tr>
        <td style="padding: 40px 40px 20px; font-family: Arial, Helvetica, sans-serif; font-size: 28px; color: #374151; line-height: 1.6; text-align:center;">
            <p style="margin:0 0 24px;">Discover how you earn Cash Back and Referral Commissions with Perk Miner.  <b>Cash Back like a pro on everyday purchases!  Members earn 2% Cashback for up to $2,500 of any purchase from our advertisers and Businesses earn 1% Cashback on up to $2,500 or their sale for offering at least one perk to our members.</b></p>

                <a href="{video_url}" class="button" target="_blank" style="margin: 12px 0 32px;">
                Watch our intro video
                </a>

            <p style="margin:0 0 28px;">Free to join (no contracts, monthly subscriptions or commitment).</p>
            <p style="margin:0 0 28px;"><b>Members:  Get exclusive member perks offered by our advertisers, plus earn cash back on all your purchases.</b>  We protect your privacy with secure messaging and never sell your contact information as a lead.  Search for businesses, products or services (our advertisers) with peace of mind (we don't track browsing history or listen to your conversations to send you unsolicited advertisements).  We connect <b>One Member</b> to <b>One Business</b> at a time.  <b><u>Our members</u></b> will never receive spammed emails, unsolicited phone calls or uninvited door-to-door sales people (advertisers don't have access to member contact information).</p>
            <p style="margin:0 0 28px;"><b>Business Owners:</b>  YOU GET ZERO WASTED ADVERTISING DOLLARS!  <font color="#FF0000"></br>No Sale or Closed Deal = Zero Fees</font></br>(900% or higher Marketing ROI Guaranteed).  No cost for exclusive leads, phone calls, website or foot traffic, appointments or meetups.  You only pay after you get paid (10% of the sale, capped at $250).  We don't collect your payment (members pay our advertisers directly for all sales).  No hidden fees, no contracts and no commitment.  Only $25 required to get started (pre-funded dollars to cover the advertising fees per transaction), which covers $250 in sales (funds remain in your account balance until you make a sale).</p>
            <p style="margin:0 0 28px;">MEMBER SELECTS A BUSINESS -> BUSINESS AND MEMBER CONNECT</br></br>MEMBER OR BUSINESS CAN END SESSION WITHOUT PENALTY OR CHOOSE TO PROCEED (BUSINESS MUST FINALIZE THE TRANSACTION).</p>
            <p style="margin:0 0 28px;"><b>Both Members and Business Owners earn Cash Back and Referral Commissions</b> (Paid by Perk Miner - from up to 84% of the ad revenue paid by our advertisers).</p>
        </td>
    </tr>

        <!-- Secondary Image -->
    <tr>
        <td style="padding: 0 40px 30px;">
            <img src="https://res.cloudinary.com/dmrntlcfd/image/upload/v1775719494/List-for-Free_pykazg.jpg" width="520" alt="PerkMiner Features"
            style="display:block; width:100%; max-width:520px; height:auto; border-radius:10px; border:0;" border="0">
        </td>
    </tr>

        <!-- Join Button -->
    <tr>
        <td align="center" style="padding: 0 40px 50px;">
            <a href="{join_url}" class="button" target="_blank" style="font-size:20px; padding:18px 48px;">
            Join PerkMiner Now
            </a>
        </td>
    </tr>

        <!-- Footer -->
    <tr>
        <td align="center" style="padding: 30px 40px; background-color:#f8f9fa; font-family: Arial, Helvetica, sans-serif; font-size: 14px; color: #6b7280; line-height:1.5; border-top:1px solid #e5e7eb;">
            <p style="margin:0 0 8px;">
                For questions regarding this email, contact
                <a href="mailto:fromperkminer@gmail.com" style="color:#4f46e5;">Need help?</a>
            </p>
            <p style="margin:0;">
                Copyright © PerkMiner 2026. All rights reserved.
            </p>
            </td>
                </tr>

                    </table>

                </td>
            </tr>
        </table>

    </body>
</html>
    """
    return html_body

def send_verification_email(user):
    code = user.email_code
    verify_url = url_for("activate", code=code, _external=True)
    html_body = f"""<p>Click <a href="{verify_url}">here</a> to confirm your email, or use code: <b>{code}</b></p>"""
    send_email(user.email, "Confirm your PerkMiner email!", html_body)

def send_business_verification_email(biz):
    code = biz.email_code
    verify_url = url_for("business_activate", code=code, _external=True)
    html_body = f"""<p>Click <a href="{verify_url}">here</a> to confirm your business email, or use code: <b>{code}</b></p>"""
    send_email(biz.business_email, "Confirm your PerkMiner business email!", html_body)

def allowed_file(filename):
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def generate_reset_token(email, user_type):
    return serializer.dumps({'email': email, 'type': user_type})

def verify_reset_token(token, user_type, max_age=3600):
    try:
        data = serializer.loads(token, max_age=max_age)
        if data.get('type') == user_type:
            return data.get('email')
    except (SignatureExpired, BadSignature):
        return None
    return None

def send_reset_email(recipient_email, reset_url):
    send_email(
        recipient_email,
        "PerkMiner password reset",
        f"<p>You requested a password reset. Click <a href='{reset_url}'>here</a> to reset your password.</p>"
        "<p>If you didn't request this, please ignore this email.</p>"
    )

# ---------------------- Role & Access Control Decorators -----------------------

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not any(r.name == 'super_admin' for r in current_user.roles):
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

def role_required(role_name):
    def decorator(f):
        @wraps(f)
        @login_required
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated or not current_user.has_role(role_name):
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def business_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('business_id'):
            flash('Please log in as a business to access this page.', 'warning')
            return redirect(url_for('business_login', next=request.path))
        return f(*args, **kwargs)
    return decorated_function

def log_finalization(staff_id, business_id, tx_id, source, amount):
    log = FinalizedTransaction(
        staff_id=staff_id,
        business_id=business_id,
        tx_id=tx_id,
        source=source,
        amount=amount,
        timestamp=datetime.utcnow()
    )
    db.session.add(log)
    db.session.commit()

def staff_password_reset_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        staff_id = session.get("staff_id")
        staff = Staff.query.get(staff_id)
        if staff and staff.password_reset_required:
            flash("You must change your password before continuing.", "warning")
            return redirect(url_for("staff_change_password"))
        return f(*args, **kwargs)
    return decorated_function

def split_mutual_commission(total_sale, n_referred):
    if n_referred < 1:
        return [], 0.0
    pool = min(total_sale * 0.002, 5.00)
    per_biz = round(pool / n_referred, 2)
    payouts = [per_biz for _ in range(n_referred)]
    leftover = round(pool - sum(payouts), 2)
    return payouts, leftover

def finalize_interaction(interaction, business, amount, staff_id=None, source=None, local_date_time=None):
    import uuid
    from datetime import datetime

    transaction_id = str(uuid.uuid4())

    # ---- Rewards setup ----
    ad_fee = min(round(amount * 0.10, 2), 250.00)
    if business.account_balance is None or business.account_balance < ad_fee:
        raise Exception("Insufficient funds to complete this transaction. Please fund your account.")

    user_referral_id = interaction.user.referral_code or "REFjoejmendez"
    user_cash_back_raw = amount * 0.02
    user_cash_back = round(min(user_cash_back_raw, 50), 2)

    u2 = User.query.filter_by(id=interaction.user.sponsor_id).first()
    tier2_user_referral_id = u2.referral_code if u2 else "REFmarjoriepint"
    u3 = User.query.filter_by(id=u2.sponsor_id).first() if u2 and u2.sponsor_id else None
    tier3_user_referral_id = u3.referral_code if u3 else "REFmarjoriepint"
    u4 = User.query.filter_by(id=u3.sponsor_id).first() if u3 and u3.sponsor_id else None
    tier4_user_referral_id = u4.referral_code if u4 else "REFmarjoriepint"
    u5 = User.query.filter_by(id=u4.sponsor_id).first() if u4 and u4.sponsor_id else None
    tier5_user_referral_id = u5.referral_code if u5 else "REFjoejmendez"

    tier2_commission_raw = amount * 0.002
    tier2_commission = round(min(tier2_commission_raw, 5.00), 2)
    tier3_commission_raw = amount * 0.002
    tier3_commission = round(min(tier3_commission_raw, 5.00), 2)
    tier4_commission_raw = amount * 0.002
    tier4_commission = round(min(tier4_commission_raw, 5.00), 2)
    tier5_commission_raw = amount * 0.02
    tier5_commission = round(min(tier5_commission_raw, 50), 2)

    top_biz = find_top_business_with_user_sponsor(business)
    root_user = User.query.get(top_biz.user_sponsor_id) if top_biz and top_biz.user_sponsor_id else None

    business_chain = []
    b = business
    visited_ids = set()
    while b and b.id not in visited_ids:
        business_chain.insert(0, b)
        visited_ids.add(b.id)
        b = Business.query.get(b.sponsor_id) if b.sponsor_id else None
    downline_tier = len(business_chain)



    tier1_business_user_referral_id = None; tier1_business_user_commission = 0
    tier2_business_user_referral_id = None; tier2_business_user_commission = 0
    tier3_business_user_referral_id = None; tier3_business_user_commission = 0
    tier4_business_user_referral_id = None; tier4_business_user_commission = 0
    tier5_business_user_referral_id = None; tier5_business_user_commission = 0

    if root_user:
        if downline_tier == 1:
            tier1_business_user_referral_id = root_user.referral_code
            tier1_business_user_commission = round(min(amount * 0.01, 25), 2)
        elif downline_tier == 2:
            tier2_business_user_referral_id = root_user.referral_code
            tier2_business_user_commission = round(min(amount * 0.002, 5.00), 2)
        elif downline_tier == 3:
            tier3_business_user_referral_id = root_user.referral_code
            tier3_business_user_commission = round(min(amount * 0.002, 5.00), 2)
        elif downline_tier == 4:
            tier4_business_user_referral_id = root_user.referral_code
            tier4_business_user_commission = round(min(amount * 0.002, 5.00), 2)
        elif downline_tier == 5:
            tier5_business_user_referral_id = root_user.referral_code
            tier5_business_user_commission = round(min(amount * 0.01, 25), 2)

    user_trans = UserTransaction(
        transaction_id=transaction_id,
        interaction_id=interaction.id,
        amount=amount,
        business_referral_id=business.referral_code,
        user_referral_id=user_referral_id,
        cash_back=user_cash_back,
        tier2_user_referral_id=tier2_user_referral_id,
        tier2_commission=tier2_commission,
        tier3_user_referral_id=tier3_user_referral_id,
        tier3_commission=tier3_commission,
        tier4_user_referral_id=tier4_user_referral_id,
        tier4_commission=tier4_commission,
        tier5_user_referral_id=tier5_user_referral_id,
        tier5_commission=tier5_commission,
        tier1_business_user_referral_id=tier1_business_user_referral_id,
        tier1_business_user_commission=tier1_business_user_commission,
        tier2_business_user_referral_id=tier2_business_user_referral_id,
        tier2_business_user_commission=tier2_business_user_commission,
        tier3_business_user_referral_id=tier3_business_user_referral_id,
        tier3_business_user_commission=tier3_business_user_commission,
        tier4_business_user_referral_id=tier4_business_user_referral_id,
        tier4_business_user_commission=tier4_business_user_commission,
        tier5_business_user_referral_id=tier5_business_user_referral_id,
        tier5_business_user_commission=tier5_business_user_commission
    )
    db.session.add(user_trans)

    business_referral_id = business.referral_code or "BIZPerkMiner"
    b2 = Business.query.filter_by(id=business.sponsor_id).first()
    tier2_business_referral_id = b2.referral_code if b2 else ""
    tier2_commission_biz = round(min(amount * 0.002, 5.00), 2)
    b3 = Business.query.filter_by(id=b2.sponsor_id).first() if b2 and b2.sponsor_id else None
    tier3_business_referral_id = b3.referral_code if b3 else ""
    tier3_commission_biz = round(min(amount * 0.002, 5.00), 2)
    b4 = Business.query.filter_by(id=b3.sponsor_id).first() if b3 and b3.sponsor_id else None
    tier4_business_referral_id = b4.referral_code if b4 else ""
    tier4_commission_biz = round(min(amount * 0.002, 5.00), 2)
    b5 = Business.query.filter_by(id=b4.sponsor_id).first() if b4 and b4.sponsor_id else None
    tier5_business_referral_id = b5.referral_code if b5 else "BIZPerkMiner"
    tier5_commission_biz = round(min(amount * 0.01, 25), 2)

    business_cash_back_raw = amount * 0.01
    business_cash_back = round(min(business_cash_back_raw, 25), 2)

    business.account_balance = (business.account_balance or 0.0) - ad_fee

    # ---- Main business commission row ----
    business_trans = BusinessTransaction(
        transaction_id=transaction_id,
        interaction_id=interaction.id,
        amount=amount,
        date_time=datetime.utcnow(),
        local_date_time=local_date_time,
        ad_fee=ad_fee,
        business_referral_id=business_referral_id,
        cash_back=business_cash_back,
        tier2_business_referral_id=tier2_business_referral_id,
        tier2_commission=tier2_commission_biz,
        tier3_business_referral_id=tier3_business_referral_id,
        tier3_commission=tier3_commission_biz,
        tier4_business_referral_id=tier4_business_referral_id,
        tier4_commission=tier4_commission_biz,
        tier5_business_referral_id=tier5_business_referral_id,
        tier5_commission=tier5_commission_biz,
        sponsoree_mutual_referral_id=None,
        sponsoree_mutual_commission=0
    )
    db.session.add(business_trans)

    # ---- Downline mutual commission ----
    referred_businesses = Business.query.filter_by(sponsor_id=business.id).all()
    payouts, leftover = split_mutual_commission(amount, len(referred_businesses))

    for idx, sponsoree in enumerate(referred_businesses):
        sponsoree_mutual_referral_id = sponsoree.referral_code
        sponsoree_mutual_commission = payouts[idx]
        mutual_trans = BusinessTransaction(
            transaction_id=transaction_id,
            interaction_id=interaction.id,
            amount=amount,
            date_time=datetime.utcnow(),
            local_date_time=local_date_time,
            ad_fee=ad_fee,
            business_referral_id=business_referral_id,
            cash_back=0,
            tier2_business_referral_id="",
            tier2_commission=0,
            tier3_business_referral_id="",
            tier3_commission=0,
            tier4_business_referral_id="",
            tier4_commission=0,
            tier5_business_referral_id="",
            tier5_commission=0,
            sponsoree_mutual_referral_id=sponsoree_mutual_referral_id,
            sponsoree_mutual_commission=sponsoree_mutual_commission
        )
        db.session.add(mutual_trans)

    db.session.commit()

    # ---- SILENT INVESTOR EARNINGS LOGIC (add after transactions and commit) ----

    # 1. Ensure amount is Decimal
    amount = Decimal(str(amount))

    # 2. Calculate and print Ad fee
    raw_ad_fee = amount * Decimal("0.10")
    ad_fee = min(raw_ad_fee, Decimal("250.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    print(f"Ad fee: {ad_fee}")

    # 3. Charity from ad fee ONLY
    charity = (ad_fee * Decimal("0.105")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    print(f"Charity: {charity}")
    ad_fee_after_charity = ad_fee - charity
    print(f"Ad fee after charity: {ad_fee_after_charity}")

    # 4. User payouts (Decimal)
    user_payouts = Decimal(str(user_cash_back))
    user_payouts += Decimal(str(tier2_commission))
    user_payouts += Decimal(str(tier3_commission))
    user_payouts += Decimal(str(tier4_commission))
    user_payouts += Decimal(str(tier5_commission))
    user_payouts += Decimal(str(tier1_business_user_commission or 0))
    user_payouts += Decimal(str(tier2_business_user_commission or 0))
    user_payouts += Decimal(str(tier3_business_user_commission or 0))
    user_payouts += Decimal(str(tier4_business_user_commission or 0))
    user_payouts += Decimal(str(tier5_business_user_commission or 0))
    print(f"Total user payouts: {user_payouts}")

    # 5. Business payouts (Decimal) — place this after you've defined "business" and before net_gross math
    # Walk up the sponsor chain from the sale business for correct tier assignments
    current_biz = business  # The business making the sale is Tier 1
    tier_biz_ids = []
    for _ in range(5):
        tier_biz_ids.append(current_biz.referral_code if current_biz else None)
        current_biz = Business.query.get(current_biz.sponsor_id) if (current_biz and current_biz.sponsor_id) else None

    tier1_business_user_referral_id = tier_biz_ids[0]
    tier2_business_user_referral_id = tier_biz_ids[1]
    tier3_business_user_referral_id = tier_biz_ids[2]
    tier4_business_user_referral_id = tier_biz_ids[3]
    tier5_business_user_referral_id = tier_biz_ids[4]

    print(f"T1 biz id: {tier1_business_user_referral_id}")
    print(f"T2 biz id: {tier2_business_user_referral_id}")
    print(f"T3 biz id: {tier3_business_user_referral_id}")
    print(f"T4 biz id: {tier4_business_user_referral_id}")
    print(f"T5 biz id: {tier5_business_user_referral_id}")

    # Define the conditional payout helper
    def biz_tier_commission_val(biz_id, commission_amt, special_exclude="BIZPerkMiner"):
        if biz_id is None or not str(biz_id).strip() or biz_id == special_exclude:
            return Decimal("0")
        return commission_amt

    # ... your tier assignment code ...

    ad_fee_dec = Decimal(str(ad_fee))
    t1_payout = min(ad_fee_dec * Decimal("0.10"), Decimal("25.00"))
    t2_payout = min(ad_fee_dec * Decimal("0.02"), Decimal("5.00"))
    t3_payout = min(ad_fee_dec * Decimal("0.02"), Decimal("5.00"))
    t4_payout = min(ad_fee_dec * Decimal("0.02"), Decimal("5.00"))
    t5_payout = min(ad_fee_dec * Decimal("0.10"), Decimal("25.00"))

    business_payouts = Decimal("0")
    business_payouts += biz_tier_commission_val(tier1_business_user_referral_id, t1_payout)
    business_payouts += biz_tier_commission_val(tier2_business_user_referral_id, t2_payout)
    business_payouts += biz_tier_commission_val(tier3_business_user_referral_id, t3_payout)
    business_payouts += biz_tier_commission_val(tier4_business_user_referral_id, t4_payout)
    business_payouts += biz_tier_commission_val(tier5_business_user_referral_id, t5_payout)

    # mutual
    mutual_sponsoree_payout = Decimal("0")
    num_sponsorees = len(referred_businesses)
    if num_sponsorees > 0:
        split_pool = min(amount * Decimal("0.002"), Decimal("5.00"))
        mutual_sponsoree_payout = split_pool
    business_payouts += mutual_sponsoree_payout

    # Print step results for debugging
    print(f"T1 payout: {t1_payout}, id: {tier1_business_user_referral_id}")
    print(f"T2 payout: {t2_payout}, id: {tier2_business_user_referral_id}")
    print(f"T3 payout: {t3_payout}, id: {tier3_business_user_referral_id}")
    print(f"T4 payout: {t4_payout}, id: {tier4_business_user_referral_id}")
    print(f"T5 payout: {t5_payout}, id: {tier5_business_user_referral_id}")
    print(f"Mutual sponsoree biz ids: {[b.referral_code for b in referred_businesses]}")
    print(f"Total business payouts: {business_payouts}")

    # 6. Net gross (leftover after all above)
    total_payouts = user_payouts + business_payouts

    # ...previous business payout code above...

    capital_reserves = Decimal("0")
    if (not tier5_business_user_referral_id or tier5_business_user_referral_id == "BIZPerkMiner"):
        capital_reserves += t5_payout

    print(f"Capital reserves from Tier 5: {capital_reserves}")

    net_gross = ad_fee_after_charity - user_payouts - business_payouts - capital_reserves
    print(f"Net gross: {net_gross}")

    # 7. Apply 45%
    silent_investor_pool = (net_gross * Decimal("0.45"))
    print(f"Silent investor pool: {silent_investor_pool}")

    # 8. Distribute, show share per
    silent_investors = User.query.join(User.roles).filter(Role.name == 'silent_investor').all()
    for investor in silent_investors:
        share = Decimal(str(investor.investor_share or 0))
        if share > 0 and silent_investor_pool > 0:
            raw_payout = silent_investor_pool * share  # high precision

            # store with 6 decimals in DB so it never hits 0.00 just because it's small
            payout = raw_payout.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

            if payout > 0:
                earning = InvestorEarnings(
                    user_id=investor.id,
                    year=datetime.utcnow().year,
                    month=datetime.utcnow().month,
                    amount=payout,
                    created_at=datetime.utcnow()
                )
                db.session.add(earning)

                investor.investor_total_earnings = (investor.investor_total_earnings or Decimal("0")) + payout
                investor.investor_earnings_balance = (investor.investor_earnings_balance or Decimal("0")) + payout

    db.session.commit()

    # ---- END SILENT INVESTOR LOGIC ----

    if staff_id:
        log_finalization(staff_id, business.id, transaction_id, source, amount)

    summary = {
        "amount": f"{amount:.2f}",
        "user_cash_back": f"{user_cash_back:.2f}",
        "business_cash_back": f"{business_cash_back:.2f}",
        "ad_fee": f"{ad_fee:.2f}",
        "net_gross": f"{amount - ad_fee:,.2f}",
        "marketing_roi": int(((amount - ad_fee) / ad_fee) * 100) if ad_fee else 0,
        "marketing_ratio": round(((amount - ad_fee) + ad_fee) / ad_fee, 2) if ad_fee else 0,
        "transaction_id": transaction_id,
    }
    return summary

def issue_store_sale_rewards(business, amount, buyer_email=None):
    """
    Issues rewards and commissions for a store sale:
      - Cashback to the buyer (if lookup by email works in your system)
      - All user and business commissions, for up to 5 tiers
    """
    # Example: TIER SETUP (adjust if you change reward rates/caps)
    user_cash_back = round(min(amount * 0.02, 50), 2)
    tier2_commission = round(min(amount * 0.002, 5.00), 2)
    tier3_commission = round(min(amount * 0.002, 5.00), 2)
    tier4_commission = round(min(amount * 0.002, 5.00), 2)
    tier5_commission = round(min(amount * 0.02, 50), 2)

    # User sponsor/referrer lookups (adjust how you find users per your referral chain)
    user = None
    if buyer_email:
        user = User.query.filter_by(email=buyer_email).first()
    # Get the referral_code(s) for this buyer, and for each tier chain, as you normally do
    # (Example logic; you may need to adapt it for cart, etc.)
    # If user exists, walk up the sponsor chain
    user_referral_code = user.referral_code if user else None
    tier2_user = User.query.get(user.sponsor_id) if user and user.sponsor_id else None
    tier2_referral_code = tier2_user.referral_code if tier2_user else None
    tier3_user = User.query.get(tier2_user.sponsor_id) if tier2_user and tier2_user.sponsor_id else None
    tier3_referral_code = tier3_user.referral_code if tier3_user else None
    tier4_user = User.query.get(tier3_user.sponsor_id) if tier3_user and tier3_user.sponsor_id else None
    tier4_referral_code = tier4_user.referral_code if tier4_user else None
    tier5_user = User.query.get(tier4_user.sponsor_id) if tier4_user and tier4_user.sponsor_id else None
    tier5_referral_code = tier5_user.referral_code if tier5_user else None

    # Business network chain, if you pay business-user commissions
    business_referral_code = business.referral_code
    # (Repeat chain lookup for other business tiers as per your app...)

    # Create a UserTransaction and BusinessTransaction to record rewards
    user_txn = UserTransaction(
        transaction_id=str(uuid.uuid4()),
        interaction_id=None,  # For store orders, you may not have an Interaction, set None or create a dummy/tied one
        date_time=datetime.utcnow(),
        amount=amount,
        user_referral_id=user_referral_code or "",
        cash_back=user_cash_back if user else 0,
        tier2_user_referral_id=tier2_referral_code or "",
        tier2_commission=tier2_commission if tier2_user else 0,
        tier3_user_referral_id=tier3_referral_code or "",
        tier3_commission=tier3_commission if tier3_user else 0,
        tier4_user_referral_id=tier4_referral_code or "",
        tier4_commission=tier4_commission if tier4_user else 0,
        tier5_user_referral_id=tier5_referral_code or "",
        tier5_commission=tier5_commission if tier5_user else 0,
        business_referral_id=business_referral_code or "",
        # Fill business_user_commission fields as needed
        # (Set to zero or real values per your advanced commission structure)
        # ...
    )
    db.session.add(user_txn)

    business_txn = BusinessTransaction(
        transaction_id=user_txn.transaction_id,
        interaction_id=None,
        date_time=datetime.utcnow(),
        amount=amount,
        business_referral_id=business_referral_code,
        cash_back=round(min(amount * 0.01, 25), 2),
        # Similarly fill tier2-tiers, sponsor mutual, etc.
        tier2_business_referral_id="", # Add logic for business referral tree
        tier2_commission=0,
        tier3_business_referral_id="",
        tier3_commission=0,
        tier4_business_referral_id="",
        tier4_commission=0,
        tier5_business_referral_id="",
        tier5_commission=0,
        ad_fee=round(amount * 0.10, 2)  # Store ad fee for record-keeping
    )
    db.session.add(business_txn)

    db.session.commit()

# ---------------------- Flask-Login User Loader ----------------------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ---------------------- Core Database Models ----------------------
from flask_login import UserMixin
from datetime import datetime

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    stripe_account_id = db.Column(db.String(128))
    earnings_balance = db.Column(db.Numeric(12,2), default=0)
    grand_total_earnings = db.Column(db.Numeric(12,2), default=0)
    withdrawn_total = db.Column(db.Numeric(12,2), default=0)
    pending_earnings = db.Column(db.Numeric(12,2), default=0)              # not yet past 7 days
    available_for_withdrawal = db.Column(db.Numeric(12,2), default=0)      # >= 7 days old
    email = db.Column(db.String(200), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    name = db.Column(db.String(100))
    referral_code = db.Column(db.String(32), unique=True)
    sponsor_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    business_referral_id = db.Column(db.String(32))
    email_confirmed = db.Column(db.Boolean, default=False)
    email_code = db.Column(db.String(16))
    profile_photo = db.Column(db.String(200))
    roles = db.relationship('Role', secondary='user_roles', backref='users')
    is_suspended = db.Column(db.Boolean, default=False)
    investor_share = db.Column(db.Numeric(5, 4), default=0)
    investor_total_earnings = db.Column(db.Numeric(12, 6), default=0)
    investor_withdrawn_total = db.Column(db.Numeric(12, 2), default=0)
    investor_earnings_balance = db.Column(db.Numeric(12, 6), default=0)
    pending_investor_earnings = db.Column(db.Numeric(12, 6), default=0)      # < 7 days
    investor_earnings_withdraw_ready = db.Column(db.Numeric(12, 6), default=0)  # >= 7 days
    def has_role(self, role_name):
        return any(role.name == role_name for role in self.roles)

class InvestorEarnings(db.Model):
    __tablename__ = 'investor_earnings'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Numeric(12, 6), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('investor_earnings', lazy=True))

class Role(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)

class UserRoles(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    role_id = db.Column(db.Integer, db.ForeignKey('role.id'))

class Business(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    business_name = db.Column(db.String(100), unique=True, nullable=False)
    store_slug = db.Column(db.String(80), unique=True)
    theme_id = db.Column(db.Integer, db.ForeignKey('theme.id'))
    custom_html = db.Column(db.Text)
    grapesjs_html = db.Column(db.Text)
    grapesjs_css = db.Column(db.Text)
    stripe_account_id = db.Column(db.String(100))
    earnings_balance = db.Column(db.Numeric(12,2), default=0)
    grand_total_earnings = db.Column(db.Numeric(12,2), default=0)
    withdrawn_total = db.Column(db.Numeric(12,2), default=0)
    pending_earnings = db.Column(db.Numeric(12,2), default=0)          # < 7 days
    available_to_withdraw = db.Column(db.Numeric(12,2), default=0)     # >= 7 days
    has_ecommerce_store = db.Column(db.Boolean, default=False)
    listing_type = db.Column(db.String(50))
    category = db.Column(db.String(50), nullable=False, default="Other")
    finalization = db.Column(db.String(50), default="All-Sales-Final")
    perks = db.Column(db.Text)
    business_email = db.Column(db.String(200), unique=True, nullable=False)
    website_approved = db.Column(db.Boolean, default=False)
    password = db.Column(db.String(60), nullable=False)
    referral_code = db.Column(db.String(32), unique=True)
    sponsor_id = db.Column(db.Integer, db.ForeignKey('business.id'))
    user_sponsor_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    email_confirmed = db.Column(db.Boolean, default=False)
    email_code = db.Column(db.String(16))
    profile_photo = db.Column(db.String(200))
    phone_number = db.Column(db.String(30))
    address = db.Column(db.String(255))
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    hours_of_operation = db.Column(db.String(100))
    website_url = db.Column(db.String(255))
    about_us = db.Column(db.Text)
    service_1 = db.Column(db.String(100))
    service_2 = db.Column(db.String(100))
    service_3 = db.Column(db.String(100))
    service_4 = db.Column(db.String(100))
    service_5 = db.Column(db.String(100))
    service_6 = db.Column(db.String(100))
    service_7 = db.Column(db.String(100))
    service_8 = db.Column(db.String(100))
    service_9 = db.Column(db.String(100))
    service_10 = db.Column(db.String(100))
    search_keywords = db.Column(db.String(500))
    draft_business_name = db.Column(db.String(100))
    draft_listing_type = db.Column(db.String(50))
    draft_category = db.Column(db.String(50), default="Other")
    draft_finalization = db.Column(db.String(50), default="All Sales Final")
    draft_profile_photo = db.Column(db.String(200))
    draft_phone_number = db.Column(db.String(30))
    draft_address = db.Column(db.String(255))
    draft_latitude = db.Column(db.Float)
    draft_longitude = db.Column(db.Float)
    draft_hours_of_operation = db.Column(db.String(100))
    draft_website_url = db.Column(db.String(255))
    draft_about_us = db.Column(db.Text)
    draft_service_1 = db.Column(db.String(100))
    draft_service_2 = db.Column(db.String(100))
    draft_service_3 = db.Column(db.String(100))
    draft_service_4 = db.Column(db.String(100))
    draft_service_5 = db.Column(db.String(100))
    draft_service_6 = db.Column(db.String(100))
    draft_service_7 = db.Column(db.String(100))
    draft_service_8 = db.Column(db.String(100))
    draft_service_9 = db.Column(db.String(100))
    draft_service_10 = db.Column(db.String(100))
    draft_search_keywords = db.Column(db.String(500))
    account_balance = db.Column(db.Float, nullable=False, default=0.0)
    ad_fee = db.Column(db.Float)
    business_registration_doc = db.Column(db.String(255))
    featured = db.Column(db.Boolean, default=False)
    rank = db.Column(db.Float, default=0.0)
    manual_feature = db.Column(db.Boolean, default=False)
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    is_suspended = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(20), nullable=False, default='not_submitted')
    homepage_html = db.Column(db.Text, nullable=True)
    contact_html = db.Column(db.Text, nullable=True)
    contact_css = db.Column(db.Text, nullable=True)
    products_html = db.Column(db.Text, nullable=True)
    products_css = db.Column(db.Text, nullable=True)
    grapesjs_js = db.Column(db.Text, nullable=True)
    products_js = db.Column(db.Text, nullable=True)
    contact_js = db.Column(db.Text, nullable=True)
    is_ecommerce_site = db.Column(db.Boolean, default=False)
    allow_website_purchases = db.Column(db.Boolean, default=False)
    online_terms_agreed = db.Column(db.Boolean, default=False)
    theme_type = db.Column(db.String(50))

class Favorite(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    business_id = db.Column(db.Integer, db.ForeignKey('business.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', backref='favorites', lazy=True)
    business = db.relationship('Business', backref='favorited_by', lazy=True)
    __table_args__ = (db.UniqueConstraint('user_id', 'business_id', name='_user_business_uc'),)

class Quote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    interaction_id = db.Column(db.Integer, db.ForeignKey('interaction.id'), nullable=False, unique=True)
    amount = db.Column(db.Float, nullable=False)
    details = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    interaction = db.relationship('Interaction', backref=db.backref('quote', uselist=False))

class UserTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.String(48), nullable=False, index=True)
    interaction_id = db.Column(db.Integer, db.ForeignKey('interaction.id'), nullable=False)
    date_time = db.Column(db.DateTime, default=datetime.utcnow)
    local_date_time = db.Column(db.String(32))
    amount = db.Column(db.Float, nullable=False)
    user_referral_id = db.Column(db.String(32), nullable=False)
    cash_back = db.Column(db.Float, nullable=False)
    tier2_user_referral_id = db.Column(db.String(32), nullable=False)
    tier2_commission = db.Column(db.Float, nullable=False)
    tier3_user_referral_id = db.Column(db.String(32), nullable=False)
    tier3_commission = db.Column(db.Float, nullable=False)
    tier4_user_referral_id = db.Column(db.String(32), nullable=False)
    tier4_commission = db.Column(db.Float, nullable=False)
    tier5_user_referral_id = db.Column(db.String(32), nullable=False)
    tier5_commission = db.Column(db.Float, nullable=False)
    business_referral_id = db.Column(db.String(32))
    tier1_business_user_referral_id = db.Column(db.String(32))
    tier1_business_user_commission = db.Column(db.Float)
    tier2_business_user_referral_id = db.Column(db.String(32))
    tier2_business_user_commission = db.Column(db.Float)
    tier3_business_user_referral_id = db.Column(db.String(32))
    tier3_business_user_commission = db.Column(db.Float)
    tier4_business_user_referral_id = db.Column(db.String(32))
    tier4_business_user_commission = db.Column(db.Float)
    tier5_business_user_referral_id = db.Column(db.String(32))
    tier5_business_user_commission = db.Column(db.Float)
    interaction = db.relationship("Interaction", backref="user_transactions", lazy=True)

class BusinessTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.String(48), nullable=False, index=True)
    interaction_id = db.Column(db.Integer, db.ForeignKey('interaction.id'), nullable=False)
    date_time = db.Column(db.DateTime, default=datetime.utcnow)
    local_date_time = db.Column(db.String(32))
    amount = db.Column(db.Float, nullable=False)
    business_referral_id = db.Column(db.String(32), nullable=False)
    cash_back = db.Column(db.Float, nullable=False)
    tier2_business_referral_id = db.Column(db.String(32), nullable=False)
    tier2_commission = db.Column(db.Float, nullable=False)
    tier3_business_referral_id = db.Column(db.String(32), nullable=False)
    tier3_commission = db.Column(db.Float, nullable=False)
    tier4_business_referral_id = db.Column(db.String(32), nullable=False)
    tier4_commission = db.Column(db.Float, nullable=False)
    tier5_business_referral_id = db.Column(db.String(32), nullable=False)
    tier5_commission = db.Column(db.Float, nullable=False)
    ad_fee = db.Column(db.Float)
    # The business who was sponsored (sponsoree) earns 0.25% whenever their sponsor generates a paid invoice
    sponsoree_mutual_referral_id = db.Column(db.String(32))
    sponsoree_mutual_commission = db.Column(db.Float, default=0)

class Interaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    business_id = db.Column(db.Integer, db.ForeignKey('business.id'), nullable=False)
    service_type = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text, nullable=False)
    budget_low = db.Column(db.Float)
    budget_high = db.Column(db.Float)
    status = db.Column(db.String(32), default="active")  # active, closed, ended, etc.
    referral_code = db.Column(db.String(32))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    awaiting_finalization = db.Column(db.Boolean, default=False)
    awaiting_payment = db.Column(db.Boolean, default=False)
    # relationships for easier querying (optional)
    user = db.relationship('User', backref='interactions', lazy=True)
    business = db.relationship('Business', backref='interactions', lazy=True)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    interaction_id = db.Column(db.Integer, db.ForeignKey('interaction.id'), nullable=False)
    sender_type = db.Column(db.String(16), nullable=False)  # "user" or "business"
    sender_id = db.Column(db.Integer, nullable=False)        # User or business id, based on sender_type
    text = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    file_url = db.Column(db.String(255))  # stores the filename or URL
    file_name = db.Column(db.String(120))  # original name for display
    interaction = db.relationship('Interaction', backref='messages', lazy=True)

class Theme(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(30))
    css_url = db.Column(db.String(150))
    thumbnail_url = db.Column(db.String(200))
    starter_html = db.Column(db.Text)
    contact_html = db.Column(db.Text, nullable=True)
    homepage_css = db.Column(db.Text)
    contact_html = db.Column(db.Text, nullable=True)
    contact_css = db.Column(db.Text, nullable=True)
    products_html = db.Column(db.Text, nullable=True)
    products_css = db.Column(db.Text, nullable=True)
    homepage_js = db.Column(db.Text, nullable=True)
    contact_js = db.Column(db.Text, nullable=True)
    products_js = db.Column(db.Text, nullable=True)
    preview_home = db.Column(db.Text, nullable=True)
    preview_home_css = db.Column(db.Text, nullable=True)
    preview_home_js = db.Column(db.Text, nullable=True)
    preview_products = db.Column(db.Text, nullable=True)
    preview_products_css = db.Column(db.Text, nullable=True)
    preview_products_js = db.Column(db.Text, nullable=True)
    preview_contact = db.Column(db.Text, nullable=True)
    preview_contact_css = db.Column(db.Text, nullable=True)
    preview_contact_js = db.Column(db.Text, nullable=True)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey('business.id'))
    name = db.Column(db.String(100))
    price = db.Column(db.Float)
    description = db.Column(db.Text)
    image_path = db.Column(db.String(200))
    stock = db.Column(db.Integer)
    featured = db.Column(db.Boolean, default=False)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey('business.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    buyer_email = db.Column(db.String(200))
    amount = db.Column(db.Float)
    stripe_checkout_id = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(30), default="paid")      # e.g., paid, fulfilled
    fulfillment_token = db.Column(db.String(64), nullable=True)  # for digital delivery (optional)
    # add other fields as needed (e.g., shipping address, download expiry...)

# If your digital products need file URLs, add a product.download_url or similar.

# Example for invites (included as you had in prior code)

class Invite(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    inviter_id = db.Column(db.Integer, nullable=True)
    inviter_type = db.Column(db.String(16), nullable=False)
    invitee_email = db.Column(db.String(200), nullable=False)
    invitee_type = db.Column(db.String(16), nullable=False)
    referral_code = db.Column(db.String(32), nullable=False)
    status = db.Column(db.String(16), nullable=False, default='pending')
    accepted_id = db.Column(db.Integer, nullable=True)
    accepted_at = db.Column(db.DateTime)

class Staff(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey('business.id'), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    hashed_password = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(20), default="staff")  # Future roles possible
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    password_reset_required = db.Column(db.Boolean, default=True)

    # Relationships
    business = db.relationship("Business", backref="staff_members")

    __table_args__ = (
        db.UniqueConstraint("business_id", "email", name="uniq_staff_per_biz"),
    )

class StaffRegisterForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    name = StringField("Name", validators=[DataRequired()])
    submit = SubmitField("Add Staff")

class StaffLoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    twofa_code = StringField('2FA Code')
    submit = SubmitField("Log In")

class Staff2FAForm(FlaskForm):
    code = StringField("Authenticator Code", validators=[DataRequired()])
    recaptcha = RecaptchaField()
    submit = SubmitField("Verify")

class StaffChangePasswordForm(FlaskForm):
    current_password = PasswordField('Current Password', validators=[DataRequired()])
    new_password = PasswordField('New Password', validators=[DataRequired(), Length(min=8)])
    confirm_new_password = PasswordField('Confirm New Password', validators=[DataRequired(), EqualTo('new_password', message='Passwords must match.')])
    submit = SubmitField('Update Password')

class UserChangePasswordForm(FlaskForm):
    current_password = PasswordField('Current Password', validators=[DataRequired()])
    new_password = PasswordField('New Password', validators=[DataRequired(), Length(min=8)])
    confirm_new_password = PasswordField(
        'Confirm New Password',
        validators=[DataRequired(), EqualTo('new_password', message='Passwords must match.')]
    )
    submit = SubmitField('Update Password')

class BusinessChangePasswordForm(FlaskForm):
    current_password = PasswordField('Current Password', validators=[DataRequired()])
    new_password = PasswordField('New Password', validators=[DataRequired(), Length(min=8)])
    confirm_new_password = PasswordField(
        'Confirm New Password',
        validators=[DataRequired(), EqualTo('new_password', message='Passwords must match.')]
    )
    submit = SubmitField('Update Password')

class FinalizedTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tx_id = db.Column(db.String(48), nullable=False, index=True)
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'))
    business_id = db.Column(db.Integer, db.ForeignKey('business.id'))
    source = db.Column(db.String(20)) # "barcode" or "message"
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    amount = db.Column(db.Float)

class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Optionally add 'transaction_id' if you want to link to a specific transaction

class ConversationParticipant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversation.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    # Optionally add role ("member", "business", "admin")

def calculate_user_grand_total(user):
    ref_code = user.referral_code
    all_txns = UserTransaction.query.all()
    total = Decimal(0)
    for t in all_txns:
        if t.user_referral_id == ref_code and (t.cash_back or 0) > 0:
            total += Decimal(t.cash_back or 0)
        if t.tier2_user_referral_id == ref_code and (t.tier2_commission or 0) > 0:
            total += Decimal(t.tier2_commission or 0)
        if t.tier3_user_referral_id == ref_code and (t.tier3_commission or 0) > 0:
            total += Decimal(t.tier3_commission or 0)
        if t.tier4_user_referral_id == ref_code and (t.tier4_commission or 0) > 0:
            total += Decimal(t.tier4_commission or 0)
        if t.tier5_user_referral_id == ref_code and (t.tier5_commission or 0) > 0:
            total += Decimal(t.tier5_commission or 0)

        # User-business commissions
        if t.tier1_business_user_referral_id == ref_code and (t.tier1_business_user_commission or 0) > 0:
            total += Decimal(t.tier1_business_user_commission or 0)
        if t.tier2_business_user_referral_id == ref_code and (t.tier2_business_user_commission or 0) > 0:
            total += Decimal(t.tier2_business_user_commission or 0)
        if t.tier3_business_user_referral_id == ref_code and (t.tier3_business_user_commission or 0) > 0:
            total += Decimal(t.tier3_business_user_commission or 0)
        if t.tier4_business_user_referral_id == ref_code and (t.tier4_business_user_commission or 0) > 0:
            total += Decimal(t.tier4_business_user_commission or 0)
        if t.tier5_business_user_referral_id == ref_code and (t.tier5_business_user_commission or 0) > 0:
            total += Decimal(t.tier5_business_user_commission or 0)
    return total

from datetime import datetime, timedelta

def calculate_user_earnings_split(user, delay_days=7):
    """
    Returns (total_earnings, available_earnings, pending_earnings)
    based on a simple date rule: earnings are available if
    transaction.date_time <= now - delay_days.
    """
    ref_code = user.referral_code
    all_txns = UserTransaction.query.all()

    total = Decimal(0)
    available = Decimal(0)

    cutoff = datetime.utcnow() - timedelta(days=delay_days)

    for t in all_txns:
        earned_here = Decimal(0)

        # Same logic as calculate_user_grand_total:
        if t.user_referral_id == ref_code and (t.cash_back or 0) > 0:
            earned_here += Decimal(t.cash_back or 0)
        if t.tier2_user_referral_id == ref_code and (t.tier2_commission or 0) > 0:
            earned_here += Decimal(t.tier2_commission or 0)
        if t.tier3_user_referral_id == ref_code and (t.tier3_commission or 0) > 0:
            earned_here += Decimal(t.tier3_commission or 0)
        if t.tier4_user_referral_id == ref_code and (t.tier4_commission or 0) > 0:
            earned_here += Decimal(t.tier4_commission or 0)
        if t.tier5_user_referral_id == ref_code and (t.tier5_commission or 0) > 0:
            earned_here += Decimal(t.tier5_commission or 0)

        # User-business commissions
        if t.tier1_business_user_referral_id == ref_code and (t.tier1_business_user_commission or 0) > 0:
            earned_here += Decimal(t.tier1_business_user_commission or 0)
        if t.tier2_business_user_referral_id == ref_code and (t.tier2_business_user_commission or 0) > 0:
            earned_here += Decimal(t.tier2_business_user_commission or 0)
        if t.tier3_business_user_referral_id == ref_code and (t.tier3_business_user_commission or 0) > 0:
            earned_here += Decimal(t.tier3_business_user_commission or 0)
        if t.tier4_business_user_referral_id == ref_code and (t.tier4_business_user_commission or 0) > 0:
            earned_here += Decimal(t.tier4_business_user_commission or 0)
        if t.tier5_business_user_referral_id == ref_code and (t.tier5_business_user_commission or 0) > 0:
            earned_here += Decimal(t.tier5_business_user_commission or 0)

        if earned_here == 0:
            continue

        total += earned_here

        # Available only if older than cutoff
        if t.date_time <= cutoff:
            available += earned_here

    pending = total - available
    return total, available, pending

def calculate_business_grand_total(business):
    ref_code = business.referral_code
    all_txns = BusinessTransaction.query.all()
    total = Decimal(0)
    for t in all_txns:
        if t.business_referral_id == ref_code and (t.cash_back or 0) > 0:
            total += Decimal(t.cash_back or 0)
        if t.tier2_business_referral_id == ref_code and (t.tier2_commission or 0) > 0:
            total += Decimal(t.tier2_commission or 0)
        if t.tier3_business_referral_id == ref_code and (t.tier3_commission or 0) > 0:
            total += Decimal(t.tier3_commission or 0)
        if t.tier4_business_referral_id == ref_code and (t.tier4_commission or 0) > 0:
            total += Decimal(t.tier4_commission or 0)
        if t.tier5_business_referral_id == ref_code and (t.tier5_commission or 0) > 0:
            total += Decimal(t.tier5_commission or 0)
        if hasattr(t, "sponsoree_mutual_referral_id") and t.sponsoree_mutual_referral_id == ref_code and (t.sponsoree_mutual_commission or 0) > 0:
            total += Decimal(t.sponsoree_mutual_commission or 0)
    return total

from datetime import datetime, timedelta

def calculate_business_earnings_split(business, delay_days=7):
    """
    Returns (total_earnings, available_earnings, pending_earnings) for a business,
    based on its referral_code and BusinessTransaction rows.
    Earnings are available if transaction.date_time <= now - delay_days.
    """
    ref_code = business.referral_code
    all_txns = BusinessTransaction.query.all()

    total = Decimal(0)
    available = Decimal(0)

    cutoff = datetime.utcnow() - timedelta(days=delay_days)

    for t in all_txns:
        earned_here = Decimal(0)

        # Tier 1: business's own cashback
        if t.business_referral_id == ref_code and (t.cash_back or 0) > 0:
            earned_here += Decimal(t.cash_back or 0)

        # Tiers 2-5 commissions
        if t.tier2_business_referral_id == ref_code and (t.tier2_commission or 0) > 0:
            earned_here += Decimal(t.tier2_commission or 0)
        if t.tier3_business_referral_id == ref_code and (t.tier3_commission or 0) > 0:
            earned_here += Decimal(t.tier3_commission or 0)
        if t.tier4_business_referral_id == ref_code and (t.tier4_commission or 0) > 0:
            earned_here += Decimal(t.tier4_commission or 0)
        if t.tier5_business_referral_id == ref_code and (t.tier5_commission or 0) > 0:
            earned_here += Decimal(t.tier5_commission or 0)

        # Mutual sponsoree commission (0.25%) if used
        if getattr(t, "sponsoree_mutual_referral_id", None) == ref_code and (t.sponsoree_mutual_commission or 0) > 0:
            earned_here += Decimal(t.sponsoree_mutual_commission or 0)

        if earned_here == 0:
            continue

        total += earned_here

        if t.date_time <= cutoff:
            available += earned_here

    pending = total - available
    return total, available, pending

def get_featured_businesses(lat, lng):
    # 1. Find nearby businesses within 10 miles using the haversine formula
    RADIUS = 10  # miles
    N_FEATURED = 10

    haversine = (
        3959 * func.acos(
            func.least(
                1.0,
                func.cos(func.radians(lat)) *
                func.cos(func.radians(Business.latitude)) *
                func.cos(func.radians(Business.longitude) - func.radians(lng)) +
                func.sin(func.radians(lat)) *
                func.sin(func.radians(Business.latitude))
            )
        )
    )

    # 2. Filter all approved businesses within radius
    all_nearby = Business.query \
        .filter(Business.status == "approved") \
        .filter(Business.latitude.isnot(None), Business.longitude.isnot(None)) \
        .add_columns(haversine.label('distance')) \
        .filter(haversine <= RADIUS) \
        .all()

    # Pull the Business objects only
    businesses = [b for (b, d) in all_nearby]

    # 3. Manually featured businesses in this group
    manual_featured = [b for (b, d) in all_nearby if b.manual_feature]

    # If 10 or more manuals, use only those
    if len(manual_featured) >= N_FEATURED:
        featured = manual_featured[:N_FEATURED]
        return featured

    # 4. Calculate rank for the rest
    # Metrics for all in range
    tx_counts = {b.id: BusinessTransaction.query.filter_by(business_referral_id=b.referral_code).count() for b in businesses}
    max_tx = max(tx_counts.values() or [1])  # default to 1 if empty

    ad_fees = {b.id: float(db.session.query(func.coalesce(func.sum(BusinessTransaction.ad_fee), 0)).filter_by(business_referral_id=b.referral_code).scalar()) for b in businesses}
    max_ad_fee = max(ad_fees.values() or [1])

    # Direct referrals: count how many businesses have this biz as sponsor
    referrals = {b.id: Business.query.filter_by(sponsor_id=b.id).count() for b in businesses}
    max_referrals = max(referrals.values() or [1])

    # Compute rank (1,000 pt scale)
    for b in businesses:
        tx_score = (tx_counts[b.id] / max_tx) * 250 if max_tx else 0
        ad_score = (ad_fees[b.id] / max_ad_fee) * 150 if max_ad_fee else 0
        ref_score = (referrals[b.id] / max_referrals) * 600 if max_referrals else 0
        b.rank = round(tx_score + ad_score + ref_score, 2)

    # 5. Exclude manuals, sort all others by rank (desc), fill up to 10
    remaining = [b for b in businesses if not b.manual_feature]
    ranked = sorted(remaining, key=lambda b: b.rank, reverse=True)
    n_needed = N_FEATURED - len(manual_featured)
    featured = manual_featured + ranked[:n_needed]

    # Always max 10
    return featured[:N_FEATURED]

def add_monthly_investor_earnings(user, year, month, investment_amount, rate):
    # Calculate this month’s earnings
    new_earnings = investment_amount * rate

    # Insert a row in investor_earnings
    earning = InvestorEarnings(
        user_id=user.id,
        year=year,
        month=month,
        amount=new_earnings,
        created_at=datetime.now()
    )
    db.session.add(earning)

    # Update running totals on the user
    user.investor_total_earnings = (user.investor_total_earnings or 0) + new_earnings
    user.investor_earnings_balance = (user.investor_earnings_balance or 0) + new_earnings

    db.session.commit()
    return new_earnings

def distribute_investor_earnings_per_transaction(transaction):
    total_revenue = transaction.amount  # e.g., $100
    charity = total_revenue * 0.105
    after_charity = total_revenue - charity

    payouts = transaction.cashback + transaction.commissions  # use actual values for this tx
    net_gross = after_charity - payouts

    silent_investor_pool = net_gross * 0.30

    silent_investors = User.query.join(User.roles).filter(Role.name == 'silent_investor').all()
    for user in silent_investors:
        share = float(user.investor_share or 0)
        if share > 0:
            payout = silent_investor_pool * share
            add_monthly_investor_earnings(
                user,
                transaction.date_time.year,
                transaction.date_time.month,
                payout,
                rate=1.0
            )

def calculate_investor_earnings_split(user, delay_days=7):
    """
    Returns (total_earnings, available_earnings, pending_earnings) for a silent investor.
    Uses InvestorEarnings.created_at and a delay window.
    """
    total = Decimal("0")
    available = Decimal("0")

    cutoff = datetime.utcnow() - timedelta(days=delay_days)

    earnings = InvestorEarnings.query.filter_by(user_id=user.id).all()
    for e in earnings:
        amount = Decimal(str(e.amount or 0))
        if amount <= 0:
            continue
        total += amount
        if (e.created_at or datetime.utcnow()) <= cutoff:
            available += amount

    pending = total - available
    return total, available, pending

def biz_tier_commission(t, tier_field, ref_field):
    ref_id = getattr(t, ref_field)
    if ref_id and str(ref_id).strip() and ref_id != "BIZPerkMiner":
        return getattr(t, tier_field) or 0
    return 0

# Add others (interaction, message, etc.) as needed here

# ---------------- STORE ROUTES ----------------

@app.route('/store_terms', methods=['GET', 'POST'])
@business_login_required
def store_terms():
    themes = Theme.query.all()
    biz_id = session.get('business_id')
    business = Business.query.get(biz_id)

    if request.method == 'POST':
        # Save theme_type and the agreement
        selected_theme = request.form.get('theme_type', '')
        if not selected_theme:
            flash('Please select a store theme.', 'danger')
            return render_template('store_terms.html', themes=themes, business=business)
        business.theme_type = selected_theme
        db.session.commit()

        # Check the agreement too (your checkbox logic, already in your template)
        agreed = request.form.get('agree_checkbox')
        if agreed == "on":
            return redirect(url_for('store_payment'))
        else:
            flash('You must accept the terms and conditions to continue.', 'danger')
            return render_template('store_terms.html', themes=themes, business=business)

    return render_template('store_terms.html', themes=themes, business=business)

@app.route('/store_payment')
@business_login_required
def store_payment():
    biz_id = session.get('business_id')
    biz = Business.query.get(biz_id)
    if not biz:
        flash("Business not found or not logged in!", "danger")
        return redirect(url_for("business_login"))

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='subscription',  # use 'payment' for a one-time charge
            line_items=[{
                'price': 'price_1T3hrXCLrqYII4jepU2edWdw',  # <-- Use your Stripe PRICE ID here!
                'quantity': 1,
            }],
            customer_email=biz.business_email,
            success_url=YOUR_DOMAIN + '/store_payment_success',
            cancel_url=YOUR_DOMAIN + '/store_terms',
        )
        return redirect(checkout_session.url)
    except Exception as e:
        import logging
        logging.error(f"Stripe checkout session creation failed: {e}")
        flash("There was a problem redirecting to payment. Please try again or contact support.", "danger")
        return redirect(url_for("store_terms"))

@app.route('/store_admin', methods=['GET', 'POST'])
@business_login_required
def store_admin():
    biz_id = session.get('business_id')
    biz = Business.query.get(biz_id)
    if request.method == 'POST':
        slug = request.form.get('store_slug', '').lower().strip()
        # Basic slug validation
        if not slug or not re.match(r'^[a-z0-9-]{3,60}$', slug):
            flash("Please use only lowercase letters, numbers, or dashes for your store URL.", "danger")
            return redirect(url_for('store_admin'))
        # Check uniqueness
        exists = Business.query.filter(func.lower(Business.store_slug) == slug, Business.id != biz.id).first()
        if exists:
            flash("Sorry, that store URL is already taken. Please choose another.", "danger")
        else:
            biz.store_slug = slug
            db.session.commit()
            flash("Your store URL was saved!", "success")
            return redirect(url_for('store_admin'))
    # ...existing context...
    return render_template('store_admin.html', business=biz)

@app.route('/store_payment_success')
@business_login_required
def store_payment_success():
    biz_id = session.get('business_id')
    biz = Business.query.get(biz_id)
    if not biz:
        flash("Business not found or not logged in!", "danger")
        return redirect(url_for("business_login"))

    # If the store has just been activated
    if not biz.has_ecommerce_store:
        biz.has_ecommerce_store = True

        # Now copy theme code to builder fields if blank
        if biz.theme_type:
            theme = Theme.query.filter_by(name=biz.theme_type).first()
            if theme:
                if not biz.grapesjs_html:
                    biz.grapesjs_html = theme.starter_html or ""
                if not biz.grapesjs_css:
                    biz.grapesjs_css = theme.homepage_css or ""
                if not biz.products_html:
                    biz.products_html = theme.products_html or ""
                if not biz.products_css:
                    biz.products_css = theme.products_css or ""
                if not biz.contact_html:
                    biz.contact_html = theme.contact_html or ""
                if not biz.contact_css:
                    biz.contact_css = theme.contact_css or ""
        else:
            flash("No theme selected — using default starter. You can change your theme and save any time.", "warning")

        db.session.commit()
        flash('Your online store is now active! You can set it up from your dashboard.', 'success')
    else:
        flash('Your online store subscription is already active.', 'info')

    return redirect(url_for('business_dashboard'))

@app.route('/store_builder', methods=['GET', 'POST'])
@business_login_required
def store_builder():
    import json

    biz_id = session.get('business_id')
    biz = Business.query.get(biz_id)
    themes = Theme.query.all()

    if request.method == 'POST':
        # CSRF protection (if using Flask-WTF, you could also use FlaskForm for the builder form)
        csrf_token_form = request.form.get('csrf_token')
        csrf_token_real = session.get('_csrf_token')
        if csrf_token_form != csrf_token_real:
            flash("Invalid CSRF token. Please reload the page and try again.", "danger")
            return redirect(url_for('store_builder'))

        # Save content for all builder pages on POST
        biz.grapesjs_html    = request.form.get('home_html', '') or ''
        biz.grapesjs_css     = request.form.get('home_css', '') or ''
        biz.grapesjs_js      = request.form.get('home_js', '') or ''
        biz.products_html    = request.form.get('products_html', '') or ''
        biz.products_css     = request.form.get('products_css', '') or ''
        biz.products_js      = request.form.get('products_js', '') or ''
        biz.contact_html     = request.form.get('contact_html', '') or ''
        biz.contact_css      = request.form.get('contact_css', '') or ''
        biz.contact_js       = request.form.get('contact_js', '') or ''
        db.session.commit()
        flash("All pages saved!", "success")
        return redirect(url_for('store_builder'))

    # Prepare current saved page data for this business for builder reloading
    saved_pages = {
        "home": {
            "html": biz.grapesjs_html or "",
            "css": biz.grapesjs_css or "",
            "js": biz.grapesjs_js or ""
        },
        "products": {
            "html": biz.products_html or "",
            "css": biz.products_css or "",
            "js": biz.products_js or ""
        },
        "contact": {
            "html": biz.contact_html or "",
            "css": biz.contact_css or "",
            "js": biz.contact_js or ""
        }
    }

    # Theme starter maps (for builder theme switching)
    theme_html_map = {str(theme.id): theme.starter_html or "" for theme in themes}
    theme_css_map = {str(theme.id): theme.homepage_css or "" for theme in themes}
    theme_js_map = {str(theme.id): theme.homepage_js or "" for theme in themes}
    theme_products_html_map = {str(theme.id): theme.products_html or "" for theme in themes}
    theme_products_css_map = {str(theme.id): theme.products_css or "" for theme in themes}
    theme_products_js_map = {str(theme.id): theme.products_js or "" for theme in themes}
    theme_contact_html_map = {str(theme.id): theme.contact_html or "" for theme in themes}
    theme_contact_css_map = {str(theme.id): theme.contact_css or "" for theme in themes}
    theme_contact_js_map = {str(theme.id): theme.contact_js or "" for theme in themes}

    # Preview values for theme gallery/quick preview
    preview_themes = {
        str(theme.id): {
            "home": theme.preview_home or "",
            "home_css": theme.preview_home_css or "",
            "home_js": theme.preview_home_js or "",
            "products": theme.preview_products or "",
            "products_css": theme.preview_products_css or "",
            "products_js": theme.preview_products_js or "",
            "contact": theme.preview_contact or "",
            "contact_css": theme.preview_contact_css or "",
            "contact_js": theme.preview_contact_js or "",
        }
        for theme in themes
    }

    # Generate a CSRF token for use in your builder form (if not using FlaskForm for the page)
    def generate_csrf_token():
        import secrets
        token = secrets.token_hex(18)
        session['_csrf_token'] = token
        return token

    if '_csrf_token' not in session:
        generate_csrf_token()

    return render_template(
        'store_builder.html',
        business=biz,
        themes=themes,
        saved_pages=json.dumps(saved_pages),
        theme_html_map=json.dumps(theme_html_map),
        theme_css_map=json.dumps(theme_css_map),
        theme_js_map=json.dumps(theme_js_map),
        theme_products_html_map=json.dumps(theme_products_html_map),
        theme_products_css_map=json.dumps(theme_products_css_map),
        theme_products_js_map=json.dumps(theme_products_js_map),
        theme_contact_html_map=json.dumps(theme_contact_html_map),
        theme_contact_css_map=json.dumps(theme_contact_css_map),
        theme_contact_js_map=json.dumps(theme_contact_js_map),
        preview_themes=json.dumps(preview_themes),
        csrf_token=session['_csrf_token'],
    )

@app.route('/save_homepage', methods=['POST'])
@login_required
def save_homepage():
    data = request.get_json()
    homepage_html = data.get('homepage_html', '')
    homepage_css = data.get('homepage_css', '')
    business = Business.query.filter_by(user_id=current_user.id).first()
    if business:
        business.homepage_html = homepage_html
        business.homepage_css = homepage_css
        db.session.commit()
        return jsonify({'success': True}), 200
    return jsonify({'success': False, 'error': 'Business not found'}), 404

from flask import render_template_string

@app.route('/stores/<store_slug>')
def public_storefront(store_slug):
    biz = Business.query.filter_by(
        store_slug=store_slug,
        has_ecommerce_store=True,
        website_approved=True
    ).first()
    if not biz or not biz.grapesjs_html:
        return render_template('storefront_coming_soon.html', business=biz), 404

    products = Product.query.filter_by(business_id=biz.id).all()
    rendered_html = render_template_string(
        biz.grapesjs_html or "",
        business=biz,
        products=products,
        cart_count=get_cart_count(biz.id)  # define this function if you want live cart badge!
    )
    return render_template(
        'public_storefront.html',
        business=biz,
        products=products,
        builder_html=rendered_html
    )

@app.route('/stores/<store_slug>/products')
def public_store_products(store_slug):
    biz = Business.query.filter_by(
        store_slug=store_slug,
        has_ecommerce_store=True,
        website_approved=True
    ).first()
    if not biz or not biz.products_html:
        return render_template('storefront_coming_soon.html', business=biz), 404

    products = Product.query.filter_by(business_id=biz.id).all()
    # Render the products HTML with real data
    rendered_products_html = render_template_string(
        biz.products_html,
        business=biz,
        products=products,
        cart_count=get_cart_count(biz.id) if "get_cart_count" in globals() else 0  # define get_cart_count if needed
    )

    return render_template(
        'store_products_public.html',
        business=biz,
        products=products,
        builder_html=rendered_products_html
    )

@app.route('/stores/<store_slug>/contact')
def public_store_contact(store_slug):
    biz = Business.query.filter_by(
        store_slug=store_slug, 
        has_ecommerce_store=True, 
        website_approved=True
    ).first()
    if not biz or not biz.contact_html:
        return render_template('storefront_coming_soon.html', business=biz), 404

    # Render contact HTML with dynamic context
    rendered_contact_html = render_template_string(
        biz.contact_html,
        business=biz
    )

    return render_template(
        'public_store_contact.html',
        business=biz,
        contact_html=rendered_contact_html
    )

@app.route('/stores/<store_slug>/checkout', methods=['GET', 'POST'])
def public_store_checkout(store_slug):
    biz = Business.query.filter_by(
        store_slug=store_slug,
        has_ecommerce_store=True,
        website_approved=True
    ).first()
    if not biz:
        return render_template('storefront_coming_soon.html', business=biz), 404
    # Filter cart and products for this biz as in your view_cart logic.
    # (You may want to adjust or clone your existing checkout.html template.)
    # Example:
    cart = get_cart()
    product_ids = [int(pid) for pid in cart.keys()]
    products = Product.query.filter(Product.id.in_(product_ids), Product.business_id==biz.id).all()
    cart_items = []
    total = 0
    for p in products:
        qty = cart[str(p.id)]
        line_total = qty * p.price
        total += line_total
        cart_items.append({"product": p, "quantity": qty, "line_total": line_total})
    # ... Insert coupon logic, grand_total, form, Stripe, etc.
    # If POST, process payment as in your global checkout, but only for products for this business.
    return render_template('store_checkout.html', business=biz,
        cart_items=cart_items, total=total, # add more as needed
    )

@app.route('/stores/<store_slug>/thank_you')
def public_store_thank_you(store_slug):
    biz = Business.query.filter_by(store_slug=store_slug).first()
    return render_template('store_thank_you.html', business=biz)

@app.route('/stores/<store_slug>/cart')
def public_store_cart(store_slug):
    biz = Business.query.filter_by(store_slug=store_slug).first()
    # Filter cart for this business as in /view_cart
    # Render as normal using store/branded template if needed
    # ...
    return render_template('store_cart.html', business=biz)

@app.route('/public_store/<int:biz_id>')
def public_store(biz_id):
    biz = Business.query.get_or_404(biz_id)
    # get products as before
    return render_template('public_store.html', biz=biz, products=products, theme=theme)

@app.route('/contact/<int:business_id>')
def contact_page(business_id):
    # Get this business and its theme
    business = Business.query.get_or_404(business_id)
    # If your business has a foreign key to Theme, use business.theme.contact_html
    # Or manually look up a theme if not linked
    theme = Theme.query.get(business.theme_id) if hasattr(business, 'theme_id') else None
    contact_html = theme.contact_html if theme and theme.contact_html else "<p>Contact page template missing.</p>"
    return render_template(
        'contact_page.html',
        business=business,
        contact_html=contact_html
    )

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
MAX_FILE_SIZE_MB = 4

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def file_size_okay(file):
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    return size <= MAX_FILE_SIZE_MB * 1024 * 1024

@app.route('/store_products', methods=['GET', 'POST'])
@business_login_required
def store_products():
    biz_id = session.get('business_id')
    biz = Business.query.get(biz_id)
    if not biz:
        flash("Business not found or not logged in!", "danger")
        return redirect(url_for("business_login"))

    products = Product.query.filter_by(business_id=biz.id).limit(50).all()

    if request.method == 'POST':
        name = request.form.get('name')
        price = request.form.get('price')
        description = request.form.get('description')
        image_file = request.files.get('image_file')
        stock = request.form.get('stock')
        featured = request.form.get('featured') == 'yes'
        image_path = None

        # Validate and upload image to Cloudinary if provided
        if image_file and image_file.filename:
            if not allowed_file(image_file.filename):
                flash("Invalid image type! Allowed: png, jpg, jpeg, gif", "danger")
                return redirect(url_for('store_products'))
            if not file_size_okay(image_file):
                flash(f"Image file too large! Max size is {MAX_FILE_SIZE_MB}MB.", "danger")
                return redirect(url_for('store_products'))
            # Upload to Cloudinary
            try:
                # Cloudinary auto-detects file type and secures filename
                result = cloudinary.uploader.upload(
                    image_file,
                    folder="perkminer_uploads",
                    resource_type="image",
                    allowed_formats=list(ALLOWED_EXTENSIONS),
                    transformation=[
                        {"width": 800, "height": 800, "crop": "limit"}
                    ]
                )
                image_path = result.get('secure_url')
            except Exception as e:
                flash(f"Cloudinary upload failed: {e}", "danger")
                return redirect(url_for('store_products'))

        if name and price and len(products) < 50:
            new_product = Product(
                business_id=biz.id,
                name=name,
                price=float(price),
                description=description,
                image_path=image_path,
                stock=int(stock) if stock else 0,
                featured=featured
            )
            db.session.add(new_product)
            db.session.commit()
            flash("Product added successfully!", "success")
            return redirect(url_for('store_products'))
        elif len(products) >= 50:
            flash("Limit reached: 50 products max.", "danger")
    return render_template('store_products.html', business=biz, products=products)

@app.route('/edit_product/<int:product_id>', methods=['GET', 'POST'])
@business_login_required
def edit_product(product_id):
    product = Product.query.get_or_404(product_id)
    biz_id = session.get('business_id')
    if product.business_id != biz_id:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('store_products'))
    if request.method == 'POST':
        product.name = request.form.get('name')
        product.price = float(request.form.get('price') or product.price)
        product.description = request.form.get('description')
        product.image_url = request.form.get('image_url')
        product.stock = int(request.form.get('stock') or product.stock)
        db.session.commit()
        flash("Product updated!", "success")
        return redirect(url_for('store_products'))
    return render_template('edit_product.html', product=product)

@app.route('/delete_product/<int:product_id>', methods=['POST'])
@business_login_required
def delete_product(product_id):
    product = Product.query.get_or_404(product_id)
    biz_id = session.get('business_id')
    if product.business_id != biz_id:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('store_products'))
    db.session.delete(product)
    db.session.commit()
    flash("Product deleted.", "success")
    return redirect(url_for('store_products'))

@app.route('/buy_product/<int:product_id>')
def buy_product(product_id):
    product = Product.query.get_or_404(product_id)
    if product.stock is not None and product.stock <= 0:
        flash("Sorry, this product is out of stock.", "danger")
        return redirect(request.referrer or '/')
    biz = Business.query.get(product.business_id)
    if not biz or not biz.stripe_account_id:
        flash("This business cannot accept payment online yet.", "danger")
        return redirect(request.referrer or '/')

    amount_usd = product.price
    ad_fee = int(amount_usd * 100 * 0.10)  # 10% fee to platform, in cents

    import stripe
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='payment',
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': product.name,
                        'description': product.description or '',
                    },
                    'unit_amount': int(amount_usd * 100),  # in cents
                },
                'quantity': 1,
            }],
            payment_intent_data={
                'application_fee_amount': ad_fee,
                'transfer_data': {
                    'destination': biz.stripe_account_id,
                },
            },
            metadata={
                "product_id": str(product.id),
                "business_id": str(biz.id),
            },
            customer_email=request.args.get('buyer_email'),
            success_url=YOUR_DOMAIN + '/stores/' + biz.store_slug + '/thank_you',
            cancel_url=request.referrer or (YOUR_DOMAIN + '/stores/' + biz.store_slug + '/products'),
        )
        return redirect(checkout_session.url)
    except Exception as e:
        import logging
        logging.error(f"Stripe checkout session failed: {e}")
        flash("Could not start checkout. Please try again.", "danger")
        return redirect(request.referrer or '/')

@app.route('/store_orders')
@business_login_required
def store_orders():
    biz_id = session.get('business_id')
    biz = Business.query.get(biz_id)
    if not biz:
        flash("Business not found or not logged in!", "danger")
        return redirect(url_for("business_login"))
    orders = Order.query.filter_by(business_id=biz.id).order_by(Order.timestamp.desc()).all()
    # Useful to join with Product for display
    product_map = {p.id: p for p in Product.query.filter_by(business_id=biz.id).all()}
    return render_template('store_orders.html', orders=orders, product_map=product_map)

# ---------------- CART & COUPON ROUTES ----------------

@app.route('/add_to_cart/<int:product_id>', methods=['POST'])
def add_to_cart_route(product_id):
    add_to_cart(product_id)
    flash("Added to cart!", "success")
    return redirect(request.referrer or url_for('view_cart'))

@app.route('/remove_from_cart/<int:product_id>', methods=['POST'])
def remove_from_cart_route(product_id):
    remove_from_cart(product_id)
    flash("Removed from cart.", "success")
    return redirect(url_for('view_cart'))

@app.route('/cart')
def view_cart():
    cart = get_cart()
    product_ids = [int(pid) for pid in cart.keys()]
    products = Product.query.filter(Product.id.in_(product_ids)).all() if product_ids else []
    cart_items = []
    total = 0
    for p in products:
        qty = cart[str(p.id)]
        line_total = qty * p.price
        total += line_total
        cart_items.append({
            "product": p,
            "quantity": qty,
            "line_total": line_total
        })

    valid_coupons = get_valid_coupons()
    coupon_code = session.get('applied_coupon')
    discount_pct = valid_coupons.get(coupon_code, 0) if coupon_code else 0
    discount = total * discount_pct
    grand_total = total - discount

    return render_template('cart.html',
        cart_items=cart_items,
        total=total,
        discount=discount,
        grand_total=grand_total,
        valid_coupons=valid_coupons
    )

@app.route('/apply_coupon', methods=['POST'])
def apply_coupon():
    code = request.form.get('coupon_code', '').strip().upper()
    valid_coupons = get_valid_coupons()
    if code in valid_coupons:
        session['applied_coupon'] = code
        flash(f"Coupon '{code}' applied! Discount: {int(valid_coupons[code]*100)}% off", "success")
    else:
        session.pop('applied_coupon', None)
        flash(f"Coupon '{code}' is not valid.", "warning")
    return redirect(url_for('view_cart'))

# ---------------- CHECKOUT & STRIPE INTEGRATION ----------------
@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    cart = get_cart()
    if not cart:
        flash("Your cart is empty.", "info")
        return redirect(url_for('view_cart'))

    class CheckoutForm(Form):
        email = StringField('Your Email', [DataRequired(), Email()])

    form = CheckoutForm(request.form)

    product_ids = [int(pid) for pid in cart.keys()]
    products = Product.query.filter(Product.id.in_(product_ids)).all()
    cart_items = []
    total = 0
    for p in products:
        qty = cart[str(p.id)]
        line_total = qty * p.price
        total += line_total
        cart_items.append({
            "product": p,
            "quantity": qty,
            "line_total": line_total
        })

    valid_coupons = get_valid_coupons()
    coupon_code = session.get('applied_coupon')
    discount_pct = valid_coupons.get(coupon_code, 0) if coupon_code else 0
    discount = total * discount_pct
    grand_total = total - discount

    if request.method == 'POST' and form.validate():
        session['checkout_email'] = form.email.data
        return redirect(url_for('start_cart_checkout'))

    return render_template('checkout.html',
        form=form,
        cart_items=cart_items,
        total=total,
        discount=discount,
        grand_total=grand_total
    )

@app.route('/start_cart_checkout')
def start_cart_checkout():
    cart = session.get("cart", {})
    buyer_email = session.get("checkout_email")
    if not cart:
        flash("Your cart is empty.", "info")
        return redirect(url_for('view_cart'))

    product_ids = [int(pid) for pid in cart.keys()]
    products = Product.query.filter(Product.id.in_(product_ids)).all()
    if not products:
        flash("No valid products in cart.", "warn")
        return redirect(url_for('view_cart'))

    business_ids = {p.business_id for p in products}
    if len(business_ids) != 1:
        flash("All items in your cart must be from the same business.", "danger")
        return redirect(url_for('view_cart'))

    biz_id = list(business_ids)[0]
    biz = Business.query.get(biz_id)
    if not biz or not biz.stripe_account_id:
        flash("This business cannot accept payment online yet.", "danger")
        return redirect(url_for('view_cart'))

    valid_coupons = get_valid_coupons()
    coupon_code = session.get('applied_coupon')
    discount_pct = valid_coupons.get(coupon_code, 0) if coupon_code else 0

    # --- Prepare line_items and totals ---
    line_items = []
    subtotal = 0
    for p in products:
        qty = cart[str(p.id)]
        price_cents = int(p.price * 100)
        line_total = qty * p.price
        subtotal += line_total
        line_items.append({
            'price_data': {
                'currency': 'usd',
                'product_data': {
                    'name': p.name,
                    'description': p.description or '',
                },
                'unit_amount': price_cents,
            },
            'quantity': qty,
        })

    discount = subtotal * discount_pct
    grand_total = subtotal - discount
    ad_fee = int(grand_total * 0.10 * 100)   # 10% of cart total in cents

    metadata = {
        "cart_items": ",".join(str(p.id) for p in products),
        "business_id": str(biz.id),
        "coupon_code": coupon_code or '',
    }

    import stripe
    try:
        # Create CHECKOUT session for everything in the cart from this business
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='payment',
            line_items=line_items,
            payment_intent_data={
                'application_fee_amount': ad_fee,         # 10% platform fee in cents
                'transfer_data': {
                    'destination': biz.stripe_account_id,
                },
            },
            metadata=metadata,
            customer_email=buyer_email,
            success_url=YOUR_DOMAIN + '/stores/' + biz.store_slug + '/thank_you',
            cancel_url=YOUR_DOMAIN + '/stores/' + biz.store_slug + '/cart',
        )
        return redirect(checkout_session.url)
    except Exception as e:
        import logging
        logging.error(f"Stripe cart checkout session failed: {e}")
        flash("Could not start checkout. Please try again.", "danger")
        return redirect(url_for('checkout'))

# -------------- STRIPE WEBHOOK (Order, Stock, Emails) --------------

@app.route('/stripe_webhook', methods=['POST'])
@csrf.exempt
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400

    if event['type'] == 'checkout.session.completed':
        session_obj = event['data']['object']
        amount = (session_obj['amount_total'] if 'amount_total' in session_obj else 0) / 100.0
        metadata = session_obj['metadata'] if 'metadata' in session_obj else {}
        business_id = metadata['business_id'] if 'business_id' in metadata else None
        buyer_email = session_obj['customer_email'] if 'customer_email' in session_obj else None

        business = Business.query.get(business_id) if business_id else None
    
        import logging
        logging.warning(f"WEBHOOK received: metadata={str(metadata)}, business_id={business_id}, found_business={bool(business)}, amount={amount}")

        # Fund Account: Update account balance
        if ('purpose' in metadata and metadata['purpose'] == 'fund_account') and business:
            old_balance = business.account_balance or 0
            business.account_balance = old_balance + amount
            db.session.commit()
            logging.warning(f"[FUND HOOK] Old: {old_balance}, Amount: {amount}, New: {business.account_balance}")
            logging.info(f"Business {business.id} funded account with ${amount:.2f}")

        # Store Purchase: Old logic
        if business and getattr(business, "has_ecommerce_store", False):
            try:
                # Rewards/cashback/commissions
                issue_store_sale_rewards(business, amount, buyer_email=buyer_email)
            except Exception as e:
                import logging
                logging.error(f"[REWARD HOOK FAIL] {e}")

            # CREATE ORDERS: Cart or single-product
            products = []
            if 'cart_items' in metadata and metadata['cart_items']:
                product_ids = [int(pid) for pid in metadata['cart_items'].split(',') if pid]
                products = Product.query.filter(Product.id.in_(product_ids)).all()
            elif 'product_id' in metadata and metadata['product_id']:
                product = Product.query.get(int(metadata['product_id']))
                if product:
                    products = [product]

            for product in products:
                qty = 1  # Adjust if you store per-product qty in cart/order metadata!
                # Reduce stock if tracked
                if product.stock is not None:
                    product.stock = max(0, product.stock - qty)
                # DIGITAL FULFILLMENT: Assign a download token if digital
                fulfillment_token = None
                download_url = None
                is_digital = getattr(product, "is_digital", False)

                if is_digital:
                    fulfillment_token = secrets.token_urlsafe(32)
                    download_url = url_for('download_product', order_token=fulfillment_token, _external=True)
                
                order = Order(
                    business_id=business.id,
                    product_id=product.id,
                    buyer_email=buyer_email,
                    amount=product.price * qty,
                    stripe_checkout_id=session_obj['id'] if 'id' in session_obj else None,
                    timestamp=datetime.utcnow(),
                    status="paid",
                    fulfillment_token=fulfillment_token
                )
                db.session.add(order)

                # BUSINESS NOTIFICATION EMAIL
                try:
                    send_order_alert(
                        business.business_email,
                        product.name,
                        amount=product.price * qty,
                        buyer_email=buyer_email
                    )
                except Exception as e:
                    import logging
                    logging.error(f"Order email failed: {e}")

                # CUSTOMER RECEIPT AND DIGITAL DELIVERY EMAIL
                try:
                    send_customer_receipt(
                        buyer_email,
                        product.name,
                        product.price * qty,
                        business.business_name,
                        download_url=download_url
                    )
                except Exception as e:
                    import logging
                    logging.error(f"Customer receipt email failed: {e}")

            db.session.commit()

    return '', 200

# --------------------- THANK YOU PAGE -------------------
@app.route('/thank_you')
def thank_you():
    session.pop('cart', None)
    return render_template('thank_you.html')

# -------------------- CUSTOMER ORDER LOOKUP (Simple) -----------------
@app.route('/order_lookup', methods=['GET', 'POST'])
def order_lookup():
    found_orders = []
    show_results = False
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        order_number = request.form.get('order_number')
        q = Order.query
        if email:
            q = q.filter(Order.buyer_email.ilike(email))
        if order_number:
            try:
                order_id_int = int(order_number)
                q = q.filter(Order.id == order_id_int)
            except ValueError:
                pass
        found_orders = q.order_by(Order.timestamp.desc()).all()
        show_results = True
    return render_template('order_lookup.html', found_orders=found_orders, show_results=show_results)

# --------- ORDER FULFILLMENT (BUSINESS ONLY, STUB) ----------

@app.route('/fulfill_order/<int:order_id>', methods=['POST'])
@business_login_required
def fulfill_order(order_id):
    order = Order.query.get_or_404(order_id)
    biz_id = session.get('business_id')
    if order.business_id != biz_id:
        flash("Unauthorized.", "danger")
        return redirect(url_for('store_orders'))
    order.status = "fulfilled"
    # Optionally add tracking, notes, etc.
    db.session.commit()
    # Optionally: send another customer notification here.
    flash("Order marked as fulfilled.", "success")
    return redirect(url_for('store_orders'))

# --------- DIGITAL PRODUCT DOWNLOAD (Stub) ----------

@app.route('/download/<order_token>')
def download_product(order_token):
    order = Order.query.filter_by(fulfillment_token=order_token).first()
    if not order or order.status not in ['paid', 'fulfilled']:
        flash("Invalid or expired download link.", "danger")
        return redirect(url_for('customer_orders'))
    product = Product.query.get(order.product_id)
    if not product or not getattr(product, "download_url", None):
        return render_template('download.html', file_url=None, expiration_text=None)  # Show failure
    # Optionally: set download expiry, etc.
    return render_template('download.html', file_url=product.download_url, expiration_text=None)

# --------- ABANDONED CART REMINDER (STUB/ADMIN) ----------
@app.route('/abandoned_cart_email/<email>')
def abandoned_cart_email(email):
    # Admin/dev trigger: send test abandoned cart reminder
    return "Reminder sent (dev)"

# --------- UPSELLS/CROSS-SELL SUGGESTIONS (AJAX Stub) ----------
@app.route('/upsell_suggest')
def upsell_suggest():
    # TODO: recommend products based on cart/items
    return jsonify({'suggested': []})

# --------- BUSINESS ANALYTICS DASHBOARD (STUB) ----------
@app.route('/business/analytics')
@business_login_required
def analytics_dashboard():
    # Add stats/data context here as you expand
    return render_template('business_analytics.html')

# --------- CUSTOMER PORTAL & ORDER VIEWS (STUB) ----------
@app.route('/customer_portal', methods=['GET', 'POST'])
def customer_portal():
    # Customer logs in or uses magic link
    return render_template('customer_portal.html')

@app.route('/customer_orders')
def customer_orders():
    # Show all authenticated customer orders
    return render_template('customer_orders.html')

@app.route('/customer_order/<int:order_id>')
def customer_order_detail(order_id):
    # Show a single customer order in detail
    return render_template('customer_order_detail.html')

# --- 4. Product Variants & Options ---
@app.route('/product/<int:product_id>')
def product_detail(product_id):
    # Show single product, options/variants
    return render_template('product_detail.html')

# --- (Optional, for managing variants in admin) ---
@app.route('/edit_variant/<int:variant_id>', methods=['GET', 'POST'])
def edit_variant(variant_id):
    # Edit product variant (size/color/etc.)
    return render_template('edit_variant.html')

@app.route("/")
def home():
    approved_listings = Business.query.filter_by(status="approved").all()
    N_FEATURED = 10  # show 10 at a time

    # User/location detection (GET params: lat/lng)
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)
    search_radius = 10  # miles

    if lat is not None and lng is not None:
        haversine = (
            3959 * func.acos(
                func.least(
                    1.0,
                    func.cos(func.radians(lat)) *
                    func.cos(func.radians(Business.latitude)) *
                    func.cos(func.radians(Business.longitude) - func.radians(lng)) +
                    func.sin(func.radians(lat)) *
                    func.sin(func.radians(Business.latitude))
                )
            )
        )

        # Businesses in range
        all_nearby = Business.query.filter_by(status="approved") \
            .filter(Business.latitude.isnot(None), Business.longitude.isnot(None)) \
            .add_columns(haversine.label('distance')) \
            .filter(haversine <= search_radius) \
            .all()
        nearby_businesses = [b for b, d in all_nearby]
        business_ids = [b.id for b in nearby_businesses]

        # Manual featured first
        manual_featured = [b for b, d in all_nearby if b.manual_feature]
        if len(manual_featured) >= N_FEATURED:
            featured_listings = manual_featured[:N_FEATURED]
        else:
            # --- Calculate rank dynamically for each business in range ---
            # 1. Transactions
            tx_counts = {
                b.id: BusinessTransaction.query.filter_by(business_referral_id=b.referral_code).count()
                for b in nearby_businesses
            }
            max_tx = max(tx_counts.values() or [1])

            # 2. Ad fees
            ad_fees = {
                b.id: float(db.session.query(func.coalesce(func.sum(BusinessTransaction.ad_fee), 0))
                    .filter_by(business_referral_id=b.referral_code).scalar())
                for b in nearby_businesses
            }
            max_ad_fee = max(ad_fees.values() or [1])

            # 3. Direct referrals
            referrals = {
                b.id: Business.query.filter_by(sponsor_id=b.id).count()
                for b in nearby_businesses
            }
            max_ref = max(referrals.values() or [1])

            # Calculate rank for all non-manual-featured
            not_manual = [b for b in nearby_businesses if not b.manual_feature]
            for b in not_manual:
                tx_score = (tx_counts[b.id] / max_tx) * 250 if max_tx else 0
                ad_score = (ad_fees[b.id] / max_ad_fee) * 150 if max_ad_fee else 0
                ref_score = (referrals[b.id] / max_ref) * 600 if max_ref else 0
                b.rank = round(tx_score + ad_score + ref_score, 2)
            # Fill up with highest rank
            n_needed = N_FEATURED - len(manual_featured)
            ranked = sorted(not_manual, key=lambda b: b.rank, reverse=True)
            featured_listings = manual_featured + ranked[:n_needed]
            # Set distance_mi for display
            dist_lookup = {b.id: d for b, d in all_nearby}
            for b in featured_listings:
                b.distance_mi = round(dist_lookup.get(b.id, 0) or 0, 2)
    else:
        # No location: show 10, manual first, then highest rank
        manual_featured = Business.query.filter_by(status="approved", manual_feature=True).order_by(Business.rank.desc()).limit(N_FEATURED).all()
        needed = N_FEATURED - len(manual_featured)
        if needed > 0:
            ranked = Business.query.filter_by(status="approved", manual_feature=False).order_by(Business.rank.desc()).limit(needed).all()
            for b in manual_featured:
                b.distance_mi = None
            for b in ranked:
                b.distance_mi = None
            featured_listings = manual_featured + ranked
        else:
            for b in manual_featured:
                b.distance_mi = None
            featured_listings = manual_featured[:N_FEATURED]

    # ------------ Totals ------------
    user_transactions = UserTransaction.query.all()
    total_user_tier1 = sum(t.cash_back or 0 for t in user_transactions)
    total_user_commission = sum(
        (t.tier2_commission or 0) + (t.tier3_commission or 0) +
        (t.tier4_commission or 0) + (t.tier5_commission or 0)
        for t in user_transactions
    )
    total_user_biz_commission = sum(
        (t.tier1_business_user_commission or 0) +
        (t.tier2_business_user_commission or 0) +
        (t.tier3_business_user_commission or 0) +
        (t.tier4_business_user_commission or 0) +
        (t.tier5_business_user_commission or 0)
        for t in user_transactions
    )
    total_member_paid = total_user_tier1 + total_user_commission + total_user_biz_commission

    business_transactions = BusinessTransaction.query.all()
    total_biz_mutual_commission = sum(
        (t.sponsoree_mutual_commission or 0)
        for t in business_transactions
        if hasattr(t, "sponsoree_mutual_commission") and t.sponsoree_mutual_commission
    )

    total_biz_paid = sum(
        (t.cash_back or 0) +
        biz_tier_commission(t, "tier2_commission", "tier2_business_referral_id") +
        biz_tier_commission(t, "tier3_commission", "tier3_business_referral_id") +
        biz_tier_commission(t, "tier4_commission", "tier4_business_referral_id") +
        biz_tier_commission(t, "tier5_commission", "tier5_business_referral_id")
        for t in business_transactions
    )
    total_biz_paid += total_biz_mutual_commission

    main_btxns = [t for t in business_transactions if not t.sponsoree_mutual_referral_id]
    total_gross_sales = sum(t.amount or 0 for t in main_btxns)
    total_ad_fees = sum((t.ad_fee or 0) for t in main_btxns)

    total_paid_out = total_member_paid + total_biz_paid
    percent_fees_paid = ((total_paid_out / total_ad_fees) * 100) if total_ad_fees > 0 else 0

    return render_template(
        "home.html",
        approved_listings=approved_listings,
        featured_listings=featured_listings,
        total_member_paid=total_member_paid,
        total_biz_paid=total_biz_paid,
        total_biz_mutual_commission=total_biz_mutual_commission,
        total_gross_sales=total_gross_sales,
        total_ad_fees=total_ad_fees,
        total_paid_out=total_paid_out,
        percent_fees_paid=percent_fees_paid
    )

@app.route("/business")
def business_home():
    return render_template("business_home.html")

@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)
    distance = request.args.get("distance", "").strip()  # “5”, “10”, “25”, or “all”

    page = request.args.get("page", 1, type=int)
    per_page = 20

    # base query: approved businesses
    base_query = Business.query.filter_by(status="approved")

    if category:
        base_query = base_query.filter(Business.category == category)
    if q:
        base_query = base_query.filter(Business.search_keywords.ilike(f"%{q}%"))

    use_location = lat is not None and lng is not None

    if use_location:
        haversine = (
            3959 * func.acos(
                func.least(
                    1.0,
                    func.cos(func.radians(lat)) *
                    func.cos(func.radians(Business.latitude)) *
                    func.cos(func.radians(Business.longitude) - func.radians(lng)) +
                    func.sin(func.radians(lat)) *
                    func.sin(func.radians(Business.latitude))
                )
            )
        ).label('distance_mi')

        query = (
            base_query
            .filter(Business.latitude.isnot(None), Business.longitude.isnot(None))
            .add_columns(haversine)
        )

        # Filter by distance if set and not "all"
        if distance and distance != "all":
            try:
                dist_num = float(distance)
                query = query.filter(haversine <= dist_num)
            except ValueError:
                pass

        # Order by distance
        query = query.order_by(haversine)

        # paginate on the combined (Business, distance) rows
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        results_raw = pagination.items  # list of (Business, distance)

        listings = []
        for biz, d in results_raw:
            biz.distance_mi = round(d, 2) if d is not None else None
            listings.append(biz)

    else:
        # No lat/lng: paginate plain business query (by rank/name or however you like)
        pagination = (
            base_query
            .order_by(Business.rank.desc(), Business.business_name.asc())
            .paginate(page=page, per_page=per_page, error_out=False)
        )
        listings = pagination.items

    return render_template(
        "search_results.html",
        results=listings,
        q=q,
        category=category,
        lat=lat,
        lng=lng,
        selected_distance=distance,
        pagination=pagination
    )

@app.route("/category/<name>")
def category_browse(name):
    # Get user location from query params if provided
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)
    distance = request.args.get("distance", "").strip()

    page = request.args.get("page", 1, type=int)
    per_page = 20

    # Base query: only approved businesses in this category
    base_query = Business.query.filter_by(category=name, status="approved")

    use_location = lat is not None and lng is not None

    if use_location:
        haversine = (
            3959 * func.acos(
                func.least(
                    1.0,
                    func.cos(func.radians(lat)) *
                    func.cos(func.radians(Business.latitude)) *
                    func.cos(func.radians(Business.longitude) - func.radians(lng)) +
                    func.sin(func.radians(lat)) *
                    func.sin(func.radians(Business.latitude))
                )
            )
        ).label("distance_mi")

        query = (
            base_query
            .filter(Business.latitude.isnot(None), Business.longitude.isnot(None))
            .add_columns(haversine)
        )

        # If a max distance is set, limit results
        if distance and distance != "all":
            try:
                dist_num = float(distance)
                query = query.filter(haversine <= dist_num)
            except ValueError:
                pass

        # Sort by nearest
        query = query.order_by(haversine)

        # paginate combined (Business, distance) rows
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        results_raw = pagination.items  # list of (Business, distance)

        listings = []
        for biz, d in results_raw:
            biz.distance_mi = round(d, 2) if d is not None else None
            listings.append(biz)
    else:
        # No lat/lng: just paginate all in this category
        pagination = (
            base_query
            .order_by(Business.rank.desc(), Business.business_name.asc())
            .paginate(page=page, per_page=per_page, error_out=False)
        )
        listings = pagination.items

    return render_template(
        "category_results.html",
        results=listings,
        category=name,
        lat=lat,
        lng=lng,
        selected_distance=distance,
        pagination=pagination
    )

@app.route("/intro")
def intro():
    return render_template("intro.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    form = RegisterForm()
    # Populate referral_code field from ?ref= on GET (invite link)
    ref_code = request.args.get('ref')
    if request.method == "GET" and ref_code:
        form.referral_code.data = ref_code

    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        password = form.password.data
        referral_code = form.referral_code.data.strip()
        if not valid_password(password):
            flash(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
            return redirect(url_for("register"))
        if not valid_email(email):
            flash("Invalid email format.")
            return redirect(url_for("register"))
        if User.query.filter_by(email=email).first():
            flash("Email already registered.")
            return redirect(url_for("register"))
        sponsor_id = None
        if referral_code:
            sponsor = User.query.filter_by(referral_code=referral_code).first()
            if sponsor:
                sponsor_id = sponsor.id
            else:
                flash("Invalid referral code.")
                return redirect(url_for("register"))
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        user_ref_code = random_referral_code(email)
        email_code = random_email_code()
        new_user = User(
            email=email,
            password=hashed_pw,
            referral_code=user_ref_code,
            sponsor_id=sponsor_id,
            email_confirmed=False,
            email_code=email_code
        )
        db.session.add(new_user)
        db.session.commit()
        send_verification_email(new_user)
        session['pending_email'] = email
        session['last_verification_sent'] = time.time()
        return redirect(url_for("verify_email"))
    return render_template("register.html", form=form)

@app.route("/verify_email", methods=["GET", "POST"])
def verify_email():
    form = VerifyCodeForm()
    pending_email = session.get('pending_email')
    user = User.query.filter_by(email=pending_email).first() if pending_email else None
    can_resend = False
    wait_seconds = 0
    if not user:
        flash("No registration found to verify.")
        return redirect(url_for("register"))
    if user.email_confirmed:
        flash("Email is already confirmed, please log in.")
        return redirect(url_for("login"))
    now = time.time()
    last_sent = session.get('last_verification_sent', 0)
    if now - last_sent >= 30:
        can_resend = True
    else:
        wait_seconds = int(30 - (now - last_sent))
    if form.validate_on_submit():
        submitted_code = form.code.data.strip().upper()
        if submitted_code == user.email_code:
            user.email_confirmed = True
            db.session.commit()
            flash("Email confirmed! You can now log in.")
            session.pop("pending_email", None)
            return redirect(url_for("login"))
        else:
            flash("Incorrect code.")
    return render_template("verify_email.html", form=form, can_resend=can_resend, wait_seconds=wait_seconds)

@app.route("/resend_verification", methods=["POST"])
def resend_verification():
    pending_email = session.get('pending_email')
    user = User.query.filter_by(email=pending_email).first() if pending_email else None
    if user and not user.email_confirmed:
        user.email_code = random_email_code()
        db.session.commit()
        send_verification_email(user)
        session['last_verification_sent'] = time.time()
        flash("Verification email resent.")
    return redirect(url_for("verify_email"))

@app.route("/activate/<code>")
def activate(code):
    user = User.query.filter_by(email_code=code).first()
    if user:
        user.email_confirmed = True
        db.session.commit()
        flash("Email confirmed! You can now log in.")
        session.pop('pending_email', None)
        return redirect(url_for("login"))
    else:
        flash("Invalid or expired code.")
        return redirect(url_for("register"))

@app.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    message = ""
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        password = form.password.data
        user = User.query.filter_by(email=email).first()
        if user and bcrypt.check_password_hash(user.password, password):
            if user.is_suspended:
                flash('Account suspended, contact <a href="mailto:fromperkpay@gmail.com">Contact Support</a>.', 'danger')
                return redirect(url_for("login"))
            if not user.email_confirmed:
                message = "Please confirm your email first (check your inbox)."
                session['pending_email'] = email
                return redirect(url_for("verify_email"))
            else:
                # --- 2FA LOGIC ---
                code = str(random.randint(100000, 999999))
                session['pending_2fa_code'] = code
                session['pending_2fa_user_id'] = user.id
                send_email(
                    user.email,
                    "Your PerkMiner Login Code",
                    f"<p>Your PerkMiner login code is: <b>{code}</b></p>"
                )
                flash("A login code has been sent to your email.")
                return redirect(url_for("two_factor"))
        else:
            message = "Login failed. Check email and password."
    return render_template("login.html", message=message, form=form)

@app.route("/two_factor", methods=["GET", "POST"])
def two_factor():
    form = TwoFactorForm()
    if form.validate_on_submit():
        code_entered = form.code.data.strip()
        code_expected = session.get('pending_2fa_code')
        user_id = session.get('pending_2fa_user_id')
        if code_expected and code_entered == code_expected and user_id:
            user = User.query.get(user_id)
            if user:
                login_user(user)
                # Clean up session
                session.pop('pending_2fa_code', None)
                session.pop('pending_2fa_user_id', None)
                flash("Login successful!")
                return redirect(url_for("dashboard"))
        flash("Incorrect code. Please try again.")
    return render_template("two_factor.html", form=form)

@app.route("/resend_login_2fa", methods=["POST"])
def resend_login_2fa():
    user_id = session.get('pending_2fa_user_id')
    if user_id:
        user = User.query.get(user_id)
        if user:
            code = str(random.randint(100000, 999999))
            session['pending_2fa_code'] = code
            send_email(
                user.email,
                "Your PerkMiner Login Code",
                f"<p>Your PerkMiner login code is: <b>{code}</b></p>"
            )
            flash("New code sent.")
    return redirect(url_for("two_factor"))

@app.route("/resend_biz_2fa", methods=["POST"])
def resend_biz_2fa():
    biz_id = session.get('pending_2fa_biz_id')
    if biz_id:
        biz = Business.query.get(biz_id)
        if biz:
            code = str(random.randint(100000, 999999))
            session['pending_2fa_code'] = code
            send_email(
                biz.business_email,
                "Your PerkMiner Business Login Code",
                f"<p>Your PerkMiner business login code is: <b>{code}</b></p>"
            )
            flash("New code sent.")
    return redirect(url_for("two_factor_biz"))

@app.route("/resend_admin_2fa", methods=["POST"])
def resend_admin_2fa():
    user_id = session.get('pending_2fa_user_id')
    if user_id:
        user = User.query.get(user_id)
        if user:
            code = str(random.randint(100000, 999999))
            session['pending_2fa_code'] = code
            send_email(
                user.email,
                "Your PerkMiner Login Code",
                f"<p>Your PerkMiner login code is: <b>{code}</b></p>"
            )
            flash("New code sent.")
    return redirect(url_for("two_factor"))

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter_by(email=email).first()
        if user:
            token = generate_reset_token(email, "user")
            reset_url = url_for('reset_password', token=token, _external=True)
            send_reset_email(email, reset_url)
        flash("If an account with that email exists, a link has been sent.")
        return redirect(url_for('login'))
    return render_template('forgot_password.html', form=form)

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    email = verify_reset_token(token, "user")
    if not email:
        flash("Reset link invalid or expired.")
        return redirect(url_for('login'))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=email).first()
        if user:
            user.password = bcrypt.generate_password_hash(form.password.data).decode('utf-8')
            db.session.commit()
            flash("Password reset, please log in.")
            return redirect(url_for('login'))
        flash("Account not found.")
    return render_template('reset_password.html', form=form)

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    form = UserChangePasswordForm()
    if form.validate_on_submit():
        # Verify current password
        if not bcrypt.check_password_hash(current_user.password, form.current_password.data):
            flash("Current password is incorrect.", "danger")
            return redirect(url_for('change_password'))

        # Set new password
        current_user.password = bcrypt.generate_password_hash(form.new_password.data).decode('utf-8')
        db.session.commit()
        flash("Your password has been updated.", "success")
        return redirect(url_for('dashboard'))

    return render_template('change_password.html', form=form)

@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/invite', methods=['POST'])
@login_required
def invite():
    invite_form = InviteForm()
    if not invite_form.validate_on_submit():
        flash('Invalid form.')
        return redirect(url_for('dashboard'))

    invitee_email = invite_form.invitee_email.data.strip()
    inviter_name = current_user.name if current_user.name else current_user.email
    invitee_type = request.form.get('invitee_type', 'user')  # New: type coming from form

    # Store the invite in the database (optional: add invitee_type)
    new_invite = Invite(
        inviter_id=current_user.id,
        inviter_type='user',
        invitee_email=invitee_email,
        invitee_type=invitee_type,
        referral_code=current_user.referral_code,
        status='pending'
    )
    db.session.add(new_invite)
    db.session.commit()

    # Build the correct registration link
    if invitee_type == 'business':
        reg_url = url_for('business_register', ref=current_user.referral_code, _external=True)
    else:
        reg_url = url_for('register', ref=current_user.referral_code, _external=True)
    video_url = url_for('intro', ref=current_user.referral_code, _external=True)
    html_body = build_invite_email(inviter_name, reg_url, video_url)
    send_email(invitee_email, f"{inviter_name} has invited you to join PerkMiner.", html_body)

    flash(
        'Business invitation sent!' if invitee_type == "business"
        else 'User invitation sent!'
    )
    return redirect(url_for('dashboard'))

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from decimal import Decimal
from datetime import datetime, timedelta
from sqlalchemy import desc, func

@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    print("Current user roles:", [role.name for role in current_user.roles])
    form = RewardForm(request.form)
    profile_form = UserProfileForm()
    invite_form = InviteForm()
    user = current_user

    # --- Earnings Calculation with 7-day delay ---
    total_earnings, available_earnings, pending_earnings = calculate_user_earnings_split(user, delay_days=7)

    # summary fields for regular member earnings
    user.grand_total_earnings = total_earnings

    # amount actually available to withdraw = 7-day available minus already withdrawn
    net_available = available_earnings - (user.withdrawn_total or Decimal(0))
    user.earnings_balance = net_available

    # NEW: detailed split
    user.pending_earnings = pending_earnings
    user.available_for_withdrawal = available_earnings

    db.session.commit()

    if request.method == "POST" and profile_form.submit.data and profile_form.validate():
        updated = False
        if profile_form.name.data and profile_form.name.data != user.name:
            user.name = profile_form.name.data
            updated = True
        file = request.files.get('profile_photo')
        if file and allowed_file(file.filename):
            upload_result = cloudinary.uploader.upload(file)
            user.profile_photo = upload_result.get('secure_url')
            updated = True
        if updated:
            db.session.commit()
            flash("Profile updated!")
        return redirect(url_for('dashboard'))

    # Calculator setup (rewards, tiers, etc)
    if request.method == "GET":
        form.downline_level.data = '1'
        form.invoice_amount.data = 0
    rewards_table = ""
    reward = None
    invoice_amount = float(form.invoice_amount.data or 0)
    downline_level = int(form.downline_level.data or 1)
    cap = None
    if not request.method == "POST" or not form.validate_on_submit():
        downline_level = 1
        form.downline_level.data = '1'
        reward = invoice_amount * 0.02
        cap = None
        rewards_desc = "As the customer, when you make a purchase in the amount of"
    else:
        invoice_amount = float(form.invoice_amount.data)
        downline_level = int(form.downline_level.data)
        if downline_level == 1:
            reward = invoice_amount * 0.02
            rewards_desc = "When you make a purchase, you earn"
            cap = None
        elif downline_level in [2, 3, 4]:
            rate = 0.0025
            cap = 6.25
            reward = min(invoice_amount * rate, cap)
            rewards_desc = f"If a person in your Tier {downline_level} level makes a purchase"
        elif downline_level == 5:
            rate = 0.02
            cap = 50
            reward = min(invoice_amount * rate, cap)
            rewards_desc = "If a person in your Tier 5 level makes a purchase"
        else:
            reward = 0
            rewards_desc = ""
    if invoice_amount > 0 and reward is not None:
        if cap:
            rewards_table += f"<h5 class='mt-4 mb-2'>{rewards_desc} of ${invoice_amount:,.2f}:</h5>"
            rewards_table += f"<div class='alert alert-success'>You earn <strong>${reward:.2f}</strong> as cashback (capped at ${cap:.2f}).</div>"
        else:
            rewards_table += f"<h5 class='mt-4 mb-2'>{rewards_desc} of ${invoice_amount:,.2f}:</h5>"
            rewards_table += f"<div class='alert alert-success'>You earn <strong>${reward:.2f}</strong> as cashback.</div>"

    sponsor = User.query.get(current_user.sponsor_id) if current_user.sponsor_id else None
    level2 = User.query.filter_by(sponsor_id=current_user.id).all()
    level3, level4, level5 = [], [], []
    for u2 in level2:
        l3s = User.query.filter_by(sponsor_id=u2.id).all()
        level3.extend(l3s)
        for u3 in l3s:
            l4s = User.query.filter_by(sponsor_id=u3.id).all()
            level4.extend(l4s)
            for u4 in l4s:
                l5s = User.query.filter_by(sponsor_id=u4.id).all()
                level5.extend(l5s)

    # --- Business network tiers ---
    user_id = current_user.id
    biz_level1 = Business.query.filter_by(user_sponsor_id=user_id).all()
    def biz_ids(bizlist): return [b.id for b in bizlist]
    biz_level2 = Business.query.filter(Business.sponsor_id.in_(biz_ids(biz_level1))).all() if biz_level1 else []
    biz_level3 = Business.query.filter(Business.sponsor_id.in_(biz_ids(biz_level2))).all() if biz_level2 else []
    biz_level4 = Business.query.filter(Business.sponsor_id.in_(biz_ids(biz_level3))).all() if biz_level3 else []
    biz_level5 = Business.query.filter(Business.sponsor_id.in_(biz_ids(biz_level4))).all() if biz_level4 else []
    has_invited_business = len(biz_level1) > 0

    # Query for active sessions for this user
    active_sessions = Interaction.query.filter_by(user_id=current_user.id, status='active').all()
    has_active_sessions = len(active_sessions) > 0
    active_sessions_count = len(active_sessions)

    # --- Businesses user has interacted with (unique, recent, limit 5, Postgres-safe) ---
    days = 60
    since = datetime.utcnow() - timedelta(days=days)
    latest_per_business = (
        db.session.query(
            Interaction.business_id,
            func.max(Interaction.created_at).label("last_dt")
        )
        .filter(
            Interaction.user_id == current_user.id,
            Interaction.created_at >= since
        )
        .group_by(Interaction.business_id)
        .order_by(func.max(Interaction.created_at).desc())
        .limit(5)
        .subquery()
    )
    businesses = (
        db.session.query(Business)
        .join(latest_per_business, latest_per_business.c.business_id == Business.id)
        .all()
    )

    # --- Silent investor earnings (7-day delay) ---
    investor_total = investor_available = investor_pending = Decimal("0")
    if current_user.has_role('silent_investor'):
        investor_total, investor_available, investor_pending = calculate_investor_earnings_split(current_user, delay_days=7)

        # update investor summary fields on the user model
        user.investor_total_earnings = investor_total
        net_investor_available = investor_available - (user.investor_withdrawn_total or Decimal("0"))
        user.investor_earnings_balance = net_investor_available

        # NEW: detailed investor split
        user.pending_investor_earnings = investor_pending
        user.investor_earnings_withdraw_ready = investor_available

        db.session.commit()

    return render_template(
        "dashboard.html",
        form=form,
        profile_form=profile_form,
        invite_form=invite_form,
        email=current_user.email,
        referral_code=current_user.referral_code,
        sponsor=sponsor if sponsor else None,
        rewards_table=rewards_table,
        level2=level2, level3=level3, level4=level4, level5=level5,
        biz_level1=biz_level1, biz_level2=biz_level2, biz_level3=biz_level3, biz_level4=biz_level4, biz_level5=biz_level5,
        has_invited_business=has_invited_business,
        user_name=current_user.name,
        profile_img_url=current_user.profile_photo,
        has_active_sessions=has_active_sessions,
        active_sessions_count=active_sessions_count,
        businesses=businesses,
        total_earnings=total_earnings,
        available_earnings=available_earnings,
        pending_earnings=pending_earnings,
        investor_total=investor_total,
        investor_available=investor_available,
        investor_pending=investor_pending,
    )

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))

@app.route("/user/qr")
@login_required
def user_qr_code():
    # The QR code will encode this full payment URL
    code_url = url_for('payment_qr_redirect', ref=current_user.referral_code, _external=True)
    img = qrcode.make(code_url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")

@app.route("/session/<int:interaction_id>/user-receipt-check")
@login_required
def check_user_receipt(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    if interaction.user_id != current_user.id:
        return {"has_receipt": False}
    # Check for receipt with both correct user and interaction:
    user_txn = UserTransaction.query.filter_by(
        interaction_id=interaction.id,
        user_referral_id=current_user.referral_code
    ).order_by(UserTransaction.date_time.desc()).first()
    return {"has_receipt": user_txn is not None}

@app.route("/session/<int:interaction_id>/user-receipt")
@login_required
def show_user_receipt(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    if interaction.user_id != current_user.id:
        abort(403)
    transaction = UserTransaction.query.filter_by(
        interaction_id=interaction.id,
        user_referral_id=current_user.referral_code
    ).order_by(UserTransaction.date_time.desc()).first()
    return render_template("user_transaction_receipt.html", transaction=transaction, interaction=interaction)

@app.route("/user/receipts")
@login_required
def user_receipts():
    transactions = (
        UserTransaction.query
        .filter_by(user_referral_id=current_user.referral_code)
        .order_by(UserTransaction.date_time.desc())
        .options(
            joinedload(UserTransaction.interaction).joinedload(Interaction.business)
        )
        .all()
    )
    return render_template("user_receipts.html", transactions=transactions)

@app.route("/export_user_receipts_csv")
@login_required
def export_user_receipts_csv():
    user = current_user
    ref_code = user.referral_code

    transactions = UserTransaction.query.filter_by(user_referral_id=ref_code).order_by(UserTransaction.date_time.desc()).all()

    import csv
    from io import StringIO
    si = StringIO()
    writer = csv.writer(si)

    # Header
    writer.writerow([f"Receipts for '{user.name or user.email}' from Perk Miner"])
    writer.writerow([])

    # Table headers
    writer.writerow([
        "Date/Time",
        "Transaction ID",
        "Amount",
        "Tier 1 (2% Cash Back)",
        "Tier 2 (Commission)",
        "Tier 3 (Commission)",
        "Tier 4 (Commission)",
        "Tier 5 (Commission)"
    ])
    for t in transactions:
        writer.writerow([
            t.date_time.strftime('%Y-%m-%d %I:%M %p'),
            t.transaction_id,
            f"{t.amount:,.2f}",
            f"{t.cash_back:,.2f}" if t.user_referral_id == ref_code else "",
            f"{t.tier2_commission:,.2f}" if t.tier2_user_referral_id == ref_code else "",
            f"{t.tier3_commission:,.2f}" if t.tier3_user_referral_id == ref_code else "",
            f"{t.tier4_commission:,.2f}" if t.tier4_user_referral_id == ref_code else "",
            f"{t.tier5_commission:,.2f}" if t.tier5_user_referral_id == ref_code else "",
        ])

    output = si.getvalue()
    return Response(output, mimetype="text/csv", headers={
        "Content-Disposition": f"attachment;filename=user_receipts_{user.referral_code}.csv"
    })

@app.route("/user/receipt/<transaction_id>")
@login_required
def user_receipt_single(transaction_id):
    # find the user transaction for this member + transaction_id
    txn = (
        UserTransaction.query
        .filter_by(transaction_id=transaction_id, user_referral_id=current_user.referral_code)
        .order_by(UserTransaction.date_time.desc())
        .first_or_404()
    )

    # try to resolve the business (via interaction.business relationship)
    business_name = None
    if txn.interaction and txn.interaction.business:
        business_name = txn.interaction.business.business_name

    # decide what to show as date/time: local_date_time if present, else UTC time
    if txn.local_date_time:
        display_datetime = txn.local_date_time
    else:
        # fallback format from UTC datetime
        display_datetime = txn.date_time.strftime('%Y-%m-%d %I:%M %p UTC')

    # for safety, we can re-compute 2% cashback with $50 cap from amount
    # but we’ll mostly trust txn.cash_back and just display it
    amount_paid = txn.amount or 0
    cashback_earned = txn.cash_back or 0

    return render_template(
        "user_receipt_single.html",
        transaction=txn,
        business_name=business_name,
        display_datetime=display_datetime,
        amount_paid=amount_paid,
        cashback_earned=cashback_earned
    )

@app.route("/user/earnings", methods=["GET"])
@login_required
def user_earnings():
    period = request.args.get("period", "all")
    year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    month = int(request.args.get("month", 0)) if request.args.get("month") else 0

    ref_code = current_user.referral_code

    all_txns = UserTransaction.query

    # Date filter
    if period == "year" and year:
        all_txns = all_txns.filter(UserTransaction.date_time >= datetime(year, 1, 1), UserTransaction.date_time < datetime(year+1, 1, 1))
    elif period == "month" and year and month:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year+1, 1, 1)
        else:
            end = datetime(year, month+1, 1)
        all_txns = all_txns.filter(UserTransaction.date_time >= start, UserTransaction.date_time < end)

    transactions = all_txns.order_by(UserTransaction.date_time.desc()).all()

    filtered = []
    for t in transactions:
        earned = False
        # Standard user earnings (cash back and user-user commissions)
        if t.user_referral_id == ref_code and t.cash_back > 0:
            earned = True
        if t.tier2_user_referral_id == ref_code and t.tier2_commission > 0:
            earned = True
        if t.tier3_user_referral_id == ref_code and t.tier3_commission > 0:
            earned = True
        if t.tier4_user_referral_id == ref_code and t.tier4_commission > 0:
            earned = True
        if t.tier5_user_referral_id == ref_code and t.tier5_commission > 0:
            earned = True
        # User-business commissions
        if t.tier1_business_user_referral_id == ref_code and t.tier1_business_user_commission > 0:
            earned = True
        if t.tier2_business_user_referral_id == ref_code and t.tier2_business_user_commission > 0:
            earned = True
        if t.tier3_business_user_referral_id == ref_code and t.tier3_business_user_commission > 0:
            earned = True
        if t.tier4_business_user_referral_id == ref_code and t.tier4_business_user_commission > 0:
            earned = True
        if t.tier5_business_user_referral_id == ref_code and t.tier5_business_user_commission > 0:
            earned = True
        if earned:
            filtered.append(t)
    transactions = filtered

    tier1_earnings = sum(
        t.cash_back for t in transactions
        if t.user_referral_id == ref_code and t.cash_back > 0
    )
    tier2_earnings = sum(t.tier2_commission for t in transactions if t.tier2_user_referral_id == ref_code)
    tier3_earnings = sum(t.tier3_commission for t in transactions if t.tier3_user_referral_id == ref_code)
    tier4_earnings = sum(t.tier4_commission for t in transactions if t.tier4_user_referral_id == ref_code)
    tier5_earnings = sum(t.tier5_commission for t in transactions if t.tier5_user_referral_id == ref_code)
    commissions_total = tier2_earnings + tier3_earnings + tier4_earnings + tier5_earnings

    # User-Business Commissions: Tier 1-5
    tier1_user_business_earnings = sum(
        t.tier1_business_user_commission for t in transactions
        if t.tier1_business_user_referral_id == ref_code and t.tier1_business_user_commission > 0
    )
    tier2_user_business_earnings = sum(
        t.tier2_business_user_commission for t in transactions
        if t.tier2_business_user_referral_id == ref_code and t.tier2_business_user_commission > 0
    )
    tier3_user_business_earnings = sum(
        t.tier3_business_user_commission for t in transactions
        if t.tier3_business_user_referral_id == ref_code and t.tier3_business_user_commission > 0
    )
    tier4_user_business_earnings = sum(
        t.tier4_business_user_commission for t in transactions
        if t.tier4_business_user_referral_id == ref_code and t.tier4_business_user_commission > 0
    )
    tier5_user_business_earnings = sum(
        t.tier5_business_user_commission for t in transactions
        if t.tier5_business_user_referral_id == ref_code and t.tier5_business_user_commission > 0
    )
    total_user_business_commission = (
        tier1_user_business_earnings +
        tier2_user_business_earnings +
        tier3_user_business_earnings +
        tier4_user_business_earnings +
        tier5_user_business_earnings
    )

    # Check if user has ever referred a business
    has_invited_business = Business.query.filter_by(user_sponsor_id=current_user.id).first() is not None

    summary = dict(
        tier1_earnings=f"{tier1_earnings:,.2f}",
        tier2_earnings=f"{tier2_earnings:,.2f}",
        tier3_earnings=f"{tier3_earnings:,.2f}",
        tier4_earnings=f"{tier4_earnings:,.2f}",
        tier5_earnings=f"{tier5_earnings:,.2f}",
        commissions_total=f"{commissions_total:,.2f}",
        tier1_user_business_earnings=f"{tier1_user_business_earnings:,.2f}",
        tier2_user_business_earnings=f"{tier2_user_business_earnings:,.2f}",
        tier3_user_business_earnings=f"{tier3_user_business_earnings:,.2f}",
        tier4_user_business_earnings=f"{tier4_user_business_earnings:,.2f}",
        tier5_user_business_earnings=f"{tier5_user_business_earnings:,.2f}",
        total_user_business_commission=f"{total_user_business_commission:,.2f}",
        total=f"{tier1_earnings + commissions_total + total_user_business_commission:,.2f}",
        period=period,
        year=year,
        month=month
    )

    # Optionally: build a business lookup for displaying business info in the table
    business_lookup = {b.referral_code: b for b in Business.query.all()}

    return render_template(
        "user_earnings.html",
        transactions=transactions,
        summary=summary,
        business_lookup=business_lookup,
        has_invited_business=has_invited_business,
        today=date.today(),
        period=period,
        year=year,
        month=month
    )

@app.route("/export_user_earnings_csv")
@login_required
def export_user_earnings_csv():
    user = current_user
    ref_code = user.referral_code

    period = request.args.get("period", "all")
    year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    month = int(request.args.get("month", 0)) if request.args.get("month") else 0

    all_txns = UserTransaction.query

    if period == "year" and year:
        all_txns = all_txns.filter(UserTransaction.date_time >= datetime(year, 1, 1), UserTransaction.date_time < datetime(year+1, 1, 1))
    elif period == "month" and year and month:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year+1, 1, 1)
        else:
            end = datetime(year, month+1, 1)
        all_txns = all_txns.filter(UserTransaction.date_time >= start, UserTransaction.date_time < end)
    transactions = all_txns.order_by(UserTransaction.date_time.desc()).all()

    # Only include transactions where this user earned something
    filtered = []
    for t in transactions:
        earned = False
        # Any tier
        if t.user_referral_id == ref_code and t.cash_back > 0:
            earned = True
        if t.tier2_user_referral_id == ref_code and t.tier2_commission > 0:
            earned = True
        if t.tier3_user_referral_id == ref_code and t.tier3_commission > 0:
            earned = True
        if t.tier4_user_referral_id == ref_code and t.tier4_commission > 0:
            earned = True
        if t.tier5_user_referral_id == ref_code and t.tier5_commission > 0:
            earned = True
        if t.tier1_business_user_referral_id == ref_code and t.tier1_business_user_commission > 0:
            earned = True
        if t.tier2_business_user_referral_id == ref_code and t.tier2_business_user_commission > 0:
            earned = True
        if t.tier3_business_user_referral_id == ref_code and t.tier3_business_user_commission > 0:
            earned = True
        if t.tier4_business_user_referral_id == ref_code and t.tier4_business_user_commission > 0:
            earned = True
        if t.tier5_business_user_referral_id == ref_code and t.tier5_business_user_commission > 0:
            earned = True
        if earned:
            filtered.append(t)
    transactions = filtered

    # Summary (like on page)
    tier1_earnings = sum(
        t.cash_back for t in transactions
        if t.user_referral_id == ref_code and t.cash_back > 0
    )
    tier2_earnings = sum(t.tier2_commission for t in transactions if t.tier2_user_referral_id == ref_code)
    tier3_earnings = sum(t.tier3_commission for t in transactions if t.tier3_user_referral_id == ref_code)
    tier4_earnings = sum(t.tier4_commission for t in transactions if t.tier4_user_referral_id == ref_code)
    tier5_earnings = sum(t.tier5_commission for t in transactions if t.tier5_user_referral_id == ref_code)
    commissions_total = tier2_earnings + tier3_earnings + tier4_earnings + tier5_earnings

    # User-Business commissions
    tier1_user_business_earnings = sum(
        t.tier1_business_user_commission for t in transactions
        if t.tier1_business_user_referral_id == ref_code and t.tier1_business_user_commission > 0
    )
    tier2_user_business_earnings = sum(
        t.tier2_business_user_commission for t in transactions
        if t.tier2_business_user_referral_id == ref_code and t.tier2_business_user_commission > 0
    )
    tier3_user_business_earnings = sum(
        t.tier3_business_user_commission for t in transactions
        if t.tier3_business_user_referral_id == ref_code and t.tier3_business_user_commission > 0
    )
    tier4_user_business_earnings = sum(
        t.tier4_business_user_commission for t in transactions
        if t.tier4_business_user_referral_id == ref_code and t.tier4_business_user_commission > 0
    )
    tier5_user_business_earnings = sum(
        t.tier5_business_user_commission for t in transactions
        if t.tier5_business_user_referral_id == ref_code and t.tier5_business_user_commission > 0
    )
    total_user_business_commission = (
        tier1_user_business_earnings +
        tier2_user_business_earnings +
        tier3_user_business_earnings +
        tier4_user_business_earnings +
        tier5_user_business_earnings
    )
    total_all = tier1_earnings + commissions_total + total_user_business_commission

    # Generate CSV
    import csv
    from io import StringIO
    si = StringIO()
    writer = csv.writer(si)

    # Header
    writer.writerow([f"Earnings for '{user.name or user.email}' from Perk Miner"])
    writer.writerow([])
    writer.writerow([f"Total Earned:", f"${total_all:,.2f}"])
    writer.writerow([f"2% Cash Back (Self/Tier 1):", f"${tier1_earnings:,.2f}"])
    writer.writerow([f"Total Commission (Tiers 2-5):", f"${commissions_total:,.2f}"])
    writer.writerow([f"Total User-Business Commission (Tiers 1-5):", f"${total_user_business_commission:,.2f}"])
    writer.writerow([])
    writer.writerow([f"Tier 2 (Commission):", f"${tier2_earnings:,.2f}"])
    writer.writerow([f"Tier 3 (Commission):", f"${tier3_earnings:,.2f}"])
    writer.writerow([f"Tier 4 (Commission):", f"${tier4_earnings:,.2f}"])
    writer.writerow([f"Tier 5 (Commission):", f"${tier5_earnings:,.2f}"])
    writer.writerow([])
    writer.writerow([f"Tier 1 User-Business Commission:", f"${tier1_user_business_earnings:,.2f}"])
    writer.writerow([f"Tier 2 User-Business Commission:", f"${tier2_user_business_earnings:,.2f}"])
    writer.writerow([f"Tier 3 User-Business Commission:", f"${tier3_user_business_earnings:,.2f}"])
    writer.writerow([f"Tier 4 User-Business Commission:", f"${tier4_user_business_earnings:,.2f}"])
    writer.writerow([f"Tier 5 User-Business Commission:", f"${tier5_user_business_earnings:,.2f}"])
    writer.writerow([])

    # Table headers (your details)
    writer.writerow([
        "Date/Time",
        "Transaction ID",
        "Tier 1 (2% Cash Back)",
        "Tier 2 (Commission)",
        "Tier 3 (Commission)",
        "Tier 4 (Commission)",
        "Tier 5 (Commission)",
        "Tier 1 User-Business",
        "Tier 2 User-Business",
        "Tier 3 User-Business",
        "Tier 4 User-Business",
        "Tier 5 User-Business"
    ])
    for t in transactions:
        writer.writerow([
            t.date_time.strftime('%Y-%m-%d %I:%M %p'),
            t.transaction_id,
            f"{t.cash_back:,.2f}" if t.user_referral_id == ref_code else "",
            f"{t.tier2_commission:,.2f}" if t.tier2_user_referral_id == ref_code else "",
            f"{t.tier3_commission:,.2f}" if t.tier3_user_referral_id == ref_code else "",
            f"{t.tier4_commission:,.2f}" if t.tier4_user_referral_id == ref_code else "",
            f"{t.tier5_commission:,.2f}" if t.tier5_user_referral_id == ref_code else "",
            f"{t.tier1_business_user_commission:,.2f}" if t.tier1_business_user_referral_id == ref_code else "",
            f"{t.tier2_business_user_commission:,.2f}" if t.tier2_business_user_referral_id == ref_code else "",
            f"{t.tier3_business_user_commission:,.2f}" if t.tier3_business_user_referral_id == ref_code else "",
            f"{t.tier4_business_user_commission:,.2f}" if t.tier4_business_user_referral_id == ref_code else "",
            f"{t.tier5_business_user_commission:,.2f}" if t.tier5_business_user_referral_id == ref_code else "",
        ])

    output = si.getvalue()
    return Response(output, mimetype="text/csv", headers={
        "Content-Disposition": f"attachment;filename=user_earnings_{user.referral_code}.csv"
    })

# USER — View Quote (read-only; user context)
@app.route("/session/<int:interaction_id>/user-quote")
@login_required
def user_quote_view(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    is_user = interaction.user_id == current_user.id
    is_biz = False
    if not is_user:
        abort(403)
    quote = Quote.query.filter_by(interaction_id=interaction.id).first()
    return render_template(
        "quote.html",
        interaction=interaction,
        quote=quote,
        is_user=is_user,
        is_biz=is_biz
    )

@app.route("/user/start-purchase/<int:biz_id>")
@login_required
def start_purchase(biz_id):
    interaction = Interaction.query.filter_by(
        user_id=current_user.id,
        business_id=biz_id,
        status='active'
    ).first()
    if not interaction:
        interaction = Interaction(
            user_id=current_user.id,
            business_id=biz_id,
            status='active',
            awaiting_payment=True,
            service_type='Purchase',              # Non-null default
            details='Direct QR purchase',         # Non-null default
            budget_low=0,
            budget_high=0,
            referral_code=current_user.referral_code
        )
        db.session.add(interaction)
        db.session.commit()
    else:
        interaction.awaiting_payment = True
        db.session.commit()
    return redirect(url_for('show_purchase_qr', interaction_id=interaction.id))

@app.route("/user/show-qr/<int:interaction_id>")
@login_required
def show_purchase_qr(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    if interaction.user_id != current_user.id:
        abort(403)
    is_finalize_payment = getattr(interaction, "awaiting_payment", False)
    is_request_for_service = (not is_finalize_payment and getattr(interaction, "service_type", None) == "Service Request")
    return render_template(
        "show_qr.html",
        interaction=interaction,
        is_finalize_payment=is_finalize_payment,
        is_request_for_service=is_request_for_service
    )

@app.route("/finalize-transaction/<int:interaction_id>", methods=["GET", "POST"])
@login_required
def finalize_transaction_user(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    if interaction.user_id != current_user.id:
        abort(403)

    now = datetime.now()
    summary = None

    if request.method == "POST":
        transaction_id = str(uuid.uuid4())
        data = request.form
        amount = float(data.get("amount", 0))
        local_dt_str = request.form.get("local_datetime")

        # USER-TO-USER COMMISSION LOGIC
        user_referral_id = current_user.referral_code
        user_cash_back_raw = amount * 0.02
        user_cash_back = round(min(user_cash_back_raw, 50), 2)  # Tier 1 cap $50

        u2 = User.query.filter_by(id=current_user.sponsor_id).first()
        tier2_user_referral_id = u2.referral_code if u2 else None
        tier2_commission = round(min(amount * 0.0025, 6.25), 2) if u2 else 0

        u3 = User.query.filter_by(id=u2.sponsor_id).first() if u2 and u2.sponsor_id else None
        tier3_user_referral_id = u3.referral_code if u3 else None
        tier3_commission = round(min(amount * 0.0025, 6.25), 2) if u3 else 0

        u4 = User.query.filter_by(id=u3.sponsor_id).first() if u3 and u3.sponsor_id else None
        tier4_user_referral_id = u4.referral_code if u4 else None
        tier4_commission = round(min(amount * 0.0025, 6.25), 2) if u4 else 0

        u5 = User.query.filter_by(id=u4.sponsor_id).first() if u4 and u4.sponsor_id else None
        tier5_user_referral_id = u5.referral_code if u5 else None
        tier5_commission = round(min(amount * 0.02, 50), 2) if u5 else 0

        # All business-downline user fields are left default/blank for pure user-purchases!
        user_trans = UserTransaction(
            transaction_id=transaction_id,
            interaction_id=interaction.id,
            amount=amount,
            date_time=datetime.utcnow(),
            local_date_time=local_dt_str,
            business_referral_id=None,
            user_referral_id=user_referral_id,
            cash_back=user_cash_back,
            tier2_user_referral_id=tier2_user_referral_id,
            tier2_commission=tier2_commission,
            tier3_user_referral_id=tier3_user_referral_id,
            tier3_commission=tier3_commission,
            tier4_user_referral_id=tier4_user_referral_id,
            tier4_commission=tier4_commission,
            tier5_user_referral_id=tier5_user_referral_id,
            tier5_commission=tier5_commission,
            tier2_business_user_referral_id=None,
            tier2_business_user_commission=0,
            tier3_business_user_referral_id=None,
            tier3_business_user_commission=0,
            tier4_business_user_referral_id=None,
            tier4_business_user_commission=0,
            tier5_business_user_referral_id=None,
            tier5_business_user_commission=0
        )
        db.session.add(user_trans)
        db.session.commit()

        summary = {
            "amount": f"{amount:.2f}",
            "user_cash_back": f"{user_cash_back:.2f}",
            "tier2_user_commission": f"{tier2_commission:.2f}" if u2 else "0.00",
            "tier3_user_commission": f"{tier3_commission:.2f}" if u3 else "0.00",
            "tier4_user_commission": f"{tier4_commission:.2f}" if u4 else "0.00",
            "tier5_user_commission": f"{tier5_commission:.2f}" if u5 else "0.00",
        }
        flash("User transaction finalized and all rewards/commissions assigned!", "success")

    return render_template(
        "finalize_transaction_user.html",
        interaction=interaction,
        now=now,
        summary=summary
    )

# BUSINESS — View/Edit Quote (business context)
@app.route("/session/<int:interaction_id>/quote", methods=["GET", "POST"])
@business_login_required
def create_quote(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    is_biz = session.get('business_id') == interaction.business_id
    is_user = False
    if not is_biz:
        flash("Only the business can send or edit a quote for this session.")
        return redirect(url_for('biz_active_session', interaction_id=interaction_id))

    quote = Quote.query.filter_by(interaction_id=interaction.id).first()
    if request.method == "POST":
        amount = request.form.get("amount")
        details = request.form.get("details")
        if not amount or not details:
            flash("Amount and quote details are required.", "danger")
        else:
            if quote:
                quote.amount = amount
                quote.details = details
            else:
                quote = Quote(
                    interaction_id=interaction.id,
                    amount=amount,
                    details=details
                )
                db.session.add(quote)
            db.session.commit()
            flash("Quote sent to user!" if not quote else "Quote was updated!", "success")
            return redirect(url_for('biz_active_session', interaction_id=interaction_id))

    return render_template(
        "quote.html",
        interaction=interaction,
        quote=quote,
        is_user=is_user,
        is_biz=is_biz
    )

@app.route("/session/<int:interaction_id>/end", methods=["POST"])
def end_session(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    # Check who is ending session
    is_user = current_user.is_authenticated and getattr(current_user, 'id', None) == interaction.user_id
    is_biz = session.get('business_id') == interaction.business_id
    is_staff = session.get('staff_id') is not None and interaction.business_id == Staff.query.get(session.get('staff_id')).business_id

    if not (is_user or is_biz or is_staff):
        abort(403)

    # Mark as ended for everyone
    interaction.status = "ended"
    db.session.commit()
    flash("Session ended.", "success")

    # Redirect accordingly
    if current_user.is_authenticated and current_user.has_role('customer_support'):
        return redirect(url_for('support_dashboard'))
    elif is_staff:
        return redirect(url_for('staff_dashboard'))
    elif is_biz:
        return redirect(url_for('biz_user_interactions'))
    else:  # user
        return redirect(url_for('user_biz_interactions'))

@app.route("/payment/<ref>")
def payment_qr_redirect(ref):
    user = User.query.filter_by(referral_code=ref).first()
    if not user:
        return "Invalid QR code.", 404

    # Determine the relevant session for this business and user
    if 'business_id' in session:
        interaction = get_interaction_for_business_and_user(
            business_id=session['business_id'],
            user_id=user.id
        )
        if interaction:
            return redirect(url_for('finalize_transaction', interaction_id=interaction.id))
        else:
            return "No active session found for this customer.", 404
    elif 'staff_id' in session:
        staff = Staff.query.get(session['staff_id'])
        if not staff:
            flash("Invalid staff session.", "danger")
            return redirect(url_for("staff_login"))
        # Staff can only see interactions for their business
        interaction = Interaction.query.filter_by(
            business_id=staff.business_id,
            user_id=user.id,
            status="active"
        ).first()
        if interaction:
            return redirect(url_for('staff_finalize_transaction', interaction_id=interaction.id))
        else:
            return "No active session found for this customer.", 404
    # If not logged in as business or staff, show generic landing
    return render_template("qr_user_landing.html", user=user)

@app.route("/business/fund-account", methods=["GET", "POST"])
@business_login_required
def fund_account():
    biz_id = session.get('business_id')
    if not biz_id:
        return redirect(url_for('business_login'))
    biz = Business.query.get_or_404(biz_id)
    account_balance = biz.account_balance or 0.0

    # Temporary demo funding logic
    if request.method == "POST":
        amount = float(request.form.get("amount", 0))
        if amount > 0:
            biz.account_balance += amount
            db.session.commit()
            flash(f"Account funded: ${amount:.2f} added.", "success")
            return redirect(url_for('business_dashboard'))

    return render_template(
        "fund_account.html",
        account_balance=account_balance,
        STRIPE_PUBLISHABLE=os.environ.get("STRIPE_PUBLISHABLE")
    )

@csrf.exempt
@app.route('/business/create-checkout-session', methods=['POST'])
@business_login_required
def business_create_checkout_session():
    data = request.get_json()
    amount = data.get('amount')

    # Get logged-in business info
    biz_id = session.get('business_id')
    biz = Business.query.get_or_404(biz_id)

    try:
        # Validate amount is a number and at least $25.00
        amount_float = float(amount)
        if amount_float < 25.0:
            return jsonify({'error': 'Minimum $25.00 required.'}), 400
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid amount.'}), 400

    try:
        session_obj = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='payment',
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': 'Fund Business Account',
                    },
                    'unit_amount': int(amount_float * 100),  # dollars to cents
                },
                'quantity': 1,
            }],
            customer_email=biz.business_email,  # always use the business's email
            success_url=YOUR_DOMAIN + '/business/dashboard?fund_success=1',
            cancel_url=YOUR_DOMAIN + '/business/fund-account?canceled=1',
            metadata={
                'purpose': 'fund_account',
                'business_id': str(biz.id)  # <-- THIS ENSURES THE WEBHOOK CAN FIND THE BUSINESS!
            }
        )
        return jsonify({'sessionId': session_obj.id})
    except Exception as e:
        import logging
        logging.error(f"Stripe Checkout creation error: {e}")
        return jsonify({'error': 'Checkout session failed.'}), 500

@app.route("/business/finalize-transaction/<int:interaction_id>", methods=["GET", "POST"])
@business_login_required
def finalize_transaction(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    if session.get('business_id') != interaction.business_id:
        abort(403)

    business = interaction.business
    now = datetime.now()
    summary = None
    is_staff = session.get('staff_id') is not None

    if request.method == "POST":
        local_dt_str = request.form.get("local_datetime")
        amount = float(request.form.get("amount", 0))
        try:
            summary = finalize_interaction(
                interaction,
                business,
                amount,
                staff_id=session.get('staff_id'),
                source=None,
                local_date_time=local_dt_str  # <-- pass the value!
            )
            flash("Transaction finalized and all rewards/commissions assigned!", "success")
        except Exception as e:
            flash(str(e), "danger")
        # No need to instantiate BusinessTransaction here; that's done in finalize_interaction.

    return render_template(
        "finalize_transaction.html",
        interaction=interaction,
        now=now,
        summary=summary,
        account_balance=business.account_balance,
        is_staff=is_staff
    )

@app.route("/service-request/<int:biz_id>", methods=["GET", "POST"])
@login_required
def service_request(biz_id):
    biz = Business.query.get_or_404(biz_id)
    form = ServiceRequestForm()

    if form.validate_on_submit():
        # 1. Create the Interaction record
        interaction = Interaction(
            user_id=current_user.id,
            business_id=biz.id,
            service_type=form.service_type.data,
            details=form.details.data,
            budget_low=form.budget_low.data,
            budget_high=form.budget_high.data,
            status="active",
            referral_code=getattr(current_user, "referral_code", None)
        )
        db.session.add(interaction)
        db.session.commit()

        # 2. Send email to business
        interaction_link = url_for('biz_user_interactions', _external=True)
        subject = "Your business has been selected"
        body = f"""
        <p>You have been selected for a service request.</p>
        <ul>
          <li><strong>Service Type:</strong> {form.service_type.data}</li>
          <li><strong>Details:</strong> {form.details.data}</li>
          <li><strong>Budget:</strong> ${form.budget_low.data} - ${form.budget_high.data}</li>
          <li><strong>User Referral Code:</strong> {getattr(current_user, 'referral_code', '')}</li>
        </ul>
        <p>
          To view and manage the request, please 
          <a href="{interaction_link}">visit your business interaction dashboard</a>.
        </p>
        """
        send_email(biz.business_email, subject, body)

        # 3. Redirect user to their dashboard of active sessions
        return redirect(url_for('user_biz_interactions'))

    return render_template("service_request.html", form=form, business=biz)

@app.route("/user/interactions")
@login_required
def user_biz_interactions():
    # Query all active interactions for this user
    interactions = Interaction.query.filter_by(user_id=current_user.id, status='active').order_by(Interaction.created_at.desc()).all()
    return render_template("user_biz_interactions.html", interactions=interactions)

@app.route("/session/<int:interaction_id>", methods=["GET", "POST"])
@login_required
def active_session(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    is_user = interaction.user_id == getattr(current_user, 'id', None)
    is_biz = session.get('business_id') == interaction.business_id
    if not (is_user or is_biz):
        flash("Access denied.")
        if session.get('business_id'):
            return redirect(url_for('biz_user_interactions'))
        else:
            return redirect(url_for('user_biz_interactions'))

    # business + shop-online flag
    business = interaction.business
    can_shop_online = (
        bool(business.website_url)
        and (business.account_balance or 0) >= 250.0
    )

    if request.method == "POST":
        if is_user and request.form.get("accept_and_pay") == "1":
            interaction.awaiting_finalization = True
            db.session.commit()
            flash("Please show your QR code to the business to complete the payment.", "success")
            return redirect(url_for('active_session', interaction_id=interaction.id))

        text = request.form.get("message_text", "").strip()
        uploaded_file = request.files.get("message_file")
        file_url = None
        file_name = None

        if uploaded_file and uploaded_file.filename:
            filename = secure_filename(uploaded_file.filename)
            upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            uploaded_file.save(upload_path)
            file_url = url_for('uploaded_file', filename=filename)
            file_name = uploaded_file.filename

        if text or file_url:
            if is_user:
                sender_type = "user"
                sender_id = current_user.id
            elif is_biz:
                sender_type = "business"
                # if staff is logged in, store their id; if owner, store business id
                staff_id = session.get('staff_id')
                sender_id = staff_id if staff_id else session['business_id']
            elif current_user.is_authenticated and current_user.has_role("customer_support"):
                sender_type = "customer_support"
                sender_id = current_user.id
            else:
                flash("Invalid sender.")
                return redirect(url_for('active_session', interaction_id=interaction_id))

            msg = Message(
                interaction_id=interaction.id,
                sender_type=sender_type,
                sender_id=sender_id,
                text=text,
                file_url=file_url,
                file_name=file_name
            )
            db.session.add(msg)
            db.session.commit()
            return redirect(url_for('active_session', interaction_id=interaction.id))

    # FETCH all messages after handling POSTs
    messages = Message.query.filter_by(interaction_id=interaction.id).order_by(Message.timestamp).all()

    # ADD sender_label to each message
    messages_with_labels = []
    for msg in messages:
        label = ""
        if msg.sender_type == "user":
            label = interaction.user.name or interaction.user.email
        elif msg.sender_type == "business":
            if msg.sender_id == interaction.business.id:
                label = interaction.business.business_name
            else:
                label = f"{interaction.business.business_name} Staff"
        elif msg.sender_type == "customer_support":
            sender = User.query.get(msg.sender_id)
            label = f"Customer Support ({sender.name or sender.email})" if sender else "Customer Support"
        messages_with_labels.append({
            "text": msg.text,
            "timestamp": msg.timestamp,
            "sender_label": label,
            "file_url": msg.file_url,
            "file_name": msg.file_name,
        })

    return render_template(
        "active_session.html",
        interaction=interaction,
        is_user=is_user,
        is_biz=is_biz,
        messages=messages_with_labels,
        can_shop_online=can_shop_online,
        business=business
    )

@app.route("/shop/confirm/<int:business_id>", methods=["GET", "POST"])
@login_required
def shop_confirm(business_id):
    # 1) find business
    business = Business.query.get_or_404(business_id)

    # 2) basic checks: must have website, be marked as ecommerce, allow purchases, and have enough balance
    if not business.website_url:
        flash("This business doesn't have an e-commerce website configured.", "warning")
        return redirect(url_for("view_listing", biz_id=business.id))

    if not getattr(business, "is_ecommerce_site", False) or not getattr(business, "allow_website_purchases", False):
        flash("This business is not configured for online purchases through PerkMiner.", "warning")
        return redirect(url_for("view_listing", biz_id=business.id))

    if (business.account_balance or 0) < 250.0:
        flash("This business must maintain at least $250 in their advertising balance "
              "before members can shop on their website through PerkMiner.", "warning")
        return redirect(url_for("view_listing", biz_id=business.id))

    # 3) if member submits the confirmation form
    if request.method == "POST":
        # find existing active interaction between this user and business, if any
        interaction = Interaction.query.filter_by(
            user_id=current_user.id,
            business_id=business.id,
            status="active"
        ).first()

        # if none, create a new one
        if not interaction:
            interaction = Interaction(
                user_id=current_user.id,
                business_id=business.id,
                # minimal required fields to satisfy your model
                service_type="Online Purchase",
                details="Member is shopping on business e-commerce site via PerkMiner.",
                budget_low=0,
                budget_high=0,
                status="active",
                referral_code=getattr(current_user, "referral_code", None)
            )
            db.session.add(interaction)
            db.session.commit()

        # 4) redirect into the existing shop_link flow
        return redirect(url_for("shop_link", interaction_id=interaction.id))

    # 5) GET request: show confirmation page
    return render_template(
        "shop_confirm.html",
        business=business
    )

@app.route("/session/<int:interaction_id>/messages")
def session_messages(interaction_id):
    # Check for EITHER user or business session
    interaction = Interaction.query.get_or_404(interaction_id)
    is_user = False
    is_biz = False
    if 'business_id' in session and session['business_id'] == interaction.business_id:
        is_biz = True
    elif current_user.is_authenticated and getattr(current_user, 'id', None) == interaction.user_id:
        is_user = True
    if not (is_user or is_biz):
        return ""
    messages = Message.query.filter_by(interaction_id=interaction.id).order_by(Message.timestamp).all()
    return render_template("partials/_messages.html", interaction=interaction, messages=messages, is_user=is_user, is_biz=is_biz)

@app.route("/session/<int:interaction_id>/quote/view")
@business_login_required
def view_quote(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    is_user = interaction.user_id == getattr(current_user, 'id', None)
    if not is_user:
        flash("Only the user may view the quote.")
        return redirect(url_for('active_session', interaction_id=interaction_id))
    quote = Quote.query.filter_by(interaction_id=interaction.id).first()
    return render_template("quote.html", interaction=interaction, quote=quote)

@app.route("/business/receipts")
@business_login_required
def business_receipts():
    biz_id = session.get('business_id')
    business = Business.query.get_or_404(biz_id)
    transactions = BusinessTransaction.query.filter_by(
        business_referral_id=business.referral_code
    ).filter(
        (BusinessTransaction.sponsoree_mutual_referral_id == None) | (BusinessTransaction.sponsoree_mutual_referral_id == "")
    ).order_by(BusinessTransaction.date_time.desc()).all()
    return render_template("business_receipts.html", transactions=transactions, business=business)

@app.route("/export_business_receipts_csv")
@business_login_required
def export_business_receipts_csv():
    biz_id = session.get('business_id')
    business = Business.query.get_or_404(biz_id)

    # Only main invoice entries: sponsoree_mutual_referral_id is None or empty string
    transactions = BusinessTransaction.query.filter_by(
        business_referral_id=business.referral_code
    ).filter(
        (BusinessTransaction.sponsoree_mutual_referral_id == None) | (BusinessTransaction.sponsoree_mutual_referral_id == "")
    ).order_by(BusinessTransaction.date_time.desc()).all()

    import csv
    from io import StringIO
    si = StringIO()
    writer = csv.writer(si)

    # Header for business and Perk Miner
    writer.writerow([f"Invoices for '{business.business_name}' from PerkMiner"])
    writer.writerow([])

    # Table headers
    writer.writerow([
        "Date/Time",
        "Interaction ID",
        "Sale Amount",
        "Ad Fee (10%, capped $250)",
        "Net Gross",
        "Marketing ROI",
        "Marketing ROI Ratio",
        "Cash Back"
    ])

    for txn in transactions:
        ad_fee = txn.ad_fee or 0
        net_gross = txn.amount - ad_fee
        roi = (net_gross / ad_fee) * 100 if ad_fee else 0
        ratio = (net_gross + ad_fee) / ad_fee if ad_fee else 0

        writer.writerow([
            txn.local_date_time if txn.local_date_time else txn.date_time.strftime('%Y-%m-%d %I:%M %p'),
            txn.interaction_id,
            f"{txn.amount:,.2f}",
            f"{ad_fee:,.2f}",
            f"{net_gross:,.2f}",
            f"{roi:.0f}%",
            f"{ratio:.1f}:1",
            f"{txn.cash_back:,.2f}"
        ])

    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=business_receipts_{business.referral_code}.csv"}
    )

@app.route("/business/earnings", methods=["GET"])
@business_login_required
def business_earnings():
    biz_id = session.get('business_id')
    business = Business.query.get_or_404(biz_id)
    ref_code = business.referral_code

    period = request.args.get("period", "all")
    year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    month = int(request.args.get("month", 0)) if request.args.get("month") else 0

    all_txns = BusinessTransaction.query

    # Date/month/year filter logic
    if period == "year" and year:
        all_txns = all_txns.filter(BusinessTransaction.date_time >= datetime(year, 1, 1), BusinessTransaction.date_time < datetime(year+1, 1, 1))
    elif period == "month" and year and month:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year+1, 1, 1)
        else:
            end = datetime(year, month+1, 1)
        all_txns = all_txns.filter(BusinessTransaction.date_time >= start, BusinessTransaction.date_time < end)
    transactions = all_txns.order_by(BusinessTransaction.date_time.desc()).all()

    # Only show transactions where this business EARNED cashback/commission
    filtered = []
    for t in transactions:
        earned = False
        if t.business_referral_id == ref_code and t.cash_back > 0:
            earned = True
        if t.tier2_business_referral_id == ref_code and t.tier2_commission > 0:
            earned = True
        if t.tier3_business_referral_id == ref_code and t.tier3_commission > 0:
            earned = True
        if t.tier4_business_referral_id == ref_code and t.tier4_commission > 0:
            earned = True
        if t.tier5_business_referral_id == ref_code and t.tier5_commission > 0:
            earned = True
        if hasattr(t, "sponsoree_mutual_referral_id") and t.sponsoree_mutual_referral_id == ref_code and (t.sponsoree_mutual_commission or 0) > 0:
            earned = True
        if earned:
            filtered.append(t)
    transactions = filtered

    # Stats/summary using the stored, capped ad_fee for each transaction
    tier1_txns = [txn for txn in transactions if txn.business_referral_id == ref_code]
    gross_earnings = sum(txn.amount for txn in tier1_txns)
    ad_fee = sum(txn.ad_fee or 0 for txn in tier1_txns)
    net_gross = sum(txn.amount - (txn.ad_fee or 0) for txn in tier1_txns)
    marketing_roi = int((net_gross / ad_fee) * 100) if ad_fee else 0
    marketing_ratio = round((net_gross + ad_fee) / ad_fee, 2) if ad_fee else 0

    tier1_earnings = sum(t.cash_back for t in transactions if t.business_referral_id == ref_code)
    tier2_earnings = sum(t.tier2_commission for t in transactions if t.tier2_business_referral_id == ref_code)
    tier3_earnings = sum(t.tier3_commission for t in transactions if t.tier3_business_referral_id == ref_code)
    tier4_earnings = sum(t.tier4_commission for t in transactions if t.tier4_business_referral_id == ref_code)
    tier5_earnings = sum(t.tier5_commission for t in transactions if t.tier5_business_referral_id == ref_code)
    sponsoree_mutual_earnings = sum(
        t.sponsoree_mutual_commission or 0 for t in transactions if hasattr(t, "sponsoree_mutual_referral_id") and t.sponsoree_mutual_referral_id == ref_code
    )
    total_cash_back = tier1_earnings + tier2_earnings + tier3_earnings + tier4_earnings + tier5_earnings + sponsoree_mutual_earnings

    summary = dict(
        gross_earnings=f"{gross_earnings:,.2f}",
        ad_fee=f"{ad_fee:,.2f}",
        net_gross=f"{net_gross:,.2f}",
        marketing_roi=marketing_roi,
        marketing_ratio=marketing_ratio,
        tier1_earnings=f"{tier1_earnings:,.2f}",
        tier2_earnings=f"{tier2_earnings:,.2f}",
        tier3_earnings=f"{tier3_earnings:,.2f}",
        tier4_earnings=f"{tier4_earnings:,.2f}",
        tier5_earnings=f"{tier5_earnings:,.2f}",
        sponsoree_mutual_earnings=f"{sponsoree_mutual_earnings:,.2f}",  # <- add this
        total_cash_back=f"{total_cash_back:,.2f}",
        period=period,
        year=year,
        month=month
    )

    return render_template(
        "business_earnings.html",
        transactions=transactions,
        summary=summary,
        business=business,
        today=date.today(),
        period=period,
        year=year,
        month=month
    )

@app.route("/export_business_earnings_csv")
@business_login_required
def export_business_earnings_csv():
    biz_id = session.get('business_id')
    business = Business.query.get_or_404(biz_id)
    ref_code = business.referral_code

    period = request.args.get("period", "all")
    year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    month = int(request.args.get("month", 0)) if request.args.get("month") else 0
    all_txns = BusinessTransaction.query

    if period == "year" and year:
        all_txns = all_txns.filter(BusinessTransaction.date_time >= datetime(year, 1, 1), BusinessTransaction.date_time < datetime(year+1, 1, 1))
    elif period == "month" and year and month:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year+1, 1, 1)
        else:
            end = datetime(year, month+1, 1)
        all_txns = all_txns.filter(BusinessTransaction.date_time >= start, BusinessTransaction.date_time < end)
    transactions = all_txns.order_by(BusinessTransaction.date_time.desc()).all()

    filtered = []
    for t in transactions:
        earned = False
        if t.business_referral_id == ref_code and t.cash_back > 0:
            earned = True
        if t.tier2_business_referral_id == ref_code and t.tier2_commission > 0:
            earned = True
        if t.tier3_business_referral_id == ref_code and t.tier3_commission > 0:
            earned = True
        if t.tier4_business_referral_id == ref_code and t.tier4_commission > 0:
            earned = True
        if t.tier5_business_referral_id == ref_code and t.tier5_commission > 0:
            earned = True
        # Also include mutual as sponsoree (matches below)
        if t.sponsoree_mutual_referral_id == ref_code and t.sponsoree_mutual_commission > 0:
            earned = True
        if earned:
            filtered.append(t)
    transactions = filtered

    all_businesses = Business.query.all()
    business_lookup = {b.referral_code: b for b in all_businesses}

    tier1_txns = [txn for txn in transactions if txn.business_referral_id == ref_code]
    gross_earnings = sum(txn.amount for txn in tier1_txns)
    ad_fee = sum(txn.ad_fee or 0 for txn in tier1_txns)
    net_gross = sum(txn.amount - (txn.ad_fee or 0) for txn in tier1_txns)
    marketing_roi = int((net_gross / ad_fee) * 100) if ad_fee else 0
    marketing_ratio = round((net_gross + ad_fee) / ad_fee, 2) if ad_fee else 0

    tier1_earnings = sum(t.cash_back for t in transactions if t.business_referral_id == ref_code)
    tier2_earnings = sum(t.tier2_commission for t in transactions if t.tier2_business_referral_id == ref_code)
    tier3_earnings = sum(t.tier3_commission for t in transactions if t.tier3_business_referral_id == ref_code)
    tier4_earnings = sum(t.tier4_commission for t in transactions if t.tier4_business_referral_id == ref_code)
    tier5_earnings = sum(t.tier5_commission for t in transactions if t.tier5_business_referral_id == ref_code)
    sponsoree_mutual_earnings = sum(
        t.sponsoree_mutual_commission or 0 for t in transactions if hasattr(t, "sponsoree_mutual_referral_id") and t.sponsoree_mutual_referral_id == ref_code
    )
    total_cash_back = (tier1_earnings + tier2_earnings + tier3_earnings +
                       tier4_earnings + tier5_earnings + sponsoree_mutual_earnings)
    
    import csv
    from io import StringIO
    si = StringIO()
    writer = csv.writer(si)

    writer.writerow([f"Business summary for '{business.business_name}' from Perk Miner"])
    writer.writerow([])
    writer.writerow([f"Gross Sales (Tier1/Self):", f"${gross_earnings:,.2f}"])
    writer.writerow([f"Advertising Fee (10%, cap $250):", f"${ad_fee:,.2f}"])
    writer.writerow([f"Net Gross Sales:", f"${net_gross:,.2f}"])
    writer.writerow([f"Marketing ROI:", f"{marketing_roi}%"])
    writer.writerow([f"Marketing ROI Ratio:", f"{marketing_ratio}:1"])
    writer.writerow([])
    writer.writerow([f"Tier 1 (Self, 1% Cash Back):", f"${tier1_earnings:,.2f}"])
    writer.writerow([f"Tier 2 (B2B Commission):", f"${tier2_earnings:,.2f}"])
    writer.writerow([f"Tier 3 (B2B Commission):", f"${tier3_earnings:,.2f}"])
    writer.writerow([f"Tier 4 (B2B Commission):", f"${tier4_earnings:,.2f}"])
    writer.writerow([f"Tier 5 (B2B Commission):", f"${tier5_earnings:,.2f}"])
    writer.writerow([f"Sponsoree Mutual Commission (0.25%):", f"${sponsoree_mutual_earnings:,.2f}"])
    writer.writerow([])
    writer.writerow([f"Total Cash Back (All Tiers):", f"${total_cash_back:,.2f}"])
    writer.writerow([])

    writer.writerow([
        "Date/Time",
        "Interaction ID",
        "Sale Amount",
        "Ad Fee (capped)",
        "Net Gross",
        "Marketing ROI",
        "Marketing ROI Ratio",
        "Tier 1 (1% Cash Back)",
        "Tier 2 (B2B Commission)",
        "Tier 3 (B2B Commission)",
        "Tier 4 (B2B Commission)",
        "Tier 5 (B2B Commission)",
        "Sponsoree Mutual Commission (0.25%)"
    ])

    for txn in transactions:
        net_gross_row = txn.amount - (txn.ad_fee or 0)
        roi_row = (net_gross_row / (txn.ad_fee or 1)) * 100 if txn.ad_fee else 0
        ratio_row = (net_gross_row + (txn.ad_fee or 0)) / (txn.ad_fee or 1) if txn.ad_fee else 0

        mutual_comm_val = ""
        if getattr(txn, "sponsoree_mutual_referral_id", None) == ref_code:
            mutual_comm_val = f"{txn.sponsoree_mutual_commission:,.2f}"
        writer.writerow([
            txn.date_time.strftime('%Y-%m-%d %I:%M %p'),
            txn.interaction_id,
            f"{txn.amount:,.2f}",
            f"{txn.ad_fee or 0:,.2f}",
            f"{net_gross_row:,.2f}",
            f"{roi_row:.0f}%",
            f"{ratio_row:.1f}:1",
            f"{txn.cash_back:,.2f}" if txn.business_referral_id == ref_code else "",
            f"{txn.tier2_commission:,.2f}" if txn.tier2_business_referral_id == ref_code else "",
            f"{txn.tier3_commission:,.2f}" if txn.tier3_business_referral_id == ref_code else "",
            f"{txn.tier4_commission:,.2f}" if txn.tier4_business_referral_id == ref_code else "",
            f"{txn.tier5_commission:,.2f}" if txn.tier5_business_referral_id == ref_code else "",
            mutual_comm_val
        ])

    output = si.getvalue()
    return Response(output, mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=business_earnings.csv"})

@app.route("/business/scan-qr/<int:interaction_id>")
@business_login_required
def scan_qr(interaction_id):
    # business protection, etc
    return render_template("scan_qr.html", interaction_id=interaction_id)

@app.route("/business/register", methods=["GET", "POST"])
def business_register():
    form = BusinessRegisterForm()
    # Populate referral_code field from ?ref= on GET (invite link)
    ref_code = request.args.get('ref')
    if request.method == "GET" and ref_code:
        form.referral_code.data = ref_code

    if form.validate_on_submit():
        business_name = form.business_name.data.strip()
        business_email = form.business_email.data.strip().lower()
        password = form.password.data
        referral_code = form.referral_code.data.strip()
        if not valid_password(password):
            flash(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
            return redirect(url_for("business_register"))
        if not valid_email(business_email):
            flash("Invalid email format.")
            return redirect(url_for("business_register"))
        if Business.query.filter_by(business_email=business_email).first():
            flash("Email already registered.")
            return redirect(url_for("business_register"))
        if Business.query.filter_by(business_name=business_name).first():
            flash("Business name already registered.")
            return redirect(url_for("business_register"))
        sponsor_id = None
        user_sponsor_id = None
        if referral_code:
            sponsor = Business.query.filter_by(referral_code=referral_code).first()
            if sponsor:
                sponsor_id = sponsor.id
            else:
                user_sponsor = User.query.filter_by(referral_code=referral_code).first()
                if user_sponsor:
                    user_sponsor_id = user_sponsor.id
                else:
                    flash("Invalid referral code.")
                    return redirect(url_for("business_register"))
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        biz_ref_code = random_business_code(business_name)
        email_code = random_email_code()
        new_biz = Business(
            business_name=business_name,
            business_email=business_email,
            password=hashed_pw,
            referral_code=biz_ref_code,
            sponsor_id=sponsor_id,
            user_sponsor_id=user_sponsor_id,
            email_confirmed=False,
            email_code=email_code,
        )
        db.session.add(new_biz)
        db.session.commit()
        send_business_verification_email(new_biz)
        session['pending_business_email'] = business_email
        session['last_business_verification_sent'] = time.time()
        return redirect(url_for("business_verify_email"))
    return render_template("business_register.html", form=form)

@app.route("/business/verify_email", methods=["GET", "POST"])
def business_verify_email():
    form = VerifyCodeForm()
    pending_email = session.get('pending_business_email')
    biz = Business.query.filter_by(business_email=pending_email).first() if pending_email else None
    can_resend = False
    wait_seconds = 0
    if not biz:
        flash("No registration found to verify.")
        return redirect(url_for("business_register"))
    if biz.email_confirmed:
        flash("Email is already confirmed, please log in.")
        return redirect(url_for("business_login"))
    now = time.time()
    last_sent = session.get('last_business_verification_sent', 0)
    if now - last_sent >= 30:
        can_resend = True
    else:
        wait_seconds = int(30 - (now - last_sent))
    if form.validate_on_submit():
        submitted_code = form.code.data.strip().upper()
        if submitted_code == biz.email_code:
            biz.email_confirmed = True
            db.session.commit()
            flash("Business email confirmed! You can now log in.")
            session.pop("pending_business_email", None)
            return redirect(url_for("business_login"))
        else:
            flash("Incorrect code.")
    return render_template("business_verify_email.html", form=form, can_resend=can_resend, wait_seconds=wait_seconds)

@app.route("/business/resend_verification", methods=["POST"])
def business_resend_verification():
    pending_email = session.get('pending_business_email')
    biz = Business.query.filter_by(business_email=pending_email).first() if pending_email else None
    if biz and not biz.email_confirmed:
        biz.email_code = random_email_code()
        db.session.commit()
        send_business_verification_email(biz)
        session['last_business_verification_sent'] = time.time()
        flash("Verification email resent.")
    return redirect(url_for("business_verify_email"))

@app.route("/business/activate/<code>")
def business_activate(code):
    biz = Business.query.filter_by(email_code=code).first()
    if biz:
        biz.email_confirmed = True
        db.session.commit()
        flash("Email confirmed! You can now log in as a business.")
        session.pop('pending_business_email', None)
        return redirect(url_for("business_login"))
    else:
        flash("Invalid or expired code.")
        return redirect(url_for("business_register"))

@app.route("/business/login", methods=["GET", "POST"])
def business_login():
    form = BusinessLoginForm()
    message = ""
    if form.validate_on_submit():
        business_email = form.business_email.data.strip().lower()
        password = form.password.data
        biz = Business.query.filter_by(business_email=business_email).first()
        if biz and bcrypt.check_password_hash(biz.password, password):
            if biz.is_suspended:
                flash('Account suspended, contact <a href="mailto:fromperkpay@gmail.com">Contact Support</a>.', 'danger')
                return redirect(url_for("business_login"))
            if not biz.email_confirmed:
                message = "Please confirm your business email first (check your inbox)."
                session['pending_business_email'] = business_email
                return redirect(url_for("business_verify_email"))
            else:
                # --- 2FA CODE ---
                code = str(random.randint(100000, 999999))
                session['pending_2fa_code'] = code
                session['pending_2fa_biz_id'] = biz.id
                send_email(
                    biz.business_email,
                    "Your PerkMiner Business Login Code",
                    f"<p>Your PerkMiner business login code is: <b>{code}</b></p>"
                )
                flash("A login code has been sent to your email.")
                return redirect(url_for("two_factor_biz"))
        else:
            message = "Login failed. Check business email and password."
    return render_template("business_login.html", message=message, form=form)

@app.route("/two_factor_biz", methods=["GET", "POST"])
def two_factor_biz():
    form = TwoFactorForm()
    if form.validate_on_submit():
        code_entered = form.code.data.strip()
        code_expected = session.get('pending_2fa_code')
        biz_id = session.get('pending_2fa_biz_id')
        if code_expected and code_entered == code_expected and biz_id:
            session['business_id'] = biz_id
            # Clean up session
            session.pop('pending_2fa_code', None)
            session.pop('pending_2fa_biz_id', None)
            flash("Login successful!")
            return redirect(url_for("business_dashboard"))
        flash("Incorrect code. Please try again.")
    return render_template("two_factor_biz.html", form=form)

@app.route('/business/forgot_password', methods=['GET', 'POST'])
def business_forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        business_email = form.email.data.strip().lower()
        biz = Business.query.filter_by(business_email=business_email).first()
        if biz:
            token = generate_reset_token(business_email, "biz")
            reset_url = url_for('business_reset_password', token=token, _external=True)
            send_reset_email(business_email, reset_url)
        flash("If a business account with that email exists, a link has been sent.")
        return redirect(url_for('business_login'))
    return render_template('forgot_password.html', form=form)

@app.route('/business/reset_password/<token>', methods=['GET', 'POST'])
def business_reset_password(token):
    email = verify_reset_token(token, "biz")
    if not email:
        flash("Reset link invalid or expired.")
        return redirect(url_for('business_login'))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        biz = Business.query.filter_by(business_email=email).first()
        if biz:
            biz.password = bcrypt.generate_password_hash(form.password.data).decode('utf-8')
            db.session.commit()
            flash("Password reset. Please log in.")
            return redirect(url_for('business_login'))
        flash("Account not found.")
    return render_template('reset_password.html', form=form)

@app.route('/business/change_password', methods=['GET', 'POST'])
@business_login_required
def business_change_password():
    form = BusinessChangePasswordForm()
    biz_id = session.get('business_id')
    biz = Business.query.get_or_404(biz_id)

    if form.validate_on_submit():
        # Verify current password
        if not bcrypt.check_password_hash(biz.password, form.current_password.data):
            flash("Current password is incorrect.", "danger")
            return redirect(url_for('business_change_password'))

        # Set new password
        biz.password = bcrypt.generate_password_hash(form.new_password.data).decode('utf-8')
        db.session.commit()
        flash("Your business account password has been updated.", "success")
        return redirect(url_for('business_dashboard'))

    return render_template('business_change_password.html', form=form, business=biz)

@app.route('/business/invite', methods=['POST'])
def business_invite():
    invite_form = BusinessInviteForm()
    biz_id = session.get('business_id')
    biz = Business.query.get(biz_id) if biz_id else None
    if not biz or not invite_form.validate_on_submit():
        flash("Business invite failed. Please log in and use a valid email.")
        return redirect(url_for("business_login"))
    invitee_email = invite_form.invitee_email.data.strip()
    subject = f"{biz.business_name} has invited you to join PerkMiner."
    reg_url = url_for('business_register', ref=biz.referral_code, _external=True)
    video_url = url_for('intro', ref=biz.referral_code, _external=True)
    html_body = build_invite_email(biz.business_name, reg_url, video_url)
    send_email(invitee_email, subject, html_body)
    flash('Business invitation sent!')
    return redirect(url_for('business_dashboard'))

@app.route("/business/interactions")
@business_login_required
def biz_user_interactions():
    # Make sure only a logged-in business can view this
    biz_id = session.get('business_id')
    if not biz_id:
        flash("You must be logged in as a business.")
        return redirect(url_for('business_login'))
    interactions = Interaction.query.filter_by(business_id=biz_id, status='active').order_by(Interaction.created_at.desc()).all()
    return render_template("biz_user_interactions.html", interactions=interactions)

@app.route("/business/interactions/<int:interaction_id>/details")
@business_login_required
def biz_interaction_details(interaction_id):
    biz_id = session.get('business_id')
    if not biz_id:
        flash("You must be logged in as a business.")
        return redirect(url_for('business_login'))
    interaction = Interaction.query.get_or_404(interaction_id)
    # Security: make sure this belongs to the logged-in business!
    if interaction.business_id != biz_id:
        flash("Access denied.")
        return redirect(url_for('biz_user_interactions'))
    return render_template("biz_request_details.html", interaction=interaction)

@app.route("/business/session/<int:interaction_id>", methods=["GET", "POST"])
@business_login_required
def biz_active_session(interaction_id):
    biz_id = session.get('business_id')
    if not biz_id:
        flash("You must be logged in as a business.")
        return redirect(url_for('business_login'))
    interaction = Interaction.query.get_or_404(interaction_id)
    if interaction.business_id != biz_id:
        flash("Access denied.")
        return redirect(url_for('biz_user_interactions'))

    # Detect this as a business view
    is_user = False
    is_biz = True

    # Support POST (sending messages) if you want chat from business side too
    if request.method == "POST":
        text = request.form.get("message_text", "").strip()
        uploaded_file = request.files.get("message_file")
        file_url = None
        file_name = None
        if uploaded_file and uploaded_file.filename:
            filename = secure_filename(uploaded_file.filename)
            upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            uploaded_file.save(upload_path)
            file_url = url_for('uploaded_file', filename=filename)
            file_name = uploaded_file.filename
        if text or file_url:
            msg = Message(
                interaction_id=interaction.id,
                sender_type="business",
                sender_id=biz_id,
                text=text,
                file_url=file_url,
                file_name=file_name
            )
            db.session.add(msg)
            db.session.commit()
            return redirect(url_for('biz_active_session', interaction_id=interaction.id))

    messages = Message.query.filter_by(interaction_id=interaction.id).order_by(Message.timestamp).all()
    # Render the same template you use for users
    return render_template(
        "active_session.html",
        interaction=interaction,
        is_user=is_user,
        is_biz=is_biz,
        messages=messages
    )

@app.route("/business/dashboard", methods=["GET", "POST"])
def business_dashboard():
    form = BusinessRewardForm(request.form)
    profile_form = BusinessProfileForm()
    invite_form = BusinessInviteForm()
    biz_id = session.get('business_id')
    biz = Business.query.get(biz_id) if biz_id else None

    if not biz or not biz.email_confirmed:
        flash("Please log in and confirm your business email to access the dashboard.")
        return redirect(url_for("business_login"))

    # ---- Update earnings with 7-day delay ----
    total_biz_earnings, available_biz_earnings, pending_biz_earnings = calculate_business_earnings_split(biz, delay_days=7)

    # summary for business earnings
    biz.grand_total_earnings = total_biz_earnings

    net_available_biz = available_biz_earnings - (biz.withdrawn_total or Decimal(0))
    biz.earnings_balance = net_available_biz

    # NEW: detailed split
    biz.pending_earnings = pending_biz_earnings
    biz.available_to_withdraw = available_biz_earnings

    db.session.commit()

    if request.args.get("fund_success") == "1":
        flash("Funds added to your account!", "success")

    editable_fields = [
        "business_name", "listing_type", "category", "finalization", "perks", "phone_number", "address", "latitude", "longitude",
        "website_url", "about_us", "hours_of_operation", "search_keywords",
        "service_1", "service_2", "service_3", "service_4", "service_5",
        "service_6", "service_7", "service_8", "service_9", "service_10"
    ]

    def get_service_field(n):
        return request.form.get(f"service_{n}", "")

    def safe_float(val):
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    # POST: Handle profile save
    if request.method == "POST":
        updated = False
        if not request.form.get("listing_type"):
            flash("Listing Type is required.")
            return redirect(url_for('business_dashboard'))

        # --- existing profile field updates ---
        if biz.status == "approved":
            for field in editable_fields:
                val = request.form.get(field)
                if field == "perks":
                    setattr(biz, "perks", val)  # always update 'perks' live
                    updated = True
                elif field in ["latitude", "longitude"]:
                    setattr(biz, f"draft_{field}", safe_float(val))
                    updated = True
                else:
                    if val is not None:
                        setattr(biz, f"draft_{field}", val)
                        updated = True
            file = request.files.get('profile_photo')
            if file and allowed_file(file.filename):
                upload_result = cloudinary.uploader.upload(file)
                biz.draft_profile_photo = upload_result.get('secure_url')
                updated = True
        else:
            for field in editable_fields:
                val = request.form.get(field)
                if field == "perks":
                    setattr(biz, "perks", val)
                    updated = True
                elif field in ["latitude", "longitude"]:
                    setattr(biz, field, safe_float(val))
                    updated = True
                else:
                    if val is not None:
                        setattr(biz, field, val)
                        updated = True
            file = request.files.get('profile_photo')
            if file and allowed_file(file.filename):
                upload_result = cloudinary.uploader.upload(file)
                biz.profile_photo = upload_result.get('secure_url')
                updated = True

        # --- NEW: online purchase flags (e‑commerce + allow website purchases) ---

        website_url = request.form.get("website_url", "").strip()
        has_website = bool(website_url)

        # checkbox values from the form
        wants_ecommerce = request.form.get("is_ecommerce_site") == "1"
        wants_allow_purchases = request.form.get("allow_website_purchases") == "1"
        agreed_online_terms = request.form.get("online_terms_agree") == "1"

        # only allow e‑commerce flag if there is a website URL
        new_is_ecommerce = has_website and wants_ecommerce

        # allow_website_purchases can only be true if:
        # - website exists
        # - e‑commerce is checked
        # - merchant has checked "agree to terms"
        if has_website and new_is_ecommerce and wants_allow_purchases and agreed_online_terms:
            new_allow_website_purchases = True
        else:
            new_allow_website_purchases = False

        # store whether terms were agreed (for audit / display)
        new_online_terms_agreed = has_website and new_is_ecommerce and agreed_online_terms

        # apply changes if values changed
        if getattr(biz, "is_ecommerce_site", False) != new_is_ecommerce:
            biz.is_ecommerce_site = new_is_ecommerce
            updated = True

        if getattr(biz, "allow_website_purchases", False) != new_allow_website_purchases:
            biz.allow_website_purchases = new_allow_website_purchases
            updated = True

        if getattr(biz, "online_terms_agreed", False) != new_online_terms_agreed:
            biz.online_terms_agreed = new_online_terms_agreed
            updated = True

        if updated:
            db.session.commit()
            flash("Business profile updated!")

        return redirect(url_for('business_dashboard'))

    # GET: Load profile form data (prefer draft if status=approved and draft exists)
    form_data = {}
    for field in editable_fields:
        draft_field = f"draft_{field}"
        val = getattr(biz, draft_field, None) if biz.status == "approved" else None
        if val not in [None, ""]:
            form_data[field] = val
        else:
            form_data[field] = getattr(biz, field, "")

    # Profile photo logic (show draft if exists, otherwise live)
    if hasattr(biz, "draft_profile_photo") and biz.status == "approved" and biz.draft_profile_photo:
        profile_img_url = biz.draft_profile_photo
    else:
        profile_img_url = biz.profile_photo if biz.profile_photo else None

    latitude = form_data.get("latitude", "")
    longitude = form_data.get("longitude", "")

    # --- Rewards and referral tree logic (unchanged) ---
    if request.method == "GET":
        form.downline_level.data = '1'
        form.invoice_amount.data = 0
    rewards_table = ""; reward = None
    invoice_amount = float(form.invoice_amount.data or 0)
    downline_level = int(form.downline_level.data or 1)
    cap = None
    if not request.method == "POST" or not form.validate_on_submit():
        downline_level = 1
        form.downline_level.data = '1'
        reward = invoice_amount * 0.01
        cap = None
        rewards_desc = "As the business, you earn"
    else:
        invoice_amount = float(form.invoice_amount.data)
        downline_level = int(form.downline_level.data)
        if downline_level == 1:
            reward = invoice_amount * 0.01
            rewards_desc = "As the business, for an invoice you create and get paid for in the amount"
            cap = None
        elif downline_level in [2, 3, 4]:
            rate = 0.002
            cap = 3.75
            reward = min(invoice_amount * rate, cap)
            rewards_desc = f"If a Tier {downline_level} business creates and gets paid for an invoice in the amount"
        elif downline_level == 5:
            rate = 0.02
            cap = 25
            reward = min(invoice_amount * rate, cap)
            rewards_desc = "If a Tier 5 business creates and gets paid for an invoice in the amount"
        else:
            reward = 0
            rewards_desc = ""
    if invoice_amount > 0 and reward is not None:
        if cap:
            rewards_table += f"<h5 class='mt-4 mb-2'>{rewards_desc} of ${invoice_amount:,.2f}:</h5>"
            rewards_table += f"<div class='alert alert-success'>Your business earns <strong>${reward:.2f}</strong> as cashback (capped at ${cap:.2f}).</div>"
        else:
            rewards_table += f"<h5 class='mt-4 mb-2'>{rewards_desc} of ${invoice_amount:,.2f}:</h5>"
            rewards_table += f"<div class='alert alert-success'>Your business earns <strong>${reward:.2f}</strong> as cashback.</div>"

    sponsor = Business.query.get(biz.sponsor_id) if biz.sponsor_id else None
    level2 = Business.query.filter_by(sponsor_id=biz.id).all()
    level3, level4, level5 = [], [], []
    for b2 in level2:
        b3s = Business.query.filter_by(sponsor_id=b2.id).all()
        level3.extend(b3s)
        for b3 in b3s:
            b4s = Business.query.filter_by(sponsor_id=b3.id).all()
            level4.extend(b4s)
            for b4 in b4s:
                b5s = Business.query.filter_by(sponsor_id=b4.id).all()
                level5.extend(b5s)

    # Add active session indicator for dashboard button
    active_biz_sessions = Interaction.query.filter_by(business_id=biz.id, status='active').all()
    has_active_biz_sessions = len(active_biz_sessions) > 0
    active_biz_sessions_count = len(active_biz_sessions)

    # Add this line to fetch payment alerts/awaiting payments
    payment_alerts = Interaction.query.filter_by(
        business_id=biz.id,
        awaiting_payment=True,
        status='active'
    ).all()

    return render_template(
        "business_dashboard.html",
        form=form,
        profile_form=profile_form,
        invite_form=invite_form,
        business=biz,
        payment_alerts=payment_alerts,  # <-- This line passes to template
        form_data=form_data,
        sponsor=sponsor,
        referral_code=biz.referral_code,
        rewards_table=rewards_table,
        level2=level2, level3=level3, level4=level4, level5=level5,
        profile_img_url=profile_img_url,
        phone_number=form_data.get("phone_number",""),
        address=form_data.get("address",""),
        latitude=latitude,
        longitude=longitude,
        active_biz_sessions_count=active_biz_sessions_count,
        has_active_biz_sessions=has_active_biz_sessions,
        total_biz_earnings=total_biz_earnings,
        available_biz_earnings=available_biz_earnings,
        pending_biz_earnings=pending_biz_earnings,
    )

@app.route("/business/logout")
def business_logout():
    session.pop('business_id', None)
    flash("Logged out as business.")
    return redirect(url_for("business_home"))

@app.route('/admin/roles-landing')
@admin_required  # or @role_required('super_admin')
def admin_roles_landing():
    return render_template('admin_roles_landing.html')  # create this template if needed

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    form = LoginForm()
    message = ""
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        password = form.password.data
        user = User.query.filter_by(email=email).first()
        if user and bcrypt.check_password_hash(user.password, password):
            # Require that this user has a super_admin role
            if user.roles and any(r.name == "super_admin" for r in user.roles):
                # 2FA logic starts here
                code = str(random.randint(100000, 999999))
                session['pending_2fa_code'] = code
                session['pending_2fa_admin_id'] = user.id
                send_email(
                    user.email,
                    "Your PerkMiner Admin Login Code",
                    f"<p>Your PerkMiner admin login code is: <b>{code}</b></p>"
                )
                flash("A login code has been sent to your admin email.")
                return redirect(url_for("two_factor_admin"))
            else:
                message = "You are not an admin."
        else:
            message = "Login failed. Check email and password."
    return render_template("admin_login.html", message=message, form=form)

@app.route("/two_factor_admin", methods=["GET", "POST"])
def two_factor_admin():
    form = TwoFactorForm()
    if form.validate_on_submit():
        code_entered = form.code.data.strip()
        code_expected = session.get('pending_2fa_code')
        admin_id = session.get('pending_2fa_admin_id')
        if code_expected and code_entered == code_expected and admin_id:
            user = User.query.get(admin_id)
            if user:
                login_user(user)
                # Clean up session
                session.pop('pending_2fa_code', None)
                session.pop('pending_2fa_admin_id', None)
                flash("Admin login successful!")
                return redirect(url_for("admin_dashboard"))
        flash("Incorrect code. Please try again.")
    return render_template("two_factor_admin.html", form=form)

@app.route('/admin/forgot_password', methods=['GET', 'POST'])
def admin_forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter_by(email=email).first()
        # Only send if super_admin
        if user and user.roles and any(r.name == "super_admin" for r in user.roles):
            token = generate_reset_token(email, "admin")
            reset_url = url_for('admin_reset_password', token=token, _external=True)
            send_reset_email(email, reset_url)
        flash("If an admin account with that email exists, a link has been sent.")
        return redirect(url_for('admin_login'))
    return render_template('forgot_password.html', form=form)

@app.route('/admin/reset_password/<token>', methods=['GET', 'POST'])
def admin_reset_password(token):
    email = verify_reset_token(token, "admin")
    if not email:
        flash("Reset link invalid or expired.")
        return redirect(url_for('admin_login'))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=email).first()
        if user and user.roles and any(r.name == "super_admin" for r in user.roles):
            user.password = bcrypt.generate_password_hash(form.password.data).decode('utf-8')
            db.session.commit()
            flash("Password reset. Please log in.")
            return redirect(url_for('admin_login'))
        flash("Admin account not found.")
    return render_template('reset_password.html', form=form)

@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    # --- Users search ---
    user_query = User.query
    user_search = request.args.get("user_search", "").strip()
    user_role = request.args.get("user_role", "")
    user_status = request.args.get("user_status", "")

    if user_search:
        user_query = user_query.filter(
            (User.email.ilike(f"%{user_search}%")) |
            (User.name.ilike(f"%{user_search}%")) |
            (User.referral_code.ilike(f"%{user_search}%"))
        )
    if user_role:
        user_query = user_query.join(User.roles).filter(Role.name == user_role)
    if user_status == "confirmed":
        user_query = user_query.filter(User.email_confirmed == True)
    elif user_status == "pending":
        user_query = user_query.filter(User.email_confirmed == False)
    elif user_status == "suspended":
        user_query = user_query.filter(User.is_suspended == True)
    users = user_query.all()

    # --- Businesses search ---
    business_query = Business.query
    biz_search = request.args.get("biz_search", "").strip()
    biz_category = request.args.get("biz_category", "")
    biz_status = request.args.get("biz_status", "")

    if biz_search:
        business_query = business_query.filter(
            (Business.business_name.ilike(f"%{biz_search}%")) |
            (Business.business_email.ilike(f"%{biz_search}%")) |
            (Business.referral_code.ilike(f"%{biz_search}%"))
        )
    if biz_category:
        business_query = business_query.filter(Business.category == biz_category)
    if biz_status:
        business_query = business_query.filter(Business.status == biz_status)
    businesses = business_query.all()

    # Now create business_forms AFTER you have 'businesses'
    business_forms = {biz.id: EmptyForm() for biz in businesses}  # Or whatever form you use

    return render_template(
        "admin_dashboard.html",
        users=users,
        businesses=businesses,
        roles=Role.query.all(),
        business_forms=business_forms,
        user_lookup={user.id: user for user in users},
    )

@app.route("/finance-dashboard", methods=["GET"])
@role_required("finance")
def finance_dashboard():
    period = request.args.get("period", "all")
    year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    month = int(request.args.get("month", 0)) if request.args.get("month") else 0

    bqry = BusinessTransaction.query
    uqry = UserTransaction.query
    if period == "year" and year:
        bqry = bqry.filter(
            BusinessTransaction.date_time >= datetime(year, 1, 1),
            BusinessTransaction.date_time < datetime(year+1, 1, 1)
        )
        uqry = uqry.filter(
            UserTransaction.date_time >= datetime(year, 1, 1),
            UserTransaction.date_time < datetime(year+1, 1, 1)
        )
    elif period == "month" and year and month:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        bqry = bqry.filter(
            BusinessTransaction.date_time >= start,
            BusinessTransaction.date_time < end
        )
        uqry = uqry.filter(
            UserTransaction.date_time >= start,
            UserTransaction.date_time < end
        )

    btxns = bqry.all()
    utxns = uqry.all()

    # Filter for main and mutual transactions
    main_btxns = [t for t in btxns if not t.sponsoree_mutual_referral_id]
    mutual_btxns = [t for t in btxns if t.sponsoree_mutual_referral_id]

    # Totals for MAIN transactions only
    total_gross_sales = sum(t.amount for t in main_btxns)
    total_ad_revenue = sum(min(t.amount * 0.10, 250) for t in main_btxns)
    total_transactions = len(main_btxns)

    total_sponsoree_mutual_commission = sum(t.sponsoree_mutual_commission or 0 for t in mutual_btxns)

    # All user commissions and user-biz commissions
    total_paid_members = sum(
        (t.cash_back or 0)
        + (t.tier2_commission or 0)
        + (t.tier3_commission or 0)
        + (t.tier4_commission or 0)
        + (t.tier5_commission or 0)
        + (t.tier1_business_user_commission or 0)
        + (t.tier2_business_user_commission or 0)
        + (t.tier3_business_user_commission or 0)
        + (t.tier4_business_user_commission or 0)
        + (t.tier5_business_user_commission or 0)
        for t in utxns
    )

    # Business payouts: Tier1 always included; Tiers 2-5 only if NOT BIZPerkMiner, plus mutuals
    total_paid_businesses = (
        sum(
            (t.cash_back or 0)
            + biz_tier_commission(t, "tier2_commission", "tier2_business_referral_id")
            + biz_tier_commission(t, "tier3_commission", "tier3_business_referral_id")
            + biz_tier_commission(t, "tier4_commission", "tier4_business_referral_id")
            + biz_tier_commission(t, "tier5_commission", "tier5_business_referral_id")
            for t in main_btxns
        )
        + total_sponsoree_mutual_commission
    )

    # Capital reserves: only those explicitly paid to BIZPerkMiner
    capital_reserves = sum(
        (t.cash_back or 0) if t.business_referral_id == "BIZPerkMiner" else 0
        for t in main_btxns
    )
    capital_reserves += sum(
        (t.tier5_commission or 0) if t.tier5_business_referral_id == "BIZPerkMiner" else 0
        for t in main_btxns
    )

    charitable_contribution_1 = total_ad_revenue * 0.10
    charitable_contribution_2 = total_ad_revenue * 0.005

    net_gross = total_ad_revenue - (
        total_paid_members
        + total_paid_businesses
        + capital_reserves
        + charitable_contribution_1
        + charitable_contribution_2
    )

    operating_capital = net_gross * 0.45
    silent_partners = net_gross * 0.45
    legal_services = net_gross * 0.07
    miscellaneous = net_gross * 0.03

    # Operating Capital breakdown
    real_estate_utilities = operating_capital * 0.30
    employees = operating_capital * 0.40
    webapp_fees = operating_capital * 0.15
    misc_services = operating_capital * 0.15

    # Silent Partners breakdown (with per-partner cap)
    joe = min(silent_partners * 0.125, 10000000)
    marjorie = min(silent_partners * 0.125, 10000000)
    pedro = min(silent_partners * 0.12, 1000000)
    paul_tara = min(silent_partners * 0.055, 500000)
    james = min(silent_partners * 0.05, 350000)
    josh = min(silent_partners * 0.03, 300000)
    angel = min(silent_partners * 0.02, 200000)
    diego = min(silent_partners * 0.02, 200000)
    esther = min(silent_partners * 0.02, 200000)
    reyna = min(silent_partners * 0.02, 200000)
    ramico = min(silent_partners * 0.02, 200000)
    michael = min(silent_partners * 0.02, 200000)
    manuela = min(silent_partners * 0.02, 200000)
    alex_s = min(silent_partners * 0.02, 200000)
    victor_r = min(silent_partners * 0.02, 200000)
    john_paul = min(silent_partners * 0.02, 200000)
    ana_pepe = min(silent_partners * 0.015, 150000)
    karen = min(silent_partners * 0.015, 150000)
    raul = min(silent_partners * 0.015, 150000)
    genesis = min(silent_partners * 0.025, 500000)
    jen = min(silent_partners * 0.025, 500000)
    jj = min(silent_partners * 0.025, 500000)
    dominick = min(silent_partners * 0.025, 500000)
    alex_m = min(silent_partners * 0.025, 500000)
    jose = min(silent_partners * 0.025, 500000)
    tito = min(silent_partners * 0.025, 500000)
    loida = min(silent_partners * 0.015, 250000)
    milvia = min(silent_partners * 0.015, 250000)
    adela = min(silent_partners * 0.015, 250000)
    shelly = min(silent_partners * 0.015, 250000)
    nana = min(silent_partners * 0.015, 250000)

    summary = dict(
        total_gross_sales=f"{total_gross_sales:,.2f}",
        total_ad_revenue=f"{total_ad_revenue:,.2f}",
        total_transactions=total_transactions,
        total_paid_businesses=f"{total_paid_businesses:,.2f}",
        total_sponsoree_mutual_commission=f"{total_sponsoree_mutual_commission:,.2f}",
        total_paid_members=f"{total_paid_members:,.2f}",
        net_gross=f"{net_gross:,.2f}",
        capital_reserves=f"{capital_reserves:,.2f}",
        operating_capital=f"{operating_capital:,.2f}",
        charitable_contribution_1=f"{charitable_contribution_1:,.2f}",
        charitable_contribution_2=f"{charitable_contribution_2:,.2f}",
        silent_partners=f"{silent_partners:,.2f}",
        legal_services=f"{legal_services:,.2f}",
        miscellaneous=f"{miscellaneous:,.2f}",
        real_estate_utilities=f"{real_estate_utilities:,.2f}",
        employees=f"{employees:,.2f}",
        webapp_fees=f"{webapp_fees:,.2f}",
        misc_services=f"{misc_services:,.2f}",
        joe=f"{joe:,.2f}",
        marjorie=f"{marjorie:,.2f}",
        tito=f"{tito:,.2f}",
        pedro=f"{pedro:,.2f}",
        paul_tara=f"{paul_tara:,.2f}",
        james=f"{james:,.2f}",
        angel=f"{angel:,.2f}",
        josh=f"{josh:,.2f}",
        diego=f"{diego:,.2f}",
        esther=f"{esther:,.2f}",
        reyna=f"{reyna:,.2f}",
        ramico=f"{ramico:,.2f}",
        michael=f"{michael:,.2f}",
        manuela=f"{manuela:,.2f}",
        alex_s=f"{alex_s:,.2f}",
        victor_r=f"{victor_r:,.2f}",
        john_paul=f"{john_paul:,.2f}",
        ana_pepe=f"{ana_pepe:,.2f}",
        karen=f"{karen:,.2f}",
        raul=f"{raul:,.2f}",
        genesis=f"{genesis:,.2f}",
        jen=f"{jen:,.2f}",
        jj=f"{jj:,.2f}",
        dominick=f"{dominick:,.2f}",
        alex_m=f"{alex_m:,.2f}",
        jose=f"{jose:,.2f}",
        loida=f"{loida:,.2f}",
        milvia=f"{milvia:,.2f}",
        adela=f"{adela:,.2f}",
        nana=f"{nana:,.2f}",
        shelly=f"{shelly:,.2f}",
        period=period,
        year=year,
        month=month
    )

    return render_template(
        "finance_dashboard.html",
        summary=summary,
        period=period,
        year=year,
        month=month
    )

@app.route("/approve_reject_dashboard")
@role_required("approve_reject_listings")
@login_required
def approve_reject_dashboard():
    pending_listings = Business.query.filter(Business.status.in_(["pending", "in_review"])).all()
    return render_template("approve_reject_dashboard.html", pending_listings=pending_listings)

@app.route("/feedback-dashboard")
@role_required("feedback_moderation")
def feedback_dashboard():
    return render_template("feedback_dashboard.html")

@app.route("/support/session/<int:interaction_id>", methods=["GET", "POST"])
@login_required
def support_session(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)

    # Permissions: only user, business, or support admin can access
    is_user = (current_user.is_authenticated and getattr(current_user, 'id', None) == interaction.user_id)
    is_biz = (session.get('business_id') == interaction.business_id)
    is_support = (current_user.is_authenticated and current_user.has_role("customer_support"))

    if not (is_user or is_biz or is_support):
        flash("Access denied.")
        return redirect(url_for('dashboard'))

    # Handle sending a new message
    if request.method == "POST":
        text = request.form.get("message_text", "").strip()
        uploaded_file = request.files.get("message_file")
        file_url = None
        file_name = None

        if uploaded_file and uploaded_file.filename:
            filename = secure_filename(uploaded_file.filename)
            upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            uploaded_file.save(upload_path)
            file_url = url_for('uploaded_file', filename=filename)
            file_name = uploaded_file.filename

        if text or file_url:
            if is_user:
                sender_type = "user"
                sender_id = current_user.id
            elif is_biz:
                sender_type = "business"
                staff_id = session.get('staff_id')
                sender_id = staff_id if staff_id else session['business_id']
            elif is_support:
                sender_type = "customer_support"
                sender_id = current_user.id
            else:
                flash("Invalid sender.")
                return redirect(url_for('support_session', interaction_id=interaction.id))

            msg = Message(
                interaction_id=interaction.id,
                sender_type=sender_type,
                sender_id=sender_id,
                text=text,
                file_url=file_url,
                file_name=file_name
            )
            db.session.add(msg)
            db.session.commit()
            return redirect(url_for('support_session', interaction_id=interaction.id))

    # FETCH all messages
    messages = Message.query.filter_by(interaction_id=interaction.id).order_by(Message.timestamp).all()

    # Build sender labels
    messages_with_labels = []
    for msg in messages:
        if msg.sender_type == "user":
            label = interaction.user.name or interaction.user.email
        elif msg.sender_type == "business":
            if msg.sender_id == interaction.business.id:
                label = interaction.business.business_name
            else:
                label = f"{interaction.business.business_name} Staff"
        elif msg.sender_type == "customer_support":
            sender = User.query.get(msg.sender_id)
            label = f"Customer Support ({sender.name or sender.email})" if sender else "Customer Support"
        else:
            label = "Unknown"
        messages_with_labels.append({
            "text": msg.text,
            "timestamp": msg.timestamp,
            "sender_label": label,
            "file_url": msg.file_url,
            "file_name": msg.file_name,
        })

    return render_template(
        "support_session.html",
        interaction=interaction,
        messages=messages_with_labels
    )

@app.route("/support-dashboard")
@role_required("customer_support")
def support_dashboard():
    # Only show "Support" service_type
    support_interactions = Interaction.query.filter_by(service_type="Support", status="active").order_by(Interaction.created_at.desc()).all()
    return render_template("support_dashboard.html", interactions=support_interactions)

@app.route("/start-support/<int:business_id>", methods=["GET", "POST"])
@login_required
def start_support(business_id):
    # Only one open support thread per issue
    interaction = Interaction.query.filter_by(
        user_id=current_user.id,
        business_id=business_id,
        service_type="Support",
        status="active"
    ).first()
    if not interaction:
        interaction = Interaction(
            user_id=current_user.id,
            business_id=business_id,
            service_type="Support",
            details="Member reports issue with transaction finalization.",
            status="active"
        )
        db.session.add(interaction)
        db.session.commit()
    return redirect(url_for('support_session', interaction_id=interaction.id))

@app.route("/support/session/<int:interaction_id>/messages")
@login_required
def support_session_messages(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    is_user = current_user.is_authenticated and current_user.id == interaction.user_id
    is_biz = session.get('business_id') == interaction.business_id
    is_support = current_user.is_authenticated and current_user.has_role("customer_support")
    if not (is_user or is_biz or is_support):
        return "", 403

    messages = Message.query.filter_by(interaction_id=interaction.id).order_by(Message.timestamp).all()
    messages_with_labels = []
    for msg in messages:
        if msg.sender_type == "user":
            label = interaction.user.name or interaction.user.email
        elif msg.sender_type == "business":
            if msg.sender_id == interaction.business.id:
                label = interaction.business.business_name
            else:
                label = f"{interaction.business.business_name} Staff"
        elif msg.sender_type == "customer_support":
            sender = User.query.get(msg.sender_id)
            label = f"Customer Support ({sender.name or sender.email})" if sender else "Customer Support"
        else:
            label = "Unknown"
        messages_with_labels.append({
            "text": msg.text,
            "timestamp": msg.timestamp,
            "sender_label": label,
            "file_url": msg.file_url,
            "file_name": msg.file_name,
        })
    return render_template(
        "partials/_support_messages.html",
        interaction=interaction,
        messages=messages_with_labels
    )

@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if any(r.name == 'super_admin' for r in user.roles):
        flash("Cannot delete a super_admin user.")
        return redirect(url_for("admin_dashboard"))
    db.session.delete(user)
    db.session.commit()
    flash(f"User {user.email} deleted.")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/user/<int:user_id>/suspend", methods=["POST"])
@admin_required
def admin_suspend_user(user_id):
    user = User.query.get_or_404(user_id)
    if any(r.name == 'super_admin' for r in user.roles):
        flash("Cannot suspend a super_admin user.")
        return redirect(url_for("admin_dashboard"))
    user.is_suspended = not user.is_suspended
    db.session.commit()
    action = "Suspended" if user.is_suspended else "Unsuspended"
    flash(f"User {user.email} {action.lower()}.")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/listing/<int:listing_id>/start_review", methods=["POST"])
@role_required("approve_reject_listings")
@login_required
def start_review(listing_id):
    biz = Business.query.get_or_404(listing_id)
    if biz.status == "pending":
        biz.status = "in_review"
        db.session.commit()
        flash(f"Listing {biz.business_name} is now in review.")
    return redirect(url_for("approve_reject_dashboard"))

@app.route("/admin/listing/<int:listing_id>/approve", methods=["POST"])
@role_required("approve_reject_listings")
@login_required
def approve_listing(listing_id):
    biz = Business.query.get_or_404(listing_id)
    if biz.status in ["pending", "in_review", "approved"]:
        # Promote draft fields to live fields if needed; this block is unchanged
        promote_fields = [
            "business_name", "listing_type", "category", "finalization", "phone_number", "address", "latitude", "longitude",
            "website_url", "about_us", "hours_of_operation", "search_keywords",
            "service_1", "service_2", "service_3", "service_4", "service_5",
            "service_6", "service_7", "service_8", "service_9", "service_10",
            "profile_photo"
        ]
        if biz.draft_category == "Other" and biz.category not in [None, "", "Other"]:
            biz.draft_category = biz.category
        for field in promote_fields:
            draft_attr = f"draft_{field}"
            draft_value = getattr(biz, draft_attr, None)
            if draft_value not in [None, ""]:
                setattr(biz, field, draft_value)
                setattr(biz, draft_attr, None)

        # Status update
        biz.status = "approved"

        # Add this to record who approved it:
        biz.approved_by = current_user.id   # (or current_user.email if you want email)

        # Only super_admin can update manual_feature
        if current_user.is_authenticated and getattr(current_user, 'has_role', None) and current_user.has_role("super_admin"):
            biz.manual_feature = bool(request.form.get("manual_feature"))
        # Otherwise, ignore

        db.session.commit()
        flash(f"Listing {biz.business_name} approved!" + (" Manually Featured." if getattr(biz, 'manual_feature', False) else ""))
    return redirect(url_for("approve_reject_dashboard"))

@app.route("/admin/listing/<int:listing_id>/reject", methods=["POST"])
@role_required("approve_reject_listings")
@login_required
def reject_listing(listing_id):
    biz = Business.query.get_or_404(listing_id)
    if biz.status in ["pending", "in_review"]:
        biz.status = "rejected"
        db.session.commit()
        flash(f"Listing {biz.business_name} rejected.")
    return redirect(url_for("approve_reject_dashboard"))

@app.route("/admin/assign-roles", methods=["GET", "POST"])
@role_required("super_admin")
def assign_roles():
    users = User.query.all()
    roles = Role.query.all()

    if request.method == "POST":
        # Get the form data (user_id and roles to assign)
        user_id = int(request.form["user_id"])
        chosen_roles = request.form.getlist("roles")

        user = User.query.get(user_id)
        if user:
            # Remove all roles, then add selected ones
            user.roles = []
            for role_name in chosen_roles:
                role = Role.query.filter_by(name=role_name).first()
                if role:
                    user.roles.append(role)
            db.session.commit()
            flash("Roles updated!", "success")
    return render_template(
        "assign_roles.html",  # create this template if it doesn't exist
        users=users,
        roles=roles
    )

@app.route("/listing-disclaimer", methods=["GET", "POST"])
@business_login_required
def listing_disclaimer():
    biz_id = session.get('business_id')
    biz = Business.query.get_or_404(biz_id)

    if request.method == 'POST':
        listing_id = request.form.get("listing_id")
        referral_code = request.form.get("referral_code")
        accept_terms = request.form.get("accept_terms")
        # (You may want to check accept_terms here if using POST to capture consent.)
        if not accept_terms:
            flash("You must accept the terms and conditions.")
            return render_template(
                "listing_disclaimer.html",
                listing_id=listing_id,
                referral_code=referral_code,
                business=biz,
                biz=biz
            )
        return redirect(url_for('send_for_review'))

    # GET (or POST fallback): pull from form or args
    listing_id = request.args.get("listing_id") or request.form.get("listing_id")
    referral_code = request.args.get("referral_code") or request.form.get("referral_code")

    return render_template(
        "listing_disclaimer.html",
        listing_id=listing_id,
        referral_code=referral_code,
        business=biz
    )

@app.route("/send-for-review", methods=["POST"])
@business_login_required
def send_for_review():
    biz_id = session.get('business_id')
    biz = Business.query.get_or_404(biz_id)

    # Always fetch these from the form FIRST
    listing_id = request.form.get("listing_id") or request.args.get("listing_id")
    referral_code = request.form.get("referral_code") or request.args.get("referral_code")

    # Check: redirect if account balance is less than $1
    if biz.account_balance is None or biz.account_balance < 1:
        flash("You must have an account balance of at least $1 to submit your listing. Please fund your account.")
        return redirect(url_for("fund_account"))

    accept_terms = request.form.get("accept_terms")
    if not accept_terms:
        flash("You must accept the terms and conditions.")
        return render_template(
            "listing_disclaimer.html",
            listing_id=listing_id,
            referral_code=referral_code,
            business=biz
        )

    # Only require/upload a doc if it hasn't been uploaded yet (first submission)
    if not biz.business_registration_doc:
        file = request.files.get("business_registration_doc")
        if not file or file.filename == '':
            flash("Business registration document is required.")
            return render_template(
                "listing_disclaimer.html",
                listing_id=listing_id,
                referral_code=referral_code,
                business=biz
            )
        # Save the file securely
        filename = secure_filename(file.filename)
        upload_folder = app.config.get("UPLOAD_FOLDER", "uploads")
        if not os.path.exists(upload_folder):
            os.makedirs(upload_folder)
        filepath = os.path.join(upload_folder, filename)
        file.save(filepath)
        biz.business_registration_doc = filename

    # Mark as pending and commit
    listing = Business.query.get(listing_id)
    if listing:
        listing.status = "pending"
        db.session.commit()
        flash("Listing submitted for admin review. You will be notified after a decision.")
    else:
        flash("Listing not found.")

    return redirect(url_for("business_dashboard"))

@app.route("/admin/user/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_user(user_id):
    user = User.query.get_or_404(user_id)
    form = EditUserForm(obj=user)
    if form.validate_on_submit():
        user.name = form.name.data
        user.email = form.email.data
        db.session.commit()
        flash("User updated.")
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_edit_user.html", user=user, form=form)

@app.route("/admin/business/<int:business_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_business(business_id):
    biz = Business.query.get_or_404(business_id)
    form = BusinessEditForm(obj=biz)
    if form.validate_on_submit():
        biz.business_name = form.business_name.data
        biz.business_email = form.business_email.data
        biz.listing_type = form.listing_type.data
        biz.category = form.category.data
        biz.finalization = form.finalization.data
        biz.phone_number = form.phone_number.data
        biz.address = form.address.data
        db.session.commit()
        flash("Business updated.")
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_edit_business.html", business=biz, form=form)

@app.route("/admin/business/<int:business_id>/suspend", methods=["POST"])
@admin_required
def admin_suspend_business(business_id):
    biz = Business.query.get_or_404(business_id)
    biz.is_suspended = not biz.is_suspended
    db.session.commit()
    action = "Suspended" if biz.is_suspended else "Unsuspended"
    flash(f"Business {biz.business_name} {action.lower()}.")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/business/<int:business_id>/delete", methods=["POST"])
@admin_required
def admin_delete_business(business_id):
    biz = Business.query.get_or_404(business_id)
    db.session.delete(biz)
    db.session.commit()
    flash(f"Business {biz.business_name} deleted.")
    return redirect(url_for("admin_dashboard"))

from flask_login import login_required

@app.route("/listing/<int:biz_id>")
def view_listing(biz_id):
    if not (current_user.is_authenticated or session.get("business_id")):
        return redirect(url_for("login"))

    distance_mi = request.args.get("distance_mi")
    biz = Business.query.get_or_404(biz_id)
    biz.distance_mi = float(distance_mi) if distance_mi else None

    has_website = bool(biz.website_url)
    is_ecom = getattr(biz, "is_ecommerce_site", False)
    allows_web = getattr(biz, "allow_website_purchases", False)
    balance = biz.account_balance or 0.0

    can_shop_online_listing = (
        has_website and
        is_ecom and
        allows_web and
        balance >= 250.0
    )

    show_online_warning = (
        has_website and
        is_ecom and
        allows_web and
        balance < 250.0
    )

    return render_template(
        "large_listing.html",
        business=biz,
        can_shop_online_listing=can_shop_online_listing,
        show_online_warning=show_online_warning,
    )

@app.route("/finance/combined-detailed-report", methods=["GET"])
@role_required("finance")
def combined_detailed_report():
    period = request.args.get("period", "all")
    year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    month = int(request.args.get("month", 0)) if request.args.get("month") else 0
    interaction_id = request.args.get("interaction_id", "").strip()
    user_email = (request.args.get("user_email", "") or "").strip().lower()
    business_email = (request.args.get("business_email", "") or "").strip().lower()

    uq = UserTransaction.query
    bq = BusinessTransaction.query

    if period == "year" and year:
        uq = uq.filter(UserTransaction.date_time >= datetime(year, 1, 1), UserTransaction.date_time < datetime(year+1, 1, 1))
        bq = bq.filter(BusinessTransaction.date_time >= datetime(year, 1, 1), BusinessTransaction.date_time < datetime(year+1, 1, 1))
    elif period == "month" and year and month:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        uq = uq.filter(UserTransaction.date_time >= start, UserTransaction.date_time < end)
        bq = bq.filter(BusinessTransaction.date_time >= start, BusinessTransaction.date_time < end)
    if interaction_id:
        uq = uq.filter(UserTransaction.interaction_id == interaction_id)
        bq = bq.filter(BusinessTransaction.interaction_id == interaction_id)

    user_lookup = {u.referral_code: u for u in User.query.all()}
    business_lookup = {b.referral_code: b for b in Business.query.all()}

    if user_email:
        utrans = [t for t in uq.all() if t.user_referral_id in user_lookup and user_lookup[t.user_referral_id].email.lower() == user_email]
        btrans = []
    elif business_email:
        btrans = [t for t in bq.all() if t.business_referral_id in business_lookup and business_lookup[t.business_referral_id].business_email.lower() == business_email]
        utrans = []
    else:
        utrans = uq.all()
        btrans = bq.all()

    # ==== USER TABS ====
    def user_tier_rows(utrans, ref_field, val_field, tier_label):
        res = []
        for t in utrans:
            uid = getattr(t, ref_field)
            amount = getattr(t, val_field) or 0
            if uid and amount > 0 and uid in user_lookup:
                res.append({
                    "tier": tier_label,
                    "date_time": t.date_time,
                    "interaction_id": t.interaction_id,
                    "user_referral_id": uid,
                    "user_name": user_lookup[uid].name or "",
                    "user_email": user_lookup[uid].email or "",
                    "earning": amount,
                    "transaction_id": t.transaction_id
                })
        return res

    user_rows = []
    user_rows += user_tier_rows(utrans, "user_referral_id", "cash_back", "Tier 1 Cashback")
    user_rows += user_tier_rows(utrans, "tier2_user_referral_id", "tier2_commission", "Tier 2 Commission")
    user_rows += user_tier_rows(utrans, "tier3_user_referral_id", "tier3_commission", "Tier 3 Commission")
    user_rows += user_tier_rows(utrans, "tier4_user_referral_id", "tier4_commission", "Tier 4 Commission")
    user_rows += user_tier_rows(utrans, "tier5_user_referral_id", "tier5_commission", "Tier 5 Commission")
    user_rows += user_tier_rows(utrans, "tier1_business_user_referral_id", "tier1_business_user_commission", "Tier 1 User-Biz")
    user_rows += user_tier_rows(utrans, "tier2_business_user_referral_id", "tier2_business_user_commission", "Tier 2 User-Biz")
    user_rows += user_tier_rows(utrans, "tier3_business_user_referral_id", "tier3_business_user_commission", "Tier 3 User-Biz")
    user_rows += user_tier_rows(utrans, "tier4_business_user_referral_id", "tier4_business_user_commission", "Tier 4 User-Biz")
    user_rows += user_tier_rows(utrans, "tier5_business_user_referral_id", "tier5_business_user_commission", "Tier 5 User-Biz")

    # ==== BUSINESS TABS ====
    def biz_tier_rows(btrans, ref_field, val_field, tier_label):
        res = []
        for t in btrans:
            bid = getattr(t, ref_field)
            earning = getattr(t, val_field) or 0
            if bid and earning > 0 and bid in business_lookup:
                res.append({
                    "tier": tier_label,
                    "date_time": t.date_time,
                    "interaction_id": t.interaction_id,
                    "business_referral_id": bid,
                    "business_name": business_lookup[bid].business_name or "",
                    "business_email": business_lookup[bid].business_email or "",
                    "earning": earning,
                    "transaction_id": t.transaction_id
                })
        return res

    tier1_biz_rows = biz_tier_rows(btrans, "business_referral_id", "cash_back", "Tier 1 Biz Cashback")
    tier2_biz_rows = biz_tier_rows(btrans, "tier2_business_referral_id", "tier2_commission", "Tier 2 Biz Cashback")
    tier3_biz_rows = biz_tier_rows(btrans, "tier3_business_referral_id", "tier3_commission", "Tier 3 Biz Cashback")
    tier4_biz_rows = biz_tier_rows(btrans, "tier4_business_referral_id", "tier4_commission", "Tier 4 Biz Cashback")
    tier5_biz_rows = biz_tier_rows(btrans, "tier5_business_referral_id", "tier5_commission", "Tier 5 Biz Cashback")
    mutual_rows = biz_tier_rows(btrans, "sponsoree_mutual_referral_id", "sponsoree_mutual_commission", "Mutual Sponsoree")

    # ========== SUMMARY LOGIC ==========
    total_user_paid = sum(row["earning"] for row in user_rows)
    total_tier1_biz_paid = sum(row["earning"] for row in tier1_biz_rows)
    total_tier2_biz_paid = sum(row["earning"] for row in tier2_biz_rows)
    total_tier3_biz_paid = sum(row["earning"] for row in tier3_biz_rows)
    total_tier4_biz_paid = sum(row["earning"] for row in tier4_biz_rows)
    total_tier5_biz_paid = sum(row["earning"] for row in tier5_biz_rows)
    total_mutual_paid = sum(row["earning"] for row in mutual_rows)
    grand_total_paid = (
        total_user_paid +
        total_tier1_biz_paid + total_tier2_biz_paid + total_tier3_biz_paid +
        total_tier4_biz_paid + total_tier5_biz_paid + total_mutual_paid
    )
    summary = dict(
        total_user_paid = f"{total_user_paid:,.2f}",
        total_tier1_biz_paid = f"{total_tier1_biz_paid:,.2f}",
        total_tier2_biz_paid = f"{total_tier2_biz_paid:,.2f}",
        total_tier3_biz_paid = f"{total_tier3_biz_paid:,.2f}",
        total_tier4_biz_paid = f"{total_tier4_biz_paid:,.2f}",
        total_tier5_biz_paid = f"{total_tier5_biz_paid:,.2f}",
        total_mutual_paid = f"{total_mutual_paid:,.2f}",
        grand_total_paid = f"{grand_total_paid:,.2f}"
    )

    return render_template(
        "combined_detailed_report.html",
        user_rows=user_rows,
        tier1_biz_rows=tier1_biz_rows,
        tier2_biz_rows=tier2_biz_rows,
        tier3_biz_rows=tier3_biz_rows,
        tier4_biz_rows=tier4_biz_rows,
        tier5_biz_rows=tier5_biz_rows,
        mutual_rows=mutual_rows,
        user_lookup=user_lookup,
        business_lookup=business_lookup,
        period=period,
        year=year,
        month=month,
        interaction_id=interaction_id,
        user_email=user_email,
        business_email=business_email,
        months=[(f"{i}", datetime(2026, i, 1).strftime('%B')) for i in range(1, 13)],
        years=[str(y) for y in range(2026, 2051)],
        summary=summary
    )

@app.route('/finance/combined-detailed-report/export/csv')
@role_required("finance")
def export_combined_detailed_report_csv():
    period = request.args.get("period", "all")
    year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    month = int(request.args.get("month", 0)) if request.args.get("month") else 0
    interaction_id = request.args.get("interaction_id", "").strip()
    user_email = (request.args.get("user_email", "") or "").strip().lower()
    business_email = (request.args.get("business_email", "") or "").strip().lower()

    uq = UserTransaction.query
    bq = BusinessTransaction.query

    # Date/month/year filter logic
    if period == "year" and year:
        uq = uq.filter(UserTransaction.date_time >= datetime(year, 1, 1), UserTransaction.date_time < datetime(year+1, 1, 1))
        bq = bq.filter(BusinessTransaction.date_time >= datetime(year, 1, 1), BusinessTransaction.date_time < datetime(year+1, 1, 1))
    elif period == "month" and year and month:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        uq = uq.filter(UserTransaction.date_time >= start, UserTransaction.date_time < end)
        bq = bq.filter(BusinessTransaction.date_time >= start, BusinessTransaction.date_time < end)
    if interaction_id:
        uq = uq.filter(UserTransaction.interaction_id == interaction_id)
        bq = bq.filter(BusinessTransaction.interaction_id == interaction_id)

    user_lookup = {u.referral_code: u for u in User.query.all()}
    business_lookup = {b.referral_code: b for b in Business.query.all()}

    utrans = uq.all()
    btrans = bq.all()

    csv_rows = []

    def user_tier_rows(utrans, ref_field, val_field, tier_label):
        for t in utrans:
            uid = getattr(t, ref_field)
            amount = getattr(t, val_field) or 0
            if uid and amount > 0 and uid in user_lookup:
                csv_rows.append([
                    "User",
                    tier_label,
                    t.date_time.strftime('%Y-%m-%d %I:%M %p'),
                    t.interaction_id,
                    uid,
                    user_lookup[uid].name or "",
                    user_lookup[uid].email or "",
                    f"{amount:.2f}",
                    t.transaction_id
                ])

    user_tier_rows(utrans, "user_referral_id", "cash_back", "Tier 1 Cashback")
    user_tier_rows(utrans, "tier2_user_referral_id", "tier2_commission", "Tier 2 Commission")
    user_tier_rows(utrans, "tier3_user_referral_id", "tier3_commission", "Tier 3 Commission")
    user_tier_rows(utrans, "tier4_user_referral_id", "tier4_commission", "Tier 4 Commission")
    user_tier_rows(utrans, "tier5_user_referral_id", "tier5_commission", "Tier 5 Commission")
    user_tier_rows(utrans, "tier1_business_user_referral_id", "tier1_business_user_commission", "Tier 1 User-Biz")
    user_tier_rows(utrans, "tier2_business_user_referral_id", "tier2_business_user_commission", "Tier 2 User-Biz")
    user_tier_rows(utrans, "tier3_business_user_referral_id", "tier3_business_user_commission", "Tier 3 User-Biz")
    user_tier_rows(utrans, "tier4_business_user_referral_id", "tier4_business_user_commission", "Tier 4 User-Biz")
    user_tier_rows(utrans, "tier5_business_user_referral_id", "tier5_business_user_commission", "Tier 5 User-Biz")

    def biz_tier_rows(btrans, ref_field, val_field, tier_label):
        for t in btrans:
            bid = getattr(t, ref_field)
            earning = getattr(t, val_field) or 0
            if bid and earning > 0 and bid in business_lookup:
                csv_rows.append([
                    "Business",
                    tier_label,
                    t.date_time.strftime('%Y-%m-%d %I:%M %p'),
                    t.interaction_id,
                    bid,
                    business_lookup[bid].business_name or "",
                    business_lookup[bid].business_email or "",
                    f"{earning:.2f}",
                    t.transaction_id
                ])
    biz_tier_rows(btrans, "business_referral_id", "cash_back", "Tier 1 Biz Cashback")
    biz_tier_rows(btrans, "tier2_business_referral_id", "tier2_commission", "Tier 2 Biz Cashback")
    biz_tier_rows(btrans, "tier3_business_referral_id", "tier3_commission", "Tier 3 Biz Cashback")
    biz_tier_rows(btrans, "tier4_business_referral_id", "tier4_commission", "Tier 4 Biz Cashback")
    biz_tier_rows(btrans, "tier5_business_referral_id", "tier5_commission", "Tier 5 Biz Cashback")
    biz_tier_rows(btrans, "sponsoree_mutual_referral_id", "sponsoree_mutual_commission", "Mutual Sponsoree")

    import csv
    from io import StringIO
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow([
        "RecordType","Tier","Date/Time","Interaction ID","Referral ID","Name","Email","Earning","Transaction ID"
    ])
    for row in csv_rows:
        writer.writerow(row)
    output = si.getvalue()
    return Response(output, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=perkminer_combined_detailed_report.csv"})

@app.route("/finance/business-detailed-report", methods=["GET"])
@role_required("finance")
def business_combined_detail_report():
    business_referral_id = request.args.get("business_referral_id", "").strip()
    business_name = (request.args.get("business_name", "") or "").strip().lower()
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    selected_year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    selected_month = int(request.args.get("month", 0)) if request.args.get("month") else 0

    qry = BusinessTransaction.query

    # Date and month/year filtering
    if selected_year and selected_month:
        start = datetime(selected_year, selected_month, 1)
        if selected_month == 12:
            end = datetime(selected_year + 1, 1, 1)
        else:
            end = datetime(selected_year, selected_month + 1, 1)
        qry = qry.filter(BusinessTransaction.date_time >= start, BusinessTransaction.date_time < end)
    if start_date:
        qry = qry.filter(BusinessTransaction.date_time >= datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        qry = qry.filter(BusinessTransaction.date_time <= datetime.strptime(end_date, "%Y-%m-%d"))
    if business_referral_id:
        qry = qry.filter(
            (BusinessTransaction.business_referral_id == business_referral_id) |
            (BusinessTransaction.tier2_business_referral_id == business_referral_id) |
            (BusinessTransaction.tier3_business_referral_id == business_referral_id) |
            (BusinessTransaction.tier4_business_referral_id == business_referral_id) |
            (BusinessTransaction.tier5_business_referral_id == business_referral_id) |
            (BusinessTransaction.sponsoree_mutual_referral_id == business_referral_id)
        )
    transactions = qry.all()
    business_lookup = {b.referral_code: b for b in Business.query.all()}

    # If business_name given, filter to businesses with that name in any relevant field
    if business_name:
        filtered_ids = [
            b.referral_code
            for b in business_lookup.values()
            if b.business_name and business_name in b.business_name.lower()
        ]
        transactions = [
            t for t in transactions
            if (
                t.business_referral_id in filtered_ids or
                t.tier2_business_referral_id in filtered_ids or
                t.tier3_business_referral_id in filtered_ids or
                t.tier4_business_referral_id in filtered_ids or
                t.tier5_business_referral_id in filtered_ids or
                t.sponsoree_mutual_referral_id in filtered_ids
            )
        ]

    # Aggregate totals per business by any field they earn in
    from collections import defaultdict
    biz_totals = defaultdict(lambda: {
        "referral_id": "",
        "business_name": "",
        "business_email": "",
        "cash_back": 0.0,
        "tier2_commission": 0.0,
        "tier3_commission": 0.0,
        "tier4_commission": 0.0,
        "tier5_commission": 0.0,
        "sponsoree_mutual_commission": 0.0,
        "total_earned": 0.0
    })

    for t in transactions:
        # Tier 1 cashback
        bid = t.business_referral_id
        if bid in business_lookup and t.cash_back:
            rec = biz_totals[bid]
            rec["referral_id"] = bid
            rec["business_name"] = business_lookup[bid].business_name or ""
            rec["business_email"] = business_lookup[bid].business_email or ""
            rec["cash_back"] += t.cash_back
            rec["total_earned"] += t.cash_back
        # Tier 2
        bid = t.tier2_business_referral_id
        if bid in business_lookup and t.tier2_commission:
            rec = biz_totals[bid]
            rec["referral_id"] = bid
            rec["business_name"] = business_lookup[bid].business_name or ""
            rec["business_email"] = business_lookup[bid].business_email or ""
            rec["tier2_commission"] += t.tier2_commission
            rec["total_earned"] += t.tier2_commission
        # Tier 3
        bid = t.tier3_business_referral_id
        if bid in business_lookup and t.tier3_commission:
            rec = biz_totals[bid]
            rec["referral_id"] = bid
            rec["business_name"] = business_lookup[bid].business_name or ""
            rec["business_email"] = business_lookup[bid].business_email or ""
            rec["tier3_commission"] += t.tier3_commission
            rec["total_earned"] += t.tier3_commission
        # Tier 4
        bid = t.tier4_business_referral_id
        if bid in business_lookup and t.tier4_commission:
            rec = biz_totals[bid]
            rec["referral_id"] = bid
            rec["business_name"] = business_lookup[bid].business_name or ""
            rec["business_email"] = business_lookup[bid].business_email or ""
            rec["tier4_commission"] += t.tier4_commission
            rec["total_earned"] += t.tier4_commission
        # Tier 5
        bid = t.tier5_business_referral_id
        if bid in business_lookup and t.tier5_commission:
            rec = biz_totals[bid]
            rec["referral_id"] = bid
            rec["business_name"] = business_lookup[bid].business_name or ""
            rec["business_email"] = business_lookup[bid].business_email or ""
            rec["tier5_commission"] += t.tier5_commission
            rec["total_earned"] += t.tier5_commission
        # Mutual
        bid = t.sponsoree_mutual_referral_id
        if bid in business_lookup and t.sponsoree_mutual_commission:
            rec = biz_totals[bid]
            rec["referral_id"] = bid
            rec["business_name"] = business_lookup[bid].business_name or ""
            rec["business_email"] = business_lookup[bid].business_email or ""
            rec["sponsoree_mutual_commission"] += t.sponsoree_mutual_commission
            rec["total_earned"] += t.sponsoree_mutual_commission

    summary_rows = [
        v for v in biz_totals.values()
        if any([v["cash_back"], v["tier2_commission"], v["tier3_commission"], v["tier4_commission"], v["tier5_commission"], v["sponsoree_mutual_commission"]])
    ]

    months = [(str(m), datetime(2026, m, 1).strftime('%B')) for m in range(1,13)]
    years = [str(y) for y in range(2020, 2051)]

    return render_template(
        "business_combined_detail_report.html",
        summary_rows=summary_rows,
        business_referral_id=business_referral_id,
        business_name=business_name,
        start_date=start_date,
        end_date=end_date,
        month=selected_month,
        year=selected_year,
        months=months,
        years=years
    )

@app.route("/finance/business-detailed-report/export/csv")
@role_required("finance")
def export_business_combined_detail_report_csv():
    business_referral_id = request.args.get("business_referral_id", "").strip()
    business_name = (request.args.get("business_name", "") or "").strip().lower()
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    selected_year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    selected_month = int(request.args.get("month", 0)) if request.args.get("month") else 0

    qry = BusinessTransaction.query
    if selected_year and selected_month:
        start = datetime(selected_year, selected_month, 1)
        if selected_month == 12:
            end = datetime(selected_year + 1, 1, 1)
        else:
            end = datetime(selected_year, selected_month + 1, 1)
        qry = qry.filter(BusinessTransaction.date_time >= start, BusinessTransaction.date_time < end)
    if start_date:
        qry = qry.filter(BusinessTransaction.date_time >= datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        qry = qry.filter(BusinessTransaction.date_time <= datetime.strptime(end_date, "%Y-%m-%d"))
    if business_referral_id:
        qry = qry.filter(
            (BusinessTransaction.business_referral_id == business_referral_id) |
            (BusinessTransaction.tier2_business_referral_id == business_referral_id) |
            (BusinessTransaction.tier3_business_referral_id == business_referral_id) |
            (BusinessTransaction.tier4_business_referral_id == business_referral_id) |
            (BusinessTransaction.tier5_business_referral_id == business_referral_id) |
            (BusinessTransaction.sponsoree_mutual_referral_id == business_referral_id)
        )
    transactions = qry.all()
    business_lookup = {b.referral_code: b for b in Business.query.all()}
    if business_name:
        filtered_ids = [
            b.referral_code
            for b in business_lookup.values()
            if b.business_name and business_name in b.business_name.lower()
        ]
        transactions = [
            t for t in transactions
            if (
                t.business_referral_id in filtered_ids or
                t.tier2_business_referral_id in filtered_ids or
                t.tier3_business_referral_id in filtered_ids or
                t.tier4_business_referral_id in filtered_ids or
                t.tier5_business_referral_id in filtered_ids or
                t.sponsoree_mutual_referral_id in filtered_ids
            )
        ]

    from collections import defaultdict
    biz_totals = defaultdict(lambda: {
        "referral_id": "",
        "business_name": "",
        "business_email": "",
        "cash_back": 0.0,
        "tier2_commission": 0.0,
        "tier3_commission": 0.0,
        "tier4_commission": 0.0,
        "tier5_commission": 0.0,
        "sponsoree_mutual_commission": 0.0,
        "total_earned": 0.0
    })
    for t in transactions:
        # Tier 1 cashback
        bid = t.business_referral_id
        if bid in business_lookup and t.cash_back:
            rec = biz_totals[bid]
            rec["referral_id"] = bid
            rec["business_name"] = business_lookup[bid].business_name or ""
            rec["business_email"] = business_lookup[bid].business_email or ""
            rec["cash_back"] += t.cash_back
            rec["total_earned"] += t.cash_back
        # Tier 2
        bid = t.tier2_business_referral_id
        if bid in business_lookup and t.tier2_commission:
            rec = biz_totals[bid]
            rec["referral_id"] = bid
            rec["business_name"] = business_lookup[bid].business_name or ""
            rec["business_email"] = business_lookup[bid].business_email or ""
            rec["tier2_commission"] += t.tier2_commission
            rec["total_earned"] += t.tier2_commission
        # Tier 3
        bid = t.tier3_business_referral_id
        if bid in business_lookup and t.tier3_commission:
            rec = biz_totals[bid]
            rec["referral_id"] = bid
            rec["business_name"] = business_lookup[bid].business_name or ""
            rec["business_email"] = business_lookup[bid].business_email or ""
            rec["tier3_commission"] += t.tier3_commission
            rec["total_earned"] += t.tier3_commission
        # Tier 4
        bid = t.tier4_business_referral_id
        if bid in business_lookup and t.tier4_commission:
            rec = biz_totals[bid]
            rec["referral_id"] = bid
            rec["business_name"] = business_lookup[bid].business_name or ""
            rec["business_email"] = business_lookup[bid].business_email or ""
            rec["tier4_commission"] += t.tier4_commission
            rec["total_earned"] += t.tier4_commission
        # Tier 5
        bid = t.tier5_business_referral_id
        if bid in business_lookup and t.tier5_commission:
            rec = biz_totals[bid]
            rec["referral_id"] = bid
            rec["business_name"] = business_lookup[bid].business_name or ""
            rec["business_email"] = business_lookup[bid].business_email or ""
            rec["tier5_commission"] += t.tier5_commission
            rec["total_earned"] += t.tier5_commission
        # Mutual
        bid = t.sponsoree_mutual_referral_id
        if bid in business_lookup and t.sponsoree_mutual_commission:
            rec = biz_totals[bid]
            rec["referral_id"] = bid
            rec["business_name"] = business_lookup[bid].business_name or ""
            rec["business_email"] = business_lookup[bid].business_email or ""
            rec["sponsoree_mutual_commission"] += t.sponsoree_mutual_commission
            rec["total_earned"] += t.sponsoree_mutual_commission

    summary_rows = [
        v for v in biz_totals.values()
        if any([v["cash_back"], v["tier2_commission"], v["tier3_commission"], v["tier4_commission"], v["tier5_commission"], v["sponsoree_mutual_commission"]])
    ]

    import csv
    from io import StringIO
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow([
        "Business Referral ID",
        "Business Name",
        "Business Email",
        "Tier 1 Cashback",
        "Tier 2 Cashback",
        "Tier 3 Cashback",
        "Tier 4 Cashback",
        "Tier 5 Cashback",
        "Mutual Sponsoree Commission (0.25% split/capped)",
        "Total Earned"
    ])
    for row in summary_rows:
        writer.writerow([
            row["referral_id"],
            row["business_name"],
            row["business_email"],
            f"{row['cash_back']:.2f}",
            f"{row['tier2_commission']:.2f}",
            f"{row['tier3_commission']:.2f}",
            f"{row['tier4_commission']:.2f}",
            f"{row['tier5_commission']:.2f}",
            f"{row['sponsoree_mutual_commission']:.2f}",
            f"{row['total_earned']:.2f}"
        ])
    output = si.getvalue()
    return Response(output, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=business_combined_detail_report.csv"})

@app.route("/finance/combined-cashback-paid", methods=["GET"])
@role_required("finance")
def combined_cashback_paid():
    period = request.args.get("period", "all")
    year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    month = int(request.args.get("month", 0)) if request.args.get("month") else 0

    uq = UserTransaction.query
    bq = BusinessTransaction.query
    if period == "year" and year:
        uq = uq.filter(UserTransaction.date_time >= datetime(year, 1, 1), UserTransaction.date_time < datetime(year + 1, 1, 1))
        bq = bq.filter(BusinessTransaction.date_time >= datetime(year, 1, 1), BusinessTransaction.date_time < datetime(year + 1, 1, 1))
    elif period == "month" and year and month:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        uq = uq.filter(UserTransaction.date_time >= start, UserTransaction.date_time < end)
        bq = bq.filter(BusinessTransaction.date_time >= start, BusinessTransaction.date_time < end)
    user_cbs = uq.all()
    bus_cbs = bq.all()

    # Only Tier 1 cashback for each (ignore commissions)
    tier1_user_cashback = sum(txn.cash_back for txn in user_cbs)
    tier1_biz_cashback = sum(txn.cash_back for txn in bus_cbs)
    grand_total = tier1_user_cashback + tier1_biz_cashback

    return render_template(
        "combined_cashback_paid.html",
        user_cbs=user_cbs,
        bus_cbs=bus_cbs,
        tier1_user_cashback=tier1_user_cashback,
        tier1_biz_cashback=tier1_biz_cashback,
        grand_total=grand_total,
        period=period,
        year=year,
        month=month,
    )

@app.route('/finance/combined-cashback-paid/export/csv')
@role_required("finance")
def export_finance_cashback_paid_csv():
    period = request.args.get("period", "all")
    year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    month = int(request.args.get("month", 0)) if request.args.get("month") else 0

    uq = UserTransaction.query
    bq = BusinessTransaction.query
    if period == "year" and year:
        uq = uq.filter(UserTransaction.date_time >= datetime(year, 1, 1), UserTransaction.date_time < datetime(year + 1, 1, 1))
        bq = bq.filter(BusinessTransaction.date_time >= datetime(year, 1, 1), BusinessTransaction.date_time < datetime(year + 1, 1, 1))
    elif period == "month" and year and month:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        uq = uq.filter(UserTransaction.date_time >= start, UserTransaction.date_time < end)
        bq = bq.filter(BusinessTransaction.date_time >= start, BusinessTransaction.date_time < end)
    user_cbs = uq.all()
    bus_cbs = bq.all()

    import csv
    from io import StringIO
    si = StringIO()
    writer = csv.writer(si)

    writer.writerow(["Perk Miner Member Commissions and Business Cash Back To Be Paid"])
    writer.writerow([])
    writer.writerow([
        "Type",
        "Referral ID",
        "Email",
        "Date/Time",
        "Tier 1 Cashback"
    ])
    for t in user_cbs:
        user = User.query.filter_by(referral_code=t.user_referral_id).first()
        writer.writerow([
            "User",
            t.user_referral_id,
            user.email if user else "",
            t.date_time.strftime('%Y-%m-%d %I:%M %p'),
            f"{t.cash_back:,.2f}"
        ])
    for t in bus_cbs:
        business = Business.query.filter_by(referral_code=t.business_referral_id).first()
        writer.writerow([
            "Business",
            t.business_referral_id,
            business.business_email if business else "",
            t.date_time.strftime('%Y-%m-%d %I:%M %p'),
            f"{t.cash_back:,.2f}"
        ])

    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=perkminer_combined_cashback_paid.csv"}
    )

@app.route("/finance/commissions-paid", methods=["GET"])
@role_required("finance")
def commissions_paid():
    period = request.args.get("period", "all")
    year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    month = int(request.args.get("month", 0)) if request.args.get("month") else 0

    qry = UserTransaction.query
    if period == "year" and year:
        qry = qry.filter(
            UserTransaction.date_time >= datetime(year, 1, 1),
            UserTransaction.date_time < datetime(year + 1, 1, 1)
        )
    elif period == "month" and year and month:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        qry = qry.filter(
            UserTransaction.date_time >= start,
            UserTransaction.date_time < end
        )

    transactions = qry.all()
    from collections import defaultdict

    users_data = defaultdict(
        lambda: {
            "referral_id": "", "name": "", "email": "",
            "tier2": 0.0, "tier3": 0.0, "tier4": 0.0, "tier5": 0.0,
            "tier1_biz": 0.0, "tier2_biz": 0.0, "tier3_biz": 0.0, "tier4_biz": 0.0, "tier5_biz": 0.0,
            "grand_total": 0.0
        }
    )
    all_users = {u.referral_code: u for u in User.query.all()}

    for t in transactions:
        # User-to-user commissions
        for tier, field, comm_field in [
            ("tier2", t.tier2_user_referral_id, t.tier2_commission),
            ("tier3", t.tier3_user_referral_id, t.tier3_commission),
            ("tier4", t.tier4_user_referral_id, t.tier4_commission),
            ("tier5", t.tier5_user_referral_id, t.tier5_commission),
        ]:
            if field and comm_field > 0 and field in all_users:
                u = all_users[field]
                udata = users_data[u.referral_code]
                udata["referral_id"] = u.referral_code
                udata["name"] = u.name or ""
                udata["email"] = u.email or ""
                udata[tier] += comm_field
                udata["grand_total"] += comm_field
        # User-business commissions
        for biz_tier, field, comm_field in [
            ("tier1_biz", t.tier1_business_user_referral_id, t.tier1_business_user_commission),
            ("tier2_biz", t.tier2_business_user_referral_id, t.tier2_business_user_commission),
            ("tier3_biz", t.tier3_business_user_referral_id, t.tier3_business_user_commission),
            ("tier4_biz", t.tier4_business_user_referral_id, t.tier4_business_user_commission),
            ("tier5_biz", t.tier5_business_user_referral_id, t.tier5_business_user_commission),
        ]:
            if field and comm_field > 0 and field in all_users:
                u = all_users[field]
                udata = users_data[u.referral_code]
                udata["referral_id"] = u.referral_code
                udata["name"] = u.name or ""
                udata["email"] = u.email or ""
                udata[biz_tier] += comm_field
                udata["grand_total"] += comm_field

    users = [u for u in users_data.values() if u["grand_total"] > 0]
    users.sort(key=lambda x: x["grand_total"], reverse=True)

    return render_template(
        "commissions_paid.html",
        users=users,
        period=period,
        year=year,
        month=month
    )

@app.route("/finance/commissions-paid/export/csv")
@role_required("finance")
def export_commissions_paid_csv():
    period = request.args.get("period", "all")
    year = int(request.args.get("year", 0)) if request.args.get("year") else 0
    month = int(request.args.get("month", 0)) if request.args.get("month") else 0

    qry = UserTransaction.query
    if period == "year" and year:
        qry = qry.filter(
            UserTransaction.date_time >= datetime(year, 1, 1),
            UserTransaction.date_time < datetime(year + 1, 1, 1)
        )
    elif period == "month" and year and month:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        qry = qry.filter(
            UserTransaction.date_time >= start,
            UserTransaction.date_time < end
        )
    transactions = qry.all()

    from collections import defaultdict
    users_data = defaultdict(lambda: {
        "referral_id": "",
        "name": "",
        "email": "",
        "tier2": 0.0,
        "tier3": 0.0,
        "tier4": 0.0,
        "tier5": 0.0,
        "tier1_biz": 0.0,
        "tier2_biz": 0.0,
        "tier3_biz": 0.0,
        "tier4_biz": 0.0,
        "tier5_biz": 0.0,
        "grand_total": 0.0
    })
    all_users = {u.referral_code: u for u in User.query.all()}

    for t in transactions:
        # User-to-user commissions
        for tier, field, comm_field in [
            ("tier2", t.tier2_user_referral_id, t.tier2_commission),
            ("tier3", t.tier3_user_referral_id, t.tier3_commission),
            ("tier4", t.tier4_user_referral_id, t.tier4_commission),
            ("tier5", t.tier5_user_referral_id, t.tier5_commission),
        ]:
            if field and comm_field > 0 and field in all_users:
                u = all_users[field]
                udata = users_data[u.referral_code]
                udata["referral_id"] = u.referral_code
                udata["name"] = u.name or ""
                udata["email"] = u.email or ""
                udata[tier] += comm_field
                udata["grand_total"] += comm_field
        # User-business commissions
        for biz_tier, field, comm_field in [
            ("tier1_biz", t.tier1_business_user_referral_id, t.tier1_business_user_commission),
            ("tier2_biz", t.tier2_business_user_referral_id, t.tier2_business_user_commission),
            ("tier3_biz", t.tier3_business_user_referral_id, t.tier3_business_user_commission),
            ("tier4_biz", t.tier4_business_user_referral_id, t.tier4_business_user_commission),
            ("tier5_biz", t.tier5_business_user_referral_id, t.tier5_business_user_commission),
        ]:
            if field and comm_field > 0 and field in all_users:
                u = all_users[field]
                udata = users_data[u.referral_code]
                udata["referral_id"] = u.referral_code
                udata["name"] = u.name or ""
                udata["email"] = u.email or ""
                udata[biz_tier] += comm_field
                udata["grand_total"] += comm_field

    users = sorted(users_data.values(), key=lambda x: x["grand_total"], reverse=True)

    from io import StringIO
    import csv
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow([
        'User Referral ID','Name','Email',
        'Tier 2 (0.25%)','Tier 3 (0.25%)','Tier 4 (0.25%)','Tier 5 (2%)',
        'Tier 1 Biz (1%)','Tier 2 Biz (0.25%)','Tier 3 Biz (0.25%)','Tier 4 Biz (0.25%)','Tier 5 Biz (1%)',
        'Grand Total'
    ])
    for u in users:
        writer.writerow([
            u["referral_id"],
            u["name"],
            u["email"],
            f"{u['tier2']:.2f}",
            f"{u['tier3']:.2f}",
            f"{u['tier4']:.2f}",
            f"{u['tier5']:.2f}",
            f"{u['tier1_biz']:.2f}",
            f"{u['tier2_biz']:.2f}",
            f"{u['tier3_biz']:.2f}",
            f"{u['tier4_biz']:.2f}",
            f"{u['tier5_biz']:.2f}",
            f"{u['grand_total']:.2f}",
        ])
    output = si.getvalue()
    return Response(output, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=commissions_paid.csv"})

# ---------------- STAFF ROUTES ----------------

@app.route("/staff/new", methods=["GET", "POST"])
@business_login_required
def staff_new():
    form = StaffRegisterForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        name = form.name.data.strip()

        # --- UNIQUE EMAIL CHECK HERE ---
        existing_staff = Staff.query.filter_by(email=email).first()
        if existing_staff:
            flash("Email address already in use with another business advertiser!", "danger")
            return render_template("your_staff_form.html", form=form)

        temp_password = secrets.token_urlsafe(10)
        hashed_pw = bcrypt.generate_password_hash(temp_password).decode('utf-8')
        staff = Staff(
            business_id=session["business_id"],
            email=email,
            hashed_password=hashed_pw,
            role="staff",
            is_active=True,
            password_reset_required=True
        )
        db.session.add(staff)
        db.session.commit()

        # Email temp password to staff
        send_email(
            staff.email,
            "Your PerkMiner Staff Login",
            f"""
            <h3>Welcome to PerkMiner!</h3>
            <p>Your staff account has been created.</p>
            <b>Temporary password:</b> {temp_password}<br>
            <b>Login here:</b> <a href="https://www.perkminer.com/staff/login">Staff Login</a><br>
            <br>
            Please change your password after you first log in.
            """
        )

        flash("Staff member created! Login instructions were emailed to the staff member.", "success")
        return redirect(url_for("business_dashboard"))
    return render_template("your_staff_form.html", form=form)

@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    form = StaffLoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        password = form.password.data
        staff = Staff.query.filter_by(email=email, is_active=True).first()
        if staff and bcrypt.check_password_hash(staff.hashed_password, password):
            code = str(random.randint(100000, 999999))
            session['pending_staff_2fa_code'] = code
            session['pending_staff_id'] = staff.id
            session['pending_staff_email'] = staff.email  # <-- ADD THIS
            send_email(
                staff.email,
                "Your PerkMiner Staff Login Code",
                f"<p>Your staff login code is: <b>{code}</b></p>"
            )
            flash("A login code has been sent to your email.")
            return redirect(url_for("staff_2fa"))
        else:
            flash("Invalid email or password.", "danger")
    return render_template("staff_login.html", form=form)

@app.route("/staff/2fa", methods=["GET", "POST"])
def staff_2fa():
    form = Staff2FAForm()
    staff_id = session.get('pending_staff_id')
    code_expected = session.get('pending_staff_2fa_code')
    if form.validate_on_submit():
        code_entered = form.code.data.strip()
        staff = Staff.query.get(staff_id)
        if code_expected and code_entered == code_expected and staff_id:
            session.clear()
            session['staff_id'] = staff_id
            # Force password change if required
            if staff.password_reset_required:
                return redirect(url_for("staff_change_password"))
            flash("Staff login successful!")
            return redirect(url_for("staff_dashboard"))
        flash("Incorrect code. Please try again.")
    return render_template("staff_2fa.html", form=form)

@app.route("/staff/resend-code")
def staff_resend_code():
    staff_email = session.get("pending_staff_email")
    if not staff_email:
        flash("Start the login process first.", "warning")
        return redirect(url_for('staff_login'))

    code = str(random.randint(100000, 999999))
    session['pending_staff_2fa_code'] = code
    send_email(
        staff_email,
        "Your PerkMiner Staff Login Code",
        f"<p>Your staff login code is: <b>{code}</b></p>"
    )
    flash("A new login code has been sent to your email.", "success")
    return redirect(url_for('staff_2fa'))

@app.route("/staff/change_password", methods=["GET", "POST"])
def staff_change_password():
    staff_id = session.get("staff_id")
    if not staff_id:
        flash("You must be logged in as staff.", "danger")
        return redirect(url_for("staff_login"))
    staff = Staff.query.get(staff_id)
    form = StaffChangePasswordForm()
    if form.validate_on_submit():
        if not bcrypt.check_password_hash(staff.hashed_password, form.current_password.data):
            flash("Current password is incorrect.", "danger")
        else:
            staff.hashed_password = bcrypt.generate_password_hash(form.new_password.data).decode('utf-8')
            staff.password_reset_required = False  # Flag is cleared!
            db.session.commit()
            flash("Password updated successfully!", "success")
            return redirect(url_for("staff_dashboard"))
    return render_template("staff_change_password.html", form=form)

@app.route("/staff/dashboard")
@staff_password_reset_required
def staff_dashboard():
    staff_id = session.get("staff_id")
    if not staff_id:
        return redirect(url_for("staff_login"))
    staff = Staff.query.get(staff_id)
    # Get sessions for this staff's business
    interactions = Interaction.query.filter_by(business_id=staff.business_id, status="active").order_by(Interaction.created_at.desc()).all()
    return render_template("staff_dashboard.html", staff=staff, interactions=interactions)

@app.route("/staff/logout")
def staff_logout():
    session.pop('staff_id', None)
    flash("Logged out as staff.")
    return redirect(url_for("business_home"))

@app.route("/staff/forgot-password", methods=["GET", "POST"])
def staff_forgot_password():
    form = StaffForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        staff = Staff.query.filter_by(email=email).first()
        if staff:
            token = generate_reset_token(email, "staff")
            reset_url = url_for('staff_reset_password', token=token, _external=True)
            send_reset_email(email, reset_url)
        flash("If the staff email exists, reset instructions have been sent.", "info")
        return redirect(url_for("staff_login"))
    return render_template("staff_forgot_password.html", form=form)

@app.route('/staff/reset_password/<token>', methods=['GET', 'POST'])
def staff_reset_password(token):
    email = verify_reset_token(token, "staff")  # "staff" must match your `user_type` in generate_reset_token
    if not email:
        flash("Reset link invalid or expired.")
        return redirect(url_for('staff_login'))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        staff = Staff.query.filter_by(email=email).first()
        if staff:
            staff.hashed_password = bcrypt.generate_password_hash(form.password.data).decode('utf-8')
            staff.password_reset_required = False  # Or True, if you want a forced change
            db.session.commit()
            flash("Password has been reset. Please log in.", "success")
            return redirect(url_for('staff_login'))
        flash("Account not found.")
    return render_template('reset_password.html', form=form)

@app.route("/staff/session/<int:interaction_id>", methods=["GET", "POST"])
def staff_active_session(interaction_id):
    staff_id = session.get("staff_id")
    if not staff_id:
        flash("Please log in as staff.")
        return redirect(url_for("staff_login"))
    staff = Staff.query.get(staff_id)
    interaction = Interaction.query.filter_by(id=interaction_id, business_id=staff.business_id).first_or_404()

    # Messaging logic
    if request.method == "POST" and "message_text" in request.form:
        text = request.form.get("message_text", "").strip()
        uploaded_file = request.files.get("message_file")
        file_url = None
        file_name = None
        if uploaded_file and uploaded_file.filename:
            filename = secure_filename(uploaded_file.filename)
            upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            uploaded_file.save(upload_path)
            file_url = url_for('uploaded_file', filename=filename)
            file_name = uploaded_file.filename
        if text or file_url:
            msg = Message(
                interaction_id=interaction.id,
                sender_type="business",  # <-- make sure this is "business", not "staff"
                sender_id=staff_id,      # <-- staff's id, not business_id
                text=text,
                file_url=file_url,
                file_name=file_name
            )
            db.session.add(msg)
            db.session.commit()
            return redirect(url_for('staff_active_session', interaction_id=interaction.id))

    messages = Message.query.filter_by(interaction_id=interaction.id).order_by(Message.timestamp).all()
    # Build labels just like in active_session
    messages_with_labels = []
    for msg in messages:
        label = ""
        if msg.sender_type == "user":
            label = interaction.user.name or interaction.user.email
        elif msg.sender_type == "business":
            if msg.sender_id == interaction.business.id:
                label = interaction.business.business_name
            else:
                label = f"{interaction.business.business_name} Staff"
        messages_with_labels.append({
            "text": msg.text,
            "timestamp": msg.timestamp,
            "sender_label": label,
            "file_url": msg.file_url,
            "file_name": msg.file_name,
        })

    return render_template("staff_active_session.html",
                           interaction=interaction,
                           staff=staff,
                           messages=messages_with_labels)

@app.route("/staff/session/<int:interaction_id>/messages")
def staff_session_messages(interaction_id):
    staff_id = session.get("staff_id")
    if not staff_id:
        return "", 403
    interaction = Interaction.query.filter_by(id=interaction_id, business_id=Staff.query.get(staff_id).business_id).first_or_404()

    # Build labeled messages
    messages = Message.query.filter_by(interaction_id=interaction.id).order_by(Message.timestamp).all()
    messages_with_labels = []
    for msg in messages:
        label = ""
        if msg.sender_type == "user":
            label = interaction.user.name or interaction.user.email
        elif msg.sender_type == "business":
            if msg.sender_id == interaction.business.id:
                label = interaction.business.business_name
            else:
                label = f"{interaction.business.business_name} Staff"
        messages_with_labels.append({
            "text": msg.text,
            "timestamp": msg.timestamp,
            "sender_label": label,
            "file_url": msg.file_url,
            "file_name": msg.file_name,
        })

    return render_template(
        "partials/_staff_messages.html",
        interaction=interaction,
        messages=messages_with_labels
    )

@app.route("/staff/scan-qr/<int:interaction_id>")
def staff_scan_qr(interaction_id):
    staff_id = session.get("staff_id")
    if not staff_id:
        flash("Please log in as staff.")
        return redirect(url_for("staff_login"))
    # Optionally check if staff can access this interaction
    return render_template("staff_scan_qr.html", interaction_id=interaction_id)

@app.route("/staff/finalize/<int:interaction_id>", methods=["GET", "POST"])
def staff_finalize_transaction(interaction_id):
    staff_id = session.get("staff_id")
    if not staff_id:
        flash("Please log in as staff.")
        return redirect(url_for("staff_login"))
    staff = Staff.query.get(staff_id)
    interaction = Interaction.query.filter_by(id=interaction_id, business_id=staff.business_id).first_or_404()
    now = datetime.now()
    summary = None
    error_message = None

    if request.method == "POST":
        amount = float(request.form.get("amount"))
        try:
            summary = finalize_interaction(
                interaction,
                staff.business,
                amount,
                staff_id=staff.id,
                source="barcode"  # or "message" if needed
            )
            flash("Transaction finalized and all rewards/commissions assigned!", "success")
            return redirect(url_for("staff_active_session", interaction_id=interaction.id))
        except Exception as e:
            error_message = str(e)

    return render_template(
        "finalize_transaction.html",
        interaction=interaction,
        now=now,
        summary=summary,
        error_message=error_message,  # Add this to display staff fund errors, if any
        account_balance=staff.business.account_balance
    )

@app.route("/staff/session/<int:interaction_id>/quote", methods=["GET", "POST"])
def staff_create_quote(interaction_id):
    staff_id = session.get("staff_id")
    if not staff_id:
        flash("Please log in as staff.")
        return redirect(url_for("staff_login"))
    staff = Staff.query.get(staff_id)
    interaction = Interaction.query.filter_by(id=interaction_id, business_id=staff.business_id).first_or_404()

    # Determine if transaction is finalized (so we can disable quote)
    is_finalized = BusinessTransaction.query.filter_by(interaction_id=interaction.id).first() is not None

    quote = Quote.query.filter_by(interaction_id=interaction.id).first()
    error_message = None

    if is_finalized:
        flash("This session has been finalized. No further quotes may be sent.", "warning")
        # Optionally, redirect or just show the view/read-only version
        return redirect(url_for('staff_active_session', interaction_id=interaction.id))

    if request.method == "POST":
        amount = request.form.get("amount")
        details = request.form.get("details")
        if not amount or not details:
            error_message = "Amount and quote details are required."
            flash(error_message, "danger")
        else:
            try:
                if quote:
                    quote.amount = amount
                    quote.details = details
                else:
                    quote = Quote(
                        interaction_id=interaction.id,
                        amount=amount,
                        details=details
                    )
                    db.session.add(quote)
                db.session.commit()
                flash("Quote sent to user!" if not quote else "Quote was updated!", "success")
                return redirect(url_for('staff_active_session', interaction_id=interaction.id))
            except Exception as e:
                error_message = "Failed to save quote: " + str(e)
                flash(error_message, "danger")
                db.session.rollback()  # Always rollback on error

    return render_template(
        "quote.html",
        interaction=interaction,
        quote=quote,
        is_staff=True,
        error_message=error_message,
        is_finalized=is_finalized  # pass this to hide the quote button in your template
    )

@app.route('/staff/remove/<int:staff_id>', methods=['POST'])
@business_login_required
def remove_staff(staff_id):
    biz_id = session.get("business_id")
    staff_member = Staff.query.filter_by(id=staff_id, business_id=biz_id).first_or_404()
    db.session.delete(staff_member)
    db.session.commit()
    flash("Staff member removed.", "success")
    return redirect(url_for("business_dashboard"))

@app.route("/owner/reports/finalized")
@business_login_required
def finalized_report():
    biz_id = session.get("business_id")
    # Optionally add date filters
    logs = FinalizedTransaction.query.filter_by(business_id=biz_id).order_by(FinalizedTransaction.timestamp.desc()).all()
    staff_lookup = {s.id: s for s in Staff.query.filter_by(business_id=biz_id)}
    return render_template("finalized_report.html", logs=logs, staff_lookup=staff_lookup)

@app.route("/seed_admins_once")
def seed_admins_once():
    from app import db, User, Role, bcrypt
    response = []

    # Roles to create
    role_names = [
        "approve_reject_listings",
        "finance",
        "feedback_moderation",
        "customer_support"
    ]
    roles = {}
    for name in role_names:
        role = Role.query.filter_by(name=name).first()
        if not role:
            role = Role(name=name)
            db.session.add(role)
            response.append(f"Added role: {name}")
        roles[name] = role
    db.session.commit()

    # Create demo admin users if needed
    admins = [
        {
            "email": "admin1@perkminer.com",
            "password": "49pi25yt!@3#",
            "role_names": [
                "approve_reject_listings", "finance", "feedback_moderation", "customer_support"
            ]
        },
        {
            "email": "finance1@perkminer.com",
            "password": "84kf68oe^2&%",
            "role_names": ["finance"]
        }
    ]
    for admin in admins:
        user = User.query.filter_by(email=admin["email"]).first()
        if not user:
            hashed_pw = bcrypt.generate_password_hash(admin["password"]).decode("utf-8")
            user = User(
                email=admin["email"],
                password=hashed_pw,
                email_confirmed=True
            )
            db.session.add(user)
            db.session.commit()
            response.append(f"Created user: {admin['email']}")
        # Assign roles
        for role_name in admin["role_names"]:
            role = Role.query.filter_by(name=role_name).first()
            if role and role not in user.roles:
                user.roles.append(role)
                response.append(f"Granted {role_name} to {admin['email']}")
        db.session.commit()

    # Assign roles to your own account if needed
    target_email = "joejmendez@gmail.com"
    target_roles = ["finance"]  # Change or add as needed
    user = User.query.filter_by(email=target_email).first()
    if user:
        for role_name in target_roles:
            role = Role.query.filter_by(name=role_name).first()
            if role and role not in user.roles:
                user.roles.append(role)
                response.append(f"Granted {role_name} to {target_email}")
        db.session.commit()
    else:
        response.append(f"User {target_email} not found; cannot assign roles.")

    response.append("Seeding complete!")
    return "<br>".join(response)

@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/news")
def news():
    return render_template("news.html")

@app.route("/faq")
def faq():
    return render_template("faq.html")

@app.route("/discontinue")
@business_login_required  # <-- use this if you have it
def discontinue():
    # Get the business_id from session
    biz_id = session.get('business_id')
    if not biz_id:
        flash("You must be logged in as a business.")
        return redirect(url_for("business_login"))

    # Fetch business from the database
    business = Business.query.get_or_404(biz_id)

    # Render the template with the business context
    return render_template("discontinue.html", business=business)

@app.route("/press-release")
def press_release():
    return render_template("press_release.html")

@app.route("/new-featured-businesses")
def new_featured_businesses():
    return render_template('your_template.html', business=business)

@app.route('/onboard/stripe')
@login_required
def onboard_stripe():
    # Check if user has a Stripe Connect account
    if not current_user.stripe_account_id:
        # Create a new Express account
        account = stripe.Account.create(
            type="express",
            email=current_user.email,
        )
        current_user.stripe_account_id = account.id
        db.session.commit()
    # Create Stripe onboarding link
    account_link = stripe.AccountLink.create(
        account=current_user.stripe_account_id,
        refresh_url=url_for('onboard_stripe', _external=True),
        return_url=url_for('dashboard', _external=True),  # or any page you want after onboarding
        type='account_onboarding'
    )
    return redirect(account_link.url)

@app.route('/onboard/business/stripe')
def onboard_business_stripe():
    # Check business login (adjust logic if you use something else)
    business_id = session.get('business_id')
    if not business_id:
        flash("Please log in as a business.")
        return redirect(url_for('business_login'))

    business = Business.query.get(business_id)
    if not business:
        flash("Business not found.")
        return redirect(url_for('business_login'))

    # Create Stripe Express account if not already created
    if not business.stripe_account_id:
        account = stripe.Account.create(
            type="express",
            email=business.business_email  # or use business's admin email
        )
        business.stripe_account_id = account.id
        db.session.commit()

    # Create onboarding link
    account_link = stripe.AccountLink.create(
        account=business.stripe_account_id,
        refresh_url=url_for('onboard_business_stripe', _external=True),
        return_url=url_for('business_dashboard', _external=True),  # Or wherever you want to redirect after onboarding
        type='account_onboarding'
    )
    return redirect(account_link.url)

def get_member_withdrawable(user, delay_days=7):
    """
    Returns (net_available, total_earnings, available_earnings, pending_earnings)
    using your existing 7-day logic and withdrawn_total.
    """
    total_earnings, available_earnings, pending_earnings = calculate_user_earnings_split(
        user, delay_days=delay_days
    )
    withdrawn = user.withdrawn_total or Decimal("0")
    net_available = available_earnings - withdrawn
    return net_available, total_earnings, available_earnings, pending_earnings

def get_investor_withdrawable(user, delay_days=7):
    """
    Returns (net_available, total, available, pending) for silent investor earnings.
    net_available accounts for investor_withdrawn_total.
    """
    total, available, pending = calculate_investor_earnings_split(user, delay_days=delay_days)
    withdrawn = user.investor_withdrawn_total or Decimal("0")
    net_available = available - withdrawn
    return net_available, total, available, pending

def get_business_withdrawable(biz, delay_days=7):
    """
    Returns (net_available, total, available, pending) for business earnings.
    """
    total, available, pending = calculate_business_earnings_split(biz, delay_days=delay_days)
    withdrawn = biz.withdrawn_total or Decimal("0")
    net_available = available - withdrawn
    return net_available, total, available, pending

def ensure_platform_balance_at_least(amount_cents, currency="usd"):
    """
    Checks the *platform* Stripe balance (no Stripe-Account header)
    and returns True if available >= amount_cents.
    """
    bal = stripe.Balance.retrieve()
    available_usd = sum(
        b["amount"]
        for b in bal["available"]
        if b["currency"] == currency
    )
    return available_usd >= amount_cents

@app.route('/withdraw', methods=['POST'])
@login_required
def withdraw():
    """
    Standard member withdrawal (1–3 business days).
    Flow:
      1) check DB (7-day delay + $10 min)
      2) check platform Stripe balance
      3) transfer platform -> member connected account
      4) standard payout from connected account
      5) update withdrawn_total / earnings_balance
    """
    MIN_PAYOUT = Decimal("10")
    user = current_user

    if not user.stripe_account_id:
        flash("Please set up your Stripe payouts first.", "warning")
        return redirect(url_for('onboard_stripe'))

    # 1) compute what is actually withdrawable from DB
    net_available, total_earnings, available_earnings, pending_earnings = get_member_withdrawable(user, delay_days=7)

    if net_available < MIN_PAYOUT:
        flash(f"You need at least ${MIN_PAYOUT} in available earnings (after the 7-day delay) to withdraw.", "warning")
        return redirect(url_for('dashboard'))

    balance_to_withdraw = net_available  # withdraw full available for now

    # standard bank transfer fee (0.25% + $0.35)
    fee = balance_to_withdraw * Decimal("0.0025") + Decimal("0.35")
    payout_amount = (balance_to_withdraw - fee).quantize(Decimal("0.01"))

    if payout_amount <= 0:
        flash("Insufficient balance after the standard payout fee is deducted.", "warning")
        return redirect(url_for('dashboard'))

    amount_cents = int(payout_amount * 100)

    # 2) check platform balance
    if not ensure_platform_balance_at_least(amount_cents):
        flash("Platform balance is too low to fund this payout. Please contact support.", "danger")
        return redirect(url_for('dashboard'))

    try:
        # 3) transfer from platform -> member connected account
        transfer = stripe.Transfer.create(
            amount=amount_cents,
            currency='usd',
            destination=user.stripe_account_id,
            description=f"PerkMiner member earnings transfer for user {user.id}"
        )

        # 4) standard payout from connected account to bank
        payout = stripe.Payout.create(
            amount=amount_cents,
            currency='usd',
            method='standard',
            statement_descriptor="PerkMiner Payout",
            stripe_account=user.stripe_account_id,
        )

        # 5) mark withdrawn on our side using the *gross* we removed from their earnings
        user.withdrawn_total = (user.withdrawn_total or Decimal("0")) + balance_to_withdraw

        # recompute summary fields to stay in sync
        user.grand_total_earnings = total_earnings
        user.pending_earnings = pending_earnings
        user.available_for_withdrawal = available_earnings
        user.earnings_balance = available_earnings - user.withdrawn_total

        db.session.commit()

        flash(
            f"Withdrawal of ${payout_amount:.2f} initiated! "
            f"Standard payout fee: ${fee:.2f} deducted.",
            "success"
        )
    except Exception as e:
        flash(f"Failed to withdraw: {e}", "danger")

    return redirect(url_for('dashboard'))

@app.route('/business/withdraw', methods=['POST'])
def business_withdraw():
    """
    Standard business withdrawal (bank transfer).
    """
    MIN_PAYOUT = Decimal("10")
    business_id = session.get('business_id')
    if not business_id:
        flash("Please log in as a business.")
        return redirect(url_for('business_login'))

    biz = Business.query.get(business_id)
    if not biz:
        flash("Business not found.", "danger")
        return redirect(url_for('business_login'))

    if not biz.stripe_account_id:
        flash("Please set up your Stripe payouts first.", "warning")
        return redirect(url_for('onboard_business_stripe'))

    # 1) compute what is withdrawable
    net_available, total, available, pending = get_business_withdrawable(biz, delay_days=7)

    if net_available < MIN_PAYOUT:
        flash(f"You need at least ${MIN_PAYOUT} in available earnings (after the 7-day delay) to withdraw.", "warning")
        return redirect(url_for('business_dashboard'))

    balance_to_withdraw = net_available

    # standard bank fee for businesses
    fee = balance_to_withdraw * Decimal("0.0025") + Decimal("0.35")
    payout_amount = (balance_to_withdraw - fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if payout_amount <= 0:
        flash("Insufficient balance after the standard payout fee is deducted.", "warning")
        return redirect(url_for('business_dashboard'))

    amount_cents = int(payout_amount * 100)

    # 2) platform balance check
    if not ensure_platform_balance_at_least(amount_cents):
        flash("Platform balance is too low to fund this business payout. Please contact support.", "danger")
        return redirect(url_for('business_dashboard'))

    try:
        # 3) transfer platform -> business connected account
        transfer = stripe.Transfer.create(
            amount=amount_cents,
            currency='usd',
            destination=biz.stripe_account_id,
            description="PerkMiner Business Payout (Standard)"
        )

        # 4) standard payout from business connected account
        payout = stripe.Payout.create(
            amount=amount_cents,
            currency='usd',
            method='standard',
            statement_descriptor="PerkMiner Biz Payout",
            stripe_account=biz.stripe_account_id
        )

        # 5) update DB
        biz.withdrawn_total = (biz.withdrawn_total or Decimal("0")) + balance_to_withdraw

        biz.grand_total_earnings = total
        biz.pending_earnings = pending
        biz.available_to_withdraw = available
        biz.earnings_balance = available - biz.withdrawn_total

        db.session.commit()

        flash(
            f"Business withdrawal of ${payout_amount:.2f} initiated! "
            f"Stripe fee: ${fee:.2f} deducted.",
            "success"
        )
    except Exception as e:
        flash(f"Failed to withdraw: {e}", "danger")

    return redirect(url_for('business_dashboard'))

@app.route('/stripe/update-info')
@login_required
def stripe_update_info():
    if not current_user.stripe_account_id:
        flash("You haven't set up payouts yet.", "warning")
        return redirect(url_for('onboard_stripe'))
    account_link = stripe.AccountLink.create(
        account=current_user.stripe_account_id,
        refresh_url=url_for('stripe_update_info', _external=True),
        return_url=url_for('dashboard', _external=True),
        type='account_onboarding'  # Use onboarding; works for updates too!
    )
    return redirect(account_link.url)

@app.route('/business/stripe/update-info')
def business_stripe_update_info():
    biz_id = session.get('business_id')
    if not biz_id:
        flash("Please log in as a business.")
        return redirect(url_for('business_login'))
    business = Business.query.get(biz_id)
    if not business or not business.stripe_account_id:
        flash("Business payouts not set up yet.", "warning")
        return redirect(url_for('onboard_business_stripe'))
    account_link = stripe.AccountLink.create(
        account=business.stripe_account_id,
        refresh_url=url_for('business_stripe_update_info', _external=True),
        return_url=url_for('business_dashboard', _external=True),
        type='account_onboarding'
    )
    return redirect(account_link.url)

@app.route("/investor_report")
@login_required
def investor_report():
    if not current_user.has_role('silent_investor'):
        abort(403)
    investor_rows = InvestorEarnings.query.filter_by(user_id=current_user.id).order_by(
        InvestorEarnings.year, InvestorEarnings.month
    ).all()
    return render_template(
        "investor_report.html",
        earnings=investor_rows,
        user=current_user
    )

@app.route("/export_investor_earnings_csv")
@login_required
def export_investor_earnings_csv():
    if not current_user.has_role('silent_investor'):
        abort(403)
    rows = InvestorEarnings.query.filter_by(user_id=current_user.id).order_by(
        InvestorEarnings.year, InvestorEarnings.month).all()
    from io import StringIO
    import csv
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(['Year', 'Month', 'Amount', 'Date Added'])
    for r in rows:
        writer.writerow([r.year, r.month, float(r.amount), r.created_at.strftime('%Y-%m-%d') if r.created_at else ""])
    output = si.getvalue()
    from flask import Response
    return Response(output, mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=investor_earnings_{current_user.id}.csv"
    })

MIN_PAYOUT = Decimal("10.00")

@app.route('/withdraw_investor', methods=['POST'])
@login_required
def withdraw_investor():
    """
    Standard silent investor withdrawal (bank transfer, 1–3 business days).
    """
    MIN_PAYOUT = Decimal("10")
    user = current_user

    if not user.stripe_account_id:
        flash("Please set up your Stripe payouts first.", "warning")
        return redirect(url_for('onboard_stripe'))

    # 1) compute what is actually withdrawable
    net_available, total, available, pending = get_investor_withdrawable(user, delay_days=7)

    if net_available < MIN_PAYOUT:
        flash(f"You need at least ${MIN_PAYOUT} in silent investor earnings (after the 7-day delay) to withdraw.", "warning")
        return redirect(url_for('dashboard'))

    balance_to_withdraw = net_available

    # standard bank transfer fee
    fee = balance_to_withdraw * Decimal("0.0025") + Decimal("0.35")
    payout_amount = (balance_to_withdraw - fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if payout_amount <= 0:
        flash("Insufficient balance after the transfer fee is deducted.", "warning")
        return redirect(url_for('dashboard'))

    amount_cents = int(payout_amount * 100)

    # 2) check platform balance
    if not ensure_platform_balance_at_least(amount_cents):
        flash("Platform balance is too low to fund this investor payout. Please contact support.", "danger")
        return redirect(url_for('dashboard'))

    try:
        # 3) transfer platform -> investor connected account
        transfer = stripe.Transfer.create(
            amount=amount_cents,
            currency='usd',
            destination=user.stripe_account_id,
            description="PerkMiner Silent Investor Withdrawal (standard)"
        )

        # 4) standard payout from investor connected account
        payout = stripe.Payout.create(
            amount=amount_cents,
            currency='usd',
            method='standard',
            statement_descriptor="PerkMiner Investor Payout",
            stripe_account=user.stripe_account_id
        )

        # 5) update DB
        user.investor_withdrawn_total = (user.investor_withdrawn_total or Decimal("0")) + balance_to_withdraw

        # recompute investor summary fields
        user.investor_total_earnings = total
        user.pending_investor_earnings = pending
        user.investor_earnings_withdraw_ready = available
        user.investor_earnings_balance = available - user.investor_withdrawn_total

        db.session.commit()

        flash(
            f"Silent investor withdrawal of ${payout_amount:.2f} initiated! "
            f"Stripe fee: ${fee:.2f} deducted.",
            "success"
        )
    except Exception as e:
        flash(f"Silent investor withdrawal failed: {e}", "danger")

    return redirect(url_for('dashboard'))

@app.route('/withdraw_instant', methods=['POST'])
@login_required
def withdraw_instant():
    """
    Instant member withdrawal.
    Flow:
      1) check DB (7-day delay + $10 min)
      2) check platform Stripe balance
      3) transfer platform -> member connected account
      4) instant payout from connected account
      5) update withdrawn_total / earnings_balance
    """
    MIN_PAYOUT = Decimal("10")
    user = current_user

    if not user.stripe_account_id:
        flash("Please set up your Stripe payouts first.", "warning")
        return redirect(url_for('onboard_stripe'))

    # 1) compute what is actually withdrawable
    net_available, total_earnings, available_earnings, pending_earnings = get_member_withdrawable(user, delay_days=7)

    if net_available < MIN_PAYOUT:
        flash(f"You need at least ${MIN_PAYOUT} in available earnings (after the 7-day delay) to withdraw.", "warning")
        return redirect(url_for('dashboard'))

    balance_to_withdraw = net_available

    # instant payout fee: 1.1% + $0.50
    fee = balance_to_withdraw * Decimal("0.011") + Decimal("0.50")
    payout_amount = (balance_to_withdraw - fee).quantize(Decimal("0.01"))

    if payout_amount <= 0:
        flash("Insufficient balance after instant payout fee.", "warning")
        return redirect(url_for('dashboard'))

    amount_cents = int(payout_amount * 100)

    # 2) platform balance check
    if not ensure_platform_balance_at_least(amount_cents):
        flash("Platform balance is too low to fund this instant payout. Please contact support.", "danger")
        return redirect(url_for('dashboard'))

    try:
        # 3) transfer from platform -> member connected account
        transfer = stripe.Transfer.create(
            amount=amount_cents,
            currency='usd',
            destination=user.stripe_account_id,
            description=f"PerkMiner member instant earnings transfer for user {user.id}"
        )

        # 4) instant payout from connected account
        payout = stripe.Payout.create(
            amount=amount_cents,
            currency='usd',
            method='instant',
            statement_descriptor="PerkMiner Payout",
            stripe_account=user.stripe_account_id
        )

        # 5) update DB
        user.withdrawn_total = (user.withdrawn_total or Decimal("0")) + balance_to_withdraw

        user.grand_total_earnings = total_earnings
        user.pending_earnings = pending_earnings
        user.available_for_withdrawal = available_earnings
        user.earnings_balance = available_earnings - user.withdrawn_total

        db.session.commit()

        flash(
            f"Instant withdrawal of ${payout_amount:.2f} initiated! "
            f"Instant payout fee: ${fee:.2f} deducted.",
            "success"
        )
    except Exception as e:
        flash(f"Instant withdrawal failed: {e}", "danger")

    return redirect(url_for('dashboard'))

@app.route('/business/withdraw_instant', methods=['POST'])
def business_withdraw_instant():
    """
    Instant business withdrawal.
    """
    MIN_PAYOUT = Decimal("10")
    business_id = session.get('business_id')
    if not business_id:
        flash("Please log in as a business.")
        return redirect(url_for('business_login'))

    biz = Business.query.get(business_id)
    if not biz:
        flash("Business not found.", "danger")
        return redirect(url_for('business_login'))

    if not biz.stripe_account_id:
        flash("Please set up your Stripe payouts first.", "warning")
        return redirect(url_for('onboard_business_stripe'))

    # 1) compute withdrawable
    net_available, total, available, pending = get_business_withdrawable(biz, delay_days=7)

    if net_available < MIN_PAYOUT:
        flash(f"You need at least ${MIN_PAYOUT} in available earnings (after the 7-day delay) to withdraw.", "warning")
        return redirect(url_for('business_dashboard'))

    balance_to_withdraw = net_available

    # instant payout fee
    fee = balance_to_withdraw * Decimal("0.011") + Decimal("0.50")
    payout_amount = (balance_to_withdraw - fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if payout_amount <= 0:
        flash("Insufficient balance after instant payout fee.", "warning")
        return redirect(url_for('business_dashboard'))

    amount_cents = int(payout_amount * 100)

    # 2) platform balance check
    if not ensure_platform_balance_at_least(amount_cents):
        flash("Platform balance is too low to fund this instant business payout. Please contact support.", "danger")
        return redirect(url_for('business_dashboard'))

    try:
        # 3) transfer platform -> business connected account
        transfer = stripe.Transfer.create(
            amount=amount_cents,
            currency='usd',
            destination=biz.stripe_account_id,
            description="PerkMiner Business Payout (Instant)"
        )

        # 4) instant payout from business connected account
        payout = stripe.Payout.create(
            amount=amount_cents,
            currency='usd',
            method='instant',
            statement_descriptor="PerkMiner Biz Payout",
            stripe_account=biz.stripe_account_id
        )

        # 5) update DB
        biz.withdrawn_total = (biz.withdrawn_total or Decimal("0")) + balance_to_withdraw

        biz.grand_total_earnings = total
        biz.pending_earnings = pending
        biz.available_to_withdraw = available
        biz.earnings_balance = available - biz.withdrawn_total

        db.session.commit()

        flash(
            f"Instant business withdrawal of ${payout_amount:.2f} initiated! "
            f"Instant payout fee: ${fee:.2f} deducted.",
            "success"
        )
    except Exception as e:
        flash(f"Instant withdrawal failed: {e}", "danger")

    return redirect(url_for('business_dashboard'))

@app.route('/withdraw_investor_instant', methods=['POST'])
@login_required
def withdraw_investor_instant():
    """
    Instant silent investor withdrawal.
    """
    MIN_PAYOUT = Decimal("10")
    user = current_user

    if not user.stripe_account_id:
        flash("Please set up your Stripe payouts first.", "warning")
        return redirect(url_for('onboard_stripe'))

    # 1) compute withdrawable
    net_available, total, available, pending = get_investor_withdrawable(user, delay_days=7)

    if net_available < MIN_PAYOUT:
        flash(f"You need at least ${MIN_PAYOUT} in silent investor earnings (after the 7-day delay) to withdraw.", "warning")
        return redirect(url_for('dashboard'))

    balance_to_withdraw = net_available

    # instant payout fee: 1.1% + $0.50
    fee = balance_to_withdraw * Decimal("0.011") + Decimal("0.50")
    payout_amount = (balance_to_withdraw - fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if payout_amount <= 0:
        flash("Insufficient balance after instant payout fee.", "warning")
        return redirect(url_for('dashboard'))

    amount_cents = int(payout_amount * 100)

    # 2) platform balance check
    if not ensure_platform_balance_at_least(amount_cents):
        flash("Platform balance is too low to fund this instant investor payout. Please contact support.", "danger")
        return redirect(url_for('dashboard'))

    try:
        # 3) transfer platform -> investor connected account
        transfer = stripe.Transfer.create(
            amount=amount_cents,
            currency='usd',
            destination=user.stripe_account_id,
            description="PerkMiner Silent Investor Withdrawal (instant)"
        )

        # 4) instant payout from investor connected account
        payout = stripe.Payout.create(
            amount=amount_cents,
            currency='usd',
            method='instant',
            statement_descriptor="PerkMiner Investor Payout",
            stripe_account=user.stripe_account_id
        )

        # 5) update DB
        user.investor_withdrawn_total = (user.investor_withdrawn_total or Decimal("0")) + balance_to_withdraw

        user.investor_total_earnings = total
        user.pending_investor_earnings = pending
        user.investor_earnings_withdraw_ready = available
        user.investor_earnings_balance = available - user.investor_withdrawn_total

        db.session.commit()

        flash(
            f"Instant silent investor withdrawal of ${payout_amount:.2f} initiated! "
            f"Instant payout fee: ${fee:.2f} deducted.",
            "success"
        )
    except Exception as e:
        flash(f"Instant withdrawal failed: {e}", "danger")

    return redirect(url_for('dashboard'))

@app.route("/favorites")
@login_required
def favorites():
    favorites = (
        Favorite.query.filter_by(user_id=current_user.id)
        .join(Business).order_by(Favorite.created_at.desc()).all()
    )
    return render_template("favorites.html", favorites=favorites)

@app.route("/favorite/<int:business_id>/add", methods=["POST"])
@login_required
def add_favorite(business_id):
    existing = Favorite.query.filter_by(user_id=current_user.id, business_id=business_id).first()
    if not existing:
        fav = Favorite(user_id=current_user.id, business_id=business_id)
        db.session.add(fav)
        db.session.commit()
    return redirect(request.referrer or url_for("favorites"))

@app.route("/favorite/<int:business_id>/remove", methods=["POST"])
@login_required
def remove_favorite(business_id):
    fav = Favorite.query.filter_by(user_id=current_user.id, business_id=business_id).first()
    if fav:
        db.session.delete(fav)
        db.session.commit()
    return redirect(request.referrer or url_for("favorites"))

@app.route("/member_tutorials")
@login_required  # if you want this locked to members only
def member_tutorials():
    return render_template("member_tutorials.html")

@app.route("/business_tutorials")
@business_login_required  # reuses your existing decorator
def business_tutorials():
    return render_template("business_tutorials.html")

@app.route("/stats")
def stats():
    # count confirmed users as members
    member_count = User.query.filter_by(email_confirmed=True).count()

    # count approved businesses as advertisers
    advertiser_count = Business.query.filter_by(status="approved").count()

    data = {
        "members": member_count,
        "advertisers": advertiser_count
    }
    return jsonify(data), 200

@app.route("/shop/link/<int:interaction_id>")
@login_required
def shop_link(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)

    # only the member in this interaction can use this
    if interaction.user_id != current_user.id:
        abort(403)

    business = interaction.business

    # must have an e-commerce URL
    if not business.website_url:
        flash("This business doesn't have an e-commerce site configured.", "warning")
        return redirect(url_for("active_session", interaction_id=interaction.id))

    # HARD requirement: at least $250 ad balance
    if (business.account_balance or 0) < 250.0:
        flash("This business must maintain at least $250 in their advertising balance "
              "before online purchases are allowed.", "warning")
        return redirect(url_for("active_session", interaction_id=interaction.id))

    # build signed token
    payload = {
        "interaction_id": interaction.id,
        "user_id": current_user.id,
        "business_id": business.id,
    }
    token = shop_serializer.dumps(payload)

    # redirect to business site with perk_token on the URL
    redirect_url = f"{business.website_url.rstrip('/')}" \
                   f"?perk_token={token}"

    return redirect(redirect_url)

@app.route("/online-purchase-instructions")
def online_purchase_instructions():
    return render_template("online_purchase_instructions.html")

@app.route("/online_marketplace")
def online_marketplace():
    base_query = Business.query.filter(
        Business.status == "approved",
        Business.website_url.isnot(None),
        Business.website_url != "",
        Business.is_ecommerce_site.is_(True),
        Business.allow_website_purchases.is_(True),
        Business.account_balance >= 250.0
    )

    # featured e-commerce businesses
    manual_featured = (
        base_query
        .filter(Business.manual_feature.is_(True))
        .order_by(Business.rank.desc())
        .limit(10)
        .all()
    )
    needed = 10 - len(manual_featured)
    if needed > 0:
        ranked = (
            base_query
            .filter(Business.manual_feature.is_(False))
            .order_by(Business.rank.desc())
            .limit(needed)
            .all()
        )
        featured_ecom = manual_featured + ranked
    else:
        featured_ecom = manual_featured[:10]

    return render_template(
        "online_marketplace.html",
        featured_ecom=featured_ecom
    )

@app.route("/online_marketplace_results")
def online_marketplace_results():
    base_query = Business.query.filter(
        Business.status == "approved",
        Business.website_url.isnot(None),
        Business.website_url != "",
        Business.is_ecommerce_site.is_(True),
        Business.allow_website_purchases.is_(True),
        Business.account_balance >= 250.0
    )

    q = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = 20

    if q:
        filtered = base_query.filter(
            Business.search_keywords.ilike(f"%{q}%")
        )
    else:
        filtered = base_query

    pagination = (
        filtered
        .order_by(Business.rank.desc(), Business.business_name.asc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )

    ecommerce_results = pagination.items

    return render_template(
        "online_marketplace_results.html",
        ecommerce_results=ecommerce_results,
        q=q,
        pagination=pagination
    )

@csrf.exempt
@app.route("/api/record_external_sale", methods=["POST"])
def record_external_sale():
    data = request.get_json(silent=True) or {}
    token = data.get("perk_token")
    amount = data.get("amount")

    if not token or amount is None:
        return jsonify({"error": "missing token or amount"}), 400

    # 1) verify token
    try:
        payload = shop_serializer.loads(token)
    except Exception:
        return jsonify({"error": "invalid token"}), 400

    interaction_id = payload.get("interaction_id")
    user_id = payload.get("user_id")
    business_id = payload.get("business_id")

    interaction = Interaction.query.get(interaction_id)
    if (
        not interaction or
        interaction.user_id != user_id or
        interaction.business_id != business_id or
        interaction.status != "active"
    ):
        return jsonify({"error": "invalid or inactive interaction"}), 400

    business = Business.query.get(business_id)
    if not business:
        return jsonify({"error": "business not found"}), 400

    # 2) validate amount
    try:
        amount_float = float(amount)
        if amount_float <= 0:
            raise ValueError()
    except ValueError:
        return jsonify({"error": "invalid amount"}), 400

    # 3) call your existing finalize_interaction logic
    try:
        summary = finalize_interaction(
            interaction=interaction,
            business=business,
            amount=amount_float,
            staff_id=None,
            source="external_cart",
            local_date_time=None
        )
    except Exception as e:
        # e.g. insufficient funds in account_balance, etc.
        return jsonify({"error": str(e)}), 400

    # 4) end the session
    interaction.status = "ended"
    interaction.awaiting_payment = False
    db.session.commit()

    return jsonify({"status": "ok", "summary": summary}), 200

@app.errorhandler(500)
def internal_server_error(error):
    # Log the full error + traceback for debugging
    import logging
    import traceback
    logging.exception("500 Internal Server Error occurred:")
    # Optional: you can also print to console in dev
    # traceback.print_exc()
    
    # Return a friendly message to the user
    return render_template(
        'errors/500.html',  # Create this template next
        error_message="Something went wrong on our end. We've been notified and are looking into it."
    ), 500

if __name__ == "__main__":
    # Optional: print all registered routes only in local dev
    for rule in app.url_map.iter_rules():
        print(f"{rule.endpoint:25s} {rule.methods} {rule}")
    
    app.run(debug=True)