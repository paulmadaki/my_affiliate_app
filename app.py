import os
from flask import Flask, render_template, request, redirect, url_resolve
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-123' # Change this later!

# DATABASE SETUP: Railway provides a "DATABASE_URL" automatically
# If running locally, it defaults to a simple file (sqlite)
app.config['SQLALCHEMY_DATABASE_VALUE'] = os.getenv('DATABASE_URL', 'sqlite:///db.sqlite')
db = SQLAlchemy(app)

# LOGIN MANAGER SETUP
login_manager = LoginManager()
login_manager.init_app(app)

# 1. THE USER TABLE (Created automatically)
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True)
    password = db.Column(db.String(100))
    balance_usd = db.Column(db.Float, default=0.0)
    chances = db.Column(db.Integer, default=1)
    referral_code = db.Column(db.String(20), unique=True)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# 2. CREATE THE TABLES
with app.app_context():
    db.create_all()

@app.route('/')
def home():
    return render_template('index.html')

# Simple Login Route (For demonstration)
@app.route('/login', methods=['POST'])
def login():
    email = request.form.get('email')
    # In a real app, you would check the password here!
    user = User.query.filter_by(email=email).first()
    if user:
        login_user(user)
    return redirect('/')

if __name__ == "__main__":
    app.run()