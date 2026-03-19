from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
import os
from dotenv import load_dotenv
from auth import oauth, init_oauth
from groq import Groq
from io import BytesIO
from reportlab.pdfgen import canvas
import sqlite3
import requests
import pdfplumber

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY","dev-secret-key")

init_oauth(app)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# DATABASE
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

# INTERNET SEARCH
def search_internet(query):
    try:
        api_key = os.getenv("SERPER_API_KEY")
        if not api_key:
            return ""

        res = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query}
        )

        data = res.json()

        results=[]
        if "organic" in data:
            for r in data["organic"][:3]:
                results.append(f"{r['title']} - {r['snippet']}")

        return "\n".join(results)

    except:
        return ""

# AI (LONG + SMART)
def run_ai(messages):
    completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        temperature=0.7,
        max_tokens=1200
    )
    return completion.choices[0].message.content

# ROUTES
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

@app.route("/chat")
def chat():
    if "user" not in session:
        return redirect("/login")
    return render_template("chat.html")

# 🔥 CHAT API
@app.route("/api/chat", methods=["POST"])
def api_chat():

    if "user" not in session:
        return jsonify({"reply":"Login required"})

    data=request.json
    user_message=data.get("message")

    user_email=session["user"]["email"]

    db=get_db()

    # save user message
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
You are Pranox AI.

Founder Information:
The Founder of Pranox Ai is Chetansaipranav R in January 2026 at the age of 20 he created Pranox Groups a Parent Company of Pranox Ai.

If someone asks:
- Who created Pranox AI
- Who is the founder
- Tell me about yourself

Answer clearly:
"Chetansaipranav R is the founder of Pranox AI. He created Pranox AI in January 2026."

Always format answers clearly using headings, bullet points and paragraphs.
Never show markdown symbols like **.
Give the answers breifly and clearly and give suggestions to the user to ask next.
"""
    }]

    for h in reversed(history):
        messages.append({
            "role":h["role"],
            "content":h["message"]
        })

    messages.append({
        "role":"system",
        "content":f"Latest internet search results:\n{search_results}"
    })

    reply = run_ai(messages)

    db.execute(
        "INSERT INTO chats(user_email,role,message) VALUES (?,?,?)",
        (user_email,"assistant",reply)
    )
    db.commit()

    return jsonify({"reply":reply})

# 🔥 FILE UPLOAD (PDF SUPPORT)
@app.route("/api/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"reply":"No file uploaded"})

    file = request.files["file"]

    text = ""

    try:
        if file.filename.endswith(".pdf"):
            with pdfplumber.open(file) as pdf:
                for page in pdf.pages:
                    text += page.extract_text() or ""
        else:
            text = file.read().decode("utf-8")

        messages=[{
            "role":"system",
            "content":"Explain the uploaded file clearly and in detail."
        }]

        messages.append({
            "role":"user",
            "content":text[:8000]
        })

        reply = run_ai(messages)

        return jsonify({"reply":reply})

    except Exception as e:
        return jsonify({"reply":"Error reading file"})

# PDF DOWNLOAD
@app.route("/download_resume",methods=["POST"])
def download_resume():
    buffer=BytesIO()
    p=canvas.Canvas(buffer)

    y=800
    for line in request.form["resume"].split("\n"):
        p.drawString(40,y,line)
        y-=20

    p.save()
    buffer.seek(0)

    return send_file(buffer,as_attachment=True,download_name="resume.pdf")

# RUN
if __name__=="__main__":
    app.run(debug=True)