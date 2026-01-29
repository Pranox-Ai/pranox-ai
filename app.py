from flask import Flask, render_template, request, redirect, url_for, session
import os
from dotenv import load_dotenv
from auth import oauth, init_oauth
from groq import Groq

# Load .env variables
load_dotenv()

app = Flask(__name__)

# Secret key from .env
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# Initialize OAuth
init_oauth(app)

# Initialize Groq Client
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# ----------- AI FUNCTION (Groq) -----------

def run_ai(prompt):
    try:
        chat = client.chat.completions.create(
            messages=[
                {"role": "user", "content": prompt}
            ],
            # ACTIVE + FREE MODEL
            model="llama3-8b-8192",
            temperature=0.7,
            max_tokens=800
        )
        return chat.choices[0].message.content.strip()
    except Exception as e:
        return f"AI Error: {str(e)}"

# ---------------- LANDING ----------------

@app.route("/")
def landing():
    return render_template("landing.html")

# ---------------- AUTH ----------------

@app.route("/login")
def login():
    redirect_uri = url_for("authorize", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/authorize")
def authorize():
    oauth.google.authorize_access_token()
    user = oauth.google.get(
        "https://openidconnect.googleapis.com/v1/userinfo"
    ).json()
    session["user"] = user
    return redirect("/dashboard")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ---------------- DASHBOARD ----------------

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login")
    return render_template("dashboard.html", user=session["user"])

# ---------------- EMAIL TOOL ----------------

@app.route("/email", methods=["GET", "POST"])
def email():
    if "user" not in session:
        return redirect("/login")

    email_text = ""
    if request.method == "POST":
        topic = request.form["topic"]
        tone = request.form["tone"]

        prompt = f"""
You are a senior corporate email writer.

Write a professional {tone} business email.

Rules:
- Subject line required
- Professional tone
- No emojis
- No markdown
- Output only the email
- Ready to send

Details:
{topic}
"""
        email_text = run_ai(prompt)

    return render_template("email.html", email=email_text)

# ---------------- RESUME TOOL ----------------

@app.route("/resume", methods=["GET", "POST"])
def resume():
    if "user" not in session:
        return redirect("/login")

    resume_text = ""
    if request.method == "POST":
        name = request.form["name"]
        skills = request.form["skills"]
        experience = request.form["experience"]
        education = request.form["education"]
        role = request.form["role"]

        prompt = f"""
You are a senior HR resume writer.

Create a complete professional resume.

Rules:
- Plain text only
- No markdown
- ATS friendly
- Minimum 300 words
- Ready to paste into Word

Name: {name}
Target Role: {role}
Skills: {skills}
Experience: {experience}
Education: {education}
"""
        resume_text = run_ai(prompt)

    return render_template("resume.html", resume=resume_text)

if __name__ == "__main__":
    app.run(debug=True)
