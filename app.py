from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
import os
from dotenv import load_dotenv
from auth import oauth, init_oauth
from groq import Groq
from io import BytesIO
from reportlab.pdfgen import canvas
import sqlite3
import requests

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

        url = "https://google.serper.dev/search"
        headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
        response = requests.post(url, json={"q": query}, headers=headers)
        data = response.json()

        results = []
        if "organic" in data:
            for r in data["organic"][:3]:
                results.append(f"{r['title']} - {r['snippet']}")

        return "\n".join(results)

    except:
        return ""

# FAST AI
def run_ai(messages):
    completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        temperature=0.5,   # 🔥 faster
        max_tokens=400     # 🔥 faster
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

@app.route("/email", methods=["GET","POST"])
def email():
    if "user" not in session:
        return redirect("/login")

    email_output=""
    if request.method=="POST":
        topic=request.form.get("topic")
        tone=request.form.get("tone")

        email_output=run_ai([
            {"role":"system","content":"Write clean human-like emails."},
            {"role":"user","content":f"{tone} email about {topic}"}
        ])

    return render_template("email.html", email=email_output)

@app.route("/resume", methods=["GET","POST"])
def resume():
    if "user" not in session:
        return redirect("/login")

    resume_output=""
    if request.method=="POST":
        prompt=f"""
Create a clean resume WITHOUT symbols like ** or ##.

Name: {request.form.get("name")}
Role: {request.form.get("role")}
Skills: {request.form.get("skills")}
Experience: {request.form.get("experience")}
Education: {request.form.get("education")}
"""
        resume_output=run_ai([
            {"role":"system","content":"Professional resume writer"},
            {"role":"user","content":prompt}
        ])

        resume_output=resume_output.replace("**","").replace("##","")

    return render_template("resume.html", resume=resume_output)

@app.route("/chat")
def chat():
    if "user" not in session:
        return redirect("/login")
    return render_template("chat.html")

@app.route("/api/chat", methods=["POST"])
def api_chat():
    if "user" not in session:
        return jsonify({"reply":"Login required"})

    user_message=request.json.get("message")
    user_email=session["user"]["email"]

    db=get_db()
    db.execute("INSERT INTO chats(user_email,role,message) VALUES (?,?,?)",
               (user_email,"user",user_message))
    db.commit()

    history=db.execute(
        "SELECT role,message FROM chats WHERE user_email=? ORDER BY id ASC LIMIT 50",
        (user_email,)
    ).fetchall()

    search_results=search_internet(user_message)

    messages=[{
        "role":"system",
        "content":"""
You are Pranox AI.

Founder: Chetansaipranav R in January 2026. 
Talk like a friendly human (like ChatGPT).

If user says hi,Hello,Hey or anything similar, reply with a friendly greeting and ask how you can help. For example:
→ "Hey! 👋 How can I help you?"

Be:
- Friendly
- Smart
- Not robotic

Give:
- Clear brief answers for all questions.
- No short answers. Always explain in detail.
- Use emojis when appropriate.
- Always give coding answers in code blocks.
- Use Bullet points and numbered lists to explain things clearly.
- Ask follow-up questions to understand user better.
- Always ask follow-up questions after answering to understand user better.
- Ask 2-3 follow-up questions after answering to understand user better.
- Answer like a human, not like a robot. Be natural and friendly.
- Answer like chatGPT.
- Answer for the question asked and by comparing with the previous conversation in same chat. Do not answer just based on the last question. Always compare with previous conversation and then answer.



Avoid explaining obvious things.
Never use these symbols in your answers: ** or ##. 
Never confuse the previous conversation history.
"""
    }]

    for h in history:
        messages.append({"role":h["role"],"content":h["message"]})

    if search_results:
        messages.append({"role":"system","content":search_results})

    reply=run_ai(messages)

    db.execute("INSERT INTO chats(user_email,role,message) VALUES (?,?,?)",
               (user_email,"assistant",reply))
    db.commit()

    return jsonify({"reply":reply})

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

# ✅ PRIVACY PAGE
@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

# ✅ TERMS PAGE
@app.route("/terms")
def terms():
    return render_template("terms.html")


if __name__=="__main__":
    app.run(debug=True)