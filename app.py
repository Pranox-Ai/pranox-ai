from flask import Flask, render_template, request, redirect, url_for, session
import os
from dotenv import load_dotenv
from auth import oauth, init_oauth
from groq import Groq
from datetime import datetime

# Load env
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# OAuth
init_oauth(app)

# Groq Client
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# ----------- AI FUNCTION -----------

def run_ai(prompt):
    try:
        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant",
            temperature=0.7,
            max_tokens=800
        )

        text = chat.choices[0].message.content.strip()

        # CLEANUP MARKDOWN / STARS
        text = text.replace("**", "")
        text = text.replace("*", "")

        return text

    except Exception as e:
        return f"AI Error: {str(e)}"


# ----------- DAILY LIMIT FUNCTION -----------

def check_limit():
    today = str(datetime.now().date())

    if "usage_date" not in session:
        session["usage_date"] = today
        session["usage_count"] = 0

    if session["usage_date"] != today:
        session["usage_date"] = today
        session["usage_count"] = 0

    if session["usage_count"] >= 5:
        return False

    session["usage_count"] += 1
    return True


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


# ---------------- EMAIL ----------------

@app.route("/email", methods=["GET", "POST"])
def email():
    if "user" not in session:
        return redirect("/login")

    email_text = ""

    if request.method == "POST":
        if not check_limit():
            return render_template("email.html", email="Daily limit reached. Try tomorrow.")

        topic = request.form["topic"]
        tone = request.form["tone"]

        prompt = f"""
Write a professional {tone} business email.

STRICT RULES:
- Output ONLY plain text
- NO bold
- NO markdown
- NO stars
- NO emojis
- Proper paragraphs
- Corporate formatting
- Ready to send

Details:
{topic}
"""
        email_text = run_ai(prompt)

    return render_template("email.html", email=email_text)


# ---------------- RESUME ----------------

@app.route("/resume", methods=["GET", "POST"])
def resume():
    if "user" not in session:
        return redirect("/login")

    resume_text = ""

    if request.method == "POST":
        if not check_limit():
            return render_template("resume.html", resume="Daily limit reached. Try tomorrow.")

        name = request.form["name"]
        skills = request.form["skills"]
        experience = request.form["experience"]
        education = request.form["education"]
        role = request.form["role"]

        prompt = f"""
Create a professional resume.

STRICT RULES:
- Plain text only
- NO bold
- NO markdown
- NO stars
- Headings in CAPITAL LETTERS
- Clean spacing
- ATS friendly
- Corporate formatting

Name: {name}
Role: {role}
Skills: {skills}
Experience: {experience}
Education: {education}
"""
        resume_text = run_ai(prompt)

    return render_template("resume.html", resume=resume_text)


# ---------------- PRIVACY ----------------

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


# ---------------- TERMS ----------------

@app.route("/terms")
def terms():
    return render_template("terms.html")


# ---------------- RUN APP ----------------

if __name__ == "__main__":
    app.run(debug=True)
