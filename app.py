from flask import Flask, jsonify, request
import random

app = Flask(__name__)

# Basic Question Bank
QUESTIONS = [
    {"id": 1, "question": "What is 2 + 2?", "options": ["3", "4", "5"], "answer": "4"},
    {"id": 2, "question": "Capital of Nigeria?", "options": ["Lagos", "Abuja", "Kano"], "answer": "Abuja"}
]

@app.route('/')
def home():
    return "<h1>Welcome to your Affiliate & Trivia Platform</h1><p>The site is live!</p>"

@app.route('/get-question')
def get_q():
    return jsonify(random.choice(QUESTIONS))

if __name__ == "__main__":
    app.run()