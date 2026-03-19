from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
import os
from dotenv import load_dotenv
from auth import oauth, init_oauth
from groq import Groq
from io import BytesIO
from reportlab.pdfgen import canvas
import sqlite3
import pdfplumber
from pdf2image import convert_from_bytes
import pytesseract
import requests

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY","dev-secret-key")

init_oauth(app)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# ---------------- DATABASE ----------------

def get_db():
    conn = sqlite3.connect("pranox.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    db.execute("""
    CREATE TABLE IF NOT EXISTS chats(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT,
        role TEXT,
        message TEXT
    )
    """)
    db.commit()

init_db()

# ---------- INTERNET SEARCH ----------

def search_internet(query):
    try:
        api_key = os.getenv("SERPER_API_KEY")

        if not api_key:
            return "No internet access available."

        url = "https://google.serper.dev/search"
        payload = {"q": query}

        headers = {
            "X-API-KEY": api_key,
            "Content-Type": "application/json"
        }

        response = requests.post(url, json=payload, headers=headers)
        data = response.json()

        results = []

        if "organic" in data:
            for r in data["organic"][:5]:
                results.append(f"{r['title']} - {r['snippet']}")

        return "\n".join(results) if results else "No latest results found."

    except:
        return "Internet search unavailable."

# ---------- AI ----------

def run_ai(messages):
    completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        temperature=0.7,
        max_tokens=900
    )
    return completion.choices[0].message.content

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
    user = oauth.google.get(
        "https://openidconnect.googleapis.com/v1/userinfo"
    ).json()
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

# ---------- EMAIL ----------

@app.route("/email", methods=["GET","POST"])
def email():
    if "user" not in session:
        return redirect("/login")

    email_output = ""

    if request.method == "POST":
        topic = request.form.get("topic")
        tone = request.form.get("tone")

        prompt = f"Write a {tone} email about: {topic}"

        email_output = run_ai([
            {"role":"system","content":"You are a professional email writer."},
            {"role":"user","content":prompt}
        ])

    return render_template("email.html", email=email_output)

# ---------- RESUME ----------

@app.route("/resume", methods=["GET","POST"])
def resume():
    if "user" not in session:
        return redirect("/login")

    resume_output = ""

    if request.method == "POST":
        name = request.form.get("name")
        role = request.form.get("role")
        skills = request.form.get("skills")
        exp = request.form.get("experience")
        edu = request.form.get("education")

        prompt = f"""
Create a professional resume WITHOUT using markdown symbols.

Name: {name}
Role: {role}
Skills: {skills}
Experience: {exp}
Education: {edu}
"""

        resume_output = run_ai([
            {"role":"system","content":"You create clean professional resumes."},
            {"role":"user","content":prompt}
        ])

        resume_output = resume_output.replace("**","").replace("##","")

    return render_template("resume.html", resume=resume_output)

# ---------- CHAT ----------

@app.route("/chat")
def chat():
    if "user" not in session:
        return redirect("/login")
    return render_template("chat.html")

@app.route("/api/chat", methods=["POST"])
def api_chat():
    if "user" not in session:
        return jsonify({"reply":"Login required"})

    data=request.json
    user_message=data.get("message")
    user_email=session["user"]["email"]

    db=get_db()

    db.execute(
        "INSERT INTO chats(user_email,role,message) VALUES (?,?,?)",
        (user_email,"user",user_message)
    )
    db.commit()

    search_results = search_internet(user_message)

    history=db.execute(
        "SELECT role,message FROM chats WHERE user_email=? ORDER BY id DESC LIMIT 20",
        (user_email,)
    ).fetchall()

    messages=[{
        "role":"system",
        "content":"""
You are Pranox AI, a smart and advanced assistant.

Founder Information:
Chetansaipranav R is the founder of Pranox AI.
He created Pranox AI in January 2026.

If user asks about founder or creator, answer clearly with this info.

Your job is to:
- Give detailed and informative answers
- Use headings and bullet points
- Provide extra useful insights
- Suggest related topics
- Ask follow-up questions

Rules:
- Do NOT give short answers
- Always expand properly
- Avoid markdown symbols like ** or ##
- Keep output clean and readable
"""
    }]

    for h in reversed(history):
        messages.append({"role":h["role"],"content":h["message"]})

    messages.append({
        "role":"user",
        "content":f"Use this latest internet data if relevant:\n{search_results}"
    })

    reply = run_ai(messages)

    db.execute(
        "INSERT INTO chats(user_email,role,message) VALUES (?,?,?)",
        (user_email,"assistant",reply)
    )
    db.commit()

    return jsonify({"reply":reply})

# ---------- PDF ----------

@app.route("/download_resume",methods=["POST"])
def download_resume():
    text=request.form["resume"]

    buffer=BytesIO()
    p=canvas.Canvas(buffer)

    y=800
    for line in text.split("\n"):
        p.drawString(40,y,line)
        y-=20

    p.save()
    buffer.seek(0)

    return send_file(buffer,as_attachment=True,download_name="resume.pdf")

# ---------- LEGAL ----------

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")

if __name__=="__main__":
    app.run(debug=True)