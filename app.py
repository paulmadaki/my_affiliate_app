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
# Ensures compatibility with Railway's PostgreSQL connection strings
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///app.db').replace("postgres://", "postgresql://")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

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
    chances = db.Column(db.Integer, default=1)
    is_active_member = db.Column(db.Boolean, default=False)

class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(500), nullable=False)
    option_a = db.Column(db.String(100), nullable=False)
    option_b = db.Column(db.String(100), nullable=False)
    option_c = db.Column(db.String(100), nullable=False)
    option_d = db.Column(db.String(100), nullable=False)
    correct_answer = db.Column(db.String(10), nullable=False) # 'A', 'B', 'C', or 'D'

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
    bank_name = db.Column(db.String(100), nullable=False)
    account_number = db.Column(db.String(20), nullable=False)
    account_name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='Pending')
    created_at = db.Column(db.DateTime, default=db.func.now())

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- UTILITIES ---
PAYSTACK_SECRET = os.getenv('PAYSTACK_SECRET')
def get_naira_rate():
    try:
        res = requests.get("https://api.exchangerate-api.com/v6/latest/USD")
        return res.json()['conversion_rates']['NGN']
    except:
        return 1500 # Fallback

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

        if User.query.filter_by(email=email).first():
            flash("Email already exists!")
            return redirect(url_for('register'))

        new_user = User(
            email=email,
            password=generate_password_hash(password, method='pbkdf2:sha256'),
            referral_code=uuid.uuid4().hex[:8],
            referred_by=referred_by
        )
        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        return redirect(url_for('dashboard'))

    code_from_link = request.args.get('ref', '')
    return render_template('register.html', ref_code=code_from_link)

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
    question = Question.query.get(data.get('question_id'))
    current_user.chances -= 1

    if data.get('choice') == question.correct_answer:
        # Check if there are any pins left in your database
        pin = RechargeCard.query.filter_by(is_used=False).first()
        
        if not pin:
            # No pins available
            db.session.commit()
            return jsonify({
                "status": "correct_but_empty",
                "message": "Correct! However, all airtime rewards for today have been claimed. Please try again tomorrow or contact support."
            })
        else:
            # Logic to award the pin
            pin.is_used = True
            pin.winner_id = current_user.id
            db.session.commit()
            return jsonify({
                "status": "win",
                "pin": pin.pin
            })
    
    db.session.commit()
    return jsonify({"status": "wrong", "message": "Incorrect!"})

@app.route('/request-payout', methods=['POST'])
@login_required
def request_payout():
    if current_user.balance_usd < 5.0:
        return jsonify({"status": "error", "message": "Minimum $5 required"})
    
    new_payout = PayoutRequest(
        user_id=current_user.id, bank_name=request.form.get('bank_name'),
        account_number=request.form.get('account_number'), account_name=request.form.get('account_name')
    )
    current_user.balance_usd -= 5.0
    db.session.add(new_payout)
    db.session.commit()
    return jsonify({"status": "success", "message": "Request submitted!"})

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run()