from flask import Flask, render_template, jsonify, request
import random

app = Flask(__name__)

# Basic Question Bank
QUESTIONS = [
    {"id": 1, "question": "What is 2 + 2?", "options": ["3", "4", "5"], "answer": "4"},
    {"id": 2, "question": "Capital of Nigeria?", "options": ["Lagos", "Abuja", "Kano"], "answer": "Abuja"}
]

@app.route('/')
def home():
   # This looks for the index.html file in your 'templates' folder
    return render_template('index.html')
@app.route('/get-question')
def get_q():
    return jsonify(random.choice(QUESTIONS))

if __name__ == "__main__":
    app.run()