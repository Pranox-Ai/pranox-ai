from flask import Flask, render_template, request, redirect, url_for, session
import os
from dotenv import load_dotenv
from auth import oauth, init_oauth
from groq import Groq

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
        text = text.replace("**", "")
        text = text.replace("*", "")
        return text

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


# ---------------- EMAIL ----------------

@app.route("/email", methods=["GET", "POST"])
def email():
    if "user" not in session:
        return redirect("/login")

    email_text = ""

    if request.method == "POST":
        topic = request.form["topic"]
        tone = request.form["tone"]

        prompt = f"""
Write a professional {tone} business email.
Plain text only. No markdown. No stars.
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
        name = request.form["name"]
        skills = request.form["skills"]
        experience = request.form["experience"]
        education = request.form["education"]
        role = request.form["role"]

        prompt = f"""
Create a professional resume.
Plain text only. No markdown. No stars.
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


# ---------------- RUN ----------------

if __name__ == "__main__":
    app.run(debug=True)
