import logging
import os
import re
import uuid
import requests
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, abort, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_wtf import CSRFProtect
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

# NOTE: smtplib, threading, MIMEText, MIMEMultipart are all removed.
# Railway blocks outbound SMTP ports (465, 587, 2525) on Free and Hobby plans.
# Resend uses HTTPS (port 443) which is never blocked — no SMTP needed at all.

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()
app = Flask(__name__)
secret_key = os.getenv('SECRET_KEY')
if not secret_key:
    raise RuntimeError('SECRET_KEY environment variable must be set for production.')
app.config['SECRET_KEY'] = secret_key
app.config['PREFERRED_URL_SCHEME'] = 'https'
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_SECURE'] = True
app.config['WTF_CSRF_TIME_LIMIT'] = 3600
app.config['WTF_CSRF_HEADERS'] = ['X-CSRFToken', 'X-CSRF-Token']
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

# --- DATABASE CONFIGURATION ---
database_url = os.getenv("DATABASE_URL")

if not database_url:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. "
        "Make sure your Postgres plugin is linked to this service in Railway."
    )

if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

print("DATABASE_URL scheme =", database_url.split("@")[0].split(":")[0])

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

db = SQLAlchemy(app)
migrate = Migrate(app, db)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.session_protection = 'strong'

# --- DATABASE MODELS ---

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    referral_code = db.Column(db.String(20), unique=True)
    referred_by = db.Column(db.String(20), nullable=True)
    balance_usd = db.Column(db.Float, default=0.0)
    chances = db.Column(db.Integer, default=5)
    is_active_member = db.Column(db.Boolean, default=False)
    whatsapp_number = db.Column(db.String(20), nullable=True)
    location = db.Column(db.String(255), nullable=True)
    security_question = db.Column(db.String(255), nullable=True)
    security_answer_hash = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.now())
    last_chance_reset = db.Column(db.DateTime, default=db.func.now())

class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(500), nullable=False)
    option_a = db.Column(db.String(100), nullable=False)
    option_b = db.Column(db.String(100), nullable=False)
    option_c = db.Column(db.String(100), nullable=False)
    option_d = db.Column(db.String(100), nullable=False)
    correct_answer = db.Column(db.String(10), nullable=False)

class RechargeCard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pin = db.Column(db.String(50), unique=True, nullable=False)
    network = db.Column(db.String(20))
    amount = db.Column(db.Integer)
    is_used = db.Column(db.Boolean, default=False)
    winner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

class PayoutRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    amount_usd = db.Column(db.Float, default=5.0)
    deduction_fee_usd = db.Column(db.Float, default=0.0)
    net_amount_usd = db.Column(db.Float, default=0.0)
    bank_name = db.Column(db.String(100), nullable=False)
    account_number = db.Column(db.String(20), nullable=False)
    account_name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='Pending')
    created_at = db.Column(db.DateTime, default=db.func.now())

class ReferralHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    referrer_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    referred_user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    earnings_usd = db.Column(db.Float, default=0.50)
    status = db.Column(db.String(20), default='Active')
    created_at = db.Column(db.DateTime, default=db.func.now())

class AnsweredQuestion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey('question.id'), nullable=False)
    answered_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    selected_answer = db.Column(db.String(70), nullable=False)
    is_correct = db.Column(db.Boolean, nullable=False)
    answered_at = db.Column(db.DateTime, default=db.func.now())

    question = db.relationship('Question', backref=db.backref('answer_records', lazy=True))
    answered_by = db.relationship('User', foreign_keys=[answered_by_id])


class PasswordResetToken(db.Model):
    # Stores one-time password reset tokens tied to a user with a 1-hour expiry.
    # Deleted after use so it cannot be reused.
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('reset_tokens', lazy=True))


# --- VALIDATION HELPERS ---

EMAIL_REGEX = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PHONE_REGEX = re.compile(r'^\+?[0-9]{7,15}$')


def is_valid_email(email: str) -> bool:
    return bool(email and EMAIL_REGEX.match(email))


def is_valid_phone(phone: str) -> bool:
    return bool(phone and PHONE_REGEX.match(phone.strip()))


def normalize_answer(answer: str) -> str:
    return answer.strip().lower()


def generate_referral_code() -> str:
    while True:
        code = uuid.uuid4().hex[:8]
        if not User.query.filter_by(referral_code=code).first():
            return code


UNLIMITED_CHANCES = 999


def has_referral_today(user_id):
    # Returns True if this user has at least one Active (paid) referral
    # whose created_at date matches today (UTC calendar date).
    today = datetime.utcnow().date()
    referrals = ReferralHistory.query.filter(
        ReferralHistory.referrer_id == user_id,
        ReferralHistory.status == 'Active'
    ).with_entities(ReferralHistory.created_at).all()
    return any(r.created_at and r.created_at.date() == today for r in referrals)


