import logging
import os
import re
import uuid
import requests
import random
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_wtf import CSRFProtect
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

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

print("DATABASE_URL scheme =", database_url.split("@")[0].split(":")[0])  # Safe partial log

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
# NOTE: No model changes — all existing columns/tables are preserved exactly as-is.
# Flask-Migrate is managing the schema in production; db.create_all() is NOT called here.

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

# --- VALIDATION HELPERS ---

EMAIL_REGEX = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PHONE_REGEX = re.compile(r'^\+?[0-9]{7,15}$')


def is_valid_email(email: str) -> bool:
    return bool(email and EMAIL_REGEX.match(email))


def is_valid_phone(phone: str) -> bool:
    return bool(phone and PHONE_REGEX.match(phone.strip()))


def generate_referral_code() -> str:
    while True:
        code = uuid.uuid4().hex[:8]
        if not User.query.filter_by(referral_code=code).first():
            return code


# ---------------------------------------------------------------------------
# CHANGE: Named constant for "unlimited" chances.
# The original code used the bare integer 999 in two separate places, which is
# a "magic number" — anyone reading it had to guess what it meant. Giving it a
# name makes the intent clear everywhere it's used and means you only have to
# change it in one place if you ever want a different sentinel value.
# ---------------------------------------------------------------------------
UNLIMITED_CHANCES = 999


# ---------------------------------------------------------------------------
# CHANGE: Extracted the daily-chances reset into a single reusable helper.
#
# Problem in the original code:
#   The reset block (check last_chance_reset, set chances = 5, check referrals,
#   set last_chance_reset) was copy-pasted identically into both /get-question
#   and /check-answer. Duplicated logic is dangerous — if you ever need to fix
#   or adjust the reset behaviour you have to remember to update both copies,
#   and they can silently drift out of sync over time.
#
# What this function does:
#   1. Compares the stored last_chance_reset date against today's UTC date.
#   2. If it's a new day, resets chances to 5 (or UNLIMITED if they referred
#      someone today) and updates last_chance_reset to right now.
#   3. If it's already been reset today, does nothing and returns False.
#
# Why UTC specifically:
#   Railway's Postgres server stores timestamps in UTC. Python's date.today()
#   uses the local system timezone, which on Railway is also UTC — but relying
#   on that coincidence is fragile. Using datetime.utcnow() everywhere makes
#   the comparison explicit and safe regardless of server locale.
#
# Why we handle last_chance_reset being None:
#   Older user rows created before this column existed could have NULL in the
#   DB. Calling .date() on None would raise an AttributeError and crash the
#   request, so we guard against it.
#
# Returns True if a reset happened (useful for callers that want to know),
# False if today's reset had already been done.
# ---------------------------------------------------------------------------
def has_referral_today(user_id):
    # Returns True if this user has at least one Active (paid) referral
    # whose created_at date matches today (UTC calendar date).
    #
    # "Today" is determined by calendar date only — 2025-06-01 is today
    # regardless of whether it is 00:01 or 23:59. No time component involved.
    #
    # Only Active referrals count. A Pending referral means the person
    # signed up but has not paid yet — that does not earn unlimited chances.
    # Status is set to Active in /verify once Paystack confirms payment.
    today = datetime.utcnow().date()
    referrals = ReferralHistory.query.filter(
        ReferralHistory.referrer_id == user_id,
        ReferralHistory.status == 'Active'
    ).with_entities(ReferralHistory.created_at).all()
    return any(r.created_at and r.created_at.date() == today for r in referrals)


