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

        # Remove markdown
        text = text.replace("**", "")

        # Strong paragraph formatting
        text = text.replace("Subject:", "\nSubject:\n")
        text = text.replace("Dear", "\nDear")
        text = text.replace("Hello", "\nHello")
        text = text.replace("Hi", "\nHi")
        text = text.replace("Regards", "\n\nRegards")
        text = text.replace("Sincerely", "\n\nSincerely")
        text = text.replace("Thank you", "\n\nThank you")

        # Break long sentences
        text = text.replace(". ", ".\n")
        text = text.replace(": ", ":\n")

        return text.strip()

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
- Use proper paragraphs
- Use line breaks after Subject, Greeting, Body, Closing
- Output plain text only

Details:
{topic}
"""

        email_text = run_ai(prompt)

    return render_template("email.html", email=email_text)


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