def maybe_reset_daily_chances(user):
    # RULE 1: Date-based daily reset — fires once per calendar day (UTC).
    # RULE 2: Unlimited upgrade — runs every call so referral bonus is instant.
    # RULE 3: Next day always resets to 5 first, then upgrade applies if earned.
    # IMPORTANT: Always commit after calling this.

    today = datetime.utcnow().date()
    today_midnight = datetime(today.year, today.month, today.day, 0, 0, 0)

    last_reset_date = None
    if user.last_chance_reset is not None:
        lr = user.last_chance_reset
        if hasattr(lr, 'date'):
            last_reset_date = lr.date()

    did_reset = False
    if last_reset_date != today:
        user.chances = 5
        user.last_chance_reset = today_midnight
        did_reset = True

    if has_referral_today(user.id):
        user.chances = UNLIMITED_CHANCES

    return did_reset


# --- EXCHANGE RATE CACHE ---
_rate_cache = {"value": None, "fetched_at": None}
RATE_CACHE_TTL_SECONDS = 600  # 10 minutes


def get_naira_rate():
    now = datetime.utcnow()
    cached = _rate_cache["value"]
    fetched_at = _rate_cache["fetched_at"]

    if cached and fetched_at and (now - fetched_at).total_seconds() < RATE_CACHE_TTL_SECONDS:
        return cached

    try:
        res = requests.get('https://open.er-api.com/v6/latest/USD', timeout=5)
        res.raise_for_status()
        payload = res.json()
        rate = payload.get('rates', {}).get('NGN')
        if rate:
            _rate_cache["value"] = rate
            _rate_cache["fetched_at"] = now
            return rate
        logger.warning('Exchange rate response missing NGN key. Full response: %s', payload)
    except Exception as exc:
        logger.warning('Failed to fetch exchange rate: %s', exc)

    if cached:
        logger.info('Returning stale cached exchange rate.')
        return cached

    logger.warning('Using hardcoded fallback exchange rate of 1500.')
    return 1500


def convert_to_nigeria_time(utc_dt):
    """Convert a UTC datetime to Nigeria's West Africa Time (WAT, UTC+1)."""
    return utc_dt + timedelta(hours=1)


# --- INIT ---
def init_db():
    if os.getenv('FLASK_ENV', 'production').lower() != 'production':
        with app.app_context():
            db.create_all()
            logger.info('Database tables created/verified.')

init_db()


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# --- UTILITIES ---
PAYSTACK_SECRET = os.getenv('PAYSTACK_SECRET')
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', 'admin@example.com')

if not PAYSTACK_SECRET:
    logger.warning('PAYSTACK_SECRET is not set. Paystack payments will be unavailable.')


# --- RESEND EMAIL CONFIG ---
# Railway blocks all outbound SMTP ports (465, 587, 2525) on Free and Hobby
# plans. Resend uses HTTPS (port 443) which Railway never blocks.
#
# Railway environment variables to set:
#   RESEND_API_KEY = re_xxxxxxxxxxxxxxxxxxxx   ← from resend.com → API Keys
#   APP_BASE_URL   = https://yourapp.up.railway.app
#
# Setup (2 minutes, free, no credit card):
#   1. Go to resend.com → Sign Up
#   2. API Keys → Create API Key → copy it
#   3. Add RESEND_API_KEY to Railway environment variables
#
# Free plan: 3,000 emails/month, 100/day.
# Uses `requests` which is already in requirements.txt — no new packages needed.
RESEND_API_KEY = os.getenv('RESEND_API_KEY')
APP_BASE_URL   = os.getenv('APP_BASE_URL', '').rstrip('/')

if not RESEND_API_KEY:
    logger.warning('RESEND_API_KEY not set. Password reset emails will be unavailable.')