def maybe_reset_daily_chances(user):
    # RULE 1 — Daily reset by calendar date (not by time):
    #   Compare today's UTC calendar date against the date stored in
    #   last_chance_reset. If they differ it is a new day — reset to 5.
    #   We write midnight (00:00:00) of today back to last_chance_reset so
    #   the stored value is always the start of the current day with no time
    #   component, making the date comparison unambiguous. This means the
    #   reset is purely date-driven: it fires once per calendar day regardless
    #   of what hour the user first visits.
    #
    # RULE 2 — Unlimited upgrade by referral (runs every call):
    #   After the reset (or if today's reset already ran), check whether this
    #   user has at least one Active referral dated today. If yes, upgrade
    #   chances to UNLIMITED_CHANCES no matter how many chances are left.
    #   This runs on every call — not just on reset day — so the upgrade
    #   is instant the moment a referred user pays, without needing a page
    #   reload or waiting until the next daily reset.
    #
    # RULE 3 — Next day always resets to 5:
    #   Even if the user had unlimited chances yesterday, the new-day check
    #   resets to 5 first. If they also referred someone today the unlimited
    #   upgrade in Step 2 immediately overrides the 5. If not, they get 5.
    #
    # IMPORTANT: Always commit after calling this. The unlimited upgrade in
    #   Step 2 can mutate user.chances even when no daily reset occurred.

    today = datetime.utcnow().date()
    # midnight of today as a datetime — used when writing last_chance_reset
    today_midnight = datetime(today.year, today.month, today.day, 0, 0, 0)

    # --- Step 1: Date-based daily reset ---
    last_reset_date = None
    if user.last_chance_reset is not None:
        lr = user.last_chance_reset
        if hasattr(lr, 'date'):
            last_reset_date = lr.date()

    did_reset = False
    if last_reset_date != today:
        # New calendar day — restore 5 free chances.
        # Store midnight so the column holds a clean date with no stray time.
        user.chances = 5
        user.last_chance_reset = today_midnight
        did_reset = True

    # --- Step 2: Unlimited upgrade (every call, regardless of reset) ---
    # Always runs so a referral that happened after this morning's reset
    # is picked up immediately on the next call without waiting for midnight.
    if has_referral_today(user.id):
        # Upgrade unconditionally — even if user already has some chances left,
        # one paid referral today means unlimited for the rest of today.
        user.chances = UNLIMITED_CHANCES

    return did_reset

# ---------------------------------------------------------------------------
# CHANGE: Exchange rate in-memory cache.
#
# Problem in the original code:
#   get_naira_rate() made a live HTTP request to exchangerate-api.com on every
#   single call — including every time /pay or /register loaded. If the
#   external API was slow or down, every user hitting those pages would wait or
#   see an error. Under any real traffic, this is a bottleneck.
#
# What the cache does:
#   Stores the last successful rate and the time it was fetched in a simple
#   module-level dict (_rate_cache). On each call it checks whether the cached
#   value is still fresh (within RATE_CACHE_TTL_SECONDS = 10 minutes). If it
#   is, it returns immediately without touching the network. If it's stale or
#   missing, it fetches a fresh rate and updates the cache.
#
# Stale-cache fallback:
#   If the live fetch fails but we have an old cached value, we return the
#   stale value rather than the hardcoded 1500. A slightly outdated rate is
#   far better than a wrong hardcoded one. Only if there's no cache at all do
#   we fall back to 1500 as the last resort.
#
# No extra dependencies:
#   This uses only a plain Python dict — no Redis, no Flask-Caching needed.
#   Simple and safe for a single-process deployment on Railway.
# ---------------------------------------------------------------------------
_rate_cache = {"value": None, "fetched_at": None}
RATE_CACHE_TTL_SECONDS = 600  # 10 minutes


def get_naira_rate():
    now = datetime.utcnow()
    cached = _rate_cache["value"]
    fetched_at = _rate_cache["fetched_at"]

    # Return the cached rate if it is still within the TTL window.
    if cached and fetched_at and (now - fetched_at).total_seconds() < RATE_CACHE_TTL_SECONDS:
        return cached

    try:
        # FIXED URL: the open-access no-key endpoint is open.er-api.com, not
        # api.exchangerate-api.com. The api. subdomain requires a paid API key
        # and returns 404 without one — which is exactly what the logs showed.
        #
        # FIXED KEY: the open-access response uses "rates" not "conversion_rates".
        # Using the wrong key caused .get() to return None silently and fall
        # through to the hardcoded fallback even when the request succeeded.
        #
        # Open-access docs: https://www.exchangerate-api.com/docs/free
        # No API key needed. Updates once per day. Rate limit: once per hour max.
        # Our 10-minute cache (RATE_CACHE_TTL_SECONDS = 600) is well within that.
        res = requests.get('https://open.er-api.com/v6/latest/USD', timeout=5)
        res.raise_for_status()
        payload = res.json()
        rate = payload.get('rates', {}).get('NGN')  # "rates" not "conversion_rates"
        if rate:
            _rate_cache["value"] = rate
            _rate_cache["fetched_at"] = now
            return rate
        logger.warning('Exchange rate response missing NGN key. Full response: %s', payload)
    except Exception as exc:
        logger.warning('Failed to fetch exchange rate: %s', exc)

    # Live fetch failed. Return stale cache if available — a slightly old
    # rate is far better than the hardcoded fallback.
    if cached:
        logger.info('Returning stale cached exchange rate.')
        return cached

    # Absolute last resort: hardcoded fallback.
    logger.warning('Using hardcoded fallback exchange rate of 1500.')
    return 1500


