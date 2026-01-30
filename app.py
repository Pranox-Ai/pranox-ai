from flask import Flask, render_template, request, redirect, url_for, session
import os
from dotenv import load_dotenv
from auth import oauth, init_oauth
from groq import Groq
from datetime import datetime

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

init_oauth(app)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# ---------- LIMIT SETTINGS ----------
EMAIL_LIMIT = 3
RESUME_LIMIT = 1

# ---------- AI FUNCTION ----------
def run_ai(prompt):
    try:
        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant"
        )

        text = chat.choices[0].message.content

        # -------- FORMAT FIX --------
        text = text.replace("**", "")
        text = text.replace("Subject:", "\nSubject:")
        text = text.replace("Dear", "\nDear")
        text = text.replace("Regards", "\n\nRegards")
        text = text.replace("Experience:", "\n\nExperience:")
        text = text.replace("Education:", "\n\nEducation:")
        text = text.replace("Skills:", "\n\nSkills:")
        text = text.strip()

        return text

    except Exception as e:
        return f"AI Error: {str(e)}"

# ---------- RESET DAILY ----------
def reset_daily():
    today = datetime.now().strftime("%Y-%m-%d")
    if session.get("date") != today:
        session["date"] = today
        session["email_count"] = 0
        session["resume_count"] = 0

# ---------- ROUTES ----------

@app.route("/")
def landing():
    return render_template("landing.html")

@app.route("/login")
def login():
    redirect_uri = url_for("authorize", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/authorize")
def authorize():
    oauth.google.authorize_access_token()
    user = oauth.google.get("https://openidconnect.googleapis.com/v1/userinfo").json()
    session["user"] = user
    return redirect("/dashboard")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login")
    return render_template("dashboard.html", user=session["user"])

@app.route("/pro")
def pro():
    if "user" not in session:
        return redirect("/login")
    return render_template("pro.html")


# ---------- EMAIL ----------
@app.route("/email", methods=["GET", "POST"])
def email():
    if "user" not in session:
        return redirect("/login")

    reset_daily()
    email_text = ""
    limit_msg = ""

    if request.method == "POST":
        if session.get("email_count", 0) >= EMAIL_LIMIT:
            limit_msg = "Daily limit reached. Upgrade to Pro for unlimited access."
        else:
            topic = request.form["topic"]
            tone = request.form["tone"]

            prompt = f"""
Write a professional {tone} business email.
Include subject. No emojis. No markdown.
Details: {topic}
"""
            email_text = run_ai(prompt)
            session["email_count"] += 1

    return render_template("email.html", email=email_text, limit_msg=limit_msg)

# ---------- RESUME ----------
@app.route("/resume", methods=["GET", "POST"])
def resume():
    if "user" not in session:
        return redirect("/login")

    reset_daily()
    resume_text = ""
    limit_msg = ""

    if request.method == "POST":
        if session.get("resume_count", 0) >= RESUME_LIMIT:
            limit_msg = "Daily limit reached. Upgrade to Pro for unlimited resumes."
        else:
            name = request.form["name"]
            skills = request.form["skills"]
            experience = request.form["experience"]
            education = request.form["education"]
            role = request.form["role"]

            prompt = f"""
Create a professional resume.
Name: {name}
Role: {role}
Skills: {skills}
Experience: {experience}
Education: {education}
"""
            resume_text = run_ai(prompt)
            session["resume_count"] += 1

    return render_template("resume.html", resume=resume_text, limit_msg=limit_msg)

if __name__ == "__main__":
    app.run(debug=True)