def send_reset_email(to_email: str, reset_url: str, user_name: str) -> bool:
    """
    Sends a password reset email via the Resend HTTPS API.

    Why Resend instead of SMTP:
      Railway blocks outbound SMTP ports (465, 587, 2525) on Free and Hobby
      plans to prevent abuse. Every SMTP provider — Gmail, Brevo, SendGrid —
      will time out because Railway drops the TCP connection before it reaches
      the mail server. Resend uses HTTPS (port 443) which is never blocked,
      so it works on every Railway plan without any special configuration.

    No background thread needed: the Resend API call takes ~200ms over HTTPS,
    far faster than an SMTP handshake. Running it synchronously is fine.

    Returns True on success, False on any error.
    Errors are logged but never raised — a failed send must never crash the route.
    """
    if not RESEND_API_KEY:
        logger.error('Cannot send reset email: RESEND_API_KEY not configured.')
        return False

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;
                padding:24px;border:1px solid #e0e0e0;border-radius:10px;">
        <h2 style="color:#0d6efd;">Password Reset</h2>
        <p>Hi <strong>{user_name}</strong>,</p>
        <p>You requested a password reset for your <strong>Rewards</strong> account.</p>
        <p>Click the button below to set a new password.
           This link expires in <strong>1 hour</strong>.</p>
        <a href="{reset_url}"
           style="display:inline-block;margin:16px 0;padding:12px 28px;
                  background:#0d6efd;color:#fff;text-decoration:none;
                  border-radius:6px;font-weight:bold;">
            Reset My Password
        </a>
        <p style="font-size:0.85rem;color:#888;">
            If the button does not work, copy and paste this link into your browser:<br>
            <a href="{reset_url}" style="color:#0d6efd;">{reset_url}</a>
        </p>
        <hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
        <p style="font-size:0.8rem;color:#aaa;">
            If you did not request this, ignore this email.
            Your password will not change.
        </p>
    </div>
    """

    text_body = (
        f"Hi {user_name},\n\n"
        f"You requested a password reset for your Rewards account.\n\n"
        f"Click the link below to set a new password. "
        f"This link expires in 1 hour:\n\n"
        f"{reset_url}\n\n"
        f"If you did not request this, you can safely ignore this email.\n\n"
        f"— The Rewards Team"
    )

    try:
        response = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {RESEND_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                # 'onboarding@resend.dev' is Resend's shared sending domain —
                # works immediately with no domain setup on the free plan.
                # When ready to use your own domain, verify it in the Resend
                # dashboard and change this to e.g. noreply@yourdomain.com
                'from': 'Rewards <onboarding@resend.dev>',
                'to': [to_email],
                'subject': 'Reset Your Password — Rewards',
                'html': html_body,
                'text': text_body,
            },
            timeout=10
        )
        response.raise_for_status()
        logger.info('Password reset email sent to %s via Resend', to_email)
        return True

    except requests.exceptions.HTTPError as exc:
        try:
            error_detail = exc.response.json()
            logger.error('Resend API error for %s: %s', to_email, error_detail)
        except:
            logger.error('Resend API error for %s: %s', to_email, exc.response.text)
        logger.exception('Failed to send reset email to %s', to_email)
        return False
    except Exception as exc:
        logger.exception('Failed to send reset email to %s: %s', to_email, exc)
        return False


@app.after_request
def set_security_headers(response):
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'interest-cohort=()'
    return response

# --- ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        referred_by = request.form.get('ref', '').strip()
        whatsapp_number = request.form.get('whatsapp_number', '').strip()
        location = request.form.get('location', '').strip()

        if not full_name or not email or not password or not whatsapp_number or not location:
            flash('Please fill in all required fields.')
            return redirect(url_for('register'))

        if len(full_name) < 3:
            flash('Please enter your full name.')
            return redirect(url_for('register'))

        if len(password) < 6:
            flash('Password must be at least 6 characters.')
            return redirect(url_for('register'))

        if not is_valid_email(email):
            flash('Please enter a valid email address.')
            return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():
            flash('Email already exists!')
            return redirect(url_for('register'))

        if not is_valid_phone(whatsapp_number):
            flash('Invalid WhatsApp number format.')
            return redirect(url_for('register'))

        if len(location) < 5:
            flash('Please enter your location in the format Country/State/City.')
            return redirect(url_for('register'))

        security_question = request.form.get('security_question', '').strip()
        custom_security_question = request.form.get('custom_security_question', '').strip()
        security_answer = request.form.get('security_answer', '').strip()

        if security_question.lower() == 'other' and custom_security_question:
            security_question = custom_security_question

        if (security_question and not security_answer) or (security_answer and not security_question):
            flash('Please provide both a recovery question and answer, or leave both blank.')
            return redirect(url_for('register'))

        if security_question and len(security_question) < 10:
            flash('Recovery question should be at least 10 characters.')
            return redirect(url_for('register'))

        if security_answer and len(security_answer) < 3:
            flash('Recovery answer should be at least 3 characters long.')
            return redirect(url_for('register'))

        if referred_by and not User.query.filter_by(referral_code=referred_by).first():
            referred_by = None

        new_user = User(
            full_name=full_name,
            email=email,
            password=generate_password_hash(password, method='pbkdf2:sha256'),
            referral_code=generate_referral_code(),
            referred_by=referred_by,
            whatsapp_number=whatsapp_number,
            location=location,
            security_question=security_question if security_question else None,
            security_answer_hash=generate_password_hash(normalize_answer(security_answer), method='pbkdf2:sha256') if security_answer else None
        )
        db.session.add(new_user)
        db.session.commit()

        if referred_by:
            referrer = User.query.filter_by(referral_code=referred_by).first()
            if referrer:
                referral_rec = ReferralHistory(
                    referrer_id=referrer.id,
                    referred_user_id=new_user.id,
                    earnings_usd=0.0,
                    status='Pending'
                )
                db.session.add(referral_rec)
                db.session.commit()

        login_user(new_user)
        return redirect(url_for('dashboard'))

    code_from_link = request.args.get('ref', '')
    naira_rate = get_naira_rate()
    return render_template('register.html', ref_code=code_from_link, naira_rate=naira_rate)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not email or not password:
            flash('Email and password are required.')
            return redirect(url_for('login'))

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))

        flash('Invalid login details.')
    session.modified = True  # Force Flask to create a session so csrf_token() works
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    maybe_reset_daily_chances(current_user)
    db.session.commit()

    pins_count = RechargeCard.query.filter_by(is_used=False).count()
    answered_records = (
        AnsweredQuestion.query
        .filter_by(answered_by_id=current_user.id)
        .join(Question)
        .order_by(AnsweredQuestion.answered_at.desc())
        .limit(5)
        .all()
    )
    for record in answered_records:
        record.answered_at = convert_to_nigeria_time(record.answered_at)
    referral_link = url_for('register', _external=True, ref=current_user.referral_code)
    return render_template(
        'dashboard.html',
        user=current_user,
        pins_count=pins_count,
        admin_email=ADMIN_EMAIL,
        answered_records=answered_records,
        referral_link=referral_link
    )


@app.route('/get-chances')
@login_required
def get_chances():
    maybe_reset_daily_chances(current_user)
    db.session.commit()
    db.session.refresh(current_user)

    chances = current_user.chances
    return jsonify({
        'display': 'unlimited' if chances >= UNLIMITED_CHANCES else chances
    })


@app.route('/delete-answered-question', methods=['POST'])
@login_required
def delete_answered_question():
    data = request.json or {}
    record_id = data.get('record_id')
    if record_id is None:
        return jsonify({"status": "error", "message": "Missing record id."}), 400

    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid record id."}), 400

    record = db.session.get(AnsweredQuestion, record_id)
    if not record or record.answered_by_id != current_user.id:
        return jsonify({"status": "error", "message": "Answered question not found."}), 404

    db.session.delete(record)
    db.session.commit()
    return jsonify({"status": "success", "message": "Answered question removed from your dashboard."})


@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html', user=current_user)


@app.route('/edit-profile', methods=['POST'])
@login_required
def edit_profile():
    data = request.json or {}
    full_name = data.get('full_name', '').strip()
    whatsapp_number = data.get('whatsapp_number', '').strip()
    location = data.get('location', '').strip()

    if not full_name:
        return jsonify({"status": "error", "message": "Full name is required."}), 400

    if whatsapp_number and not is_valid_phone(whatsapp_number):
        return jsonify({"status": "error", "message": "Invalid WhatsApp number format."}), 400

    if location and len(location) < 5:
        return jsonify({"status": "error", "message": "Location must be at least 5 characters (e.g., Country/State/City)."}), 400

    try:
        current_user.full_name = full_name
        current_user.whatsapp_number = whatsapp_number if whatsapp_number else None
        current_user.location = location if location else None
        db.session.commit()
        return jsonify({"status": "success", "message": "Profile updated successfully!"})
    except Exception as exc:
        logger.exception('Error updating profile: %s', exc)
        return jsonify({"status": "error", "message": "An error occurred while updating your profile."}), 500


@app.route('/change-password', methods=['POST'])
@login_required
def change_password():
    data = request.json or {}
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')
    confirm_password = data.get('confirm_password', '')

    if not current_password or not new_password or not confirm_password:
        return jsonify({"status": "error", "message": "All fields are required."}), 400

    if not check_password_hash(current_user.password, current_password):
        return jsonify({"status": "error", "message": "Current password is incorrect."}), 401

    if len(new_password) < 8:
        return jsonify({"status": "error", "message": "New password must be at least 8 characters long."}), 400

    if new_password != confirm_password:
        return jsonify({"status": "error", "message": "New password and confirmation do not match."}), 400

    if new_password == current_password:
        return jsonify({"status": "error", "message": "New password must be different from current password."}), 400

    try:
        current_user.password = generate_password_hash(new_password, method='pbkdf2:sha256')
        db.session.commit()
        return jsonify({"status": "success", "message": "Password changed successfully!"})
    except Exception as exc:
        logger.exception('Error changing password: %s', exc)
        return jsonify({"status": "error", "message": "An error occurred while changing your password."}), 500


@app.route('/update-security-question', methods=['POST'])
@login_required
def update_security_question():
    data = request.json or {}
    security_question = data.get('security_question', '').strip()
    custom_security_question = data.get('custom_security_question', '').strip()
    security_answer = data.get('security_answer', '').strip()

    if security_question.lower() == 'other' and custom_security_question:
        security_question = custom_security_question

    if not security_question or not security_answer:
        return jsonify({"status": "error", "message": "A security question and answer are both required."}), 400

    if len(security_question) < 10:
        return jsonify({"status": "error", "message": "Recovery question should be at least 10 characters."}), 400

    if len(security_answer) < 3:
        return jsonify({"status": "error", "message": "Recovery answer should be at least 3 characters long."}), 400

    try:
        current_user.security_question = security_question
        current_user.security_answer_hash = generate_password_hash(normalize_answer(security_answer), method='pbkdf2:sha256')
        db.session.commit()
        return jsonify({"status": "success", "message": "Recovery question saved successfully."})
    except Exception as exc:
        logger.exception('Error updating security question: %s', exc)
        return jsonify({"status": "error", "message": "An error occurred while saving your recovery question."}), 500


@app.route('/admin/questions')
@login_required
def admin_questions():
    if current_user.email != ADMIN_EMAIL:
        flash('Admin access only.')
        return redirect(url_for('dashboard'))

    questions = Question.query.order_by(Question.id).all()
    question_status = []
    for q in questions:
        correct_answer = next((record for record in q.answer_records if record.is_correct), None)
        question_status.append({
            'id': q.id,
            'text': q.text,
            'answered': bool(correct_answer),
            'answered_by': correct_answer.answered_by.email if correct_answer else None,
            'answered_at': correct_answer.answered_at if correct_answer else None
        })
    return render_template('admin_questions.html', question_status=question_status)

@app.route('/pay')
@login_required
def pay():
    if not PAYSTACK_SECRET:
        flash('Payment configuration is missing. Please contact support.')
        return redirect(url_for('dashboard'))

    rate = get_naira_rate()
    amount_kobo = int(2 * rate * 100)
    headers = {'Authorization': f'Bearer {PAYSTACK_SECRET}'}
    data = {
        'email': current_user.email,
        'amount': amount_kobo,
        'callback_url': url_for('verify_payment', _external=True)
    }

    try:
        r = requests.post(
            'https://api.paystack.co/transaction/initialize',
            headers=headers, json=data, timeout=10
        )
        r.raise_for_status()
        payload = r.json()
        authorization_url = payload.get('data', {}).get('authorization_url')
        if not authorization_url:
            raise ValueError('Missing Paystack authorization URL')
        return redirect(authorization_url)
    except Exception as exc:
        logger.exception('Paystack payment initialization failed: %s', exc)
        flash('Unable to start payment at this time. Please try again later.')
        return redirect(url_for('dashboard'))

@app.route('/verify')
@login_required
def verify_payment():
    reference = request.args.get('reference')
    if not reference:
        flash('Missing payment reference.')
        return redirect(url_for('dashboard'))

    if not PAYSTACK_SECRET:
        flash('Payment configuration is missing. Please contact support.')
        return redirect(url_for('dashboard'))

    headers = {'Authorization': f'Bearer {PAYSTACK_SECRET}'}
    try:
        r = requests.get(
            f'https://api.paystack.co/transaction/verify/{reference}',
            headers=headers, timeout=10
        )
        r.raise_for_status()
        res = r.json()
    except Exception as exc:
        logger.exception('Paystack verification failed: %s', exc)
        flash('Unable to verify payment at this time.')
        return redirect(url_for('dashboard'))

    data = res.get('data', {})
    if res.get('status') and data.get('status') == 'success':
        if not current_user.is_active_member:
            current_user.is_active_member = True
            if current_user.referred_by:
                referrer = User.query.filter_by(referral_code=current_user.referred_by).first()
                if referrer:
                    referrer.balance_usd += 0.50
                    referral_rec = ReferralHistory.query.filter_by(
                        referred_user_id=current_user.id
                    ).first()
                    if referral_rec:
                        referral_rec.earnings_usd = 0.50
                        referral_rec.status = 'Active'
                    maybe_reset_daily_chances(referrer)
            db.session.commit()
            flash('Account Activated!')
        else:
            flash('Your account is already active.')
    else:
        logger.warning('Paystack verification returned unsuccessful response: %s', res)
        flash('Payment was not successful. Please try again.')

    return redirect(url_for('dashboard'))


# --- TRIVIA ENGINE ---

@app.route('/get-question')
@login_required
def get_question():
    maybe_reset_daily_chances(current_user)
    db.session.commit()
    db.session.refresh(current_user)

    if current_user.chances <= 0:
        return jsonify({
            "status": "no_chances",
            "message": "You have used all your chances for today. Come back tomorrow! or refer a friend to get unlimited chances for the day"
        })

    question_ids = db.session.query(Question.id).all()
    if not question_ids:
        return jsonify({"status": "error", "message": "No questions available right now."})

    q = None
    for _ in range(5):
        q_id = random.choice(question_ids)[0]
        candidate = db.session.get(Question, q_id)
        already_won = AnsweredQuestion.query.filter_by(
            question_id=candidate.id, is_correct=True
        ).first()
        if not already_won:
            q = candidate
            break

    if q is None:
        return jsonify({
            "status": "all_answered",
            "message": "All questions have been answered today. Check back later!"
        })

    return jsonify({
        "id": q.id,
        "question": q.text,
        "options": {"A": q.option_a, "B": q.option_b, "C": q.option_c, "D": q.option_d},
        "chances_remaining": current_user.chances if current_user.chances < UNLIMITED_CHANCES else "unlimited"
    })


@app.route('/check-answer', methods=['POST'])
@login_required
def check_answer():
    data = request.json
    if not data or 'question_id' not in data or 'choice' not in data:
        return jsonify({"status": "error", "message": "Invalid request."})

    question = db.session.get(Question, data.get('question_id'))
    if not question:
        return jsonify({"status": "error", "message": "Question not found."})

    answered_correct = AnsweredQuestion.query.filter_by(
        question_id=question.id, is_correct=True
    ).first()
    if answered_correct:
        return jsonify({"status": "already_answered", "message": "This question has already been answered."})

    maybe_reset_daily_chances(current_user)
    db.session.commit()
    db.session.refresh(current_user)

    if current_user.chances <= 0:
        return jsonify({
            "status": "no_chances",
            "message": "You have no chances remaining for today. Come back tomorrow!"
        })

    if current_user.chances < UNLIMITED_CHANCES:
        current_user.chances -= 1

    is_correct = data.get('choice') == question.correct_answer
    answer_record = AnsweredQuestion(
        question_id=question.id,
        answered_by_id=current_user.id,
        selected_answer=data.get('choice'),
        is_correct=is_correct
    )
    db.session.add(answer_record)

    chances_left = current_user.chances if current_user.chances < UNLIMITED_CHANCES else "unlimited"

    if is_correct:
        pin = RechargeCard.query.filter_by(is_used=False).with_for_update().first()

        if not pin:
            db.session.commit()
            return jsonify({
                "status": "correct_but_empty",
                "message": "Correct! However, all airtime rewards have been claimed. Contact support.",
                "chances_remaining": chances_left
            })

        pin.is_used = True
        pin.winner_id = current_user.id
        db.session.commit()
        return jsonify({
            "status": "win",
            "pin": pin.pin,
            "chances_remaining": chances_left
        })

    db.session.commit()
    return jsonify({
        "status": "wrong",
        "message": "Incorrect answer!",
        "chances_remaining": chances_left
    })

@app.route('/request-payout', methods=['POST'])
@login_required
def request_payout():
    amount_requested = 5.0

    bank_name = request.form.get('bank_name', '').strip()
    account_number = request.form.get('account_number', '').strip()
    account_name = request.form.get('account_name', '').strip()

    if not bank_name or not account_number or not account_name:
        return jsonify({'status': 'error', 'message': 'Please provide bank name, account number, and account name.'})

    if current_user.balance_usd < amount_requested:
        return jsonify({'status': 'error', 'message': 'Minimum $5 required'})

    pending_payout = PayoutRequest.query.filter_by(
        user_id=current_user.id,
        status='Pending'
    ).first()

    if pending_payout:
        return jsonify({'status': 'error', 'message': 'You have a pending withdrawal. Please wait for it to be processed.'})

    deduction_fee = amount_requested * 0.10
    net_amount = amount_requested - deduction_fee

    new_payout = PayoutRequest(
        user_id=current_user.id,
        amount_usd=amount_requested,
        deduction_fee_usd=deduction_fee,
        net_amount_usd=net_amount,
        bank_name=bank_name,
        account_number=account_number,
        account_name=account_name
    )
    current_user.balance_usd -= amount_requested
    db.session.add(new_payout)
    db.session.commit()

    return jsonify({
        'status': 'success',
        'message': 'Withdrawal request submitted!',
        'details': {
            'original_amount': amount_requested,
            'deduction_fee': round(deduction_fee, 2),
            'net_amount': round(net_amount, 2)
        }
    })

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        logger.info('Forgot-password request received for email: %s', email)

        if not email or not is_valid_email(email):
            flash('Please enter a valid email address.')
            return redirect(url_for('forgot_password'))

        if not RESEND_API_KEY:
            logger.error(
                'Forgot-password: RESEND_API_KEY not set. '
                'Add it to Railway environment variables.'
            )
            flash('If that email is registered, a reset link has been sent. Check your inbox (and spam folder).')
            return redirect(url_for('login'))

        try:
            user = User.query.filter_by(email=email).first()
        except Exception as exc:
            logger.exception('Forgot-password: DB error looking up email %s: %s', email, exc)
            flash('If that email is registered, a reset link has been sent. Check your inbox (and spam folder).')
            return redirect(url_for('login'))

        if user:
            PasswordResetToken.query.filter_by(user_id=user.id).delete()

            token_value = uuid.uuid4().hex + uuid.uuid4().hex  # 64-char hex
            expires_at = datetime.utcnow() + timedelta(hours=1)
            reset_token = PasswordResetToken(
                user_id=user.id,
                token=token_value,
                expires_at=expires_at
            )
            db.session.add(reset_token)
            db.session.commit()

            reset_url = f"{APP_BASE_URL}{url_for('reset_password', token=token_value)}"

            # Resend API is HTTPS — fast (~200ms), no thread needed.
            # The old code used a background thread because SMTP was slow and
            # blocking. That is no longer necessary.
            sent = send_reset_email(user.email, reset_url, user.full_name)
            if sent:
                logger.info('Forgot-password: reset email delivered for user id=%s', user.id)
            else:
                logger.error('Forgot-password: reset email FAILED for user id=%s', user.id)
        else:
            logger.info('Forgot-password: no user found for email %s (returning generic message).', email)

        # Same message whether found or not — prevents user enumeration.
        flash('If that email is registered, a reset link has been sent. Check your inbox (and spam folder).')
        return redirect(url_for('login'))

    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    reset_token = PasswordResetToken.query.filter_by(token=token).first()

    if not reset_token or reset_token.expires_at < datetime.utcnow():
        if reset_token:
            db.session.delete(reset_token)
            db.session.commit()
        flash('This password reset link is invalid or has expired. Please request a new one.')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        new_password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if len(new_password) < 6:
            flash('Password must be at least 6 characters.')
            return redirect(url_for('reset_password', token=token))

        if new_password != confirm_password:
            flash('Passwords do not match.')
            return redirect(url_for('reset_password', token=token))

        user = reset_token.user
        user.password = generate_password_hash(new_password, method='pbkdf2:sha256')

        db.session.delete(reset_token)
        db.session.commit()

        logger.info('Password reset successful for user %s', user.email)
        flash('Your password has been reset. You can now log in with your new password.')
        return redirect(url_for('login'))

    return render_template('reset_password.html', token=token)


@app.route('/recover-security-question', methods=['GET', 'POST'])
def recover_security_question():
    step = 'email'
    email = ''
    security_question = None

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        security_answer = request.form.get('security_answer', '').strip()

        if not email or not is_valid_email(email):
            flash('Please enter a valid email address.')
            return redirect(url_for('recover_security_question'))

        user = User.query.filter_by(email=email).first()
        if not user or not user.security_question:
            flash('If that email is registered and has a recovery question configured, you will be guided to it.')
            return redirect(url_for('login'))

        if not security_answer:
            security_question = user.security_question
            step = 'question'
            return render_template(
                'recover_security_question.html',
                step=step,
                email=email,
                question=security_question
            )

        if not user.security_answer_hash or not check_password_hash(user.security_answer_hash, normalize_answer(security_answer)):
            flash('Your recovery answer is incorrect. Please try again.')
            security_question = user.security_question
            step = 'question'
            return render_template(
                'recover_security_question.html',
                step=step,
                email=email,
                question=security_question
            )

        PasswordResetToken.query.filter_by(user_id=user.id).delete()
        token_value = uuid.uuid4().hex + uuid.uuid4().hex
        expires_at = datetime.utcnow() + timedelta(hours=1)
        reset_token = PasswordResetToken(
            user_id=user.id,
            token=token_value,
            expires_at=expires_at
        )
        db.session.add(reset_token)
        db.session.commit()

        flash('Answer accepted. Please choose a new password.')
        return redirect(url_for('reset_password', token=token_value))

    return render_template('recover_security_question.html', step=step, email=email)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/withdrawal-history')
@login_required
def withdrawal_history():
    withdrawals = PayoutRequest.query.filter_by(user_id=current_user.id).order_by(PayoutRequest.created_at.desc()).all()
    return render_template('withdrawal_history.html', withdrawals=withdrawals)

@app.route('/referral-history')
@login_required
def referral_history():
    referrals = ReferralHistory.query.filter_by(referrer_id=current_user.id).order_by(ReferralHistory.created_at.desc()).all()
    return render_template('referral_history.html', referrals=referrals)

@app.route('/api/exchange-rate')
def api_exchange_rate():
    rate = get_naira_rate()
    return jsonify({"rate": rate, "amount_in_naira": round(2 * rate, 2)})


# --- QUESTION MANAGEMENT API ---

VALID_ANSWERS = {'a', 'b', 'c', 'd'}
QUESTION_FIELDS = ('text', 'option_a', 'option_b', 'option_c', 'option_d', 'correct_answer')


def _validate_question_payload(data: dict, index: int = None) -> str | None:
    """
    Validate a single question dict.  Returns an error string on failure,
    or None when the payload is valid.  `index` is included in the message
    for bulk requests so the caller knows which item failed.
    """
    prefix = f"Question {index}: " if index is not None else ""

    for field in QUESTION_FIELDS:
        value = data.get(field)
        if not value or not str(value).strip():
            return f"{prefix}'{field}' is required and must not be empty."

    answer = str(data['correct_answer']).strip().lower()
    if answer not in VALID_ANSWERS:
        return (
            f"{prefix}'correct_answer' must be one of 'a', 'b', 'c', 'd' "
            f"(got '{data['correct_answer']}')."
        )

    return None


@app.route('/api/questions', methods=['POST'])
@csrf.exempt
def api_add_question():
    """
    POST /api/questions
    Insert a single quiz question.

    Request body (JSON):
        {
            "text":           "Question text",
            "option_a":       "First choice",
            "option_b":       "Second choice",
            "option_c":       "Third choice",
            "option_d":       "Fourth choice",
            "correct_answer": "a"   // case-insensitive: a/b/c/d
        }

    Responses:
        201  {"status": "success", "message": "...", "question_id": <int>}
        400  {"status": "error",   "message": "..."}
        500  {"status": "error",   "message": "..."}
    """
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"status": "error", "message": "Request body must be a JSON object."}), 400

    error = _validate_question_payload(data)
    if error:
        return jsonify({"status": "error", "message": error}), 400

    try:
        question = Question(
            text=data['text'].strip(),
            option_a=data['option_a'].strip(),
            option_b=data['option_b'].strip(),
            option_c=data['option_c'].strip(),
            option_d=data['option_d'].strip(),
            correct_answer=data['correct_answer'].strip().lower(),
        )
        db.session.add(question)
        db.session.commit()
        logger.info("Question inserted via API: id=%d text=%.60r", question.id, question.text)
        return jsonify({
            "status": "success",
            "message": "Question inserted successfully.",
            "question_id": question.id,
        }), 201
    except Exception as exc:
        db.session.rollback()
        logger.exception("Failed to insert question via API: %s", exc)
        return jsonify({"status": "error", "message": "An error occurred while inserting the question."}), 500


@app.route('/api/questions/bulk', methods=['POST'])
@csrf.exempt
def api_add_questions_bulk():
    """
    POST /api/questions/bulk
    Insert multiple quiz questions in a single atomic transaction.
    All questions are validated before any are written; if any question
    fails validation the entire request is rejected with no DB changes.

    Request body (JSON array):
        [
            {
                "text":           "Question text",
                "option_a":       "First choice",
                "option_b":       "Second choice",
                "option_c":       "Third choice",
                "option_d":       "Fourth choice",
                "correct_answer": "b"
            },
            ...
        ]

    Responses:
        201  {"status": "success", "message": "...", "inserted_count": <int>}
        400  {"status": "error",   "message": "..."}
        500  {"status": "error",   "message": "..."}
    """
    data = request.get_json(silent=True)
    if not data or not isinstance(data, list):
        return jsonify({"status": "error", "message": "Request body must be a JSON array of question objects."}), 400

    if len(data) == 0:
        return jsonify({"status": "error", "message": "The questions array must not be empty."}), 400

    # Validate every question before touching the database.
    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            return jsonify({
                "status": "error",
                "message": f"Question {idx}: each item must be a JSON object.",
            }), 400

        error = _validate_question_payload(item, index=idx)
        if error:
            return jsonify({"status": "error", "message": error}), 400

    # All valid — insert inside a single transaction (all-or-nothing).
    try:
        questions = [
            Question(
                text=item['text'].strip(),
                option_a=item['option_a'].strip(),
                option_b=item['option_b'].strip(),
                option_c=item['option_c'].strip(),
                option_d=item['option_d'].strip(),
                correct_answer=item['correct_answer'].strip().lower(),
            )
            for item in data
        ]
        db.session.add_all(questions)
        db.session.commit()
        inserted_ids = [q.id for q in questions]
        logger.info(
            "Bulk question insert via API: count=%d ids=%s",
            len(questions),
            inserted_ids,
        )
        return jsonify({
            "status": "success",
            "message": f"{len(questions)} question(s) inserted successfully.",
            "inserted_count": len(questions),
        }), 201
    except Exception as exc:
        db.session.rollback()
        logger.exception("Failed to bulk-insert questions via API: %s", exc)
        return jsonify({"status": "error", "message": "An error occurred while inserting questions."}), 500

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/support')
def support():
    return render_template('contact.html')

if __name__ == "__main__":
    app.run()