from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_cors import CORS
import os
from dotenv import load_dotenv
from auth import oauth, init_oauth
from groq import Groq

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# ✅ ENABLE CORS FOR FLUTTER
CORS(app)

# OAuth
init_oauth(app)

# Groq
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

def run_ai(prompt):
    chat = client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model="llama-3.1-8b-instant",
        temperature=0.7,
        max_tokens=800
    )
    return chat.choices[0].message.content.strip().replace("*", "")

# ---------------- WEB ----------------

@app.route("/")
def landing():
    return render_template("landing.html")

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")

# ---------------- API (FLUTTER) ----------------

@app.route("/api/email", methods=["POST"])
def api_email():
    data = request.get_json()

    prompt = f"""
Write a professional {data.get('tone')} business email.
Plain text only.

Details:
{data.get('topic')}
"""
    result = run_ai(prompt)

    return jsonify({"email": result})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    data = request.get_json()

    prompt = f"""
Create a professional resume.
Plain text only.

Name: {data.get('name')}
Role: {data.get('role')}
Skills: {data.get('skills')}
Experience: {data.get('experience')}
Education: {data.get('education')}
"""
    result = run_ai(prompt)

    return jsonify({"resume": result})


if __name__ == "__main__":
    app.run(debug=True)
