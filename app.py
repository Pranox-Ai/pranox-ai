from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, Response
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

    url = "https://google.serper.dev/search"

    payload = {"q": query}

    headers = {
        "X-API-KEY": os.getenv("SERPER_API_KEY"),
        "Content-Type": "application/json"
    }

    response = requests.post(url, json=payload, headers=headers)

    data = response.json()

    results = []

    if "organic" in data:
        for r in data["organic"][:5]:
            results.append(f"{r['title']} - {r['snippet']}")

    return "\n".join(results)


# ---------- AI ----------

def run_ai(messages):

    completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        temperature=0.7,
        max_tokens=400
    )

    return completion.choices[0].message.content


def run_ai_stream(messages):

    completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        temperature=0.7,
        max_tokens=400,
        stream=True
    )

    for chunk in completion:
        if chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


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


# ---------- CHAT PAGE ----------

@app.route("/chat")
def chat():

    if "user" not in session:
        return redirect("/login")

    return render_template("chat.html")


# ---------- CHAT API ----------

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
"Chetansaipranav R is the founder of Pranox AI. He created Pranox AI in January 2026 at the age of 20."

Always format answers clearly using headings, bullet points and paragraphs.
Never show markdown symbols like **.
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



# ---------- FILE ANALYSIS ----------

@app.route("/api/upload", methods=["POST"])
def upload():

    if "user" not in session:
        return jsonify({"reply":"Login required"})

    file=request.files.get("file")
    question=request.form.get("message","")

    if not file:
        return jsonify({"reply":"No file uploaded."})

    filename=file.filename.lower()
    text=""

    if filename.endswith(".pdf"):

        file.seek(0)
        file_bytes=file.read()

        try:
            with pdfplumber.open(BytesIO(file_bytes)) as pdf:
                for page in pdf.pages:
                    page_text=page.extract_text()
                    if page_text:
                        text+=page_text+"\n"
        except:
            pass

        if text.strip()=="":
            images=convert_from_bytes(file_bytes)
            for img in images:
                text+=pytesseract.image_to_string(img)

    elif filename.endswith(".txt"):

        text=file.read().decode("utf-8","ignore")

    else:
        return jsonify({"reply":"Unsupported file type."})

    prompt=f"""
Document content:
{text[:5000]}

User question:
{question}

Explain clearly.
"""

    reply=run_ai([
        {"role":"system","content":"You analyze uploaded documents."},
        {"role":"user","content":prompt}
    ])

    return jsonify({"reply":reply})


# ---------- CHAT HISTORY ----------

@app.route("/api/history")
def history():

    if "user" not in session:
        return jsonify({"history":[]})

    user_email=session["user"]["email"]

    db=get_db()

    rows=db.execute(
        "SELECT role,message FROM chats WHERE user_email=? ORDER BY id ASC",
        (user_email,)
    ).fetchall()

    history=[{"role":r["role"],"message":r["message"]} for r in rows]

    return jsonify({"history":history})


# ---------- RESUME PDF ----------

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