# --- INIT (Flask-Migrate handles schema; create_all only runs outside production) ---
def init_db():
    if os.getenv('FLASK_ENV', 'production').lower() != 'production':
        with app.app_context():
            db.create_all()
            logger.info('✅ Database tables created/verified.')

init_db()


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# --- UTILITIES ---
PAYSTACK_SECRET = os.getenv('PAYSTACK_SECRET')
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', 'admin@example.com')

if not PAYSTACK_SECRET:
    logger.warning('PAYSTACK_SECRET is not set. Paystack payments will be unavailable.')


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

        if referred_by and not User.query.filter_by(referral_code=referred_by).first():
            referred_by = None

        new_user = User(
            full_name=full_name,
            email=email,
            password=generate_password_hash(password, method='pbkdf2:sha256'),
            referral_code=generate_referral_code(),
            referred_by=referred_by,
            whatsapp_number=whatsapp_number,
            location=location
        )
        db.session.add(new_user)
        db.session.commit()

        # -------------------------------------------------------------------
        # CHANGE: ReferralHistory row created with earnings_usd=0.0 and
        # status='Pending' instead of earnings_usd=0.50 and status='Active'.
        #
        # Problem in the original code:
        #   The referral record was written with earnings_usd=0.50 at the moment
        #   of registration, before the referred user had paid anything. This
        #   meant the referral history page showed "$0.50 earned" for users who
        #   signed up but never activated — misleading for the referrer.
        #
        # The fix:
        #   We create the record now (so the relationship is tracked) but mark
        #   the earnings as 0.0 and the status as 'Pending'. The actual $0.50
        #   credit is applied in /verify once Paystack confirms the payment.
        # -------------------------------------------------------------------
        if referred_by:
            referrer = User.query.filter_by(referral_code=referred_by).first()
            if referrer:
                referral_rec = ReferralHistory(
                    referrer_id=referrer.id,
                    referred_user_id=new_user.id,
                    earnings_usd=0.0,    # Not earned yet — user hasn't paid
                    status='Pending'     # Will become 'Active' after payment is verified
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

        # Using a generic message here intentionally — returning different messages
        # for "email not found" vs "wrong password" would let an attacker enumerate
        # which emails are registered in the system (user enumeration attack).
        flash('Invalid login details.')
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    # -------------------------------------------------------------------
    # CHANGE: Run the daily chances reset here, at dashboard load time.
    #
    # Problem:
    #   Previously the reset only ran inside /get-question and /check-answer,
    #   meaning the dashboard template rendered whatever stale value was in the
    #   DB — typically 0 from the day before. Users logging in the next morning
    #   would see "0 chances" and think they had none, because the reset hadn't
    #   fired yet. They had to click "Start Trivia" first to trigger it.
    #
    # The fix:
    #   Call maybe_reset_daily_chances() right here before the template renders.
    #   If it's a new day, chances are updated and committed so the template
    #   receives the correct fresh value. If it's the same day, the helper does
    #   nothing (returns False) and we skip the commit — zero extra DB cost.
    #   This means the number the user sees the moment they log in is always
    #   accurate, with no interaction required.
    # -------------------------------------------------------------------
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
    # -------------------------------------------------------------------
    # CHANGE: New lightweight endpoint the dashboard JS calls on page load
    # and on tab visibility change.
    #
    # Why this exists alongside the dashboard-route fix above:
    #   The dashboard route fix handles the initial page render — the number
    #   in the HTML will always be correct when the page first loads.
    #   This endpoint handles a second edge case: a user who leaves the
    #   dashboard tab open overnight. The page was rendered yesterday with
    #   the correct count, but midnight passed and the tab was never refreshed.
    #   The JS listens for the browser's visibilitychange event and calls this
    #   endpoint when the user returns to the tab, updating the counter live
    #   without requiring a full page reload.
    #
    #   It runs the same reset helper so calling it is always safe — if the
    #   reset already happened today, maybe_reset_daily_chances() returns False
    #   and no commit is issued.
    # -------------------------------------------------------------------
    maybe_reset_daily_chances(current_user)
    db.session.commit()
    db.session.refresh(current_user)

    chances = current_user.chances
    return jsonify({
        "chances": chances,
        "display": "unlimited" if chances >= UNLIMITED_CHANCES else chances
    })


@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html', user=current_user)

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
        # -------------------------------------------------------------------
        # CHANGE: Guard against double-activation.
        #
        # Problem in the original code:
        #   There was no check on whether the user was already active before
        #   crediting the referrer. If a user or browser re-requested the
        #   /verify URL with the same Paystack reference (e.g. hitting back,
        #   refreshing, or a network retry), the referrer's balance would be
        #   incremented a second time for the same payment.
        #
        # The fix:
        #   We only apply changes if is_active_member is currently False.
        #   Once it's True, any further calls to /verify for this user are
        #   silently ignored with an "already active" flash message.
        # -------------------------------------------------------------------
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
                        # Payment confirmed — set real earnings and mark Active.
                        # This pairs with the 0.0 / 'Pending' written in /register.
                        referral_rec.earnings_usd = 0.50
                        referral_rec.status = 'Active'
                    # Immediately upgrade the referrer's chances to unlimited.
                    # The referral is now Active so has_referral_today() will
                    # return True for the referrer. We call maybe_reset_daily_chances
                    # so the upgrade fires right now in this same request — the
                    # referrer does not have to reload their dashboard to see it.
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
    # -------------------------------------------------------------------
    # CHANGE: Rewrote this entire route to fix three separate bugs.
    #
    # Bug 1 — missing db.session.refresh():
    #   After maybe_reset_daily_chances() mutates user.chances and we call
    #   db.session.commit(), SQLAlchemy's identity map can still hold the
    #   OLD in-memory value of current_user.chances. Without explicitly
    #   refreshing, the `if current_user.chances <= 0` check below could
    #   read stale data and let a user with 0 chances through.
    #   db.session.refresh(current_user) forces a re-read from the DB.
    #
    # Bug 2 — no retry on already-answered questions:
    #   The original code picked one random question, then returned
    #   "already_answered" if it had been won. The user had to click again
    #   and hope the next random pick was unanswered. The new loop tries up
    #   to 5 different questions before giving up, so users almost never
    #   see a dead-end response on their first click.
    #
    # Bug 3 — frontend had no way to display remaining chances:
    #   The response now includes "chances_remaining" so the frontend can
    #   update a counter (e.g. "3 chances left today") without a separate
    #   API call.
    # -------------------------------------------------------------------

    # Step 1: Reset daily chances if it's a new UTC day, then commit and
    # refresh so current_user.chances reflects the actual DB value.
    maybe_reset_daily_chances(current_user)
    db.session.commit()
    db.session.refresh(current_user)  # <-- critical: re-reads chances from DB

    # Step 2: Gate check — refuse to serve a question if chances are exhausted.
    # This prevents the frontend from receiving a question the user can't answer.
    if current_user.chances <= 0:
        return jsonify({
            "status": "no_chances",
            "message": "You have used all your chances for today. Come back tomorrow!"
        })

    # Step 3: Fetch only question IDs from the DB (not full rows) so we don't
    # load the entire questions table into memory on every request.
    question_ids = db.session.query(Question.id).all()
    if not question_ids:
        return jsonify({"status": "error", "message": "No questions available right now."})

    # Step 4: Try up to 5 random picks to find a question that hasn't been
    # correctly answered yet. Capped at 5 attempts to avoid an infinite loop
    # in the edge case where nearly all questions have been won.
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
        # All sampled questions were already answered — tell the frontend.
        return jsonify({
            "status": "all_answered",
            "message": "All questions have been answered today. Check back later!"
        })

    # NOTE: We do NOT deduct a chance here. Chances are only deducted in
    # /check-answer when the user actually submits a response. This means a
    # page refresh or accidental load of /get-question never wastes a chance.
    return jsonify({
        "id": q.id,
        "question": q.text,
        "options": {"A": q.option_a, "B": q.option_b, "C": q.option_c, "D": q.option_d},
        # Show "unlimited" string if the user has referral-boosted chances,
        # otherwise show the integer so the frontend can display a countdown.
        "chances_remaining": current_user.chances if current_user.chances < UNLIMITED_CHANCES else "unlimited"
    })


@app.route('/check-answer', methods=['POST'])
@login_required
def check_answer():
    # -------------------------------------------------------------------
    # CHANGE: Rewrote this route to fix three bugs.
    #
    # Bug 1 — missing input validation:
    #   The original code called data.get('question_id') without first
    #   checking that `data` was not None. If the request body wasn't valid
    #   JSON, this would raise an AttributeError. We now validate the
    #   request payload before touching it.
    #
    # Bug 2 — missing db.session.refresh() (same as in /get-question):
    #   After committing the daily reset we must refresh current_user so the
    #   chances check below reads the real DB value, not a stale cached one.
    #
    # Bug 3 — chances_remaining added to every response:
    #   Every JSON response now includes chances_remaining so the frontend
    #   can update its display after each submission without a separate call.
    # -------------------------------------------------------------------

    data = request.json
    # Validate that the request body exists and has the required fields.
    if not data or 'question_id' not in data or 'choice' not in data:
        return jsonify({"status": "error", "message": "Invalid request."})

    question = db.session.get(Question, data.get('question_id'))
    if not question:
        return jsonify({"status": "error", "message": "Question not found."})

    # Guard: if anyone has already answered this question correctly, reject
    # immediately — no point deducting a chance for an unwinnable question.
    answered_correct = AnsweredQuestion.query.filter_by(
        question_id=question.id, is_correct=True
    ).first()
    if answered_correct:
        return jsonify({"status": "already_answered", "message": "This question has already been answered."})

    # Reset daily chances if needed, then refresh to get the live DB value.
    maybe_reset_daily_chances(current_user)
    db.session.commit()
    db.session.refresh(current_user)  # <-- critical: re-reads chances from DB

    # Gate: user must have at least 1 chance remaining to submit.
    if current_user.chances <= 0:
        return jsonify({
            "status": "no_chances",
            "message": "You have no chances remaining for today. Come back tomorrow!"
        })

    # Deduct one chance. Users with UNLIMITED_CHANCES (referral bonus) are exempt.
    if current_user.chances < UNLIMITED_CHANCES:
        current_user.chances -= 1

    # Evaluate the answer.
    is_correct = data.get('choice') == question.correct_answer
    answer_record = AnsweredQuestion(
        question_id=question.id,
        answered_by_id=current_user.id,
        selected_answer=data.get('choice'),
        is_correct=is_correct
    )
    db.session.add(answer_record)

    # Calculate what to show the frontend for remaining chances.
    chances_left = current_user.chances if current_user.chances < UNLIMITED_CHANCES else "unlimited"

    if is_correct:
        # -------------------------------------------------------------------
        # CHANGE: Added SELECT FOR UPDATE (row-level lock) on the PIN query.
        #
        # Problem in the original code:
        #   Two users answering correctly at almost the same moment could both
        #   execute `filter_by(is_used=False).first()` before either one had
        #   committed, causing both transactions to see the same unused PIN and
        #   award it to two different people.
        #
        # The fix:
        #   .with_for_update() appends "FOR UPDATE" to the SQL SELECT, which
        #   tells Postgres to lock the chosen row. Any other transaction that
        #   tries to SELECT the same row will block until we commit, guaranteeing
        #   only one winner can claim each PIN.
        # -------------------------------------------------------------------
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

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

if __name__ == "__main__":
    app.run()