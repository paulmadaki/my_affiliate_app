import os
import uuid
import requests
import random
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'tech-growth-2026-key')

# --- DATABASE CONFIGURATION ---
# FIX 1: Railway provides "postgres://" but SQLAlchemy 1.4+ requires "postgresql://"
database_url = os.getenv("DATABASE_URL")

if not database_url:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. "
        "Make sure your Postgres plugin is linked to this service in Railway."
    )

# Fix the scheme Railway gives us
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

print("DATABASE_URL scheme =", database_url.split("@")[0].split(":")[0])  # Safe partial log

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    # FIX 2: Prevents "SSL connection has been closed unexpectedly" errors
    # that happen when Railway recycles idle connections
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --- DATABASE MODELS ---

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    referral_code = db.Column(db.String(20), unique=True)
    referred_by = db.Column(db.String(20), nullable=True)
    balance_usd = db.Column(db.Float, default=0.0)
    chances = db.Column(db.Integer, default=5)
    is_active_member = db.Column(db.Boolean, default=False)
    whatsapp_number = db.Column(db.String(20), nullable=True)
    location = db.Column(db.String(255), nullable=True)  # Format: Country/State/City
    created_at = db.Column(db.DateTime, default=db.func.now())

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
    deduction_fee_usd = db.Column(db.Float, default=0.0)  # 10% deduction
    net_amount_usd = db.Column(db.Float, default=0.0)  # Amount after deduction
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
    status = db.Column(db.String(20), default='Active')  # Active, Inactive, etc.
    created_at = db.Column(db.DateTime, default=db.func.now())

# --- INITIALIZE DATABASE ---
# FIX 3: Wrap in a function so it's safe with Gunicorn multi-worker startup
def init_db():
    with app.app_context():
        db.create_all()
        print("✅ Database tables created/verified.")

init_db()

# FIX 4: Use db.session.get() — Query.get() is deprecated in SQLAlchemy 2.x
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# --- UTILITIES ---
PAYSTACK_SECRET = os.getenv('PAYSTACK_SECRET')

def get_naira_rate():
    try:
        res = requests.get("https://api.exchangerate-api.com/v6/latest/USD", timeout=5)
        return res.json()['conversion_rates']['NGN']
    except Exception:
        return 1500  # Fallback

# --- ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        referred_by = request.form.get('ref')
        whatsapp_number = request.form.get('whatsapp_number')
        location = request.form.get('location')

        if not email or not password or not whatsapp_number or not location:
            flash("Please fill in all required fields.")
            return redirect(url_for('register'))

        if len(password) < 6:
            flash("Password must be at least 6 characters.")
            return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():
            flash("Email already exists!")
            return redirect(url_for('register'))

        # Validate WhatsApp number (basic validation)
        if not whatsapp_number.replace('+', '').isdigit():
            flash("Invalid WhatsApp number format!")
            return redirect(url_for('register'))

        new_user = User(
            email=email,
            password=generate_password_hash(password, method='pbkdf2:sha256'),
            referral_code=uuid.uuid4().hex[:8],
            referred_by=referred_by,
            whatsapp_number=whatsapp_number,
            location=location
        )
        db.session.add(new_user)
        db.session.commit()
        
        # Track referral if applicable
        if referred_by:
            referrer = User.query.filter_by(referral_code=referred_by).first()
            if referrer:
                referral_rec = ReferralHistory(
                    referrer_id=referrer.id,
                    referred_user_id=new_user.id,
                    earnings_usd=0.50
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
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid login details.')
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    pins_count = RechargeCard.query.filter_by(is_used=False).count()
    return render_template('dashboard.html', user=current_user, pins_count=pins_count)

@app.route('/pay')
@login_required
def pay():
    rate = get_naira_rate()
    amount_kobo = int(2 * rate * 100)
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET}"}
    data = {
        "email": current_user.email,
        "amount": amount_kobo,
        "callback_url": url_for('verify_payment', _external=True)
    }
    r = requests.post("https://api.paystack.co/transaction/initialize", headers=headers, json=data)
    return redirect(r.json()['data']['authorization_url'])

@app.route('/verify')
@login_required
def verify_payment():
    ref = request.args.get('reference')
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET}"}
    r = requests.get(f"https://api.paystack.co/transaction/verify/{ref}", headers=headers)
    res = r.json()

    if res['status'] and res['data']['status'] == 'success':
        current_user.is_active_member = True
        if current_user.referred_by:
            referrer = User.query.filter_by(referral_code=current_user.referred_by).first()
            if referrer:
                referrer.balance_usd += 0.50
                # Update referral history status
                referral_rec = ReferralHistory.query.filter_by(
                    referred_user_id=current_user.id
                ).first()
                if referral_rec:
                    referral_rec.status = 'Active'
        db.session.commit()
        flash("Account Activated!")
    return redirect(url_for('dashboard'))

# --- TRIVIA ENGINE ---

@app.route('/get-question')
@login_required
def get_question():
    if current_user.chances <= 0:
        return jsonify({"status": "error", "message": "No chances left!"})

    all_q = Question.query.all()
    if not all_q:
        return jsonify({"status": "error", "message": "No questions in database."})

    q = random.choice(all_q)
    return jsonify({
        "id": q.id, "question": q.text,
        "options": {"A": q.option_a, "B": q.option_b, "C": q.option_c, "D": q.option_d}
    })

@app.route('/check-answer', methods=['POST'])
@login_required
def check_answer():
    data = request.json
    # FIX 5: Use db.session.get() instead of deprecated Query.get()
    question = db.session.get(Question, data.get('question_id'))

    if not question:
        return jsonify({"status": "error", "message": "Question not found."})

    current_user.chances -= 1

    if data.get('choice') == question.correct_answer:
        pin = RechargeCard.query.filter_by(is_used=False).first()

        if not pin:
            db.session.commit()
            return jsonify({
                "status": "correct_but_empty",
                "message": "Correct! However, all airtime rewards for today have been claimed. Please try again tomorrow or contact support."
            })
        else:
            pin.is_used = True
            pin.winner_id = current_user.id
            db.session.commit()
            return jsonify({"status": "win", "pin": pin.pin})

    db.session.commit()
    return jsonify({"status": "wrong", "message": "Incorrect!"})

@app.route('/request-payout', methods=['POST'])
@login_required
def request_payout():
    amount_requested = 5.0  # Fixed minimum
    
    if current_user.balance_usd < amount_requested:
        return jsonify({"status": "error", "message": "Minimum $5 required"})
    
    # Check for pending withdrawals (prevent duplicates)
    pending_payout = PayoutRequest.query.filter_by(
        user_id=current_user.id,
        status='Pending'
    ).first()
    
    if pending_payout:
        return jsonify({"status": "error", "message": "You have a pending withdrawal. Please wait for it to be processed."})
    
    # Calculate 10% deduction
    deduction_fee = amount_requested * 0.10
    net_amount = amount_requested - deduction_fee
    
    # Create payout request
    new_payout = PayoutRequest(
        user_id=current_user.id,
        amount_usd=amount_requested,
        deduction_fee_usd=deduction_fee,
        net_amount_usd=net_amount,
        bank_name=request.form.get('bank_name'),
        account_number=request.form.get('account_number'),
        account_name=request.form.get('account_name')
    )
    current_user.balance_usd -= amount_requested
    db.session.add(new_payout)
    db.session.commit()
    
    return jsonify({
        "status": "success",
        "message": "Withdrawal request submitted!",
        "details": {
            "original_amount": amount_requested,
            "deduction_fee": round(deduction_fee, 2),
            "net_amount": round(net_amount, 2)
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