from flask import Flask, render_template, request, redirect, session
import os
from dotenv import load_dotenv
from auth import oauth, init_oauth
from groq import Groq

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

# 🔐 REQUIRED for mobile + external browser login
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
)

init_oauth(app)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# ---------- AI FUNCTION ----------

def run_ai(prompt):
    try:
        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant",
            temperature=0.7,
            max_tokens=800
        )
        text = chat.choices[0].message.content.strip()
        return text.replace("**", "").replace("*", "")
    except Exception as e:
        return f"AI Error: {e}"

# ---------- ROUTES ----------

@app.route("/")
def landing():
    return render_template("landing.html")

# 🔐 LOGIN (FINAL FIX)
@app.route("/login")
def login():
    redirect_uri = "https://pranox-ai.onrender.com/authorize"
    return oauth.google.authorize_redirect(redirect_uri)

# 🔐 CALLBACK (FINAL + SAFE)
@app.route("/authorize")
def authorize():
    try:
        token = oauth.google.authorize_access_token()

        user = oauth.google.get(
            "https://openidconnect.googleapis.com/v1/userinfo"
        ).json()

        session.clear()
        session["user"] = {
            "name": user.get("name"),
            "email": user.get("email"),
            "picture": user.get("picture"),
        }

        return redirect("/dashboard")

    except Exception as e:
        print("OAuth Error:", e)
        return redirect("/")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login")
    return render_template("dashboard.html", user=session["user"])

@app.route("/email", methods=["GET", "POST"])
def email():
    if "user" not in session:
        return redirect("/login")

    email_text = ""
    if request.method == "POST":
        prompt = f"""
Write a professional {request.form['tone']} business email.
Plain text only. No markdown.
Details:
{request.form['topic']}
"""
        email_text = run_ai(prompt)

    return render_template("email.html", email=email_text)

@app.route("/resume", methods=["GET", "POST"])
def resume():
    if "user" not in session:
        return redirect("/login")

    resume_text = ""
    if request.method == "POST":
        prompt = f"""
Create a professional resume.
Plain text only. No markdown.

Name: {request.form['name']}
Role: {request.form['role']}
Skills: {request.form['skills']}
Experience: {request.form['experience']}
Education: {request.form['education']}
"""
        resume_text = run_ai(prompt)

    return render_template("resume.html", resume=resume_text)

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")

if __name__ == "__main__":
    app.run(debug=True)
